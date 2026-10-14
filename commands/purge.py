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
wipes signatures, destroys partition table, then verifies the disk is blank
(M4a-13). Any failed step ends PURGE INCOMPLETE with exit status 1, and the
registry entry and the pool's anchors in origin stay: the disk may still
hold the pool.
"""

import os

from lib import engine, forensics, sh
from lib.cleanup import flush_device_cache, prompt_eject_or_attach
from lib.config import Config
from lib.drives import SYSTEM_POOLS, validate_external_block_device
from lib.identity import match_registry, read_pool_label
from lib.log import Log
from lib.zfs import ZFS


def _destroy_labelled_pool(name: str, guid: str, vdev: str, zfs: ZFS, log: Log) -> str:
    """Destroy the pool whose label is on the disk being purged.

    The pool is imported device-exact under a private altroot (I-G) so
    nothing it contains is mounted over the running system. A pool of the
    same name that is imported with a different GUID lives on another disk
    and is left alone; the wipe below erases this disk's labels anyway.

    Returns "" when the wipe may go on, otherwise why it must not: a pool
    that is still imported from this disk would rewrite its labels.
    """
    if name in SYSTEM_POOLS:
        log.warn(f"Label says '{name}': not importing a system pool name; wiping only")
        return ""
    if zfs.pool_exists(name):
        if zfs.pool_guid(name) != guid:
            log.warn(f"A different pool named '{name}' is imported: wiping only")
            return ""
    elif not zfs.import_backup_pool(name, vdev):
        log.warn(f"Could not import '{name}': the wipe erases its labels; verified below")
        return ""
    # The pool's state goes with it: keep it first (I7).
    _ = forensics.capture(name, "before purge destroys it", log)
    r = sh.run(f"zpool destroy {name}", log=log)
    if r.ok:
        log.ok(f"Pool {name} destroyed")
        return ""
    return f"zpool destroy {name} failed: {r.stderr.strip() or f'exit {r.returncode}'}"


def _step(cmd: str, what: str, failures: list[str], log: Log) -> bool:
    """Run one wipe command; record ``what`` with the error when it fails."""
    r = sh.run(cmd, log=log)
    if not r.ok:
        failures.append(f"{what}: {r.stderr.strip() or f'exit {r.returncode}'}")
    return r.ok


def _wipe(target_dev: str, part1: str, log: Log) -> list[str]:
    """Overwrite both ends, wipe signatures and the partition table, verify.

    Every step runs even when an earlier one failed (more erased is never
    worse); the returned list says what failed or what is still there.
    """
    failures: list[str] = []
    log.step(2, 5, "Overwriting first 10MB...")
    if _step(
        f"dd if=/dev/urandom of={target_dev} bs=1M count=10 conv=fsync",
        "overwrite of the first 10MB",
        failures,
        log,
    ):
        log.ok("First 10MB overwritten")

    log.step(3, 5, "Overwriting last 10MB...")
    disk_bytes = sh.run(f"blockdev --getsize64 {target_dev}").output
    if not disk_bytes.isdigit():
        failures.append(f"size of {target_dev} unreadable: last 10MB not overwritten")
    elif int(disk_bytes) > 20 * 1024 * 1024:
        offset = (int(disk_bytes) - 10 * 1024 * 1024) // 512
        if _step(
            f"dd if=/dev/urandom of={target_dev} bs=512 seek={offset} count=20480 conv=fsync",
            "overwrite of the last 10MB",
            failures,
            log,
        ):
            log.ok("Last 10MB overwritten")

    log.step(4, 5, "Wiping filesystem signatures...")
    if _step(f"wipefs -a {target_dev}", "wipefs", failures, log):
        log.ok("Signatures wiped")
    # The ZFS labels live in the partition: read them there while it exists.
    # flushbufs: the writes went through the whole disk's node, not part1's.
    if os.path.exists(part1):
        _ = sh.run(f"blockdev --flushbufs {part1}")
        label = read_pool_label(part1)
        if label is not None:
            failures.append(f"ZFS label still on {part1}: pool {label.name} ({label.guid})")

    log.step(5, 5, "Destroying partition table...")
    if _step(f"sgdisk --zap-all {target_dev}", "sgdisk --zap-all", failures, log):
        log.ok("Partition table destroyed")

    _ = sh.run(f"blockdev --flushbufs {target_dev}")
    parts = sh.run(f"lsblk -nr -o NAME,TYPE {target_dev}")
    left = [ln.split()[0] for ln in parts.lines if ln.split()[1:2] == ["part"]]
    if left:
        failures.append(f"partitions still present: {', '.join(left)}")
    # blkid -p: exit 2 is "nothing found"; 0 means a signature is still there.
    probe = sh.run(f"blkid -p {target_dev}")
    if probe.returncode == 0:
        failures.append(f"signature still on {target_dev}: {probe.output}")
    elif probe.returncode != 2:
        failures.append(f"blkid -p {target_dev} failed: exit {probe.returncode}")
    return failures


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
    c1 = log.ask_text("    > ", accept=("yes",), label=f"Type 'yes' to purge {ident.disk}")
    if c1 != "yes":
        log.info("Aborted")
        return

    log.info(f"Type the kernel device name to confirm ({ident.name}):")
    c2 = log.ask_text(
        "    > ",
        accept=(ident.name,),
        label=f"Type the kernel device name ({ident.name})",
    )
    if c2 != ident.name:
        log.fatal("Device name mismatch. Aborted.")

    # ── Destroy pool ─────────────────────────────────────────────────────
    log.step(1, 5, "Destroying ZFS pool...")
    stop = ""
    if ident.label:
        stop = _destroy_labelled_pool(ident.label.name, ident.label.guid, ident.part1, zfs, log)
    else:
        log.info("No ZFS label on this disk: nothing to destroy")
    failures = [stop] if stop else _wipe(target_dev, ident.part1, log)

    if failures:
        if stop and ident.label:
            advice = [
                "Nothing was overwritten: the pool is still imported from this",
                f"disk. Export it (zpool export {ident.label.name}), then purge again.",
            ]
        else:
            advice = [
                "The disk may still hold the pool. Purge again, or look:",
                f"  blkid -p {target_dev}; lsblk {target_dev}",
            ]
        log.banner_error(
            "PURGE INCOMPLETE",
            [
                *(f"✗ {f}" for f in failures),
                "",
                *advice,
                "Registry entries and the pool's anchors in origin are kept.",
            ],
        )
        raise SystemExit(1)

    # The wiped pool's anchors in origin (M2R-6): nothing can use them now.
    anchors_left = ""
    if ident.label and ident.label.name not in SYSTEM_POOLS:
        anchors_left = engine.drop_anchors_of(ident.label.guid, log)

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
            *([anchors_left] if anchors_left else []),
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
