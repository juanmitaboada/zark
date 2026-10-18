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

Used by:
  - zark mount local / zark chroot / zark repair-boot (installed system
    from a live USB)
"""

from lib.backup_layout import choose_dataset
from lib.cleanup import Cleanup
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
        ``zfs mount``; this function NEVER runs ``zfs set mountpoint``. That
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
    for ds in zfs.list_datasets("rpool", recursive=True):
        if ds.name in ("rpool", root_ds) or "keystore" in ds.name:
            continue
        if ds.canmount == "off" or ds.mountpoint in ("none", "-", "legacy"):
            continue
        if run(f"zfs mount {ds.name}", log=log).ok:
            mp = zfs.get_property(ds.name, "mountpoint")
            if mp:
                cleanup.track_mount(mp)
                mount_points.append(mp)
        elif readonly:
            not_mounted.append(ds.name)

    # ── Import + mount bpool (/boot) ──────────────────────────────────────
    if not zfs.pool_exists("bpool"):
        if zfs.pool_import("bpool", altroot=altroot, no_mount=True, readonly=readonly):
            cleanup.track_pool("bpool")
        else:
            log.warn("Could not import bpool - /boot will be unavailable in the chroot")
    bpool_boot = f"bpool/BOOT/{ubuntu_name}"
    if zfs.dataset_exists(bpool_boot) and run(f"zfs mount {bpool_boot}", log=log).ok:
        bp_mp = zfs.get_property(bpool_boot, "mountpoint")
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
    log.info("  Fix it from a live USB: sudo ./zark fix-rpool-mountpoint")


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
    log.info("  sudo ./zark repair-boot  (re-imports by /dev/disk/by-id, rewrites zpool.cache)")
