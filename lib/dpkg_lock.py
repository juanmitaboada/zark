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
dpkg's locks around zark's imports of a backup pool (M18-9, M18-10, M18-12).

A kernel removal whose APT hook and prerm passed with no backup pool
imported runs update-grub from its postrm after the files are gone; a
backup pool imported in between makes the grub guard refuse it and leaves
dpkg half-done. So zark never imports a backup pool while apt, dpkg or
unattended-upgrades runs, and none of them can start while zark's
``zpool import`` runs:

- :func:`holders` reads both locks without taking them: ``F_GETLK`` on a
  read-only descriptor. dpkg and APT lock with ``fcntl`` record locks
  (dpkg lib/dpkg/file.c, APT apt-pkg/contrib/fileutl.cc), which
  ``flock(2)`` does not see.
- :func:`hold` takes ``lock-frontend`` as APT does (``F_SETLK``, whole
  file, without waiting), then reads ``lock``, and keeps the first for the
  ``with`` block: the import. APT and dpkg take ``lock-frontend`` first and
  fail, or wait, while zark holds it; once the import has ended the pool is
  imported and the hook and the kernel prerm see it.

A process's ``fcntl`` locks go when it closes *any* descriptor of the file,
and when it ends however it ends; a forked child neither holds nor drops
them. So inside a hold zark opens no other descriptor of lock-frontend
(``holders(skip_frontend=True)``), a dead zark leaves nothing behind, and
the ``zpool import`` child cannot drop the lock.

Nothing is created: a file that does not exist is not held, and with no
lock-frontend (no dpkg run ever on this system) nothing is taken. On a live
session neither lock is read: the live's dpkg is not the system zark guards.
"""

import errno
import fcntl
import os
import struct
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from lib import sh
from lib.config import zark_command
from lib.log import Log

# dpkg's lib/dpkg/dpkg.h; module attributes so the tests can point them away.
LOCK_FRONTEND = "/var/lib/dpkg/lock-frontend"
LOCK = "/var/lib/dpkg/lock"

# struct flock on x86_64 and arm64 glibc: l_type, l_whence (short), padding,
# l_start, l_len (off_t), l_pid (pid_t), padding: 32 bytes.
_FLOCK = "hhqqi4x"
_WHOLE_FILE_WRLCK = struct.pack(_FLOCK, fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
_UNLOCK = struct.pack(_FLOCK, fcntl.F_UNLCK, os.SEEK_SET, 0, 0, 0)

_STATE = {"held": False}  # inside hold(): what the tests read to see what runs under it


@dataclass(frozen=True)
class Holder:
    """A lock someone else holds, or that could not be read (fail closed)."""

    path: str
    pid: int = 0
    comm: str = ""
    error: str = ""

    def who(self) -> str:
        """``1234 unattended-upgr``, the pid alone, or the read error."""
        if self.error:
            return f"unreadable: {self.error}"
        return f"{self.pid} {self.comm}" if self.comm else str(self.pid)

    def held_by(self) -> str:
        """``/var/lib/dpkg/lock-frontend held by 1234 unattended-upgr``."""
        if self.error:
            return f"{self.path} {self.who()}"
        return f"{self.path} held by {self.who()}"

    def is_held_by(self) -> str:
        """``/var/lib/dpkg/lock-frontend is held by 1234 (unattended-upgr)``."""
        if self.error:
            return f"{self.path} is {self.who()}"
        name = f" ({self.comm})" if self.comm else ""
        return f"{self.path} is held by {self.pid}{name}"


def describe(held: list[Holder]) -> str:
    """The holders as the refusal and the read-back name them."""
    return "; ".join(h.held_by() for h in held) or f"{LOCK_FRONTEND} taken by another process"


class DpkgBusy(Exception):
    """apt, dpkg or unattended-upgrades holds a dpkg lock: zark does not
    import a backup pool now. Caught at the entry point (exit 75)."""

    def __init__(self, held: list[Holder]):
        self.holders = held
        super().__init__(self.message(""))

    def message(self, command: str) -> str:
        """The refusal, naming the command to run again (``zark`` alone when
        none is given)."""
        again = f"{zark_command()} {command}".rstrip()
        return (
            f"apt or dpkg is running ({describe(self.holders)}): zark does not import a "
            + "backup pool while packages are being installed or removed. "
            + f"Wait for it to end, then run `{again}` again."
        )


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _query(path: str) -> Holder | None:
    """Who holds ``path``'s whole-file write lock, None when nobody does or
    the file does not exist; any other error counts as held."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)  # never O_CREAT
    except FileNotFoundError:
        return None
    except OSError as e:
        return Holder(path, error=e.strerror or str(e))
    try:
        got = fcntl.fcntl(fd, fcntl.F_GETLK, _WHOLE_FILE_WRLCK)
    except OSError as e:
        return Holder(path, error=e.strerror or str(e))
    finally:
        os.close(fd)
    l_type, _, _, _, pid = struct.unpack(_FLOCK, got)
    if l_type == fcntl.F_UNLCK:
        return None
    return Holder(path, pid, _comm(pid))


def holders(*, skip_frontend: bool = False) -> list[Holder]:
    """The dpkg locks another process holds. ``skip_frontend`` only from
    inside :func:`hold`: opening and closing lock-frontend there would drop
    zark's own lock."""
    paths = [LOCK] if skip_frontend else [LOCK_FRONTEND, LOCK]
    return [h for path in paths if (h := _query(path)) is not None]


def refuse_if_busy() -> None:
    """Raise :class:`DpkgBusy` when either lock is held (nothing on a live)."""
    if sh.is_live_usb():
        return
    if held := holders():
        raise DpkgBusy(held)


@contextmanager
def hold(log: Log) -> Iterator[None]:
    """Hold lock-frontend, with lock free, for the ``with`` block (the
    import alone); raise :class:`DpkgBusy` before it when either is held.
    Released by the ``finally`` on every way out, and by the kernel when
    zark dies. Nothing on a live."""
    if sh.is_live_usb():
        yield
        return
    fd = -1
    try:
        # The take inside the try: a signal right after it still releases (M18R-15).
        try:
            fd = os.open(LOCK_FRONTEND, os.O_RDWR | os.O_CLOEXEC)  # never O_CREAT
        except FileNotFoundError:
            log.dbg(f"No {LOCK_FRONTEND}: dpkg has never run here, nothing to hold")
        except OSError as e:
            raise DpkgBusy([Holder(LOCK_FRONTEND, error=e.strerror or str(e))]) from e
        if fd >= 0:
            try:
                _ = fcntl.fcntl(fd, fcntl.F_SETLK, _WHOLE_FILE_WRLCK)
            except OSError as e:
                os.close(fd)
                fd = -1
                if e.errno in (errno.EAGAIN, errno.EACCES):
                    raise DpkgBusy(holders()) from e
                raise DpkgBusy([Holder(LOCK_FRONTEND, error=e.strerror or str(e))]) from e
            log.dbg(f"{LOCK_FRONTEND} taken at {time.time():.3f} by pid {os.getpid()}")
        if held := holders(skip_frontend=True):
            raise DpkgBusy(held)
        _STATE["held"] = True
        yield
    finally:
        _STATE["held"] = False
        if fd >= 0:
            try:
                _ = fcntl.fcntl(fd, fcntl.F_SETLK, _UNLOCK)
            finally:
                os.close(fd)
            log.dbg(f"{LOCK_FRONTEND} released at {time.time():.3f} by pid {os.getpid()}")


def is_held() -> bool:
    """True inside :func:`hold` (for the tests of what runs under it)."""
    return _STATE["held"]
