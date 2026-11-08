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
What ``zark fullcheck`` reads and records, apart from its flow (M19):

  * the restore check's reader (M19-3 3a): a point's snapshot, mounted
    read-only, walked without following symlinks; every regular file read to
    its end once per inode, every symlink's target and every directory read;
    a device, fifo or socket never opened; nothing on another file system
    entered. A read that fails is recorded with its path and the walk goes on;
  * the result, and its record in the registry's ``last_check`` (M19-7 7a,
    M19R-6): the only source of what status.json and the MOTD say about it.
"""

import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lib import registry, sh
from lib.config import Config, parse_utc_iso
from lib.log import Log

LAST_CHECK_KEY = "last_check"
# clean: every check asked for passed; damage: a check found something wrong
# (a repair included); not_finished: interrupted, skipped or suspended;
# failed: a check could not run (the import, the passphrase, a key).
RESULTS = ("clean", "damage", "not_finished", "failed")
SCRUB_STATES = ("finished", "paused", "not_run")
CHECK_MNT = "/run/zark/fullcheck"
READ_CHUNK = 1 << 20
MAX_ERROR_PATHS = 20


@dataclass
class ReadResult:
    """What a walk read."""

    files: int = 0
    bytes: int = 0
    errors: int = 0
    error_paths: list[str] = field(default_factory=list)

    def failed(self, path: str, err: OSError) -> None:
        """One read that failed: counted, and named while there is room."""
        self.errors += 1
        if len(self.error_paths) < MAX_ERROR_PATHS:
            self.error_paths.append(f"{path}: {err.strerror or err}")


def _read_file(path: str, out: ReadResult, done: Callable[[int], None] | None) -> None:
    """Read ``path`` to its end; O_NOFOLLOW: a file swapped for a link is not followed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        out.failed(path, e)
        return
    try:
        while True:
            chunk = os.read(fd, READ_CHUNK)
            if not chunk:
                break
            out.bytes += len(chunk)
            if done is not None:
                done(len(chunk))
        out.files += 1
    except OSError as e:
        out.failed(path, e)
    finally:
        os.close(fd)


def read_tree(root: str, done: Callable[[int], None] | None = None) -> ReadResult:
    """Read everything under ``root`` (M19-3 3a); ``done`` hears each chunk's size."""
    out = ReadResult()
    try:
        root_dev = os.lstat(root).st_dev
    except OSError as e:
        out.failed(root, e)
        return out
    seen: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        here = stack.pop()
        try:
            with os.scandir(here) as entries:
                listed = list(entries)
        except OSError as e:
            out.failed(here, e)
            continue
        for entry in listed:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as e:
                out.failed(entry.path, e)
                continue
            if st.st_dev != root_dev:
                continue  # another file system mounted below: not this point's
            if stat.S_ISDIR(st.st_mode):
                stack.append(entry.path)
            elif stat.S_ISLNK(st.st_mode):
                try:
                    _ = os.readlink(entry.path)
                except OSError as e:
                    out.failed(entry.path, e)
            elif stat.S_ISREG(st.st_mode):
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                _read_file(entry.path, out, done)
    return out


@dataclass
class Check:  # pylint: disable=too-many-instance-attributes
    """One run's ``last_check``, as the registry keeps it."""

    started_at: str
    ended_at: str = ""
    result: str = ""  # one of RESULTS
    point: str | None = None
    scrub: dict[str, Any] | None = None
    restore: dict[str, Any] | None = None
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        """The registry's (and status.json's) ``last_check`` object."""
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "result": self.result,
            "point": self.point,
            "scrub": self.scrub,
            "restore": self.restore,
            "message": self.message,
        }


def scrub_record(  # pylint: disable=too-many-arguments
    state: str,
    *,
    examined: int | None = None,
    repaired: int | None = None,
    errors: int | None = None,
    permanent_errors: int = 0,
    seconds: float | None = None,
) -> dict[str, Any]:
    """The ``scrub`` object of ``last_check``."""
    return {
        "state": state,
        "examined_bytes": examined,
        "repaired_bytes": repaired,
        "errors": errors,
        "permanent_errors": permanent_errors,
        "seconds": None if seconds is None else int(seconds),
    }


def restore_record(datasets: int, read: ReadResult, seconds: float) -> dict[str, Any]:
    """The ``restore`` object of ``last_check``."""
    return {
        "datasets": datasets,
        "files": read.files,
        "bytes": read.bytes,
        "errors": read.errors,
        "error_paths": list(read.error_paths),
        "seconds": int(seconds),
    }


def _nonneg(value: Any) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return value if ok else None


def valid(value: Any) -> dict[str, Any] | None:
    """A registry ``last_check`` a reader can use, None otherwise: one that
    does not parse is shown as absent, never fatal to the registry."""
    if not isinstance(value, dict) or value.get("result") not in RESULTS:
        return None
    if not isinstance(value.get("ended_at"), str) or parse_utc_iso(value["ended_at"]) is None:
        return None
    if not isinstance(value.get("started_at"), str):
        return None
    return value


def last_check(info: Any) -> dict[str, Any] | None:
    """The ``last_check`` of a registry entry (a DriveInfo), when usable."""
    extra = getattr(info, "extra", None)
    return valid(extra.get(LAST_CHECK_KEY)) if isinstance(extra, dict) else None


def rate(prev: dict[str, Any] | None, part: str, size_key: str) -> float | None:
    """Bytes a second of ``part`` (``scrub`` or ``restore``) in a previous
    ``last_check``, None when it measured none (M19R-9)."""
    obj = prev.get(part) if prev else None
    if not isinstance(obj, dict):
        return None
    size, secs = _nonneg(obj.get(size_key)), _nonneg(obj.get("seconds"))
    return size / secs if size and secs else None


def save(guid: str, check: Check, log: Log) -> bool:
    """Write ``check`` as the drive's ``last_check`` in the registry; never
    from a live (its registry is the stick's, and nothing is recorded there,
    M19-6 6a). A failed write is a warning."""
    if sh.is_live_usb():
        return False
    cfg = Config.load()
    info = next((i for i in cfg.known_drives.values() if i.guid == guid), None)
    if info is None:
        log.warn("The drive is no longer registered: its full check is not recorded")
        return False
    info.extra[LAST_CHECK_KEY] = check.to_json()
    try:
        cfg.save_drives()
    except (registry.RegistryError, OSError) as e:
        log.warn(f"Could not record the full check in {cfg.drives_file_path}: {e}")
        return False
    return True


def now_s() -> float:
    """A monotonic clock, for the durations recorded."""
    return time.monotonic()
