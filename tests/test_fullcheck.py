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
Tests of ``zark fullcheck`` (M19): the scrub's state and rule R1, the pause
at another command's import, the restore check, the command's flow and its
records.

``tests/test_unit.py`` imports every Test* class here by name, so its runner
finds them; run that file, not this one.
"""

import errno
import json
import os
import signal
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import commands.fullcheck as fc_cmd
from lib import cli_spec, dpkg_lock, fullcheck, scrub
from lib.log import Log
from lib.zfs import ZFS
from tests.mock_sh import MockShell, patch_sh

POOL = "backup2"


def _log() -> Log:
    return Log(log_file="/dev/null")


def status_json(pool: str = POOL, **stats: Any) -> str:
    """``zpool status -j --json-int`` of one pool, its ``scan_stats`` as given
    (none when empty), in the shape of zpool_main.c:9926-10010 (2.4.4)."""
    entry: dict[str, Any] = {"name": pool, "state": "ONLINE"}
    if stats:
        entry["scan_stats"] = stats
    return json.dumps({"output_version": {"command": "zpool status"}, "pools": {pool: entry}})


def scanning(**more: Any) -> dict[str, Any]:
    """A scrub under way: 40 of 100 GiB issued, not paused."""
    gib = 1 << 30
    return {
        "function": "SCRUB",
        "state": "SCANNING",
        "start_time": 1762682400,
        "end_time": 0,
        "to_examine": 100 * gib,
        "examined": 60 * gib,
        "skipped": 0,
        "processed": 0,
        "errors": 0,
        "scrub_pause": 0,
        "issued": 40 * gib,
        **more,
    }


# The text of print_scan_scrub_resilver_status (zpool_main.c:7695-7830, 2.2.2).
TEXT_RUNNING = """  pool: backup2
 state: ONLINE
  scan: scrub in progress since Mon Nov  9 10:00:00 2026
\t60.0G / 100G scanned at 120M/s, 40.0G / 100G issued at 80.0M/s
\t0B repaired, 40.00% done, 00:12:48 to go
config:
"""
TEXT_PAUSED = """  pool: backup2
  scan: scrub paused since Mon Nov  9 10:30:00 2026
\tscrub started on Mon Nov  9 10:00:00 2026
\t60.0G / 100G scanned, 40.0G / 100G issued
\t0B repaired, 40.00% done
"""
TEXT_FINISHED = """  pool: backup2
  scan: scrub repaired 1.50M in 01:02:03 with 3 errors on Mon Nov  9 11:02:03 2026
