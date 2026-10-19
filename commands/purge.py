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
zark purge - Securely wipe a managed backup drive.

Destroys ZFS pool, overwrites start/end with random data,
wipes signatures, destroys partition table, then verifies the disk is blank
(M4a-13). Any failed step ends PURGE INCOMPLETE with exit status 1, and the
registry entry and the pool's anchors in origin stay: the disk may still
hold the pool.
"""

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple

from lib import anchors, forensics, sh
from lib.cleanup import flush_device_cache, prompt_eject_or_attach
from lib.config import Config
from lib.drives import SYSTEM_POOLS, validate_external_block_device
from lib.identity import match_registry, read_pool_label
from lib.log import Log
from lib.zfs import ZFS, backup_altroot

# One line of purge's report (M4aF-4): True done, False failed, None not done
# (with the reason). The final banner lists every line, so it says what
# happened to the disk, not only what failed.
Mark = tuple[bool | None, str]


class Wiped(NamedTuple):
    """The wipe's report lines, and whether every check after it came back clean."""

    marks: list[Mark]
    checks_clean: bool


class Stop(NamedTuple):
    """Why purge overwrites nothing after the destroy step, and what to do."""

    reason: str
    advice: str


def _destroy_labelled_pool(  # pylint: disable=too-many-return-statements
    name: str, guid: str, vdev: str, zfs: ZFS, log: Log
) -> tuple[Mark, Stop | None]:
    """Destroy the pool whose label is on the disk being purged.

    The pool is imported device-exact under a private altroot (I-G) so
    nothing it contains is mounted over the running system. A pool of the
    same name that is imported with a different GUID lives on another disk
    and is left alone; the wipe below erases this disk's labels anyway. A
    pool found imported read-only is exported and imported read-write
    first: destroying it read-only returns 0 but writes nothing to its
    labels (M4aR-1).

    Returns the report line and, when the wipe must not run, why and what
    to do: a pool still imported from this disk would rewrite its labels,
    and one that was not destroyed is still on it.
    """
    if name in SYSTEM_POOLS:
        log.warn(f"Label says '{name}': not importing a system pool name; wiping only")
        return (None, f"pool {name} not imported (a system pool name): wiped only"), None
    found = zfs.pool_exists(name)
    if found and zfs.pool_guid(name) != guid:
        log.warn(f"A different pool named '{name}' is imported: wiping only")
        return (None, f"pool {name} not destroyed: the imported one is another disk's"), None
    if not found and not zfs.import_backup_pool(name, vdev):
        log.warn(f"Could not import '{name}': the wipe erases its labels; verified below")
        return (None, f"pool {name} could not be imported: its labels are wiped below"), None
    # The pool's state goes with it: keep it first, as purge found it (I7).
    _ = forensics.capture(name, "before purge destroys it", log)
    export_first = Stop(
        "nothing overwritten: the pool is still imported from this disk",
        f"Export it (zpool export {name}), then purge again.",
    )
    done = f"pool {name} destroyed"
    if found and sh.run(f"zpool get -H -o value readonly {name}").output == "on":
        r = sh.run(f"zpool export {name}", log=log)
        if not r.ok:
            return (
                False,
                f"pool {name} is imported read-only and could not be exported: {_error(r)}",
            ), export_first
        if not zfs.import_backup_pool(name, vdev, guid=guid):
            return (
                False,
                f"pool {name} was exported but could not be imported read-write to destroy it",
            ), Stop(
                "nothing overwritten: the pool was not destroyed",
                "Purge again: it imports the pool read-write itself. To see the disk: "
                + f"zpool import -d {vdev}",
            )
        done += " (it was imported read-only: exported and imported read-write first)"
    r = sh.run(f"zpool destroy {name}", log=log)
    if r.ok:
        log.ok(done[0].upper() + done[1:])
        # The import made it (M4aX.4); rmdir removes only an empty directory.
        _ = sh.run(f"rmdir {backup_altroot(name)}")
        return (True, done), None
    return (False, f"zpool destroy {name} failed: {_error(r)}"), export_first


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


