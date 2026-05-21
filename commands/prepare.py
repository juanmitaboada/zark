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
zark prepare — Prepare a new blank drive for backup.

Creates ZFS pool (no encryption — raw send brings its own),
does initial raw send from rpool, sends keystore, sends bpool,
registers the drive in known_drives.json.
"""

import time
from pathlib import Path

from lib import sh
from lib.cleanup import flush_device_cache, prompt_eject_or_attach
from lib.config import Config, DriveInfo
from lib.drives import get_drive_id, get_drive_info, validate_external_block_device
from lib.log import Log
from lib.zfs import ZFS, syncoid_exclude_flag


def run(
    args: list[str],
):  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    """Main entry point for the prepare command."""
    log = Log()
    cfg = Config.load()
    zfs = ZFS(log)

    target_dev = args[0] if args else ""
    validate_external_block_device(target_dev, log, command="prepare")

    log.banner("PREPARE NEW BACKUP DRIVE")

    # ── Verify rpool keystore accessible ─────────────────────────────────
    if not Path("/run/keystore/rpool/system.key").exists():
        log.fatal(
            "/run/keystore/rpool/system.key not found",
            solutions=["Run from the system whose backup you want to create"],
        )

    # ── Show drive info ──────────────────────────────────────────────────
    model, size, transport = get_drive_info(target_dev)
    log.info(f"Device:    {target_dev}")
    log.info(f"Model:     {model}")
    log.info(f"Size:      {size}")
    log.info(f"Transport: {transport}")

    # ── Check drive is empty ─────────────────────────────────────────────
    existing_parts = sh.run(f"lsblk -no NAME {target_dev} | tail -n +2").output
    sigs = sh.run(f"blkid {target_dev}").output
    if existing_parts or sigs:
        log.fatal(
            "Drive is NOT empty — has partitions or signatures",
            solutions=[f"Wipe first: sudo ./zark purge {target_dev}"],
        )

    log.ok("Drive appears empty ✓")

    # ── Drive ID ─────────────────────────────────────────────────────────
    # dev_name = Path(target_dev).name
    base_drive_id = get_drive_id(target_dev)
    log.dbg(f"Drive ID: {base_drive_id}")

    # ── Pool name ────────────────────────────────────────────────────────
    default_pool = "backup"
    counter = 1
    while default_pool in cfg.known_drives or zfs.pool_exists(default_pool):
        counter += 1
        default_pool = f"backup{counter}"

    new_pool = log.ask_input("Pool name for this backup drive", default_pool)
    if not new_pool.isidentifier():
        log.fatal(f"Invalid pool name '{new_pool}'")
    if new_pool in cfg.known_drives:
        log.fatal(f"Pool name '{new_pool}' already registered")

    log.ok(f"Pool name: {new_pool}")

    # ── Confirmation ─────────────────────────────────────────────────────
    log.info("The pool will be created WITHOUT its own encryption.")
    log.info("Encryption comes from rpool raw send (same key/passphrase).")
    if not log.ask("Proceed with drive preparation?"):
        log.info("Aborted")
        return

    start = time.time()

    # ── Create pool ──────────────────────────────────────────────────────
    log.step(1, 3, f"Creating ZFS pool '{new_pool}'...")

    r = sh.run(
        "zpool create -f -o ashift=12 -O atime=off -O xattr=sa "
        + "-O dnodesize=auto -O normalization=formD -m none "
        + f"{new_pool} {target_dev}",
        log=log,
    )
    if not r.ok:
        log.fatal(f"Failed to create pool: {r.stderr.strip()}")
    log.ok(f"Pool '{new_pool}' created")

    # ── Initial raw send ─────────────────────────────────────────────────
    log.step(2, 3, f"Initial raw send from rpool → {new_pool}/rpool...")

    rpool_used = sh.run("zfs list -H -o used rpool").output
    log.info(f"~{rpool_used} to transfer")

    # Syncoid raw send (excludes keystore — sent separately).
    # syncoid 2.3.0+ uses --exclude-datasets; older Ubuntu releases
    # (22.04 - 25.10, sanoid 2.1.0 - 2.2.0-2) only know --exclude.
    excl = syncoid_exclude_flag()
    r = sh.run(
        "syncoid --recursive --no-privilege-elevation --sendoptions=w "
        + f"{excl}=rpool/keystore "
        + f"rpool {new_pool}/rpool",
        log=log,
    )
    if r.ok:
        log.ok("rpool synced ✓")
    else:
        log.warn("rpool sync had warnings — check log")

    # Send keystore separately (outside rpool tree to avoid encryption dependency)
    log.info("Sending keystore...")
    snap_ts = sh.run("date '+%Y%m%d_%H%M%S'").output
    _ = sh.run(f"zfs snapshot rpool/keystore@prepare_{snap_ts}")
    r = sh.run_pipe(
        f"zfs send rpool/keystore@prepare_{snap_ts}",
        f"zfs receive -F {new_pool}/keystore",
    )
    if r.ok:
        log.ok(f"Keystore synced to {new_pool}/keystore ✓")
    else:
        log.warn("Keystore sync had errors")

    # Sync bpool.
    if zfs.pool_exists("bpool"):
        log.info("Syncing bpool (kernels + grub)...")
        r = sh.run(
            f"syncoid --recursive --no-privilege-elevation bpool {new_pool}/bpool",
            log=log,
        )
        if r.ok:
            log.ok("bpool synced ✓")
        else:
            log.warn("bpool sync had warnings")

    # Fix keylocation
    log.info("Configuring keystore location...")
    _ = zfs.set_property(
        f"{new_pool}/rpool",
        "keylocation",
        "file:///run/keystore/rpool/system.key",
    )

    # Sync mountpoints from origin
    log.info("Syncing mountpoints from origin...")
    r = sh.run("zfs list -H -o name,mountpoint rpool")
    if r.ok:
        for line in r.lines:
            parts = line.split("\t")
            if len(parts) >= 2:
                ds, mp = parts[0].strip(), parts[1].strip()
                if mp in ("none", "-", "legacy"):
                    continue
                dst = f"{new_pool}/{ds}"
                if zfs.dataset_exists(dst):
                    _ = zfs.set_property(dst, "mountpoint", mp)
    log.ok("Mountpoints synced ✓")

    # ── Register and export ──────────────────────────────────────────────
    log.step(3, 3, "Registering drive...")

    new_guid = zfs.pool_guid(new_pool)
    log.ok(f"Pool GUID: {new_guid}")

    _ = zfs.pool_export(new_pool)
    flush_device_cache(log)

    # Auto-register in known_drives.json
    cfg.known_drives[new_pool] = DriveInfo(
        name=new_pool,
        guid=new_guid,
        drive_id=base_drive_id or "<unknown>",
    )
    cfg.save_drives()

    elapsed = int(time.time() - start)
    mins, secs = divmod(elapsed, 60)

    log.banner_ok(
        "DRIVE PREPARED",
        [
            f"Pool:     {log.W}{new_pool}{log.N}  (GUID: {new_guid})",
            f"Drive ID: {log.W}{base_drive_id}{log.N}",
            f"Duration: {log.W}{mins}m {secs}s{log.N}",
            "",
            f"Registered in: {log.W}{cfg.drives_file_path}{log.N}",
            "",
            f"Run backup: {log.W}sudo ./zark backup{log.N}",
        ],
    )

    # Default to NOT ejecting: the typical workflow after `prepare` is
    # `zark backup` against the drive that was just prepared. Auto-
    # ejecting would force a pointless unplug/replug cycle. Operators
    # who really want to disconnect now can answer "y".
    prompt_eject_or_attach(target_dev, new_pool, log, default_eject=False)
