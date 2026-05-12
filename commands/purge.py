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
zark purge — Securely wipe a managed backup drive.

Destroys ZFS pool, overwrites start/end with random data,
wipes signatures, destroys partition table.
"""

from pathlib import Path

from lib import sh
from lib.config import Config
from lib.drives import get_drive_id, get_drive_info, validate_external_block_device
from lib.log import Log


def run(
    args: list[str],
):  # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    """Main entry point for the purge command."""
    log = Log()
    cfg = Config.load()
    target_dev = args[0] if args else ""

    validate_external_block_device(target_dev, log, command="purge")

    log.banner("PURGE BACKUP DRIVE", "⚠  IRREVERSIBLE OPERATION")

    model, size, _ = get_drive_info(target_dev)
    drive_id = get_drive_id(target_dev)

    log.info(f"Device: {target_dev}")
    log.info(f"Model:  {model}")
    log.info(f"Size:   {size}")

    # Match against known drives
    matched_pool = None
    for name, info in cfg.known_drives.items():
        if info.drive_id == drive_id:
            matched_pool = name
            break

    if matched_pool:
        log.ok(f"Drive recognized as: {matched_pool}")
    else:
        log.warn("Drive is NOT registered in known_drives.json")
        if not log.ask("Purge unregistered drive? (DANGEROUS)"):
            log.info("Aborted")
            return

    # Double confirmation
    log.info("Type 'yes' to confirm:")
    try:
        c1 = input("    > ").strip()
    except EOFError:
        c1 = ""
    if c1 != "yes":
        log.info("Aborted")
        return

    dev_base = Path(target_dev).name
    log.info(f"Type device name to confirm ({dev_base}):")
    try:
        c2 = input("    > ").strip()
    except EOFError:
        c2 = ""
    if c2 != dev_base:
        log.fatal("Device name mismatch. Aborted.")

    # ── Destroy pool ─────────────────────────────────────────────────────
    log.step(1, 5, "Destroying ZFS pool...")
    if matched_pool:
        _ = sh.run(f"zpool import {matched_pool}")
        r = sh.run(f"zpool destroy {matched_pool}", log=log)
        if r.ok:
            log.ok("Pool destroyed")
        else:
            log.warn("Could not destroy pool — continuing with wipe")

    # ── Overwrite start ──────────────────────────────────────────────────
    log.step(2, 5, "Overwriting first 10MB...")
    _ = sh.run(f"dd if=/dev/urandom of={target_dev} bs=1M count=10 conv=fsync", log=log)
    log.ok("First 10MB overwritten")

    # ── Overwrite end ────────────────────────────────────────────────────
    log.step(3, 5, "Overwriting last 10MB...")
    disk_bytes = sh.run(f"blockdev --getsize64 {target_dev}").output
    if disk_bytes.isdigit() and int(disk_bytes) > 20 * 1024 * 1024:
        offset = (int(disk_bytes) - 10 * 1024 * 1024) // 512
        _ = sh.run(
            f"dd if=/dev/urandom of={target_dev} bs=512 seek={offset} count=20480 conv=fsync",
            log=log,
        )
        log.ok("Last 10MB overwritten")

    # ── Wipe signatures ──────────────────────────────────────────────────
    log.step(4, 5, "Wiping filesystem signatures...")
    _ = sh.run(f"wipefs -a {target_dev}", log=log)
    log.ok("Signatures wiped")

    # ── Destroy partition table ──────────────────────────────────────────
    log.step(5, 5, "Destroying partition table...")
    _ = sh.run(f"sgdisk --zap-all {target_dev}", log=log)
    log.ok("Partition table destroyed")

    # Remove from known_drives.json
    if matched_pool and matched_pool in cfg.known_drives:
        del cfg.known_drives[matched_pool]
        cfg.save_drives()
        log.ok(f"Removed '{matched_pool}' from known_drives.json")

    log.banner_ok(
        "DRIVE PURGED",
        [
            f"Device: {target_dev} ({model})",
            "Drive is blank and ready for reuse.",
        ],
    )