def _label_mark(part1: str, when: str, log: Log) -> Mark:
    """Whether a ZFS label is still readable on ``part1``; logged either way."""
    # flushbufs: the writes went through the whole disk's node, not part1's.
    _ = sh.run(f"blockdev --flushbufs {part1}", log=log)
    label = read_pool_label(part1)
    if label is not None:
        text = f"ZFS label still on {part1} {when}: pool {label.name} ({label.guid})"
        log.error(text)
        return (False, text)
    text = f"no ZFS label on the first partition {when}"
    log.ok(text[0].upper() + text[1:])
    return (True, text)


@contextmanager
def _held_from_udev(disk: str, log: Log) -> Iterator[bool]:
    """Hold a BSD LOCK_EX on the whole disk while the body runs; yield whether it is held.

    When a writer closes the disk, udev re-reads its partition table only if
    it can take LOCK_EX|LOCK_NB on it, and gives up the re-read while that
    fails (src/shared/reread-partition-table.c:255-271); its workers'
    LOCK_SH|LOCK_NB requeues the event (src/udev/udev-worker.c:124-136). The
    lock is waited for up to 10 s (udevd may be probing). The descriptor is
    read-only, so closing it triggers no new event.
    """
    try:
        fd = os.open(disk, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as e:
        log.warn(f"Cannot open {disk} to hold it from udev: {e}")
        yield False
        return
    try:
        for _ in range(100):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        else:
            log.warn(f"Cannot lock {disk}: another program held it for 10 s")
            yield False
            return
        log.dbg(f"Holding {disk} from udev (LOCK_EX)")
        yield True
    finally:
        os.close(fd)


def _wipe(target_dev: str, part1: str, log: Log, steps: bool = True) -> Wiped:
    """Overwrite both ends, wipe signatures and the partition table, verify.

    Every step runs even when an earlier one failed (more erased is never
    worse); the report has one line per step and per check. Nothing is
    written when the disk cannot be held from udev. ``steps`` False: the
    step lines unnumbered, inside another command's own steps (the self-test).
    """

    def say(num: int, msg: str) -> None:
        if steps:
            log.step(num, 5, msg)
        else:
            log.info(f"  {msg}")

    marks: list[Mark] = []
    checks: list[Mark] = []  # also in marks: what was read back, not run
    # The ZFS labels live in part1, and zdb -l reads them at offsets of the
    # device it is given, so only part1's node shows them: they are read
    # there right after the overwrite. That is the check that sees a dd that
    # returned 0 without reaching the medium. udevd re-reads the partition
    # table when a writer closes the disk (60-block.rules "watch"; udev-
    # watch.c), and the first dd destroys the protective MBR, so part1's node
    # would go at once; the disk is held with LOCK_EX meanwhile, which makes
    # udevd leave it alone (systemd docs/BLOCK_DEVICE_LOCKING.md).
    with _held_from_udev(target_dev, log) as held:
        if not held:
            # Unheld, udev would take part1 at the first dd's close and the
            # label check could not run; a second purge then has no part1 to
            # check either (M4aV-1). The contention is transient: stop here.
            marks.append((False, f"nothing overwritten: {target_dev} could not be held from udev"))
            return Wiped(marks, checks_clean=False)
        had_part1 = _present(part1)
        say(2, "Overwriting first 10MB...")
        _ = _step(
            f"dd if=/dev/urandom of={target_dev} bs=1M count=10 conv=fsync",
            "first 10MB overwritten",
            "overwrite of the first 10MB",
            marks,
            log,
        )

        say(3, "Overwriting last 10MB...")
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
        if _present(part1):
            checks.append(_label_mark(part1, "after the overwrite", log))
        elif had_part1:
            # Held, part1 still goes if the drive resets during the dd.
            text = "labels on the first partition not read: it went away during the overwrite"
            log.warn(text[0].upper() + text[1:])
            checks.append((None, text))
        marks += checks

    say(4, "Wiping filesystem signatures...")
    _ = _step(f"wipefs -a {target_dev}", "signatures wiped", "wipefs -a", marks, log)
    if _present(part1):  # wipefs left the partition table
        checks.append(_label_mark(part1, "after wipefs", log))
        marks.append(checks[-1])

    say(5, "Destroying partition table...")
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
    checks.append(
        (False, f"partitions still present: {', '.join(left)}")
        if left
        else (True, "no partition left")
    )
    # blkid -p: exit 2 is "nothing found"; 0 means a signature is still there.
    probe = sh.run(f"blkid -p {target_dev}")
    if probe.returncode == 0:
        checks.append((False, f"signature still on {target_dev}: {probe.output}"))
    elif probe.returncode != 2:
        checks.append((False, f"blkid -p {target_dev} failed: exit {probe.returncode}"))
    else:
        checks.append((True, "no signature found (blkid -p)"))
    marks += checks[-2:]
    return Wiped(marks, checks_clean=all(ok is True for ok, _ in checks))


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
    stop: Stop | None = None
    if ident.label:
        first, stop = _destroy_labelled_pool(
            ident.label.name, ident.label.guid, ident.part1, zfs, log
        )
    else:
        log.info("No ZFS label on this disk: nothing to destroy")
        first = (None, "no ZFS label on this disk: no pool to destroy")
    wiped = Wiped([], checks_clean=False)
    if stop:
        marks = [first, (None, stop.reason)]
    else:
        wiped = _wipe(target_dev, ident.part1, log)
        marks = [first, *wiped.marks]

    if any(ok is False for ok, _ in marks):
        look = f"  blkid -p {target_dev}; lsblk {target_dev}"
        if stop:
            advice = [stop.advice]
        elif wiped.checks_clean and (ident.label is None or first[0] is True):
            # M4aP.11: only a step failed, and no pool is left to find.
            advice = ["Purge again to run the failed step."]
        elif ident.label is None:
            advice = ["Something is left on the disk. Purge again, or look:", look]
        else:
            advice = ["The disk may still hold the pool. Purge again, or look:", look]
        if matched:
            kept = f"Kept: the registry entries {', '.join(matched)} and the pool's anchors."
        elif ident.label:
            kept = "Not registered. The pool's anchors in origin, if any, are kept."
        else:
            kept = "Not registered and no pool label: nothing to keep."
        log.banner_error("PURGE INCOMPLETE", [*_report_lines(marks), "", *advice, kept])
        raise SystemExit(1)

    # The wiped pool's anchors in origin (M2R-6): nothing can use them now,
    # nor those of every entry removed below, matched by drive ID with
    # another GUID (M2V-7 a).
    guids = [ident.label.guid] if ident.label and ident.label.name not in SYSTEM_POOLS else []
    guids += [cfg.known_drives[name].guid for name in matched]
    anchors_left = [
        left
        for guid in dict.fromkeys(g for g in guids if g)
        if (left := anchors.drop_anchors_of(guid, log))
    ]

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

    # How the eject prompt and the unplug banner name it (I18).
    if matched:
        label = matched[0]
    elif ident.label:
        label = ident.label.name
    else:
        label = f"{ident.model} ({target_dev})"
    log.banner_ok(
        "DRIVE PURGED",
        [
            *_report_lines(marks),
            "",
            f"Device: {target_dev} ({ident.model})",
            "Drive is blank and ready for reuse.",
            *anchors_left,
        ],
    )

    # Default to ejecting: a purged drive is, by definition, being
    # retired or repurposed - the operator is most likely about to
    # disconnect it. Answering "n" is the escape hatch for the rarer
    # case of preparing a freshly-purged drive in the same session.
    prompt_eject_or_attach(
        target_dev,
        label,
        log,
        default_eject=True,
        autoeject=False,  # the registry entry is gone; ask the operator
    )