config:
"""
TEXT_RESILVER = """  scan: resilver in progress since Mon Nov  9 10:00:00 2026
\t1G / 2G scanned, 1G / 2G issued
\t1G resilvered, 50.00% done, no estimated completion time
"""


class TestScrubState:  # pylint: disable=missing-function-docstring
    """M19 1.1: the scan read from JSON on 2.4, from the C-locale text on 2.2."""

    def test_json_scanning(self) -> None:
        s = scrub.parse_json(status_json(**scanning()), POOL)
        assert s is not None and s.running and s.scrubbing and not s.paused
        assert s.percent == 40 and s.bytes_total == 100 << 30 and s.issued == 40 << 30

    def test_json_paused_is_scrubbing_but_not_running(self) -> None:
        s = scrub.parse_json(status_json(**scanning(scrub_pause=1762684200)), POOL)
        assert s is not None and s.scrubbing and s.paused and not s.running

    def test_json_finished_and_canceled(self) -> None:
        done = scrub.parse_json(
            status_json(**scanning(state="FINISHED", errors=2, processed=4096)), POOL
        )
        assert done is not None and not done.scrubbing and done.percent == 100
        assert done.errors == 2 and done.processed == 4096
        gone = scrub.parse_json(status_json(**scanning(state="CANCELED")), POOL)
        assert gone is not None and gone.state == "CANCELED" and not gone.running

    def test_json_resilver_and_error_scrub_are_not_a_running_scrub(self) -> None:
        res = scrub.parse_json(status_json(**scanning(function="RESILVER")), POOL)
        err = scrub.parse_json(status_json(**scanning(function="ERRORSCRUB")), POOL)
        assert res is not None and res.resilvering and not res.running
        assert err is not None and not err.running and not err.resilvering

    def test_json_never_scanned_and_other_pools(self) -> None:
        none = scrub.parse_json(status_json(), POOL)
        assert none is not None and none.function == "NONE"
        assert scrub.parse_json(status_json("other", **scanning()), POOL) is None
        assert scrub.parse_json("not json", POOL) is None
        assert scrub.parse_json(status_json(**scanning(state="WEIRD")), POOL) is None

    def test_json_skipped_bytes_reduce_the_total(self) -> None:
        s = scrub.parse_json(status_json(**scanning(skipped=20 << 30)), POOL)
        assert s is not None and s.bytes_total == 80 << 30 and s.percent == 50

    def test_text_states(self) -> None:
        run = scrub.parse_text(TEXT_RUNNING)
        assert run is not None and run.running and run.percent == 40 and run.processed == 0
        paused = scrub.parse_text(TEXT_PAUSED)
        assert paused is not None and paused.scrubbing and paused.paused and paused.percent == 40
        done = scrub.parse_text(TEXT_FINISHED)
        assert done is not None and done.state == "FINISHED" and done.errors == 3
        assert done.processed == int(1.5 * 1024 * 1024)
        res = scrub.parse_text(TEXT_RESILVER)
        assert res is not None and res.resilvering and not res.running
        none = scrub.parse_text("  scan: none requested\n")
        assert none is not None and none.function == "NONE"
        canceled = scrub.parse_text("  scan: scrub canceled on Mon Nov  9 10:00:00 2026\n")
        assert canceled is not None and canceled.state == "CANCELED"
        assert scrub.parse_text("  pool: x\n") is None
        assert scrub.parse_text("  scan: something new\n") is None

    def test_nice_bytes(self) -> None:
        assert scrub.nice_bytes("0B") == 0
        assert scrub.nice_bytes("1.21G") == int(1.21 * (1 << 30))
        assert scrub.nice_bytes("512") == 512
        assert scrub.nice_bytes("x") is None

    def test_read_takes_json_and_falls_back_to_the_text(self) -> None:
        mock = MockShell()
        mock.on(f"zpool status -j --json-int {POOL}").succeeds(status_json(**scanning()))
        with patch_sh(mock):
            s = scrub.read(POOL)
        assert s is not None and s.percent == 40
        assert mock.was_not_called("env LC_ALL=C")
        mock = MockShell()
        mock.on(f"zpool status -j --json-int {POOL}").fails("invalid option 'j'", rc=2)
        mock.on(f"env LC_ALL=C zpool status {POOL}").succeeds(TEXT_PAUSED)
        with patch_sh(mock):
            s = scrub.read(POOL)
        assert s is not None and s.paused
        mock = MockShell()
        mock.on_prefix("zpool status").fails("no such pool")
        mock.on_prefix("env LC_ALL=C").fails("no such pool")
        with patch_sh(mock):
            assert scrub.read(POOL) is None


def r1_mock(readonly: str | None = "off", health: str | None = "ONLINE") -> MockShell:
    """The two reads of R1; None makes a read fail."""
    mock = MockShell()
    for prop, value in (("readonly", readonly), ("health", health)):
        resp = mock.on(f"zpool get -H -o value {prop} {POOL}")
        if value is None:
            _ = resp.fails("cannot open")
        else:
            _ = resp.succeeds(value)
    mock.on_prefix("zpool scrub").succeeds()
    return mock


class TestScrubRule:  # pylint: disable=missing-function-docstring
    """M19 R1 (M19R-1): no zpool scrub unless read-write and not suspended."""

    def _calls(self, mock: MockShell, fn: Any) -> bool:
        with patch_sh(mock), redirect_stdout(StringIO()):
            return bool(fn(POOL, _log()))

    def test_allowed_on_a_read_write_healthy_pool(self) -> None:
        mock = r1_mock()
        assert self._calls(mock, scrub.start) and mock.was_called(f"zpool scrub {POOL}")
        mock = r1_mock()
        assert self._calls(mock, scrub.pause) and mock.was_called(f"zpool scrub -p {POOL}")
        mock = r1_mock(health="DEGRADED")
        assert self._calls(mock, scrub.start)

    def test_refused_read_only_or_unreadable(self) -> None:
        for readonly in ("on", "", None, "-"):
            for fn in (scrub.start, scrub.pause):
                mock = r1_mock(readonly=readonly)
                assert not self._calls(mock, fn), readonly
                assert mock.was_not_called("zpool scrub"), readonly

    def test_refused_suspended_or_unreadable_health(self) -> None:
        for health in ("SUSPENDED", "", None):
            for fn in (scrub.start, scrub.pause):
                mock = r1_mock(health=health)
                assert not self._calls(mock, fn), health
                assert mock.was_not_called("zpool scrub"), health

    def test_readonly_is_read_before_health(self) -> None:
        mock = r1_mock(readonly="on")
        assert not self._calls(mock, scrub.start)
        assert mock.was_not_called("value health")

    def test_is_suspended(self) -> None:
        for health, want in (("ONLINE", False), ("SUSPENDED", True), ("", True), (None, True)):
            with patch_sh(r1_mock(health=health)):
                assert scrub.is_suspended(POOL) is want, health


class TestPauseInterrupted:  # pylint: disable=missing-function-docstring
    """M19-5 5a, M19R-10: only a running scrub is paused, behind R1."""

    def _run(self, stats: dict[str, Any] | None, **r1: str | None) -> tuple[MockShell, str]:
        mock = r1_mock(**r1)
        mock.on(f"zpool status -j --json-int {POOL}").succeeds(
            status_json(**stats) if stats else status_json()
        )
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            scrub.pause_interrupted(POOL, _log())
        return mock, out.getvalue()

    def test_a_running_scrub_is_paused_with_one_line(self) -> None:
        mock, out = self._run(scanning())
        assert mock.was_called(f"zpool scrub -p {POOL}")
        assert "a full check's scrub was interrupted; paused" in out

    def test_nothing_else_is_touched(self) -> None:
        for stats in (
            None,
            scanning(scrub_pause=1762684200),
            scanning(state="FINISHED"),
            scanning(state="CANCELED"),
            scanning(function="RESILVER"),
            scanning(function="ERRORSCRUB"),
        ):
            mock, _ = self._run(stats)
            assert mock.was_not_called("zpool scrub"), stats
            assert mock.was_not_called("zpool get"), stats

    def test_a_suspended_pool_is_not_paused(self) -> None:
        mock, out = self._run(scanning(), health="SUSPENDED")
        assert mock.was_not_called("zpool scrub")
        assert "was not paused" in out

    def test_an_unreadable_state_touches_nothing(self) -> None:
        mock = r1_mock()
        mock.on_prefix("zpool status").fails("boom")
        mock.on_prefix("env LC_ALL=C").fails("boom")
        with patch_sh(mock), redirect_stdout(StringIO()):
            scrub.pause_interrupted(POOL, _log())
        assert mock.was_not_called("zpool scrub")


DEV = "/dev/disk/by-id/usb-Made_Up_Disk_0123456789AB-0:0-part1"


def import_mock(guid: str = "111", **r1: str | None) -> MockShell:
    """A read-write import of POOL from DEV, its GUID, R1's reads, a running scrub."""
    mock = r1_mock(**r1)
    mock.on(f"zpool list {POOL}").fails()
    mock.on_prefix("mkdir -p").succeeds()
    mock.on_prefix("zpool import").succeeds()
    mock.on(f"zpool get -H -o value guid {POOL}").succeeds(guid)
    mock.on(f"zpool status -j --json-int {POOL}").succeeds(status_json(**scanning()))
    mock.on_prefix("udevadm settle").succeeds()
    mock.on(f"zpool export {POOL}").succeeds()
    return mock


