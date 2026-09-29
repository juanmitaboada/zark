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

from lib import sh
from lib.cleanup import flush_device_cache, prompt_eject_or_attach
from lib.config import Config
from lib.drives import SYSTEM_POOLS, validate_external_block_device
from lib.identity import match_registry
from lib.log import Log
from lib.zfs import ZFS


def _destroy_labelled_pool(name: str, guid: str, vdev: str, zfs: ZFS, log: Log) -> None:
    """Destroy the pool whose label is on the disk being purged.

    The pool is imported device-exact under a private altroot (I-G) so
    nothing it contains is mounted over the running system. A pool of the
    same name that is imported with a different GUID lives on another disk
    and is left alone; the wipe below erases this disk's labels anyway.
    """
    if name in SYSTEM_POOLS:
        log.warn(f"Label says '{name}' — not importing a system pool name; wiping only")
        return
    if zfs.pool_exists(name):
        if zfs.pool_guid(name) != guid:
            log.warn(f"A different pool named '{name}' is imported — wiping only")
            return
    elif not zfs.import_backup_pool(name, vdev):
        log.warn(f"Could not import '{name}' — continuing with wipe")
        return
    r = sh.run(f"zpool destroy {name}", log=log)
    if r.ok:
        log.ok(f"Pool {name} destroyed")
    else:
        log.warn(f"Could not destroy {name} — continuing with wipe")


def run(
    args: list[str],
):  # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    """Main entry point for the purge command."""
    log = Log()
    cfg = Config.load()
    cfg.check_registry(log, fatal=True)
    zfs = ZFS(log)
    target_arg = args[0] if args else ""

    ident = validate_external_block_device(target_arg, log, command="purge")
    target_dev = ident.disk

    log.banner("PURGE BACKUP DRIVE", "⚠  IRREVERSIBLE OPERATION")

    log.info(f"Device: {target_dev}  ({ident.by_id_path or 'no by-id name'})")
    log.info(f"Model:  {ident.model}  Serial: {ident.serial}")
    log.info(f"Size:   {ident.size}  Transport: {ident.transport}")
    if ident.label:
        log.info(f"Pool on disk: {ident.label.name} (GUID {ident.label.guid})")

    matched = match_registry(ident, cfg.known_drives)
    if matched:
        log.ok(f"Drive recognized as: {', '.join(matched)}")
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

    log.info(f"Type the kernel device name to confirm ({ident.name}):")
    try:
        c2 = input("    > ").strip()
    except EOFError:
        c2 = ""
    if c2 != ident.name:
        log.fatal("Device name mismatch. Aborted.")

    # ── Destroy pool ─────────────────────────────────────────────────────
    log.step(1, 5, "Destroying ZFS pool...")
    if ident.label:
        _destroy_labelled_pool(ident.label.name, ident.label.guid, ident.part1, zfs, log)
    else:
        log.info("No ZFS label on this disk — nothing to destroy")

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

    # Remove every registry entry that described this disk
    if matched:
        for name in matched:
            del cfg.known_drives[name]
        cfg.save_drives()
        log.ok(f"Removed {', '.join(matched)} from known_drives.json")

    # Flush kernel buffers before asking about the bridge power-down.
    # Even though purge does not leave a pool behind, the dd/wipefs/
    # sgdisk writes pass through the same write-back caching, and a
    # dirty unplug here can leave residual signatures on NAND that
    # confuse a later `prepare`.
    flush_device_cache(log)

    label = matched[0] if matched else target_dev
    log.banner_ok(
        "DRIVE PURGED",
        [
            f"Device: {target_dev} ({ident.model})",
            "Drive is blank and ready for reuse.",
        ],
    )

    # Default to ejecting: a purged drive is, by definition, being
    # retired or repurposed — the operator is most likely about to
    # disconnect it. Answering "n" is the escape hatch for the rarer
    # case of preparing a freshly-purged drive in the same session.
    prompt_eject_or_attach(
        target_dev,
        label,
        log,
        default_eject=True,
        autoeject=False,  # the registry entry is gone; ask the operator
    )
