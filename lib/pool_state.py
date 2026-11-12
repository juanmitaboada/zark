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
What zark learns about an imported pool without waiting on its drive (A1E-7).

Once a backup drive is gone, a ``zfs`` or ``zpool`` call on its pool can wait
in state D for as long as the pool is suspended, and nothing ends that wait
but the drive's return and a ``zpool clear``, or a reboot. Two reads never
wait:

  * the pool's state, from ``/proc/spl/kstat/zfs/<pool>/state``: the
    module prints ``spa_state_to_name`` there, a lock-less read that answers
    ``SUSPENDED`` before it looks at the vdevs (OpenZFS 2.2.2
    module/zfs/spa_stats.c:782-816, spa_misc.c:2745-2784; 2.4.1 and 2.4.4
    spa_stats.c:783-816). The file exists while the pool is imported;
  * whether a device link exists.

Anything else zark asks of such a pool goes through :func:`run_bounded`: the
child runs in a session of its own and is polled until a deadline; one still
running then is abandoned, never waited for, never killed and waited for
(a child in state D does not die), and never registered as the main
thread's running child (``lib.signals``).
"""

import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

KSTAT_DIR = Path("/proc/spl/kstat/zfs")
HEALTHY = frozenset({"ONLINE", "DEGRADED"})
# States that say at once that the drive is not usable (spa_state_to_name).
LOST = frozenset({"SUSPENDED", "REMOVED", "UNAVAIL", "FAULTED", "OFFLINE"})
# What state() returns when the file is not there (the pool is not
# imported) or cannot be read; the watch calls both "unknown".
MISSING = "(missing)"
UNREADABLE = "(unreadable)"


def state(pool: str, kstat_dir: Path = KSTAT_DIR) -> str:
    """The pool's state word, :data:`MISSING` or :data:`UNREADABLE`."""
    if not pool or "/" in pool or pool in (".", ".."):
        return UNREADABLE
    try:
        return (kstat_dir / pool / "state").read_text(encoding="ascii").strip() or UNREADABLE
    except FileNotFoundError:
        return MISSING
    except (OSError, UnicodeDecodeError):
        return UNREADABLE


def imported(pool: str, kstat_dir: Path = KSTAT_DIR) -> bool:
    """Whether the module holds ``pool`` (its kstat directory exists)."""
    return state(pool, kstat_dir) != MISSING


@dataclass
class Child:
    """A child of :func:`start`: polled, never waited for."""

    command: str
    proc: subprocess.Popen[bytes]
    out: IO[bytes]
    err: IO[bytes]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""

    def poll(self) -> int | None:
        """Its status once it ended (output read then), None while it runs."""
        if self.returncode is not None:
            return self.returncode
        rc = self.proc.poll()
        if rc is None:
            return None
        self.returncode = rc
        for stream, name in ((self.out, "stdout"), (self.err, "stderr")):
            _ = stream.seek(0)
            setattr(self, name, stream.read().decode(errors="replace"))
            stream.close()
        return rc


def start(argv: list[str]) -> Child:
    """Start ``argv`` in a session of its own, its output to unnamed files
    (a pipe left unread could block it), with the C locale for parsing."""
    out = tempfile.TemporaryFile()  # pylint: disable=consider-using-with
    err = tempfile.TemporaryFile()  # pylint: disable=consider-using-with
    env = {**os.environ, "LC_ALL": "C"}
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True
    )
    return Child(" ".join(argv), proc, out, err)


@dataclass
class Bounded:
    """What :func:`run_bounded` saw: ``rc`` None when the child was still
    running at the deadline (``child`` then keeps it, to poll later)."""

    rc: int | None
    stdout: str = ""
    stderr: str = ""
    child: Child | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        """Ended with status 0."""
        return self.rc == 0


def run_bounded(
    argv: list[str],
    deadline_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Bounded:
    """Run ``argv`` and wait for it at most ``deadline_s`` seconds."""
    try:
        child = start(argv)
    except OSError as e:
        return Bounded(126, "", str(e))
    end = clock() + deadline_s
    while (rc := child.poll()) is None:
        if clock() >= end:
            return Bounded(None, "", "", child)
        sleep(0.05)
    return Bounded(rc, child.stdout, child.stderr, child)