class TestImportPausesScrub:  # pylint: disable=missing-function-docstring
    """M19-5 5a, M19R-10: import_backup_pool(pause_scrub=True) after the GUID check."""

    def _import(self, mock: MockShell, **kw: Any) -> bool:
        with patch_sh(mock), redirect_stdout(StringIO()):
            return ZFS(_log()).import_backup_pool(POOL, DEV, guid="111", **kw)

    def test_a_read_write_import_pauses_a_running_scrub_after_the_guid_check(self) -> None:
        mock = import_mock()
        assert self._import(mock)
        calls = mock.calls
        pause = calls.index(f"zpool scrub -p {POOL}")
        assert calls.index(f"zpool get -H -o value guid {POOL}") < pause
        assert max(i for i, c in enumerate(calls) if c.startswith("zpool import")) < pause

    def test_fullcheck_passes_false_and_nothing_is_read(self) -> None:
        mock = import_mock()
        assert self._import(mock, pause_scrub=False)
        assert mock.was_not_called("zpool status") and mock.was_not_called("zpool scrub")

    def test_a_read_only_import_never_reads_the_scan(self) -> None:
        mock = import_mock()
        assert self._import(mock, readonly=True)
        assert mock.was_not_called("zpool status") and mock.was_not_called("zpool scrub")

    def test_another_disk_is_exported_before_anything_is_paused(self) -> None:
        mock = import_mock(guid="999")
        assert not self._import(mock)
        assert mock.was_called(f"zpool export {POOL}")
        assert mock.was_not_called("zpool status") and mock.was_not_called("zpool scrub")

    def test_a_suspended_pool_is_left_as_it_is(self) -> None:
        mock = import_mock(health="SUSPENDED")
        assert self._import(mock)
        assert mock.was_not_called("zpool scrub")

    def test_the_pause_runs_outside_the_dpkg_hold(self) -> None:
        mock = import_mock()
        held: list[str] = []
        real = mock.mock_run

        def spy(cmd: str, **kw: Any) -> Any:
            if dpkg_lock.is_held():
                held.append(cmd)
            return real(cmd, **kw)

        with patch.object(mock, "mock_run", side_effect=spy):
            assert self._import(mock)
        assert any(c.startswith("zpool import") for c in held), held
        assert not any("scrub" in c or "status" in c for c in held), held


# ── commit 3: the command, the reader, the record ────────────────────────


GUID = "111"


def _cr(live: bool = False) -> "fc_cmd.CheckRun":
    return fc_cmd.CheckRun(POOL, GUID, live)


class TestFullcheckWords:  # pylint: disable=missing-function-docstring
    """M19-1 1a: zark fullcheck [scrub | restore]."""

    def test_both_by_default_one_by_word(self) -> None:
        assert fc_cmd.parts_asked([], _log()) == (True, True)
        assert fc_cmd.parts_asked(["scrub"], _log()) == (False, True)
        assert fc_cmd.parts_asked(["restore"], _log()) == (True, False)
        assert fc_cmd.parts_asked(["--debug"], _log()) == (True, True)

    def test_anything_else_is_refused(self) -> None:
        for args in (["both"], ["scrub", "restore"]):
            try:
                with redirect_stdout(StringIO()):
                    _ = fc_cmd.parts_asked(args, _log())
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError(args)

    def test_the_spec_declares_the_words(self) -> None:
        assert cli_spec.command("fullcheck").words() == {"scrub", "restore"}
        assert "fullcheck" in cli_spec.command_names()


class TestFullcheckPrecondition:  # pylint: disable=missing-function-docstring
    """M19-11 11a, M19R-3: a pool imported by anyone is refused before any question."""

    def _why(self, listing: str | None) -> str:
        mock = MockShell()
        resp = mock.on("zpool list -H -o name,guid")
        _ = resp.fails("boom") if listing is None else resp.succeeds(listing)
        with patch_sh(mock):
            return fc_cmd.precondition(POOL, GUID)

    def test_not_imported(self) -> None:
        assert self._why("rpool\t5\nbpool\t6") == ""
        assert self._why("") == ""

    def test_imported_this_drive_or_another_disk(self) -> None:
        mine = self._why(f"rpool\t5\n{POOL}\t{GUID}")
        assert "already imported" in mine and "umount" in mine
        other = self._why(f"{POOL}\t999")
        assert "another disk" in other and f"zpool export {POOL}" in other and "999" in other

    def test_an_unreadable_list_refuses(self) -> None:
        assert "cannot list" in self._why(None)

    def test_run_refuses_before_any_import_or_question(self) -> None:
        drive = MagicMock(name="drive")
        drive.name, drive.guid = POOL, GUID
        mock = MockShell()
        mock.on("zpool list -H -o name,guid").succeeds(f"{POOL}\t{GUID}")
        with (
            patch_sh(mock),
            patch.object(fc_cmd, "_choose", return_value=drive),
            patch.object(fc_cmd, "restore_part") as rest,
            patch.object(fc_cmd, "scrub_part") as scr,
            patch.object(fc_cmd.sh, "is_live_usb", return_value=False),
            redirect_stdout(StringIO()),
        ):
            try:
                fc_cmd.run([])
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("not refused")
        assert not rest.called and not scr.called
        assert mock.was_not_called("zpool import") and mock.was_not_called("zpool export")


