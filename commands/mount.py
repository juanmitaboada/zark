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
zark mount — Mount a backup pool for inspection.

Imports the pool once under the altroot /mnt/zark/<pool> (``-N -R``, plus
``readonly=on`` in read-only mode), unlocks its keystore, loads the keys and
mounts each dataset with ``zfs mount``. Read-only mode never writes to the
backup: the pool itself is imported read-only.
"""

from lib import sh
from lib.cleanup import Cleanup
from lib.config import Config
from lib.drives import backup_device, scan_connected_drives, select_drive
from lib.keystore import Keystore, open_keystore
from lib.log import Log
from lib.mount import mount_system_pools
from lib.zfs import ZFS

MNT_BASE = "/mnt/zark"
SYSTEM_MNT = "/mnt/zark/system"
SYSTEM_TARGETS = ("local", "system", "rpool")


def _mount_local_system(log: Log, zfs: ZFS, cleanup: Cleanup) -> None:
    """Mount the installed system's rpool/bpool for inspection (live USB).

    The local-disk counterpart to the backup-pool flow below: from a live
    session it imports the top-level rpool/bpool under an altroot and mounts
    the boot environment, leaving it mounted for the operator. Read-only by
    default. Use 'zark umount local' (or 'zark chroot' for a working shell).
    """
    if zfs.pool_exists("rpool"):
        log.fatal(
            "rpool is already imported — this looks like the running system.\n"
            "  Boot a live USB to inspect the installed system's pools.",
        )
    if not sh.is_live_usb():
        log.warn("This does not look like a live USB session.")
        if not log.ask("Continue anyway?", default=False):
            log.fatal("Aborted — boot a live USB and retry.")

    mode_idx = log.ask_choice(
        "Mount mode:",
        [
            f"Read-only  {log.G}(recommended — safe){log.N}",
            f"Read-write {log.R}(can modify the system){log.N}",
        ],
    )
    readonly = mode_idx == 0
    result = mount_system_pools(
        SYSTEM_MNT,
        None,  # prompt inside, re-asking on a typo (I9)
        log,
        zfs,
        Keystore(log),
        cleanup,
        readonly=readonly,
    )
    if result is None:
        log.fatal("Could not mount the system — see messages above.")
    root_path, ubuntu_name = result

    log.banner_ok(
        f"SYSTEM MOUNTED ({ubuntu_name})",
        [
            f"Mount point: {log.W}{root_path}{log.N}",
            f"Mode:        {log.W}{'read-only' if readonly else 'read-write'}{log.N}",
            "",
            f"{log.Y}Browse:{log.N}  ls {root_path}/",
            f"{log.Y}Chroot:{log.N}  sudo ./zark chroot",
            "",
            f"{log.Y}To unmount:{log.N}  sudo ./zark umount local",
        ],
    )
    # Leave it mounted for the operator.
    cleanup.disable()


def run(
    args: list[str],
):  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
    """Mount a backup pool for inspection / chroot / recovery."""
    log = Log()
    cfg = Config.load()
    cfg.check_registry(log, fatal=False)
    zfs = ZFS(log)
    cleanup = Cleanup(log)
    cleanup.register()

    # Local/system target: mount the installed system's pools (live USB),
    # not a removable backup drive. Kept as an explicit keyword so the
    # default (no-arg) behaviour — scan backup drives — is unchanged.
    target = next((a for a in args if not a.startswith("-")), None)
    if target in SYSTEM_TARGETS:
        log.banner("MOUNT SYSTEM", "Mount the installed ZFS system (live USB)")
        _mount_local_system(log, zfs, cleanup)
        return

    log.banner("MOUNT BACKUP POOL", "Mount for inspection / chroot / recovery")

    # ── Find drives ──────────────────────────────────────────────────────
    log.info("Scanning for backup drives...")
    drives = scan_connected_drives(cfg, log)

    if not drives:
        log.warn("No backup drives connected")
        return

    drive = select_drive(drives, log, known_only=False)
    if not drive:
        return

    pool_name = drive.name
    mnt_point = f"{MNT_BASE}/{pool_name}"

    if zfs.pool_exists(pool_name):
        log.warn(f"Pool {pool_name} is already imported")
        log.info("To unmount: sudo ./zark umount")
        cleanup.disable()
        return

    # ── Mount mode ───────────────────────────────────────────────────────
    mode_idx = log.ask_choice(
        "Mount mode:",
        [
            f"Read-only  {log.G}(recommended — safe){log.N}",
            f"Read-write {log.R}(can modify backup data){log.N}",
        ],
    )
    readonly = mode_idx == 0

    # ── Import once, under the user-visible altroot (I-G) ────────────────
    # Read-only mode imports the pool readonly=on: nothing on the backup is
    # written, not even the keystore's ext4 journal replay.
    log.info(f"Importing pool {pool_name} (altroot={mnt_point})...")
    if not zfs.import_backup_pool(
        pool_name,
        backup_device(drive),
        altroot=mnt_point,
        readonly=readonly,
        guid=drive.guid,
    ):
        log.fatal(f"Cannot import pool {pool_name}")
    cleanup.track_pool(pool_name)

    ks = Keystore(log)
    if not open_keystore(ks, pool_name, log, readonly=readonly):
        log.fatal("Cannot open keystore", causes=["Wrong passphrase (3 attempts)"])
    loaded = ks.load_pool_keys(f"{pool_name}/rpool")
    ks.umount()
    log.ok(f"Loaded {loaded} encryption key(s)")

    # ── Mount datasets ───────────────────────────────────────────────────
    log.info("Mounting datasets...")
    datasets = zfs.list_datasets(f"{pool_name}/rpool", recursive=True)

    mounted = 0
    for ds in datasets:
        if ds.canmount == "off" or ds.mountpoint in ("none", "-", "legacy"):
            continue
        r = sh.run(f"zfs mount {ds.name}")
        if r.ok:
            mounted += 1
        else:
            log.dbg(f"Skip {ds.name}: {r.stderr.strip()}")

    log.ok(f"Mounted {mounted} datasets")

    if mounted == 0:
        log.fatal(
            "No datasets mounted",
            solutions=[f"Check: zfs get mountpoint,canmount -r {pool_name}/rpool"],
        )

    # ── Detect root dataset for chroot instructions ────────────────────
    root_ds = ""
    for ds in datasets:
        if "/ROOT/" in ds.name and ds.name.count("/") == 3 and ds.canmount != "off":
            root_ds = ds.name
            break
    root_path = ""
    if root_ds:
        # Effective mountpoint with altroot
        root_path = zfs.get_property(root_ds, "mountpoint")

    # ── Show results ─────────────────────────────────────────────────────
    result_lines = [
        f"Mount point: {log.W}{mnt_point}{log.N}",
        f"Mode:        {log.W}{'read-only' if readonly else 'read-write'}{log.N}",
        f"Datasets:    {log.W}{mounted}{log.N}",
        "",
        f"{log.Y}Browse data:{log.N}",
        f"  ls {mnt_point}/",
        "",
        f"{log.Y}Access old snapshots:{log.N}",
        (f"  ls {root_path}/.zfs/snapshot/" if root_path else f"  ls {mnt_point}/.zfs/snapshot/"),
        "",
    ]

    if root_path:
        result_lines += [
            f"{log.Y}Chroot into the backup system:{log.N}",
            f"  sudo mount --bind /proc {root_path}/proc",
            f"  sudo mount --bind /sys  {root_path}/sys",
            f"  sudo mount --bind /dev  {root_path}/dev",
            f"  sudo chroot {root_path}",
            "",
        ]

    result_lines += [
        f"{log.Y}To unmount:{log.N}",
        "  sudo ./zark umount",
    ]

    log.banner_ok(f"POOL {pool_name} MOUNTED", result_lines)

    # Leave mounted for the user
    cleanup.disable()
