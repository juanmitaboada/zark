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
The backup drive's watch while ``zark backup`` has its pool imported (A1E-7).

On eli, backup2 unplugged during a backup left zark waiting for ever, with
nothing said. A pool without redundancy suspends at the first write that
fails (``failmode=wait``, the default, kept: M26-2 2a), and every call that
needs it then waits until the drive is back and someone runs ``zpool clear``.
zark cannot end that wait, and it does not run the clear itself (M26-4 4b):
a pool resumed after its drive lost power may have lost writes the drive
had acknowledged (M26R-1). What it does is say so at once, in the terminal,
zark.log, a desktop notification and ``progress.json``'s ``drive_gone``, and
latch the run, which then never ends COMPLETED.

A daemon thread, started by backup after its import (M26-3 3b) and stopped
before Cleanup and the read-back's import, reads every :data:`TICK_S`
seconds the device link the pool was imported from and the pool's state in
the kstat file (``lib.pool_state``); it never runs ``zpool`` to learn the
state. The latch (never cleared, M26R-24, M26R-29):

  * at once when the state is ``SUSPENDED``, ``REMOVED``, ``UNAVAIL``,
    ``FAULTED`` or ``OFFLINE``;
  * on two consecutive ticks for a missing link, a state that cannot be
    read or ``TRANSITIONING``: a link absent for an instant while udev
    handles an event must not cost a healthy backup. A link or a state
    file the watch never saw is not watched.