class TestFullcheckFlow:  # pylint: disable=missing-function-docstring
    """M19-2 2a, M19-6 6a: the order, the parts asked, the live rule."""

    def _run(self, args: list[str], live: bool = False) -> tuple[list[str], int]:
        order: list[str] = []
        drive = MagicMock(name="drive")
        drive.name, drive.guid = POOL, GUID
        rb = MagicMock(skipped="", ok=True)

        def scrub_part(*_a: Any, **_k: Any) -> bool:
            order.append("scrub")
            return True

        def readback(*_a: Any, **_k: Any) -> Any:
            order.append("readback")
            return rb

        code = 0
        with (
            patch.object(fc_cmd, "_choose", return_value=drive),
            patch.object(fc_cmd, "precondition", return_value=""),
            patch.object(fc_cmd, "backup_device", return_value=DEV),
            patch.object(
                fc_cmd, "restore_part", side_effect=lambda *a, **k: order.append("restore")
            ),
            patch.object(fc_cmd, "scrub_part", side_effect=scrub_part),
            patch.object(fc_cmd.ZFS, "verify_exported_pool_readback", side_effect=readback),
            patch.object(fc_cmd, "_finish", side_effect=lambda *a: order.append("finish")),
            patch.object(fc_cmd.Cleanup, "register"),
            patch.object(fc_cmd.sh, "is_live_usb", return_value=live),
            redirect_stdout(StringIO()),
        ):
            try:
                fc_cmd.run(args)
            except SystemExit as e:
                code = int(e.code or 0)
        return order, code

    def test_both_parts_in_order(self) -> None:
        assert self._run([]) == (["restore", "scrub", "readback", "finish"], 0)

    def test_one_part_by_word(self) -> None:
        assert self._run(["restore"]) == (["restore", "finish"], 0)
        assert self._run(["scrub"]) == (["scrub", "readback", "finish"], 0)

    def test_a_live_runs_the_restore_check_only_and_refuses_the_scrub_word(self) -> None:
        assert self._run([], live=True) == (["restore", "finish"], 0)
        assert self._run(["scrub"], live=True) == ([], 1)


def _fake_tree(root: Path) -> Path:
    """A point's tree: files, a hard link, a symlink out of the tree, a fifo."""
    (root / "d" / "e").mkdir(parents=True)
    (root / "a").write_bytes(b"x" * 3000)
    (root / "d" / "b").write_bytes(b"y" * 5)
    (root / "d" / "e" / "c").write_bytes(b"")
    os.link(root / "a", root / "d" / "hard")
    outside = root.parent / "outside"
    outside.write_bytes(b"z" * 7777)
    (root / "link").symlink_to(outside)
    os.mkfifo(root / "fifo")
    return root


def _bounded(root: Path, done: Any = None) -> fullcheck.ReadResult:
    """read_tree, failing instead of hanging: opening the tree's fifo would
    block until a writer comes, so an alarm turns that into a failure."""

    def blocked(_sig: int, _frame: Any) -> None:
        raise AssertionError("read_tree blocked for 10 s: it opened the fifo")

    old = signal.signal(signal.SIGALRM, blocked)
    _ = signal.alarm(10)
    try:
        return fullcheck.read_tree(str(root), done)
    finally:
        _ = signal.alarm(0)
        _ = signal.signal(signal.SIGALRM, old)


