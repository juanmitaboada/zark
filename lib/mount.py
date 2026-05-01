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