Every later entry into ``SUSPENDED`` repeats the clear instruction
(M26R-21); every exit from it is recorded as a resume, whoever caused it
(zed included, M26R-13). While the drive is away, ``zpool status -p`` is
read through the bounded runner, one child at a time, so the counters the
user's clear will zero are in zark.log first (M26R-6, M26R-25).
"""

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib import pool_state, progress
from lib.config import zark_command
from lib.log import Log
from lib.zfs import parse_damage

TICK_S = 2.0
EVIDENCE_EVERY_S = 30.0
EVIDENCE_DEADLINE_S = 10.0
REOPEN_HINT_S = 10.0
JOIN_LIMIT_S = 5.0
# What latches on two ticks, not one (M26R-24, M26R-29).
_DOUBTFUL = frozenset({pool_state.MISSING, pool_state.UNREADABLE, "TRANSITIONING"})
_NOT_REOPENED = frozenset({"REMOVED", "UNAVAIL", "FAULTED", "OFFLINE"})


def local_time(t: float) -> str:
    """``HH:MM:SS`` in local time."""
    return time.strftime("%H:%M:%S", time.localtime(t))


@dataclass(frozen=True)
class Latch:
    """When the drive was first seen gone, and whether the pool suspended."""

    lost_at: float
    suspended: bool


@dataclass
class Evidence:
    """The last ``zpool status -p`` read while the drive was away."""

    at: float = 0.0
    counters: list[str] = field(default_factory=list)
    errors_line: str = ""


class DriveWatch:  # pylint: disable=too-many-instance-attributes
    """Watch ``pool``'s drive at ``device``; :meth:`tick` is one look."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        pool: str,
        device: str,
        log: Log,
        *,
        notify: Callable[[str, str], None] | None = None,
        kstat_dir: Path = pool_state.KSTAT_DIR,
        clock: Callable[[], float] = time.time,
        runner: Callable[[list[str], float], pool_state.Bounded] = pool_state.run_bounded,
    ) -> None:
        self.pool = pool
        self.device = device
        self.log = log
        self._notify = notify or (lambda _t, _b: None)
        self._kstat_dir = kstat_dir
        self._clock = clock
        self._runner = runner
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latch: Latch | None = None
        self._first_doubt: float | None = None
        self._link_seen = False
        self._state_seen = False
        self._prev_state = ""
        self._suspended_now = False
        self.resumed_at: float | None = None
        self.state = ""
        self.link = True
        self._unopened_since: float | None = None
        self._hinted_reopen = False
        self._evidence_child: pool_state.Child | None = None
        self.evidence = Evidence()

    # ── the thread ───────────────────────────────────────────────────────

    def start(self) -> "DriveWatch":
        """Start watching; returns self."""
        self._thread = threading.Thread(target=self._loop, name="zark-drive-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop watching; waits at most :data:`JOIN_LIMIT_S` for the thread
        (one of its children may be blocked: it is never waited for)."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(JOIN_LIMIT_S)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # pylint: disable=broad-exception-caught
                self.log.dbg(f"drive watch: {type(e).__name__}: {e}")
            _ = self._stop.wait(TICK_S)

    # ── what the run asks ────────────────────────────────────────────────

    @property
    def latched(self) -> Latch | None:
        """The latch, None while the drive was never seen gone."""
        with self._lock:
            return self._latch

    def sentence(self) -> str:
        """``<pool>'s drive went away at <time> (pool suspended)``, "" when
        not latched: what the engine and the banner say (M26R-14)."""
        latch = self.latched
        if latch is None:
            return ""
        with self._lock:
            suspended = latch.suspended or self._suspended_now
        tail = " (pool suspended)" if suspended else ""
        return f"{self.pool}'s drive went away at {local_time(latch.lost_at)}{tail}"

    # ── one look ─────────────────────────────────────────────────────────

    def tick(self) -> None:
        """Read the link and the state once and act on what changed."""
        now = self._clock()
        state = pool_state.state(self.pool, self._kstat_dir)
        link = os.path.lexists(self.device) if self.device else True
        with self._lock:
            self.state, self.link = state, link
            latched_now = self._latch is None and self._check_latch(now, state, link)
            entered = state == "SUSPENDED" and self._prev_state != "SUSPENDED"
            left = self._prev_state == "SUSPENDED" and state != "SUSPENDED"
            self._prev_state = state
            if state == "SUSPENDED":
                self._suspended_now = True
            latch = self._latch
        if latched_now and latch is not None:
            self._announce(latch)
        elif latch is not None and entered:
            self.log.warn(
                f"{self.pool} is suspended now ({local_time(now)}). "
                + f"Plug the drive in if it is not, then: sudo zpool clear {self.pool}"
            )
        if latch is not None and left:
            with self._lock:
                self.resumed_at = now
            self.log.info(f"{self.pool} was resumed at {local_time(now)}")
        if latch is not None:
            self._reopen_hint(now, state, link)
            self._read_evidence(now)
            self._publish()

    def _check_latch(self, now: float, state: str, link: bool) -> bool:
        """Set the latch when this look says the drive is gone (lock held).

        A link or a state file never seen is not watched: a module without
        the kstat file, or a device path that never existed, says nothing
        about this drive."""
        self._link_seen = self._link_seen or link
        self._state_seen = self._state_seen or state not in _DOUBTFUL
        if state in pool_state.LOST:
            first = self._first_doubt if self._first_doubt is not None else now
            self._latch = Latch(first, state == "SUSPENDED")
            return True
        doubtful = (self._link_seen and not link) or (self._state_seen and state in _DOUBTFUL)
        if not doubtful:
            self._first_doubt = None
            return False
        if self._first_doubt is None:
            self._first_doubt = now
            return False
        self._latch = Latch(self._first_doubt, False)
        return True

    def _announce(self, latch: Latch) -> None:
        how = "is suspended" if latch.suspended else "is not suspended yet"
        lines = [
            f"{self.pool}'s drive went away at {local_time(latch.lost_at)} ({self.pool} {how}).",
            "zark cannot end or resume this run itself. Either:",
            f"  - plug the drive back in, then in another terminal: sudo zpool clear {self.pool}",
            "    Writes the drive had accepted before it lost power may be lost: this run "
            + "will not be marked complete, and after it ends run: "
            + f"{zark_command()} fullcheck",
            "  - or reboot with the drive unplugged: nothing imports it at boot, and its "
            + "next import starts from the last state it saved.",
        ]
        for line in lines:
            self.log.error(line)
        self._notify(
            f"{self.pool}: drive went away",
            f"Plug it back in, then: sudo zpool clear {self.pool}",
        )

    def _reopen_hint(self, now: float, state: str, link: bool) -> None:
        if not (link and state in _NOT_REOPENED):
            self._unopened_since, self._hinted_reopen = None, False
            return
        if self._unopened_since is None:
            self._unopened_since = now
        if not self._hinted_reopen and now - self._unopened_since >= REOPEN_HINT_S:
            self._hinted_reopen = True
            self.log.warn(f"{self.pool} has not reopened its drive: sudo zpool clear {self.pool}")

    def _read_evidence(self, now: float) -> None:
        """``zpool status -p`` while the drive is away, one child at a time
        (M26R-6, M26R-25); a child still running is polled at the next tick."""
        child = self._evidence_child
        if child is not None:
            if child.poll() is None:
                return
            self._evidence_child = None
            self._keep_evidence(now, child.stdout)
            return
        if self.evidence.at and now - self.evidence.at < EVIDENCE_EVERY_S:
            return
        if self.link and self.state in pool_state.HEALTHY:
            return
        got = self._runner(["zpool", "status", "-p", self.pool], EVIDENCE_DEADLINE_S)
        if got.rc is None:
            self._evidence_child = got.child
            self.log.dbg(f"drive watch: zpool status -p {self.pool} has not returned yet")
            self.evidence.at = now
            return
        self._keep_evidence(now, got.stdout)

    def _keep_evidence(self, now: float, text: str) -> None:
        damage = parse_damage(text)
        errors = next((ln.strip() for ln in text.splitlines() if ln.startswith("errors:")), "")
        self.evidence = Evidence(now, damage.counters, errors)
        for line in damage.counters:
            self.log.warn(f"  {self.pool} while its drive was away (READ WRITE CKSUM): {line}")
        if errors:
            self.log.warn(f"  {self.pool} while its drive was away: {errors}")

    def document(self) -> dict[str, Any] | None:
        """``progress.json``'s ``drive_gone`` (M26-10 10a), None before the latch."""
        latch = self.latched
        if latch is None:
            return None
        with self._lock:
            suspended: bool | None = (
                None
                if self.state in (pool_state.MISSING, pool_state.UNREADABLE)
                else self.state == "SUSPENDED"
            )
            resumed = self.resumed_at
            back = self.link
        return {
            "since": _iso(latch.lost_at),
            "pool": self.pool,
            "suspended": suspended,
            "back": back,
            "resumed_at": _iso(resumed) if resumed is not None else None,
            "hint": f"{self.pool}'s drive went away. Plug it back in, then: "
            + f"sudo zpool clear {self.pool}",
        }

    def _publish(self) -> None:
        progress.set_drive_gone(self.document())


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def start(pool: str, device: str, log: Log, notify: Callable[[str, str], None]) -> DriveWatch:
    """A started watch of ``pool``'s drive at ``device`` (backup's)."""
    return DriveWatch(pool, device, log, notify=notify).start()