class TestRestoreReader:  # pylint: disable=missing-function-docstring
    """M19-3 3a: every file once, symlinks not followed, no special file opened."""

    def test_the_tree_is_read_whole_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_tree(Path(tmp) / "point")
            seen: list[int] = []
            got = _bounded(root, seen.append)
        assert got.files == 3 and got.bytes == 3005 and got.errors == 0, got
        assert sum(seen) == 3005

    def test_a_failed_read_is_named_and_the_walk_goes_on(self) -> None:
        real = os.open

        def failing(path: Any, flags: int, *a: Any) -> int:
            if str(path).endswith("/d/b"):
                raise OSError(errno.EIO, "Input/output error")
            return real(path, flags, *a)

        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_tree(Path(tmp) / "point")
            with patch.object(fullcheck.os, "open", side_effect=failing):
                got = _bounded(root)
        assert got.errors == 1 and got.files == 2, got
        assert got.error_paths == [f"{root}/d/b: Input/output error"]

    def test_a_read_that_fails_midway_is_named_and_the_walk_goes_on(self) -> None:
        """EIO comes from read(2), not open(2), when a block fails its checksum."""
        real_open, real_read = os.open, os.read
        bad: set[int] = set()

        def opening(path: Any, flags: int, *a: Any) -> int:
            fd = real_open(path, flags, *a)
            if str(path).endswith("/d/b"):
                bad.add(fd)
            return fd

        def reading(fd: int, n: int) -> bytes:
            if fd in bad:
                bad.discard(fd)  # the number is reused by the next open
                raise OSError(errno.EIO, "Input/output error")
            return real_read(fd, n)

        with tempfile.TemporaryDirectory() as tmp:
            root = _fake_tree(Path(tmp) / "point")
            with (
                patch.object(fullcheck.os, "open", side_effect=opening),
                patch.object(fullcheck.os, "read", side_effect=reading),
            ):
                got = _bounded(root)
        assert got.errors == 1 and got.files == 2, got
        assert got.error_paths == [f"{root}/d/b: Input/output error"]

    def test_the_names_kept_are_bounded(self) -> None:
        out = fullcheck.ReadResult()
        for n in range(fullcheck.MAX_ERROR_PATHS + 5):
            out.failed(f"/p/{n}", OSError(errno.EIO, "Input/output error"))
        assert out.errors == fullcheck.MAX_ERROR_PATHS + 5
        assert len(out.error_paths) == fullcheck.MAX_ERROR_PATHS

    def test_a_missing_root_is_an_error(self) -> None:
        got = fullcheck.read_tree("/nonexistent/zark-test-point")
        assert got.errors == 1 and got.files == 0

    def test_one_snapshot_is_mounted_read_only_in_its_own_directory(self) -> None:
        mock = MockShell()
        mock.on_prefix("mkdir -p").succeeds()
        mock.on_prefix("mount -t zfs").succeeds()
        mock.on_prefix("umount").succeeds()
        cleanup = fc_cmd.Cleanup(_log())
        with (
            patch_sh(mock),
            patch.object(fc_cmd, "read_tree", return_value=fullcheck.ReadResult(files=2)),
            redirect_stdout(StringIO()),
        ):
            got = fc_cmd.read_snapshot(
                f"{POOL}/rpool/home@p1", "/run/zark/fullcheck/3", cleanup, _log()
            )
        assert got.files == 2
        assert mock.was_called(f"mount -t zfs -o ro {POOL}/rpool/home@p1 /run/zark/fullcheck/3")
        assert mock.was_called("umount /run/zark/fullcheck/3")
        assert "zfsutil" not in " ".join(mock.calls)
        assert cleanup.pending()  # the mount was tracked before it was made

    def test_a_snapshot_that_will_not_mount_is_an_error(self) -> None:
        mock = MockShell()
        mock.on_prefix("mkdir -p").succeeds()
        mock.on_prefix("mount -t zfs").fails("no")
        with patch_sh(mock), redirect_stdout(StringIO()):
            got = fc_cmd.read_snapshot(
                "p/x@s", "/run/zark/fullcheck/0", fc_cmd.Cleanup(_log()), _log()
            )
        assert got.errors == 1 and got.files == 0


def _plan(n: int) -> Any:
    rows = []
    for i in range(n):
        row = MagicMock(rel=f"rpool/d{i}", referenced=10, restored=True)
        row.snap = MagicMock(stored=f"rpool/d{i}", name="zark_p")
        row.snap.name = "zark_p"
        rows.append(row)
    return MagicMock(rows=rows)


class TestRestoreCheck:  # pylint: disable=missing-function-docstring
    """M19 2.2: the point's rows read, the keystore read-only, a key that does not load."""

    def test_every_row_read_and_the_result_kept(self) -> None:
        cr = _cr()
        point = MagicMock()
        point.label_utc.return_value = "2026-11-09 10:00:00 UTC"
        results = [
            fullcheck.ReadResult(files=1, bytes=5),
            fullcheck.ReadResult(
                files=2, bytes=7, errors=1, error_paths=["/x: Input/output error"]
            ),
        ]
        with (
            patch.object(fc_cmd, "read_snapshot", side_effect=results) as reader,
            patch.object(fc_cmd, "point_name", return_value="zark_p"),
            redirect_stdout(StringIO()),
        ):
            fc_cmd._read_point(cr, point, _plan(2), fc_cmd.Cleanup(_log()), _log())  # pylint: disable=protected-access
        dirs = [c.args[1] for c in reader.call_args_list]
        assert dirs == ["/run/zark/fullcheck/0", "/run/zark/fullcheck/1"]
        assert reader.call_args_list[0].args[0] == f"{POOL}/rpool/d0@zark_p"
        assert cr.damage and cr.check.restore is not None
        assert cr.check.restore["files"] == 3 and cr.check.restore["errors"] == 1
        assert cr.check.restore["error_paths"] == ["/x: Input/output error"]

    def _open(self, *, loaded_ok: bool, ks_ok: bool = True) -> tuple[Any, int]:
        cr = _cr()
        zfs = MagicMock()
        zfs.datasets_needing_key.return_value = [] if loaded_ok else [f"{POOL}/rpool/USERDATA"]
        opened: list[bool] = []

        def open_ks(_ks: Any, _pool: str, _log: Log, *, readonly: bool = False) -> bool:
            opened.append(readonly)
            return ks_ok

        code = 0
        with (
            patch.object(fc_cmd.drive_marks, "log_metadata", return_value={}),
            patch.object(fc_cmd, "find_be", return_value="ubuntu_x"),
            patch.object(fc_cmd, "list_snapshots", return_value=[]),
            patch.object(fc_cmd, "restore_points", return_value=[MagicMock()]),
            patch.object(fc_cmd, "complete_points", return_value=set()),
            patch.object(fc_cmd, "run_complete_points", return_value=set()),
            patch.object(fc_cmd, "choose_point", return_value=MagicMock()),
            patch.object(fc_cmd, "point_name", return_value="zark_p"),
            patch.object(fc_cmd.exclusions, "read_point_mark", return_value=None),
            patch.object(fc_cmd, "open_keystore", side_effect=open_ks),
            patch.object(fc_cmd.Keystore, "load_pool_keys", return_value=1),
            patch.object(fc_cmd.Keystore, "umount"),
            patch.object(fc_cmd, "build_plan", return_value=_plan(0)),
            patch.object(fc_cmd, "save") as save,
            redirect_stdout(StringIO()),
        ):
            try:
                _ = fc_cmd._open_point(cr, zfs, DEV, fc_cmd.Cleanup(_log()), _log())  # pylint: disable=protected-access
            except SystemExit as e:
                code = int(e.code or 0)
        assert opened == [True]
        return (save.call_args.args[1] if save.called else None), code

    def test_the_keystore_is_opened_read_only(self) -> None:
        check, code = self._open(loaded_ok=True)
        assert check is None and code == 0

    def test_a_key_that_does_not_load_fails_the_check(self) -> None:
        check, code = self._open(loaded_ok=False)
        assert code == 1 and check is not None and check.result == "failed"
        assert "did not load" in check.message

    def test_a_wrong_passphrase_fails_the_check(self) -> None:
        check, code = self._open(loaded_ok=True, ks_ok=False)
        assert code == 1 and check is not None and check.result == "failed"


