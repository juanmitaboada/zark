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
zark fullcheck - verify a backup drive's copies without altering them (M19).

``zark fullcheck [scrub | restore]``; both parts when neither word is given
(M19-2 2a, Design C), in this order, each import a fresh one by zark under
M18's rules (the dpkg hold around ``zpool import`` alone):

  1. the restore check, read-only (M19-3 3a, M19-4 4a): the pool imported
     ``readonly=on``, recover's point question (the newest complete point by
     default), the drive's keystore opened read-only from its head zvol and
     every key loaded, each dataset recover would restore mounted from the
     point's snapshot read-only and every file read; nothing is written;
  2. the scrub of the whole pool (M19-10 10a), read-write and unattended,
     behind rule R1 (lib/scrub.py); a Ctrl-C pauses it (the pause belongs to
     this step alone, M19R-5) and the next ``fullcheck`` resumes it; a
     suspended pool is neither paused nor exported (M19-13 13a);
  3. the read-back of the scrub's writes, with M18's wait for apt;
  4. the result in the registry's ``last_check``, the banner, the eject
     question.

The scrub writes only its own progress, the labels and the pool's history,
and rewrites a damaged metadata copy from a good one; it changes no file,
snapshot or property (R2). A pool imported by anyone is refused before the
first question (M19-11 11a). On a live only the restore check runs and
nothing is recorded (M19-6 6a).
"""

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NoReturn

from lib import cli_spec, drive_marks, exclusions, scrub, sh, signals, survey, zfs_features
from lib.backup_layout import find_be
from lib.cleanup import Cleanup, prompt_eject_or_attach
from lib.config import Config, now_utc_iso, zark_command
from lib.drives import ConnectedDrive, backup_device, scan_connected_drives, select_drive
from lib.fullcheck import (
    CHECK_MNT,
    MAX_ERROR_PATHS,
    Check,
    ReadResult,
    last_check,
    rate,
    read_tree,
    restore_record,
    save,
    scrub_record,
)
from lib.keystore import Keystore, open_keystore
from lib.log import Log
from lib.restore_plan import RestorePlan, build_plan, choose_point, list_snapshots
from lib.restore_points import (
    Point,
    complete_points,
    point_name,
    restore_points,
    run_complete_points,
)
from lib.zfs import ZFS, Damage, KeyStatusError

SPEC = cli_spec.command("fullcheck")
SCRUB, RESTORE = SPEC.word("scrub"), SPEC.word("restore")
TOTAL_STEPS = 4
POLL_S = 5.0
REPORT_EVERY_S = 60.0
# The figures of the plan's estimate when the drive has no measured rate
# (a 2.5" disk and a SATA SSD, each behind USB 3).
DISK_RATE, SSD_RATE = 100 * 1000**2, 400 * 1000**2
SKIP_NOTE = "It would be reported as not done."
R2 = (
    "The scrub writes only its own progress, the pool's labels and history, and "
    + "rewrites a damaged metadata copy from a good one; it changes no file, "
    + "snapshot or property."
)


@dataclass
class CheckRun:  # pylint: disable=too-many-instance-attributes
    """What this run checked so far, recorded once at its end."""

    pool: str
    guid: str
    live: bool
    check: Check = field(default_factory=lambda: Check(started_at=now_utc_iso()))
    recorded: bool = False
    lines: list[str] = field(default_factory=list)  # the result banner's findings
    damage: bool = False

    def record(self, result: str, message: str, log: Log) -> None:
        """The run's ``last_check``, once (nothing from a live)."""
        if self.recorded:
            return
        self.recorded = True
        self.check.result = result
        self.check.message = message
        self.check.ended_at = now_utc_iso()
        if not self.live:
            _ = save(self.guid, self.check, log)


def parts_asked(args: list[str], log: Log) -> tuple[bool, bool]:
    """(restore, scrub) as the words ask; both without a word."""
    words = [a for a in args if not a.startswith("-")]
    unknown = [w for w in words if w not in (SCRUB, RESTORE)]
    if unknown or len(words) > 1:
        log.fatal(f"Usage: {zark_command()} fullcheck [{SCRUB} | {RESTORE}]")
    if not words:
        return True, True
    return words[0] == RESTORE, words[0] == SCRUB


