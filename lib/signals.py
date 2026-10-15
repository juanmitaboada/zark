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
How every zark command stops on a signal (H12, M4b-1).

One handler for SIGINT, SIGTERM and SIGHUP:

  1. the three are ignored first, so a second one (logind sends SIGTERM and
     SIGHUP together, systemd 259.5 src/core/unit.c:4901-4927) is dropped
     instead of interrupting what follows;
  2. it raises KeyboardInterrupt (SIGINT) or ``SystemExit(128 + n)``, which
     leaves the wait for a child in :mod:`lib.sh`; that wait stops the child
     first (:func:`stop_children`), then lets the exception go on;
  3. the command unwinds and its Cleanup runs once, from ``atexit``, with the
     three signals still ignored: the children it starts (``zpool export``)
     inherit that, since only handlers other than ``SIG_IGN`` go back to
     ``SIG_DFL`` at exec (Linux 7.0 kernel/signal.c:520-534).

Children run in a process group of their own (:mod:`lib.sh`), because
``sh -c`` stays the parent of its command (dash 0.5.12) and a signal to the
shell alone would not reach ``zfs``; so the terminal's Ctrl-C reaches zark
only, and zark passes it on.
"""

import os
import signal
import subprocess
import time
from types import FrameType
from typing import NoReturn

from lib.log import Log

EXIT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# How long a stopped child may take to exit before it is killed.
STOP_WAIT_SECONDS = 30.0

_received: list[int] = []
_log: list[Log] = []


def received() -> int | None:
    """The signal that is stopping this run, None while none arrived."""
    return _received[0] if _received else None


def child_signal() -> int:
    """What a child gets when zark stops it: Ctrl-C passed on as SIGINT,
    anything else as SIGTERM."""
    return signal.SIGINT if received() == signal.SIGINT else signal.SIGTERM


def _on_signal(signum: int, _frame: FrameType | None) -> NoReturn:
    for sig in EXIT_SIGNALS:
        _ = signal.signal(sig, signal.SIG_IGN)
    _received.append(signum)
    (_log[0] if _log else Log()).warn(
        f"Interrupted by {signal.Signals(signum).name}: stopping the running command, "
        + "then cleaning up (further signals are ignored)"
    )
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


def install(log: Log | None = None) -> None:
    """Install the handler for every exit signal (once per process); it
    reports through ``log`` (a default :class:`Log` when None)."""
    _log[:] = [log] if log is not None else []
    for sig in EXIT_SIGNALS:
        _ = signal.signal(sig, _on_signal)


def stop_children(
    procs: list[subprocess.Popen[str]] | list[subprocess.Popen[bytes]],
    sig: int | None = None,
    wait: float = STOP_WAIT_SECONDS,
) -> None:
    """Stop each child's process group with ``sig`` (default
    :func:`child_signal`), wait up to ``wait`` seconds for all of them, then
    SIGKILL what is left. A child that already exited is skipped."""
    sig = child_signal() if sig is None else sig
    for p in procs:
        if p.poll() is None:
            _killpg(p.pid, sig)
    deadline = time.monotonic() + wait
    for p in procs:
        try:
            _ = p.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            _killpg(p.pid, signal.SIGKILL)
            _ = p.wait()


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