def _zfs(**kw: Any) -> Any:
    """A ZFS whose imports and exports succeed."""
    zfs = MagicMock()
    zfs.import_backup_pool.return_value = True
    zfs.pool_export.return_value = True
    zfs.pool_damage.return_value = fc_cmd.Damage()
    for k, v in kw.items():
        setattr(zfs, k, v)
    return zfs


def _scans(*stats: dict[str, Any] | None) -> Any:
    """``scrub.read`` answering each of ``stats`` in turn (None: unreadable)."""
    out = [scrub.parse_json(status_json(**s), POOL) if s is not None else None for s in stats]
    return patch.object(fc_cmd.scrub, "read", side_effect=out)


class TestScrubPart:  # pylint: disable=missing-function-docstring
    """M19 2.1: R1 before every call, the pause for step 2 alone, a suspended pool."""

    def _part(
        self, zfs: Any, *scans: dict[str, Any] | None, suspended_at: int = 0, start_ok: bool = True
    ) -> tuple[bool | None, Any, fc_cmd.CheckRun, MockShell, int]:
        cr = _cr()
        cleanup = fc_cmd.Cleanup(_log())
        polls = {"n": 0}

        def is_suspended(_pool: str) -> bool:
            polls["n"] += 1
            return bool(suspended_at) and polls["n"] >= suspended_at

        mock = r1_mock()
        if not start_ok:
            mock = r1_mock(readonly="on")
        got: bool | None = None
        code = 0
        with (
            patch_sh(mock),
            _scans(*scans),
            patch.object(fc_cmd.scrub, "is_suspended", side_effect=is_suspended),
            patch.object(fc_cmd.time, "sleep"),
            patch.object(fc_cmd.zfs_features, "check_drive"),
            patch.object(fc_cmd, "save"),
            redirect_stdout(StringIO()),
        ):
            try:
                got = fc_cmd.scrub_part(cr, zfs, DEV, cleanup, _log())
            except SystemExit as e:
                code = int(e.code or 0)
        return got, cleanup, cr, mock, code

    def test_a_clean_scrub_imports_with_pause_scrub_false_and_untracks_the_pause(self) -> None:
        zfs = _zfs()
        done = scanning(state="FINISHED", examined=100 << 30)
        got, cleanup, cr, mock, _ = self._part(zfs, {}, scanning(), done)
        assert got is True and not cr.damage
        assert zfs.import_backup_pool.call_args.kwargs == {
            "readonly": False, "guid": GUID, "pause_scrub": False
        }  # fmt: skip
        assert mock.was_called(f"zpool scrub {POOL}")
        assert not cleanup._actions  # pylint: disable=protected-access
        assert zfs.pool_export.called and cr.check.scrub is not None
        assert cr.check.scrub["state"] == "finished"

    def test_a_running_scrub_is_not_started_again_and_a_paused_one_is_resumed(self) -> None:
        zfs = _zfs()
        _, _, _, mock, _ = self._part(zfs, scanning(), scanning(state="FINISHED"))
        assert mock.was_not_called("zpool scrub")
        _, _, _, mock, _ = self._part(zfs, scanning(scrub_pause=1), scanning(state="FINISHED"))
        assert mock.was_called(f"zpool scrub {POOL}")

    def test_a_ctrl_c_during_the_poll_leaves_the_pause_registered(self) -> None:
        """M19R-5: from step 2's import to its export the cleanup holds the
        pause, so a signal in the poll pauses the scrub, then exports."""
        zfs = _zfs()
        cr = _cr()
        cleanup = fc_cmd.Cleanup(_log())
        mock = r1_mock()
        mock.on(f"zpool list {POOL}").succeeds(POOL)
        mock.on_prefix("zfs unload-key").succeeds()
        mock.on(f"zpool export {POOL}").succeeds()
        with (
            patch_sh(mock),
            _scans({}, scanning(), scanning()),
            patch.object(fc_cmd.scrub, "is_suspended", return_value=False),
            # The poll's first sleep is the Ctrl-C; the cleanup's own sleeps return.
            patch.object(fc_cmd.time, "sleep", side_effect=[KeyboardInterrupt(), *[None] * 9]),
            patch.object(fc_cmd.zfs_features, "check_drive"),
            patch.object(fc_cmd, "save", return_value=False),
            redirect_stdout(StringIO()),
        ):
            try:
                fc_cmd.scrub_part(cr, zfs, DEV, cleanup, _log())
            except KeyboardInterrupt:
                pass
            assert cleanup._actions and POOL in cleanup._pools  # pylint: disable=protected-access
            cleanup._run()  # pylint: disable=protected-access
        calls = mock.calls
        pause = calls.index(f"zpool scrub -p {POOL}")
        assert pause < calls.index(f"zpool export {POOL}")
        assert cr.check.result == "not_finished"

    def test_errors_and_repairs_are_damage(self) -> None:
        for end in (
            scanning(state="FINISHED", errors=1),
            scanning(state="FINISHED", processed=4096),
        ):
            got, _, cr, _, _ = self._part(_zfs(), {}, end)
            assert got is True and cr.damage, end
        zfs = _zfs()
        zfs.pool_damage.return_value = fc_cmd.Damage(errors=["backup2/rpool/x:<0x1>"])
        got, _, cr, _, _ = self._part(zfs, {}, scanning(state="FINISHED"))
        assert cr.damage and cr.check.scrub is not None and cr.check.scrub["permanent_errors"] == 1

    def test_a_canceled_scrub_is_not_finished(self) -> None:
        got, _, cr, _, _ = self._part(_zfs(), {}, scanning(state="CANCELED"))
        assert got is False and cr.recorded and cr.check.result == "not_finished"

    def test_a_pool_suspended_mid_scrub_is_neither_paused_nor_exported(self) -> None:
        zfs = _zfs()
        got, cleanup, cr, mock, _ = self._part(zfs, {}, scanning(), suspended_at=1)
        assert got is False and cr.check.result == "not_finished"
        assert "suspended" in cr.check.message and "zpool clear" in cr.check.message
        assert mock.was_not_called("zpool scrub -p")
        assert not zfs.pool_export.called
        assert not cleanup._pools and not cleanup._actions  # pylint: disable=protected-access

    def test_a_resilver_or_an_unreadable_state_is_refused(self) -> None:
        _, _, cr, mock, code = self._part(_zfs(), scanning(function="RESILVER"))
        assert code == 1 and cr.check.result == "failed" and mock.was_not_called("zpool scrub")
        _, _, cr, mock, code = self._part(_zfs(), None)
        assert code == 1 and cr.check.result == "failed" and mock.was_not_called("zpool scrub")

    def test_r1_refusing_the_start_fails_the_check(self) -> None:
        _, cleanup, cr, mock, code = self._part(_zfs(), {}, start_ok=False)
        assert code == 1 and cr.check.result == "failed"
        assert mock.was_not_called("zpool scrub")
        assert not cleanup._actions  # pylint: disable=protected-access


