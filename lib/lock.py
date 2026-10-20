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
One lock for every zark command that changes state (M4a-5).

The kernel holds it: ``flock(LOCK_EX | LOCK_NB)`` on ``/run/zark/zark.lock``,
released when the descriptor closes, which happens on any death of the
process, SIGKILL included, so a lock is never left stale. The descriptor is
not inherited by child processes (PEP 446), so a ``zfs send`` that outlives
a killed zark does not keep it. The file's content (pid, command, start) is
only for the refusal message. ``/run`` is shared by the installed zark and a
copy run from a stick, so both see the same lock.

Read-only commands run without it: ``status`` (the alerts timer runs it
hourly), ``explore``, ``monitor`` (watches a running backup),
``registry`` and ``exclude`` with no subcommand or ``list``, and ``selftest status``,
``report`` and ``certificate`` (M4s-16).
"""

import fcntl
import os
import time
from typing import NoReturn

from lib.log import Log

LOCK_PATH = "/run/zark/zark.lock"
EX_TEMPFAIL = 75  # sysexits.h: try again later

_READ_ONLY = ("status", "explore", "monitor")
_STATE: dict[str, int] = {}  # "fd": the descriptor of the lock this process holds


class LockHeld(Exception):
    """Another zark run holds the lock; the text says who."""


def selftest_subcommand(args: list[str]) -> str:
    """The subcommand ``zark selftest`` runs for ``args``: ``run`` when none is named."""
    return args[0] if args and not args[0].startswith("-") else "run"


def needs_lock(command: str, args: list[str]) -> bool:
    """Whether ``command`` (as typed, kebab-case) changes state."""
    if command in _READ_ONLY:
        return False
    if command in ("registry", "exclude"):
        return bool(args) and args[0] != "list"
    if command == "setup" and args == ["--remove"]:
        # It touches no pool, and the package's prerm runs it (M3P.2): a
        # backup holding the lock must not leave the guards behind.
        return False
    if command == "selftest":
        return selftest_subcommand(args) not in ("status", "report", "certificate")
    return True


def acquire(command: str) -> None:
    """Take the lock for this process, or raise :class:`LockHeld`."""
    if "fd" in _STATE:
        return
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = os.read(fd, 512).decode("utf-8", errors="replace").strip()
        finally:
            os.close(fd)
        raise LockHeld(holder or "another zark command") from None
    os.ftruncate(fd, 0)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    _ = os.write(fd, f"zark {command} (pid {os.getpid()}, since {stamp})\n".encode())
    _STATE["fd"] = fd


def guard(command: str, args: list[str]) -> None:
    """Entry-point check: take the lock when the command needs it, else return."""
    if not needs_lock(command, args):
        return
    try:
        acquire(command)
    except LockHeld as e:
        _refuse(f"Another zark run is in progress: {e}. Wait for it to end, then run again.")
    except OSError as e:
        _refuse(f"Cannot take the lock {LOCK_PATH}: {e}")


def _refuse(message: str) -> NoReturn:
    Log().error(message)
    raise SystemExit(EX_TEMPFAIL)
