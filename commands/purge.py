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

# One line of purge's report (M4aF-4): True done, False failed, None not done
# (with the reason). The final banner lists every line, so it says what
# happened to the disk, not only what failed.
Mark = tuple[bool | None, str]


def _destroy_labelled_pool(
    name: str, guid: str, vdev: str, zfs: ZFS, log: Log
) -> tuple[Mark, bool]:
    """Destroy the pool whose label is on the disk being purged.

    The pool is imported device-exact under a private altroot (I-G) so
    nothing it contains is mounted over the running system. A pool of the
    same name that is imported with a different GUID lives on another disk
    and is left alone; the wipe below erases this disk's labels anyway.

    Returns the report line and whether the wipe must not run: a pool that
    is still imported from this disk would rewrite its labels.
    """
    if name in SYSTEM_POOLS:
        log.warn(f"Label says '{name}': not importing a system pool name; wiping only")
        return (None, f"pool {name} not imported (a system pool name): wiped only"), False
    if zfs.pool_exists(name):
        if zfs.pool_guid(name) != guid:
            log.warn(f"A different pool named '{name}' is imported: wiping only")
            return (None, f"pool {name} not destroyed: the imported one is another disk's"), False
    elif not zfs.import_backup_pool(name, vdev):
        log.warn(f"Could not import '{name}': the wipe erases its labels; verified below")
        return (None, f"pool {name} could not be imported: its labels are wiped below"), False
    # The pool's state goes with it: keep it first (I7).
    _ = forensics.capture(name, "before purge destroys it", log)
    r = sh.run(f"zpool destroy {name}", log=log)
    if r.ok:
        log.ok(f"Pool {name} destroyed")
        return (True, f"pool {name} destroyed"), False
    return (False, f"zpool destroy {name} failed: {_error(r)}"), True


def _error(r: sh.RunResult) -> str:
    return r.stderr.strip() or f"exit {r.returncode}"


def _present(path: str) -> bool:
    """Whether ``path`` exists; its own function so tests can answer for one path."""
    return os.path.exists(path)


def _step(cmd: str, done: str, what: str, marks: list[Mark], log: Log) -> bool:
    """Run one wipe command and add its line to the report."""
    r = sh.run(cmd, log=log)
    marks.append((True, done) if r.ok else (False, f"{what} failed: {_error(r)}"))
    if r.ok:
        log.ok(done[0].upper() + done[1:])
    return r.ok


def _wipe(target_dev: str, part1: str, log: Log) -> list[Mark]:
    """Overwrite both ends, wipe signatures and the partition table, verify.

    Every step runs even when an earlier one failed (more erased is never
    worse); the report has one line per step and per check.
    """
    marks: list[Mark] = []
    log.step(2, 5, "Overwriting first 10MB...")
    _ = _step(
        f"dd if=/dev/urandom of={target_dev} bs=1M count=10 conv=fsync",
        "first 10MB overwritten",
        "overwrite of the first 10MB",
        marks,
        log,
    )

    log.step(3, 5, "Overwriting last 10MB...")
    disk_bytes = sh.run(f"blockdev --getsize64 {target_dev}").output
    if not disk_bytes.isdigit():
        marks.append((False, f"last 10MB not overwritten: size of {target_dev} unreadable"))
    elif int(disk_bytes) > 20 * 1024 * 1024:
        offset = (int(disk_bytes) - 10 * 1024 * 1024) // 512
        _ = _step(
            f"dd if=/dev/urandom of={target_dev} bs=512 seek={offset} count=20480 conv=fsync",
            "last 10MB overwritten",
            "overwrite of the last 10MB",
            marks,
            log,
        )
    else:
        marks.append((None, "last 10MB: the disk is 20MB or less, covered by the first"))

    log.step(4, 5, "Wiping filesystem signatures...")
    _ = _step(f"wipefs -a {target_dev}", "signatures wiped", "wipefs -a", marks, log)
    # The ZFS labels live in the partition: read them there while it exists.
    # flushbufs: the writes went through the whole disk's node, not part1's.
    if _present(part1):
        _ = sh.run(f"blockdev --flushbufs {part1}")
        label = read_pool_label(part1)
        if label is not None:
            marks.append((False, f"ZFS label still on {part1}: pool {label.name} ({label.guid})"))
        else:
            marks.append((True, "no ZFS label left on the first partition"))

    log.step(5, 5, "Destroying partition table...")
    _ = _step(
        f"sgdisk --zap-all {target_dev}",
        "partition table destroyed",
        "sgdisk --zap-all",
        marks,
        log,
    )

    _ = sh.run(f"blockdev --flushbufs {target_dev}")
    parts = sh.run(f"lsblk -nr -o NAME,TYPE {target_dev}")
    left = [ln.split()[0] for ln in parts.lines if ln.split()[1:2] == ["part"]]
    marks.append(
        (False, f"partitions still present: {', '.join(left)}")
        if left
        else (True, "no partition left")
    )
    # blkid -p: exit 2 is "nothing found"; 0 means a signature is still there.
    probe = sh.run(f"blkid -p {target_dev}")
    if probe.returncode == 0:
        marks.append((False, f"signature still on {target_dev}: {probe.output}"))
    elif probe.returncode != 2:
        marks.append((False, f"blkid -p {target_dev} failed: exit {probe.returncode}"))
    else:
        marks.append((True, "no signature found (blkid -p)"))
    return marks


def _report_lines(marks: list[Mark]) -> list[str]:
    sign = {True: "✓", False: "✗", None: "-"}
    return [f"{sign[ok]} {text}" for ok, text in marks]


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

    log.banner("PURGE BACKUP DRIVE", "⚠  IRREVERSIBLE OPERATION", warn=True)  # M4aF-5

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
    stop = False
    if ident.label:
        first, stop = _destroy_labelled_pool(
            ident.label.name, ident.label.guid, ident.part1, zfs, log
        )
    else:
        log.info("No ZFS label on this disk: nothing to destroy")
        first = (None, "no ZFS label on this disk: no pool to destroy")
    if stop:
        marks = [first, (None, "nothing overwritten: the pool is still imported from this disk")]
    else:
        marks = [first, *_wipe(target_dev, ident.part1, log)]

    if any(ok is False for ok, _ in marks):
        if stop and ident.label:
            advice = [f"Export it (zpool export {ident.label.name}), then purge again."]
        else:
            advice = [
                "The disk may still hold the pool. Purge again, or look:",
                f"  blkid -p {target_dev}; lsblk {target_dev}",
            ]
        if matched:
            kept = f"Kept: the registry entries {', '.join(matched)} and the pool's anchors."
        elif ident.label:
            kept = "Not registered. The pool's anchors in origin, if any, are kept."
        else:
            kept = "Not registered and no pool label: nothing to keep."
        log.banner_error("PURGE INCOMPLETE", [*_report_lines(marks), "", *advice, kept])
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