class TestScrubInterrupted:  # pylint: disable=missing-function-docstring
    """M19R-5, M19-13 13a: what the cleanup does at a signal, in each step."""

    def test_in_step_2_the_reads_then_the_pause_then_the_export(self) -> None:
        cr = _cr()
        cleanup = fc_cmd.Cleanup(_log())
        cleanup.track_pool(POOL)
        cleanup.track_action(fc_cmd._action(cr, cleanup, _log()))  # pylint: disable=protected-access
        mock = r1_mock()
        mock.on(f"zpool status -j --json-int {POOL}").succeeds(status_json(**scanning()))
        mock.on(f"zpool list {POOL}").succeeds(POOL)
        mock.on_prefix("zfs unload-key").succeeds()
        mock.on(f"zpool export {POOL}").succeeds()
        with patch_sh(mock), patch.object(fc_cmd, "save"), redirect_stdout(StringIO()):
            cleanup._run()  # pylint: disable=protected-access
        calls = mock.calls
        pause = calls.index(f"zpool scrub -p {POOL}")
        assert calls.index(f"zpool get -H -o value readonly {POOL}") < pause
        assert pause < calls.index(f"zpool export {POOL}")
        assert cr.check.result == "not_finished" and cr.check.scrub == fullcheck.scrub_record(
            "paused"
        )

    def test_in_step_2_a_suspended_pool_is_not_paused_or_exported(self) -> None:
        cr = _cr()
        cleanup = fc_cmd.Cleanup(_log())
        cleanup.track_pool(POOL)
        cleanup.track_action(fc_cmd._action(cr, cleanup, _log()))  # pylint: disable=protected-access
        mock = r1_mock(health="SUSPENDED")
        mock.on(f"zpool list {POOL}").succeeds(POOL)
        with patch_sh(mock), patch.object(fc_cmd, "save"), redirect_stdout(StringIO()):
            cleanup._run()  # pylint: disable=protected-access
        assert mock.was_not_called("zpool scrub") and mock.was_not_called("zpool export")
        assert cr.check.result == "not_finished" and "suspended" in cr.check.message

    def test_in_steps_1_and_3_no_scrub_and_no_readonly_read(self) -> None:
        for signal_received in (2, None):
            cr = _cr()
            cleanup = fc_cmd.Cleanup(_log())
            cleanup.track_action(fc_cmd._on_exit(cr, _log()))  # pylint: disable=protected-access
            mock = MockShell()
            with (
                patch_sh(mock),
                patch.object(fc_cmd.signals, "received", return_value=signal_received),
                patch.object(fc_cmd, "save"),
                redirect_stdout(StringIO()),
            ):
                cleanup._run()  # pylint: disable=protected-access
            assert mock.was_not_called("zpool scrub") and mock.was_not_called("readonly")
            want = "not_finished" if signal_received else "failed"
            assert cr.check.result == want, signal_received

    def test_step_1_registers_no_scrub_action(self) -> None:
        cr = _cr()
        cleanup = fc_cmd.Cleanup(_log())
        zfs = _zfs()
        with (
            patch.object(fc_cmd.zfs_features, "check_drive"),
            patch.object(fc_cmd, "_open_point", return_value=(MagicMock(), _plan(0))),
            patch.object(fc_cmd, "_read_point"),
            redirect_stdout(StringIO()),
        ):
            fc_cmd.restore_part(cr, zfs, DEV, cleanup, _log(), ask_repeat=False)
        assert not cleanup._actions and not cleanup._pools  # pylint: disable=protected-access
        assert zfs.import_backup_pool.call_args.kwargs["readonly"] is True


