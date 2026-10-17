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
import re
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import TextIO, cast

from lib.log import Log

EXIT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# How long a stopped child may take to exit before it is killed.
STOP_WAIT_SECONDS = 30.0

_received: list[int] = []
_log: list[Log] = []
_teardown: list[bool] = []
_interactive: list[bool] = []
_running: list[str] = []
_installed: list[bool] = []


def received() -> int | None:
    """The signal that is stopping this run, None while none arrived."""
    return _received[0] if _received else None


def child_signal() -> int:
    """What a child gets when zark stops it: Ctrl-C passed on as SIGINT,
    anything else as SIGTERM."""
    return signal.SIGINT if received() == signal.SIGINT else signal.SIGTERM


def _on_signal(signum: int, _frame: FrameType | None) -> None:
    if signum == signal.SIGINT and _interactive:
        return  # the terminal's Ctrl-C belongs to the child that owns it
    for sig in EXIT_SIGNALS:
        _ = signal.signal(sig, signal.SIG_IGN)
    _received.append(signum)
    doing = f"stopping {_running[0]}, then cleaning up" if _running else "cleaning up"
    (_log[0] if _log else Log()).warn(
        f"Interrupted by {signal.Signals(signum).name}: {doing} (further signals are ignored)"
    )
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


def install(log: Log | None = None) -> None:
    """Install the handler for every exit signal, and the guard of stdout and
    stderr (once per process); both report through ``log`` (a default
    :class:`Log` when None)."""
    guard_output(log)
    for sig in EXIT_SIGNALS:
        _ = signal.signal(sig, _on_signal)
    _installed[:] = [True]


def guard_output(log: Log | None = None) -> None:
    """Guard stdout and stderr against a reader that goes away (see
    :class:`_Guarded`); what that stops reports through ``log``."""
    _log[:] = [log] if log is not None else []
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if not isinstance(stream, _Guarded):
            setattr(sys, name, cast(TextIO, _Guarded(stream)))


def teardown() -> None:
    """The cleanup has started: a dead stdout no longer stops anything, and
    the three exit signals are ignored (M4bR-2), whether a signal started the
    stop (the handler ignored them already) or the command ended normally
    and runs its cleanup itself; so a signal during the cleanup is discarded
    by the kernel (Linux 7.0 kernel/signal.c:935, 4337-4360) and every step
    runs. Only when :func:`install` ran: a test's Cleanup leaves its
    process alone."""
    _teardown[:] = [True]
    if _installed:
        for sig in EXIT_SIGNALS:
            _ = signal.signal(sig, signal.SIG_IGN)


def resume() -> None:
    """The normal-end cleanup is over and the command goes on (a read-back,
    an eject prompt): the handler is back, and a dead stdout stops the
    command again. Not called when a signal stopped the run."""
    _teardown.clear()
    if _installed:
        for sig in EXIT_SIGNALS:
            _ = signal.signal(sig, _on_signal)


def running(name: str | None) -> None:
    """What zark is waiting for now, named in the interruption line
    (:func:`child_name`); None once the wait is over."""
    _running[:] = [name] if name else []


def child_name(cmd: str) -> str:
    """The executable and its subcommand of a command line, for a message:
    ``zfs send`` from ``zfs send -w rpool/x@p``, ``sh`` from ``sh -c '...'``
    (a second word is kept only when it is a plain word)."""
    words = cmd.split()
    if len(words) > 1 and re.fullmatch(r"[a-z][a-z0-9-]*", words[1]):
        return f"{words[0]} {words[1]}"
    return words[0] if words else ""


class _Guarded:
    """A text stream whose reader may go away (V-6 general, M4b-2).

    SIGPIPE stays ignored in zark (Python's default), so a write to a dead
    pipe raises BrokenPipeError instead of killing zark before its cleanup.
    The first one ends the command as a hangup does; from then on the stream
    goes to /dev/null, so neither the cleanup nor the exit flush fails.
    """

    def __init__(self, stream: TextIO):
        self._stream = stream

    def write(self, text: str) -> int:
        """Write, or stop the command when the reader is gone."""
        try:
            return self._stream.write(text)
        except BrokenPipeError:
            _stream_gone(self._stream)
            return len(text)

    def flush(self) -> None:
        """Flush, or stop the command when the reader is gone."""
        try:
            self._stream.flush()
        except BrokenPipeError:
            _stream_gone(self._stream)

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def _stream_gone(stream: TextIO) -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, stream.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError):
        pass
    if _received or _teardown:
        return  # already stopping, or cleaning up: the rest goes to zark.log only
    for sig in EXIT_SIGNALS:
        _ = signal.signal(sig, signal.SIG_IGN)
    _received.append(signal.SIGPIPE)
    (_log[0] if _log else Log()).file(
        "[WARN]    Output closed (its reader went away): stopping, then cleaning up"
    )
    raise SystemExit(128 + signal.SIGPIPE)


def run_interactive(argv: list[str], stop_with: int = signal.SIGHUP) -> int:
    """Run a child that owns the terminal (``zark chroot``'s shell,
    ``simulate``'s QEMU) and return its exit status.

    It stays in zark's process group, so the terminal is its own; while it
    runs a Ctrl-C is the child's and zark's handler lets it pass. SIGTERM or
    SIGHUP still stop the command: the child gets ``stop_with`` first (an
    interactive bash passes a SIGHUP on to its jobs), then up to
    ``STOP_WAIT_SECONDS`` and SIGKILL, then the cleanup unmounts what the
    child had (W-1's first trigger: a chroot whose terminal closed).
    """
    proc = subprocess.Popen(argv)  # pylint: disable=consider-using-with
    _interactive[:] = [True]
    running(child_name(" ".join(argv)))
    try:
        return proc.wait()
    except BaseException:
        try:
            os.kill(proc.pid, stop_with)
        except ProcessLookupError:
            pass
        try:
            _ = proc.wait(timeout=STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            _ = proc.wait()
        raise
    finally:
        _interactive.clear()
        running(None)


def stop_children(
    procs: list[subprocess.Popen[str]] | list[subprocess.Popen[bytes]],
    sig: int | None = None,
    wait: float | None = None,
) -> None:
    """Stop each child's process group with ``sig`` (default
    :func:`child_signal`), wait up to ``wait`` seconds (default
    :data:`STOP_WAIT_SECONDS`, read when called) for all of them, then
    SIGKILL what is left. A child that already exited is skipped.

    The wait covers the whole group, not only the child zark started
    (M4bF-10 10a): ``sh -c`` dies with the signal while the command behind
    it may ignore it, and the cleanup must not run under a child that is
    still writing."""
    sig = child_signal() if sig is None else sig
    for p in procs:
        if p.poll() is None:
            _killpg(p.pid, sig)
    deadline = time.monotonic() + (STOP_WAIT_SECONDS if wait is None else wait)
    for p in procs:
        try:
            _ = p.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            _killpg(p.pid, signal.SIGKILL)
            _ = p.wait()
        if not _group_gone(p.pid, deadline):
            _killpg(p.pid, signal.SIGKILL)
            _ = _group_gone(p.pid, time.monotonic() + 1.0)


def _group_gone(pgid: int, deadline: float) -> bool:
    """Whether process group ``pgid`` has no member left by ``deadline``
    (``killpg(pgid, 0)`` fails with ESRCH once it is empty)."""
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # a member we cannot signal is still a member
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
