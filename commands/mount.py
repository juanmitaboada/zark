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

Uses ZFS altroot: export → reimport with -R → zfs mount.
For read-only mode: mount normally, then remount each VFS mountpoint
as ro (doesn't modify ZFS properties).
"""

import os
import shutil
from pathlib import Path

from lib import sh
from lib.cleanup import Cleanup
from lib.config import Config
from lib.drives import scan_connected_drives, select_drive
from lib.keystore import SYSTEM_KEY_PATH, Keystore
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
    passphrase = log.ask_password("Passphrase for rpool")

    result = mount_system_pools(
        SYSTEM_MNT,
        passphrase,
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

    passphrase = log.ask_password(f"Passphrase for {pool_name}")

    # ── Phase 1: Import -N to access keystore ────────────────────────────
    log.info(f"Importing pool {pool_name} (phase 1: keystore)...")
    if not zfs.pool_import(pool_name, no_mount=True):
        log.fatal(f"Cannot import pool {pool_name}")

    ks = Keystore(log)
    if not ks.mount(pool_name, passphrase):
        _ = zfs.pool_export(pool_name)
        log.fatal("Cannot open keystore", causes=["Wrong passphrase"])

    tmp_key = f"/tmp/zark_key_{os.getpid()}"
    _ = shutil.copy2(SYSTEM_KEY_PATH, tmp_key)
    os.chmod(tmp_key, 0o600)
    log.dbg(f"Saved system.key to {tmp_key}")
    ks.umount()

    # ── Phase 2: Export → reimport with altroot ──────────────────────────
    log.info(f"Reimporting with altroot={mnt_point}...")
    _ = sh.run(f"zfs unload-key -r {pool_name}")
    _ = sh.run(f"zpool export {pool_name}") or sh.run(f"zpool export -f {pool_name}")

    Path(mnt_point).mkdir(parents=True, exist_ok=True)
    r = sh.run(f"zpool import -R {mnt_point} -N {pool_name}", log=log)
    if not r.ok:
        r = sh.run(f"zpool import -f -R {mnt_point} -N {pool_name}", log=log)
        if not r.ok:
            os.remove(tmp_key)
            log.fatal("Cannot reimport with altroot")

    cleanup.track_pool(pool_name)
    log.ok(f"Pool reimported with altroot={mnt_point}")

    # ── Reload keys ──────────────────────────────────────────────────────
    log.info("Loading encryption keys...")
    r_keys = sh.run(f"zfs get -H keystatus -r {pool_name}/rpool")
    loaded = 0
    if r_keys.ok:
        for line in r_keys.lines:
            parts = line.split("\t")
            if len(parts) >= 3 and "unavailable" in parts[2] and "@" not in parts[0]:
                ds_name = parts[0].strip()
                if sh.run(f"zfs load-key -L file://{tmp_key} {ds_name}").ok:
                    loaded += 1
                    log.dbg(f"Key loaded: {ds_name}")
    log.ok(f"Loaded {loaded} encryption key(s)")
    os.remove(tmp_key)

    # ── Mount datasets (read-write first, then remount ro) ───────────────
    log.info("Mounting datasets...")
    datasets = zfs.list_datasets(f"{pool_name}/rpool", recursive=True)

    # Mount all datasets normally (ZFS needs rw to create mountpoint dirs)
    mounted = 0
    mount_points: list[str] = []
    for ds in datasets:
        if ds.canmount == "off" or ds.mountpoint in ("none", "-", "legacy"):
            continue
        r = sh.run(f"zfs mount {ds.name}")
        if r.ok:
            mounted += 1
            mp = zfs.get_property(ds.name, "mountpoint")
            if mp:
                mount_points.append(mp)
        else:
            log.dbg(f"Skip {ds.name}: {r.stderr.strip()}")

    log.ok(f"Mounted {mounted} datasets")

    if mounted == 0:
        log.fatal(
            "No datasets mounted",
            solutions=[f"Check: zfs get mountpoint,canmount -r {pool_name}/rpool"],
        )

    # Apply read-only at VFS level (doesn't modify ZFS properties)
    if readonly:
        log.info("Applying read-only protection...")
        for mp in mount_points:
            _ = sh.run(f"mount -o remount,ro {mp}")
        log.ok("All datasets remounted read-only")

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