class TestFullcheckResult:  # pylint: disable=missing-function-docstring
    """M19-9 9a: 0 clean, 1 otherwise; the record; the eject question."""

    def _finish(self, cr: fc_cmd.CheckRun, imported: bool = False) -> tuple[int, Any, Any]:
        zfs = _zfs()
        zfs.pool_exists.return_value = imported
        code = 0
        with (
            patch.object(fc_cmd, "save") as save,
            patch.object(fc_cmd, "prompt_eject_or_attach") as eject,
            redirect_stdout(StringIO()),
        ):
            try:
                fc_cmd._finish(cr, zfs, DEV, MagicMock(), _log())  # pylint: disable=protected-access
            except SystemExit as e:
                code = int(e.code or 0)
        return code, save, eject

    def test_clean_damage_not_finished(self) -> None:
        code, save, eject = self._finish(_cr())
        assert code == 0 and save.call_args.args[1].result == "clean" and eject.called
        cr = _cr()
        cr.damage = True
        code, save, _ = self._finish(cr)
        assert code == 1 and save.call_args.args[1].result == "damage"
        cr = _cr()
        with patch.object(fc_cmd, "save"):
            cr.record("not_finished", "the check was interrupted", _log())
        code, save, _ = self._finish(cr)
        assert code == 1 and not save.called

    def test_no_eject_while_the_pool_is_still_imported(self) -> None:
        _, _, eject = self._finish(_cr(), imported=True)
        assert not eject.called

    def test_nothing_recorded_on_a_live(self) -> None:
        cr = _cr(live=True)
        code, save, _ = self._finish(cr)
        assert code == 0 and not save.called and cr.check.result == "clean"


class TestCheckRecord:  # pylint: disable=missing-function-docstring
    """M19-7 7a, M19R-6, M19R-9: the registry's last_check and the rates it gives."""

    def test_save_writes_the_registry_and_keeps_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "known_drives.json"
            reg.write_text(
                json.dumps(
                    {POOL: {"guid": GUID, "drive_id": "ata-x", "retention": {"window_days": 9}}}
                ),
                encoding="utf-8",
            )
            check = fullcheck.Check(
                started_at="2026-11-09T09:00:00Z", ended_at="2026-11-09T10:00:00Z"
            )
            check.result = "clean"
            with (
                patch.dict(os.environ, {"ZARK_CONFIG_DIR": tmp}),
                patch.object(fullcheck.sh, "is_live_usb", return_value=False),
                redirect_stdout(StringIO()),
            ):
                assert fullcheck.save(GUID, check, _log())
                assert not fullcheck.save("999", check, _log())
            doc = json.loads(reg.read_text(encoding="utf-8"))[POOL]
        assert doc["last_check"]["result"] == "clean" and doc["retention"] == {"window_days": 9}

    def test_never_from_a_live(self) -> None:
        """M19-6 6a: not even for a drive the (stick's) registry knows."""
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "known_drives.json"
            before = json.dumps({POOL: {"guid": GUID, "drive_id": "ata-x"}})
            reg.write_text(before, encoding="utf-8")
            with (
                patch.dict(os.environ, {"ZARK_CONFIG_DIR": tmp}),
                patch.object(fullcheck.sh, "is_live_usb", return_value=True),
                redirect_stdout(StringIO()),
            ):
                assert not fullcheck.save(GUID, fullcheck.Check(started_at="x"), _log())
            assert reg.read_text(encoding="utf-8") == before

    def test_valid_and_rate(self) -> None:
        good = {
            "started_at": "2026-11-09T09:00:00Z",
            "ended_at": "2026-11-09T10:00:00Z",
            "result": "damage",
            "scrub": {"examined_bytes": 1000, "seconds": 10},
            "restore": {"bytes": 50, "seconds": 0},
        }
        assert fullcheck.valid(good) is good
        bads: list[Any] = [{**good, "result": "weird"}, {**good, "ended_at": "x"}, [], None]
        for bad in bads:
            assert fullcheck.valid(bad) is None, bad
        assert fullcheck.rate(good, "scrub", "examined_bytes") == 100.0
        assert fullcheck.rate(good, "restore", "bytes") is None
        assert fullcheck.rate(None, "scrub", "examined_bytes") is None

    def test_the_estimates(self) -> None:
        assert "measured" in fc_cmd.estimate(3600 * 100, 100.0, stream=True)
        assert "1 h 00 min" in fc_cmd.estimate(3600 * 100, 100.0, stream=False)
        assert "on a disk" in fc_cmd.estimate(10**12, None, stream=True)
        assert "unknown until measured" in fc_cmd.estimate(10**12, None, stream=False)