def precondition(pool: str, guid: str) -> str:
    """Why ``fullcheck`` refuses to start: the pool is imported by anyone
    (M19-11 11a, M19R-3); "" when it is not. Before the first question."""
    r = sh.run("zpool list -H -o name,guid")
    if not r.ok:
        return f"cannot list the imported pools: {r.stderr.strip() or 'zpool list failed'}"
    for line in r.lines:
        fields = line.split("\t")
        if fields[0] != pool:
            continue
        actual = fields[1] if len(fields) > 1 else ""
        if actual == guid:
            return (
                f"{pool} is already imported (a mount or another command): "
                + f"export it first: {zark_command()} umount"
            )
        return (
            f"a pool named {pool} is imported from another disk (GUID {actual or '?'}, "
            + f"not {guid}): export it first: sudo zpool export {pool}"
        )
    return ""


def _fmt_s(seconds: float) -> str:
    minutes = int(seconds // 60)
    return (
        f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{max(1, minutes)} min"
    )


def _gib(n: int | None) -> str:
    return "?" if n is None else f"{n / (1 << 30):.1f} GiB"


def estimate(size: int, measured: float | None, *, stream: bool) -> str:
    """How long ``size`` bytes take: from the drive's measured rate, else
    the plan's figures for a stream (the scrub) and "unknown" for a walk
    (M19R-9: a walk of small files runs far below a stream's rate)."""
    if measured:
        return f"about {_fmt_s(size / measured)} at this drive's last measured rate"
    if stream:
        return (
            f"about {_fmt_s(size / DISK_RATE)} on a disk, {_fmt_s(size / SSD_RATE)} on an SSD"
            + " (no rate measured on this drive yet)"
        )
    return "unknown until measured (the first full check measures it)"


def _fail(cr: CheckRun, message: str, log: Log) -> NoReturn:
    """A check that could not run: recorded as ``failed``, then FATAL."""
    cr.record("failed", message, log)
    log.fatal(message)


# ── step 1: the restore check ─────────────────────────────────────────────


def _repeat_restore(pool: str, log: Log) -> bool:
    """After a scrub paused by an earlier run: read the point again first? (default no)"""
    scan = scrub.read(pool)  # a read; R1 applies to zpool scrub only
    if scan is None or not (scan.scrubbing and scan.paused):
        return True
    done = f" at {scan.percent} %" if scan.percent is not None else ""
    return log.ask(f"A scrub of {pool} is paused{done}: repeat the restore check first?", False)


def restore_part(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    cr: CheckRun,
    zfs: ZFS,
    device: str,
    cleanup: Cleanup,
    log: Log,
    ask_repeat: bool,
) -> None:
    """Step 1 (M19 2.2): read a point back as recover would restore it."""
    pool = cr.pool
    log.step(1, TOTAL_STEPS, f"Restore check of {pool}, read-only...")
    zfs_features.check_drive(pool, device, log, "zark fullcheck")
    cleanup.track_pool(pool)
    if not zfs.import_backup_pool(pool, device, readonly=True, guid=cr.guid):
        cleanup.untrack_pool(pool)
        _fail(cr, f"Cannot import {pool} read-only", log)
    if ask_repeat and not _repeat_restore(pool, log):
        log.info("Restore check skipped: the paused scrub goes on")
        if zfs.pool_export(pool, wait_udev=True):
            cleanup.untrack_pool(pool)
        return
    point, plan = _open_point(cr, zfs, device, cleanup, log)
    _read_point(cr, point, plan, cleanup, log)
    if not zfs.pool_export(pool, wait_udev=True):
        _fail(cr, f"Could not export {pool} after the restore check", log)
    cleanup.untrack_pool(pool)


def _open_point(  # pylint: disable=too-many-locals
    cr: CheckRun, zfs: ZFS, device: str, cleanup: Cleanup, log: Log
) -> tuple[Point, RestorePlan]:
    """recover's point question, the drive's keys loaded, recover's plan."""
    pool = cr.pool
    try:
        meta = drive_marks.log_metadata(pool, log, strict=True)
    except survey.ReadError as e:
        _fail(cr, f"Cannot read the metadata of {pool}: {e}", log)
    be = find_be(pool, log)
    if not be:
        _fail(cr, f"No boot environment under {pool}/rpool/ROOT", log)
    snaps = [s for s in list_snapshots(pool, log) if s.dataset != "keystore"]
    points = restore_points(snaps, f"rpool/ROOT/{be}")
    if not points:
        _fail(cr, f"No restore point on {pool}", log)
    last = meta.get("last-point", "")
    complete = complete_points(snaps, last)
    point = choose_point(points, log, complete, run_complete_points(snaps, last))
    cr.check.point = point_name(point)
    try:
        excluded = exclusions.read_point_mark(pool, point_name(point), snaps)
    except survey.ReadError as e:
        _fail(cr, f"Cannot read what this point left out on {pool}: {e}", log)
    except ValueError as e:
        log.warn(f"{e}: every dataset is read as if nothing were excluded")
        excluded = None
    ks = Keystore(log)
    if not open_keystore(ks, pool, log, readonly=True):
        _fail(cr, f"Cannot open the keystore of {pool} (wrong passphrase, 3 attempts)", log)
    cleanup.track_keystore(ks)
    loaded = ks.load_pool_keys(f"{pool}/rpool")
    ks.umount()
    try:
        missing = zfs.datasets_needing_key(f"{pool}/rpool", strict=True)
    except KeyStatusError as e:
        _fail(cr, f"Cannot read which keys loaded on {pool}: {e}", log)
    if missing:
        _fail(cr, f"The drive's key did not load for {len(missing)} dataset(s): {missing[0]}", log)
    log.ok(f"Loaded {loaded} encryption key(s) from the drive's keystore")
    return point, build_plan(pool, device, be, point, snaps, log, complete, excluded)


def _read_point(cr: CheckRun, point: Point, plan: RestorePlan, cleanup: Cleanup, log: Log) -> None:
    """Every row recover would restore, mounted from its snapshot and read."""
    rows = [r for r in plan.rows if r.restored and r.snap is not None]
    total = sum(r.referenced for r in rows)
    prev = last_check(Config.load().known_drives.get(cr.pool))
    log.info(
        f"Reading {len(rows)} dataset(s) of point {point.label_utc()} ({_gib(total)}): "
        + estimate(total, rate(prev, "restore", "bytes"), stream=False)
    )
    began = time.monotonic()
    read = ReadResult()
    for n, row in enumerate(rows):
        assert row.snap is not None
        got = read_snapshot(
            f"{cr.pool}/{row.snap.stored}@{row.snap.name}", f"{CHECK_MNT}/{n}", cleanup, log
        )
        read.files += got.files
        read.bytes += got.bytes
        read.errors += got.errors
        read.error_paths += got.error_paths[: max(0, MAX_ERROR_PATHS - len(read.error_paths))]
        mark = f"{got.errors} error(s)" if got.errors else "ok"
        log.info(f"  {row.rel}: {got.files} file(s), {_gib(got.bytes)}, {mark}")
    cr.check.restore = restore_record(len(rows), read, time.monotonic() - began)
    name = point_name(point)
    if read.errors:
        cr.damage = True
        cr.lines += [f"Restore check: {read.errors} read error(s) in point {name}:"]
        cr.lines += [f"  {p}" for p in read.error_paths]
    else:
        cr.lines.append(
            f"Restore check: point {name} read back: {read.files} file(s), "
            + f"{_gib(read.bytes)} in {len(rows)} dataset(s)"
        )


def read_snapshot(full: str, mnt: str, cleanup: Cleanup, log: Log) -> ReadResult:
    """Mount one snapshot read-only at ``mnt`` (its own directory), read it, unmount."""
    _ = sh.run(f"mkdir -p {mnt}")
    cleanup.track_mount(mnt)
    if not sh.run(f"mount -t zfs -o ro {full} {mnt}", log=log).ok:
        out = ReadResult()
        out.failed(full, OSError(0, "could not be mounted read-only"))
        _remove_dir(mnt)
        return out
    try:
        return read_tree(mnt)
    finally:
        if not sh.run(f"umount {mnt}", log=log).ok:
            log.warn(f"Could not unmount {mnt}; the cleanup tries again")
        else:
            _remove_dir(mnt)


def _remove_dir(path: str) -> None:
    """An empty mount directory goes; one still holding anything stays."""
    try:
        os.rmdir(path)
    except OSError:
        pass


# ── step 2: the scrub ─────────────────────────────────────────────────────


def _pause_on_exit(cr: CheckRun, cleanup: Cleanup, log: Log) -> None:
    """The cleanup's first action while step 2 runs (M19R-5, M19-13 13a)."""
    pool = cr.pool
    if scrub.is_suspended(pool):
        suspended(cr, cleanup, log)
        return
    if scrub.pause(pool, log):
        scan = scrub.read(pool)
        at = f" at {scan.percent} %" if scan is not None and scan.percent is not None else ""
        log.warn(
            f"The scrub of {pool} is paused{at}; `{zark_command()} fullcheck` resumes it "
            + "(from its last checkpoint, up to 2 h back)"
        )
        cr.check.scrub = scrub_record("paused")
    cr.record("not_finished", "the check was interrupted during the scrub", log)


def suspended(cr: CheckRun, cleanup: Cleanup, log: Log) -> None:
    """A suspended pool: no pause and no export, either would wait for the drive."""
    pool = cr.pool
    cleanup.untrack_pool(pool)
    message = (
        f"{pool} is suspended (the drive went away while it was imported): plug it in "
        + f"again, then `sudo zpool clear {pool}` and `sudo zpool export {pool}`; the "
        + f"scrub resumes at the next `{zark_command()} fullcheck`"
    )
    log.error(message)
    cr.check.scrub = scrub_record("not_run")
    cr.record("not_finished", message, log)


def _report(scan: scrub.Scan, began: float, pool: str, log: Log) -> None:
    pct = f"{scan.percent} %" if scan.percent is not None else "? %"
    issued, total = scan.issued, scan.bytes_total
    line = f"Scrub of {pool}: {pct}"
    if issued is not None and total:
        line += f" ({_gib(issued)} of {_gib(total)})"
        spent = time.monotonic() - began
        if spent > 0 and issued:
            line += f", {issued / spent / 1e6:.0f} MB/s"
    log.info(line)


def scrub_part(cr: CheckRun, zfs: ZFS, device: str, cleanup: Cleanup, log: Log) -> bool:
    """Step 2 (M19 2.1); True when the scrub ran to its end."""
    pool = cr.pool
    log.step(2, TOTAL_STEPS, f"Scrub of {pool}, read-write...")
    log.info(R2)
    zfs_features.check_drive(pool, device, log, "zark fullcheck", writing=True)
    cleanup.track_pool(pool)
    if not zfs.import_backup_pool(pool, device, readonly=False, guid=cr.guid, pause_scrub=False):
        cleanup.untrack_pool(pool)
        _fail(cr, f"Cannot import {pool} read-write", log)
    scan = scrub.read(pool)
    if scan is None:
        _fail(cr, f"Cannot read the scrub state of {pool}", log)
    if scan.resilvering:
        _fail(cr, f"{pool} is resilvering: run the full check once it has ended", log)
    action = _action(cr, cleanup, log)
    cleanup.track_action(action)
    if scan.running:
        log.info(f"The scrub of {pool} resumed with the import")
    elif not scrub.start(pool, log):
        cleanup.untrack_action(action)
        _fail(cr, f"Cannot start the scrub of {pool}: {scrub.refusal(pool) or 'see the log'}", log)
    elif scan.scrubbing and scan.paused:
        log.info(
            f"Resuming the scrub of {pool}" + (f" at {scan.percent} %" if scan.percent else "")
        )
    prev = last_check(Config.load().known_drives.get(pool))
    began = time.monotonic()
    shown = 0.0
    reported = False
    while True:
        time.sleep(POLL_S)
        if scrub.is_suspended(pool):
            cleanup.untrack_action(action)
            suspended(cr, cleanup, log)
            return False
        now = scrub.read(pool)
        if now is None:
            continue
        if not reported and now.bytes_total:
            reported = True
            log.info(
                f"Scrubbing {_gib(now.bytes_total)}: "
                + estimate(now.bytes_total, rate(prev, "scrub", "examined_bytes"), stream=True)
            )
        if now.function == "SCRUB" and now.state == "SCANNING":
            if time.monotonic() - shown >= REPORT_EVERY_S:
                shown = time.monotonic()
                _report(now, began, pool, log)
            continue
        break
    cleanup.untrack_action(action)
    return _scrub_ended(cr, zfs, now, time.monotonic() - began, cleanup, log)


def _action(cr: CheckRun, cleanup: Cleanup, log: Log) -> Callable[[], None]:
    def action() -> None:
        _pause_on_exit(cr, cleanup, log)

    return action


def _scrub_ended(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    cr: CheckRun, zfs: ZFS, scan: scrub.Scan, seconds: float, cleanup: Cleanup, log: Log
) -> bool:
    pool = cr.pool
    damage: Damage = zfs.pool_damage(pool)
    finished = scan.function == "SCRUB" and scan.state == "FINISHED"
    cr.check.scrub = scrub_record(
        "finished" if finished else "not_run",
        examined=scan.examined,
        repaired=scan.processed,
        errors=scan.errors,
        permanent_errors=len(damage.errors),
        seconds=seconds,
    )
    if not zfs.pool_export(pool, wait_udev=True):
        _fail(cr, f"Could not export {pool} after the scrub", log)
    cleanup.untrack_pool(pool)
    if not finished:
        cr.record("not_finished", f"the scrub of {pool} ended {scan.state.lower()}", log)
        return False
    found = bool(scan.errors) or bool(scan.processed) or damage.any
    if found:
        cr.damage = True
        repaired = f", {_gib(scan.processed)} repaired" if scan.processed else ""
        cr.lines.append(f"Scrub: {scan.errors or 0} error(s){repaired}")
        cr.lines += [f"  {c}" for c in damage.counters]
        cr.lines += [f"  {e}" for e in damage.errors[:20]]
    else:
        cr.lines.append(f"Scrub: every block of {pool} read back clean ({_gib(scan.examined)})")
    return True


# ── the command ───────────────────────────────────────────────────────────


def _finish(cr: CheckRun, zfs: ZFS, device: str, cfg: Config, log: Log) -> None:
    """Step 4: the record, the banner, the eject question; exit 1 unless clean."""
    log.step(4, TOTAL_STEPS, "Result")
    if cr.recorded:
        title, ok = "FULL CHECK NOT FINISHED", False
        cr.lines.append(cr.check.message)
    elif cr.damage:
        cr.record("damage", "the full check found damage", log)
        title, ok = "FULL CHECK FOUND DAMAGE", False
    else:
        cr.record("clean", "", log)
        title, ok = "FULL CHECK PASSED", True
    if cr.live:
        cr.lines.append("On a live session nothing is recorded")
    if ok:
        log.banner_ok(title, cr.lines)
    else:
        log.banner_error(title, cr.lines)
    if zfs.pool_exists(cr.pool):
        log.warn(f"{cr.pool} is still imported: no eject")
    else:
        prompt_eject_or_attach(device, cr.pool, log, True, cfg.drive_autoeject(cr.pool))
    if not ok:
        sys.exit(1)


def _choose(cfg: Config, log: Log) -> ConnectedDrive | None:
    log.info("Scanning for backup drives...")
    drives = scan_connected_drives(cfg, log)
    if not drives:
        log.warn("No backup drives connected")
        return None
    return select_drive(drives, log, known_only=True)


def run(args: list[str]) -> None:  # pylint: disable=too-many-branches
    """Verify a backup drive's copies without altering them."""
    log = Log()
    cfg = Config.load()
    cfg.check_registry(log, fatal=False)
    do_restore, do_scrub = parts_asked(args, log)
    live = sh.is_live_usb()
    if live and do_scrub and not do_restore:
        log.fatal("The scrub imports the drive read-write: run it on the installed system")
    if live and do_scrub:
        log.info("On a live session only the restore check runs (the scrub writes to the pool)")
        do_scrub = False
    log.banner("FULL CHECK", "Verify a backup drive's copies without altering them")
    drive = _choose(cfg, log)
    if drive is None:
        return
    why = precondition(drive.name, drive.guid)
    if why:
        log.fatal(why)
    device = backup_device(drive)
    if not device:
        log.fatal(f"Cannot locate the device of {drive.name} for a device-exact import")
    zfs = ZFS(log)
    cleanup = Cleanup(log)
    cleanup.register()
    cr = CheckRun(drive.name, drive.guid, live)
    # Anything that ends the run before step 4 records it (nothing from a live).
    on_exit = _on_exit(cr, log)
    cleanup.track_action(on_exit)
    if do_restore:
        restore_part(cr, zfs, device, cleanup, log, ask_repeat=do_scrub)
    else:
        log.step(1, TOTAL_STEPS, "Restore check: not asked")
    if do_scrub and scrub_part(cr, zfs, device, cleanup, log):
        log.step(3, TOTAL_STEPS, f"Reading {drive.name} back after the scrub...")
        rb = zfs.verify_exported_pool_readback(drive.name, device, cleanup, skip_note=SKIP_NOTE)
        if rb.skipped:
            cr.lines += rb.describe(again=False)
            cr.record("not_finished", rb.summary(drive.name), log)
        elif not rb.ok:
            cr.damage = True
            cr.lines += rb.describe(again=False)
    elif not do_scrub:
        log.step(2, TOTAL_STEPS, "Scrub: not asked")
    cleanup.untrack_action(on_exit)
    _finish(cr, zfs, device, cfg, log)


def _on_exit(cr: CheckRun, log: Log) -> Callable[[], None]:
    """Recorded when the run ends before step 4: ``not_finished`` after a
    signal, ``failed`` otherwise (a fatal error, a crash)."""

    def action() -> None:
        if signals.received() is not None:
            cr.record("not_finished", "the check was interrupted", log)
        else:
            cr.record("failed", "the check stopped before its end", log)

    return action
