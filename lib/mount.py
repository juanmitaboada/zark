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

from lib.cleanup import Cleanup
from lib.keystore import KeystoreLike, open_keystore
from lib.log import Log
from lib.sh import run
from lib.zfs import ZFS


def find_system_root_dataset() -> str | None:
    """Return the ``rpool/ROOT/<ubuntu_name>`` boot-environment dataset, or None.

    The system layout (as opposed to a backup pool's nested ``<pool>/rpool``)
    keeps the root filesystem one level under ``rpool/ROOT``. We pick the first
    non-snapshot child of ``rpool/ROOT`` — there is normally exactly one active
    boot environment; if several exist, the caller is recovering an installed
    system and the first is the conventional choice.
    """
    for line in run("zfs list -H -o name -r rpool/ROOT").lines:
        ds = line.strip()
        # rpool/ROOT/<name>  → exactly two slashes, no snapshot marker, and the
        # parent container rpool/ROOT itself is excluded.
        if ds and ds != "rpool/ROOT" and "@" not in ds and ds.count("/") == 2:
            return ds
    return None


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
        is transient — the caller's ``Cleanup`` exports cleanly on exit.
      * Datasets mount at ``altroot`` + their *stored* mountpoint via plain
        ``zfs mount``; this function NEVER runs ``zfs set mountpoint``. That
        honours the absolute project rule against mountpoint changes while the
        keystore zvol is imported — the altroot import is the sanctioned
        mitigation, and ``zfs mount <ds>`` mounts ``canmount=noauto`` boot
        environments without touching properties.
      * ``readonly=True`` applies a VFS-level ``mount -o remount,ro`` to each
        mountpoint after mounting (ZFS still needs rw to create mountpoint
        directories first), leaving ZFS properties untouched — same technique
        as ``zark mount``'s read-only inspection mode.

    Returns ``(root_path, ubuntu_name)`` on success (``root_path`` is the
    effective filesystem root, i.e. ``altroot``), or ``None`` on any failure.
    All pools, mounts and the keystore are registered with ``cleanup`` as they
    succeed, so a partial failure still tears down cleanly.
    """
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    # pylint: disable=too-many-return-statements,too-many-branches,too-many-locals
    # ── Import rpool (no-mount) under altroot ─────────────────────────────
    if not zfs.pool_exists("rpool"):
        if not zfs.pool_import("rpool", device=device, altroot=altroot, no_mount=True):
            log.error("Cannot import rpool (tried clean import and -f)")
            return None
        cleanup.track_pool("rpool")
    else:
        log.warn("rpool is already imported — using it as-is (not re-importing with altroot)")

    # ── Unlock keystore + load keys ───────────────────────────────────────
    # passphrase=None prompts here, re-asking on a typo (I9).
    opened = (
        open_keystore(keystore, "rpool", log)
        if passphrase is None
        else keystore.mount("rpool", passphrase)
    )
    if not opened:
        log.error("Cannot open keystore — check passphrase")
        return None
    cleanup.track_keystore(keystore)
    loaded = keystore.load_pool_keys("rpool")
    log.ok(f"Loaded {loaded} encryption key(s)")

    # ── Locate the boot-environment root dataset ──────────────────────────
    root_ds = find_system_root_dataset()
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

    # ── Import + mount bpool (/boot) ──────────────────────────────────────
    if not zfs.pool_exists("bpool"):
        if zfs.pool_import("bpool", altroot=altroot, no_mount=True):
            cleanup.track_pool("bpool")
        else:
            log.warn("Could not import bpool — /boot will be unavailable in the chroot")
    bpool_boot = f"bpool/BOOT/{ubuntu_name}"
    if zfs.dataset_exists(bpool_boot) and run(f"zfs mount {bpool_boot}", log=log).ok:
        bp_mp = zfs.get_property(bpool_boot, "mountpoint")
        if bp_mp:
            cleanup.track_mount(bp_mp)
            mount_points.append(bp_mp)

    # ── Optional read-only protection (VFS level, properties untouched) ───
    if readonly:
        for mp in mount_points:
            run(f"mount -o remount,ro {mp}")
        log.ok("System mounted read-only")

    return altroot, ubuntu_name
