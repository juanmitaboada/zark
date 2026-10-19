# Copyright 2026 Juanmi Taboada
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Mounting the installed system's pools from a live USB.

Imports rpool/bpool under an altroot (-R) and mounts each dataset with
`zfs mount`, which prefixes the stored mountpoint with the altroot. This
handles every ZFS mountpoint type without modifying dataset properties.
A mountpoint that crosses a symlink of the installed tree is resolved
inside it first (layout_target), as the backup mount does.

Used by:
  - zark mount local / zark chroot / zark repair-boot (installed system
    from a live USB)
"""

import os
import shlex

from lib.backup_layout import choose_dataset
from lib.cleanup import Cleanup
from lib.config import zark_command
from lib.keystore import KeystoreLike, open_keystore
from lib.log import Log
from lib.sh import run
from lib.zfs import ZFS


def find_system_root_dataset(log: Log) -> str | None:
    """Return the ``rpool/ROOT/<ubuntu_name>`` boot-environment dataset, or None.

    The system layout (as opposed to a backup pool's nested ``<pool>/rpool``)
    keeps the root filesystem one level under ``rpool/ROOT``. With several
    boot environments, the one mounted at ``/`` is the running system's;
    otherwise (a live session) the operator chooses (M4a-8).
    """
    found: list[str] = []
    for line in run("zfs list -H -o name -r rpool/ROOT").lines:
        ds = line.strip()
        # rpool/ROOT/<name>  → exactly two slashes, no snapshot marker, and the
        # parent container rpool/ROOT itself is excluded.
        if ds and ds != "rpool/ROOT" and "@" not in ds and ds.count("/") == 2:
            found.append(ds)
    if len(found) > 1:
        running = run("findmnt -n -o SOURCE /").output.strip()
        if running in found:
            return running
    return choose_dataset(found, "rpool", log) or None


MAX_SYMLINKS = 40  # as the kernel's path walk (MAXSYMLINKS)


def resolve_in_tree(root: str, path: str) -> str | None:
    """``path`` (absolute, inside ``root``) resolved as a chroot at ``root``
    would: component by component, an absolute link restarting at ``root``,
    ``..`` never above it (M4c-22). Returns the path with no symlink left,
    or None after more than :data:`MAX_SYMLINKS` links. Components that do
    not exist are kept as they are."""
    rel = os.path.relpath(path, root)
    parts = [] if rel == "." else rel.split("/")
    current: list[str] = []
    links = 0
    while parts:
        c = parts.pop(0)
        if c in ("", "."):
            continue
        if c == "..":
            if current:
                _ = current.pop()
            continue
        here = os.path.join(root, *current, c)
        if os.path.islink(here):
            links += 1
            if links > MAX_SYMLINKS:
                return None
            link = os.readlink(here)
            if link.startswith("/"):
                current = []
            parts = link.split("/") + parts
            continue
        current.append(c)
    return os.path.join(root, *current) if current else root


def layout_target(path: str, mnt: str, root: str, readonly: bool) -> tuple[str, str]:
    """Where ``path`` (a target under ``mnt``) is mounted: ``(target, "")``,
    or ``("", why it is skipped)``.

    A symlink or .. in the backup must not lead out of the tree onto the
    running system. Read-only, it is resolved inside the tree as a chroot
    would (an absolute /var/run -> /run of the backup is the tree's /run):
    nothing in a read-only pool changes between the resolution and the
    mount (M4c-22). Read-write, one that leaves the tree is refused. Either
    way the resolved path, with no link left, is mounted.
    """
    if not readonly:
        target = os.path.realpath(path)
        if not target.startswith(f"{root}/"):
            return "", f"{path} resolves to {target}"
        return target, ""
    resolved = resolve_in_tree(root, os.path.join(root, os.path.relpath(path, mnt)))
    if resolved is None:
        return "", f"{path}: too many symlinks"
    if resolved == root:
        # Mounted there, it would hide the boot environment's root.
        return "", f"{path} resolves to the root"
    return resolved, ""


def _mount_in_tree(ds: str, altroot: str, zfs: ZFS, log: Log, readonly: bool) -> str:
    """Mount ``ds`` of the installed tree at ``altroot``; where, or "".

    libzfs joins the altroot and the mountpoint as strings and mkdirs and
    mounts that path, so the kernel follows every link in it: the tree's
    /var/run -> /run would land on the live session's /run
    (lib/libzfs/libzfs_dataset.c:2731-2758, libzfs_mount.c:465-474,
    os/linux/libzfs_mount_os.c:331-348). When the path crosses no link,
    ``zfs mount`` as before.
    """
    mp = zfs.get_property(ds, "mountpoint")
    if not mp.startswith("/"):
        return ""
    root = os.path.realpath(altroot)
    target, why = layout_target(mp, altroot, root, readonly)
    if why:
        log.warn(f"{ds} not mounted: {why}")
        return ""
    if target == os.path.join(root, os.path.relpath(mp, altroot)):
        return mp if run(f"zfs mount {ds}", log=log).ok else ""
    if not readonly:
        _ = run(f"mkdir -p {shlex.quote(target)}", log=log)
    opts = "ro,zfsutil" if readonly else "zfsutil"
    ok = run(f"mount -t zfs -o {opts} {ds} {shlex.quote(target)}", log=log).ok
    return target if ok else ""


def _in_other_boot_environment(name: str, root_ds: str) -> bool:
    """Whether ``name`` is a boot environment's root, or below one, other than
    ``root_ds`` (``rpool/ROOT/<be>[/...]``)."""
    parent = root_ds.rsplit("/", 1)[0] + "/"
    if not name.startswith(parent):
        return False
    return name != root_ds and not name.startswith(root_ds + "/")


def mount_system_pools(
    altroot: str,
    passphrase: str | None,
    log: Log,
    zfs: ZFS,
    keystore: KeystoreLike,
    cleanup: Cleanup,
    *,
    readonly: bool = False,
    device: str | None = None,
) -> tuple[str, str] | None:
    """Import top-level ``rpool``/``bpool`` under ``altroot`` and mount the system.

    Used by ``zark chroot``, ``zark mount local`` and ``zark repair-boot``
    when targeting the local installed system from a live USB, where
    ``rpool``/``bpool`` are not yet imported. ``passphrase=None`` asks for
    it, re-prompting on a typo.

    Behaviour and safety:

      * Both pools are imported with ``-R altroot -N`` via
        :meth:`ZFS.pool_import`, inheriting its clean-then-``-f`` fallback so a
        pool left in use by an unclean shutdown still imports. The forced state
        is transient - the caller's ``Cleanup`` exports cleanly on exit.
      * Datasets mount at ``altroot`` + their *stored* mountpoint via plain
        ``zfs mount``, or, when that path crosses a symlink of the installed
        tree, at the path :func:`layout_target` resolves with ``mount -t zfs
        -o [ro,]zfsutil``; this function NEVER runs ``zfs set mountpoint``. That
        honours the absolute project rule against mountpoint changes while the
        keystore zvol is imported - the altroot import is the sanctioned
        mitigation, and ``zfs mount <ds>`` mounts ``canmount=noauto`` boot
        environments without touching properties.
      * ``readonly=True`` imports both pools ``-o readonly=on`` and opens the
        keystore read-only (LUKS ``--readonly``, ext4 ``ro,noload``), so the
        live session writes nothing to the installed system, not even its
        labels (M4c-21). Datasets then mount read-only without their ZIL
        replayed (the last writes of an unclean shutdown are not seen), and
        one whose mountpoint directory is missing cannot have it created: it
        is listed as not mounted instead of failing the command.

    Returns ``(root_path, ubuntu_name)`` on success (``root_path`` is the
    effective filesystem root, i.e. ``altroot``), or ``None`` on any failure.
    All pools, mounts and the keystore are registered with ``cleanup`` as they
    succeed, so a partial failure still tears down cleanly.
    """
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    # pylint: disable=too-many-return-statements,too-many-branches,too-many-locals
    # ── Import rpool (no-mount) under altroot ─────────────────────────────
    if not zfs.pool_exists("rpool"):
        # pool_import scans by-id first; the hint is only its fallback.
        if not zfs.pool_import(
            "rpool", device=device, altroot=altroot, no_mount=True, readonly=readonly
        ):
            log.error("Cannot import rpool (tried clean import and -f)")
            return None
        cleanup.track_pool("rpool")
    else:
        log.warn("rpool is already imported - using it as-is (not re-importing with altroot)")

    # ── Unlock keystore + load keys ───────────────────────────────────────
    # passphrase=None prompts here, re-asking on a typo (I9).
    opened = (
        open_keystore(keystore, "rpool", log, readonly=readonly)
        if passphrase is None
        else keystore.mount("rpool", passphrase, readonly=readonly)
    )
    if not opened:
        log.error("Cannot open keystore - check passphrase")
        return None
    cleanup.track_keystore(keystore)
    loaded = keystore.load_pool_keys("rpool")
    log.ok(f"Loaded {loaded} encryption key(s)")

    # ── Locate the boot-environment root dataset ──────────────────────────
    root_ds = find_system_root_dataset(log)
    if not root_ds:
        log.error("Cannot find root dataset under rpool/ROOT")
        return None
    ubuntu_name = root_ds.split("/")[-1]
    log.ok(f"Root dataset: {root_ds}")

    # ── Mount root + children (altroot + stored mountpoint) ───────────────
    mount_points: list[str] = []
    if not run(f"zfs mount {root_ds}", log=log).ok:
        log.error(f"Cannot mount root dataset {root_ds}")
        return None
    root_mp = zfs.get_property(root_ds, "mountpoint")
    if root_mp:
        cleanup.track_mount(root_mp)
        mount_points.append(root_mp)

    not_mounted: list[str] = []
    other_bes: list[str] = []
    for ds in zfs.list_datasets("rpool", recursive=True):
        if ds.name in ("rpool", root_ds) or "keystore" in ds.name:
            continue
        if _in_other_boot_environment(ds.name, root_ds):
            # Its root has mountpoint=/, which would land on this tree's root
            # (M4dR-5): the whole subtree stays unmounted, said once below.
            if ds.name.count("/") == root_ds.count("/"):
                other_bes.append(ds.name.rsplit("/", 1)[-1])
            continue
        if ds.canmount == "off" or ds.mountpoint in ("none", "-", "legacy"):
            continue
        mp = _mount_in_tree(ds.name, altroot, zfs, log, readonly)
        if mp:
            cleanup.track_mount(mp)
            mount_points.append(mp)
        elif readonly:
            not_mounted.append(ds.name)

    if other_bes:
        log.info(f"Other boot environments are not mounted: {', '.join(other_bes)}")

    # ── Import + mount bpool (/boot) ──────────────────────────────────────
    if not zfs.pool_exists("bpool"):
        # The operator's hint reaches bpool as it reaches rpool (M5a-12); the
        # by-id scan still goes first.
        if zfs.pool_import(
            "bpool", device=device, altroot=altroot, no_mount=True, readonly=readonly
        ):
            cleanup.track_pool("bpool")
        else:
            log.warn("Could not import bpool - /boot will be unavailable in the chroot")
    bpool_boot = f"bpool/BOOT/{ubuntu_name}"
    if zfs.dataset_exists(bpool_boot):
        bp_mp = _mount_in_tree(bpool_boot, altroot, zfs, log, readonly)
        if bp_mp:
            cleanup.track_mount(bp_mp)
            mount_points.append(bp_mp)

    if readonly:
        log.ok("System mounted read-only (pools imported readonly=on)")
        if not_mounted:
            log.warn(
                "Not mounted (no mountpoint directory, which a read-only pool cannot "
                + f"create): {', '.join(not_mounted)}",
            )

    return altroot, ubuntu_name


def rpool_mountpoint_lost() -> bool:
    """True when rpool carries the mountpoint=none left by zark recover ≤ 1.0.12.

    The Ubuntu installer creates rpool with canmount=off, mountpoint=/.
    Only that layout (rpool/ROOT present, canmount=off, a *local* none) is
    reported, so a pool deliberately set up otherwise is left alone.
    """
    r = run("zfs get -H -o property,value,source mountpoint,canmount rpool")
    if not r.ok:
        return False
    props = {f[0]: (f[1], f[2]) for f in (line.split("\t") for line in r.lines) if len(f) == 3}
    return (
        props.get("mountpoint") == ("none", "local")
        and props.get("canmount", ("", ""))[0] == "off"
        and run("zfs list -H -o name rpool/ROOT").ok
    )


def warn_rpool_mountpoint_lost(log: Log) -> None:
    """Explain rpool's lost mountpoint and how to fix it, when it applies."""
    if not rpool_mountpoint_lost():
        return
    log.warn("rpool has mountpoint=none; the Ubuntu installer sets / (zark recover ≤ 1.0.12)")
    log.info("  New datasets directly under rpool will not mount, and backup drives")
    log.info("  prepared from this system cannot be browsed with 'zark mount'.")
    log.info(f"  Fix it from a live USB: {zark_command()} fix-rpool-mountpoint")


_STABLE_VDEV_PREFIXES = ("/dev/disk/", "/dev/mapper/")


def kernel_named_vdevs(pools: tuple[str, ...] = ("rpool", "bpool")) -> list[str]:
    """Vdev paths of the system pools that are kernel names (/dev/sdb4)."""
    found: list[str] = []
    for pool in pools:
        r = run(f"zpool list -vHP {pool}")
        for line in r.lines[1:] if r.ok else []:
            path = line.strip().split("\t")[0]
            if path.startswith("/dev/") and not path.startswith(_STABLE_VDEV_PREFIXES):
                found.append(path)
    return found


def warn_kernel_named_vdevs(log: Log) -> None:
    """Explain the boot risk of kernel-named vdevs and how to fix it, when it applies."""
    paths = kernel_named_vdevs()
    if not paths:
        return
    log.warn(f"rpool/bpool use kernel device names ({', '.join(paths)})")
    log.info("  A USB disk plugged in at boot can take that name and stop the")
    log.info("  pool import (emergency shell). Fix it from a live USB:")
    log.info(
        f"  {zark_command()} repair-boot  (re-imports by /dev/disk/by-id, rewrites zpool.cache)"
    )
