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
Backup pool mounting for inspection, chroot, and recovery.

Uses ZFS altroot (-R) mechanism: export pool, reimport with
-R <mount_root>, then zfs mount. This correctly handles all
ZFS mountpoint types without needing 'mount -t zfs' or
modifying dataset properties.

Used by:
  - zark mount    (interactive inspection, .zfs/snapshot access)
  - zark recover  (internal: mount restored system for grub-install)
"""

from pathlib import Path

from lib.cleanup import Cleanup
from lib.keystore import Keystore
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
    passphrase: str,
    log: Log,
    zfs: ZFS,
    keystore: Keystore,
    cleanup: Cleanup,
    *,
    readonly: bool = False,
    device: str | None = None,
) -> tuple[str, str] | None:
    """Import top-level ``rpool``/``bpool`` under ``altroot`` and mount the system.

    This is the system-layout counterpart to :meth:`MountedPool.mount_rpool`
    (which handles a backup pool's *nested* ``<pool>/rpool``). It is used by
    ``zark chroot`` and by ``zark mount`` when targeting the local installed
    system from a live USB, where ``rpool``/``bpool`` are not yet imported.

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
    if not keystore.mount("rpool", passphrase):
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


class MountedPool:
    """A backup pool mounted at a specific location via altroot."""

    def __init__(
        self,
        pool_name: str,
        mount_root: str,
        log: Log,
        zfs: ZFS,
        keystore: Keystore,
        cleanup: Cleanup,
    ):
        self.pool_name = pool_name
        self.mount_root = mount_root
        self.log = log
        self.zfs = zfs
        self.keystore = keystore
        self.cleanup = cleanup
        self._mounted = False

    def mount_rpool(self, readonly: bool = True) -> bool:
        """
        Mount backup pool using altroot.

        Strategy:
          1. Export pool (keys get unloaded)
          2. Reimport with -R <mount_root> [and readonly if requested]
          3. Reload encryption keys
          4. zfs mount each dataset

        Datasets mount at their stored mountpoint prepended with mount_root.
        E.g. mountpoint=/ → /mnt/zark/backup/
             mountpoint=/home → /mnt/zark/backup/home/
        """
        pool = self.pool_name
        mnt = self.mount_root

        # ── Export to reimport with altroot ───────────────────────────────
        self.log.dbg(f"Exporting {pool} for altroot reimport...")
        run(f"zfs unload-key -r {pool}")
        r = run(f"zpool export {pool}")
        if not r.ok:
            r = run(f"zpool export -f {pool}")
            if not r.ok:
                self.log.error(f"Cannot export {pool} for reimport: {r.stderr.strip()}")
                return False

        # ── Reimport with -R altroot ─────────────────────────────────────
        Path(mnt).mkdir(parents=True, exist_ok=True)

        ro_flag = "-o readonly=on" if readonly else ""
        r = run(f"zpool import {ro_flag} -R {mnt} -N {pool}", log=self.log)
        if not r.ok:
            r = run(f"zpool import -f {ro_flag} -R {mnt} -N {pool}", log=self.log)
            if not r.ok:
                self.log.error(f"Cannot reimport {pool} with altroot: {r.stderr.strip()}")
                return False

        self.cleanup.track_pool(pool)
        self.log.ok(f"Pool reimported with altroot={mnt}")

        # ── Reload keys ──────────────────────────────────────────────────
        self.log.dbg("Reloading encryption keys after reimport...")
        loaded = self.keystore.load_pool_keys(f"{pool}/rpool")
        if loaded == 0:
            self.log.warn("No keys loaded after reimport — datasets may not mount")
        else:
            self.log.dbg(f"Reloaded {loaded} key(s)")

        # ── Mount datasets ───────────────────────────────────────────────
        # With altroot, zfs mount uses stored mountpoint + altroot prefix
        rpool = f"{pool}/rpool"
        datasets = self.zfs.list_datasets(rpool, recursive=True)
        if not datasets:
            self.log.error(f"No datasets found under {rpool}")
            return False

        mounted = 0
        for ds in datasets:
            # Skip containers (canmount=off or mountpoint=none)
            if ds.canmount == "off" or ds.mountpoint in ("none", "-", "legacy"):
                self.log.dbg(f"Skip {ds.name}: canmount={ds.canmount} mp={ds.mountpoint}")
                continue

            r = run(f"zfs mount {ds.name}", log=self.log)
            if r.ok:
                mounted += 1
                # Track the effective mount point for cleanup
                effective_mp = self.zfs.get_property(ds.name, "mountpoint")
                if effective_mp:
                    self.cleanup.track_mount(effective_mp)
            else:
                self.log.dbg(f"Skip mount {ds.name}: {r.stderr.strip()}")

        self.log.ok(f"Mounted {mounted} datasets under {mnt}")
        self._mounted = mounted > 0
        return self._mounted

    def mount_bpool(self) -> bool:
        """Mount bpool datasets from backup if they exist."""
        bpool = f"{self.pool_name}/bpool"
        if not self.zfs.dataset_exists(bpool):
            self.log.dbg("No bpool in backup — skipping")
            return False

        datasets = self.zfs.list_datasets(bpool, recursive=True)
        for ds in datasets:
            if ds.canmount == "off" or ds.mountpoint in ("none", "-"):
                continue
            run(f"zfs mount {ds.name}")
        return True

    def prepare_chroot(self, root_path: str) -> bool:
        """
        Set up bind mounts for chroot at root_path.
        Used by recover (grub-install) and manual chroot.
        """
        rp = Path(root_path)
        if not (rp / "usr" / "bin").exists():
            self.log.error(f"Not a valid root filesystem: {root_path}")
            return False

        bind_mounts = [
            ("proc", "/proc"),
            ("sys", "/sys"),
            ("dev", "/dev"),
            ("dev/pts", "/dev/pts"),
        ]

        for name, source in bind_mounts:
            target = rp / name
            target.mkdir(parents=True, exist_ok=True)
            r = run(f"mount --bind {source} {target}")
            if r.ok:
                self.cleanup.track_mount(str(target))
            else:
                self.log.warn(f"Failed to bind mount {source} → {target}")

        # EFI vars
        efivars = rp / "sys/firmware/efi/efivars"
        if efivars.exists():
            run(f"mount -t efivarfs efivarfs {efivars}")
            self.cleanup.track_mount(str(efivars))

        self.log.ok(f"Chroot environment ready at {root_path}")
        return True

    def find_root_dataset(self) -> str | None:
        """Find the ubuntu_xxxxx root dataset in backup's rpool."""
        root_parent = f"{self.pool_name}/rpool/ROOT"
        datasets = self.zfs.list_datasets(root_parent, recursive=False)
        for ds in datasets:
            if ds.name != root_parent and ds.name.count("/") == 3:
                return ds.name
        return None

    def umount_all(self):
        """Unmount everything via ZFS."""
        run("zfs unmount -a")

    @property
    def rpool_path(self) -> Path:
        """Get the effective mount path of the rpool after altroot import."""
        return Path(self.mount_root)
