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

import json
from contextlib import redirect_stdout
from io import StringIO
from typing import Any

from lib import scrub
from lib.log import Log
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
