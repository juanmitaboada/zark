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
``/var/lib/zark/status.json``: the state between runs, for other programs
(docs/STATUS-FILES.md, schema version 1; M7-4 4a, M7-13).

Written by every command that changes state at its end, by a backup when
its result is known, and by ``zark status`` (by hand and from the alerts
timer), never from a live session, whose ``/var/lib`` goes at shutdown.

Every write is a read-merge-write under ``flock(LOCK_EX)`` on
``/run/zark/status.lock``, waited for at most :data:`LOCK_WAIT_S`: the timer's
``zark status`` takes no ``zark.lock`` and may run while a backup ends. What
only a backup measures (a drive's ``space``, ``last_run`` and
``last_backup_point``) is carried forward from the previous file, by GUID; a
drive no longer in the registry is dropped. A previous file of a newer
``schema_version`` is never written over. A failed write is a warning, never
a failed command.
"""

import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib import sh, status, statusdirs
from lib.config import VERSION, Config, now_utc_iso, parse_utc_iso
from lib.log import Log
from lib.status import CAP_CRITICAL, CAP_WARNING, RPO_WARNING_DAYS, Status, cap_level, notices
from lib.zfs import Space

KIND = "zark-status"
SCHEMA_VERSION = 1
STATUS_PATH = statusdirs.STATE_DIR / "status.json"
LOCK_PATH = statusdirs.RUN_DIR / "status.lock"
LOCK_WAIT_S = 10.0
# Three refresh intervals of the hourly alerts timer (M7-12 12a): readers
# take the file as stale after it.
STALE_AFTER_S = 3 * 3600
RESULTS = ("completed", "incomplete", "not_verified", "aborted", "interrupted")


@dataclass(frozen=True)
class LastRun:
    """A backup run's ending, for one drive (``last_run``)."""

    result: str  # one of RESULTS
    started_at: str
    ended_at: str
    point: str | None = None
    datasets_missing: int = 0
    message: str = ""

    def to_json(self) -> dict[str, Any]:
        """The ``last_run`` object."""
        return {
            "command": "backup",
            "result": self.result,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "point": self.point,
            "datasets_missing": self.datasets_missing,
            "message": self.message,
        }


@dataclass(frozen=True)
class DriveUpdate:
    """What this writer measured for one drive; a None field is carried
    forward from the previous file. ``unless_started_at``: keep a previous
    ``last_run`` that started then (M7-7 7a: an orphan whose run had already
    recorded its result)."""

    last_run: LastRun | None = None
    space: Space | None = None
    last_backup_point: str | None = None
    unless_started_at: str = ""


def unusable_registry(error: str) -> str:
    """The warning when the registry cannot be read or parsed (M7R-1)."""
    return f"known_drives.json cannot be used ({error}): status.json not written"


def _warn(log: Log, text: str) -> None:
    log.warn(f"status.json: {text}")


def read(path: Path | None = None) -> dict[str, Any] | None:
    """The previous file as a dict, None when absent, unreadable or of
    another kind. Raises nothing."""
    try:
        doc = json.loads((path or STATUS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("kind") != KIND:
        return None
    return doc


def _previous_drives(prev: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The previous file's drive entries by GUID."""
    drives = prev.get("drives") if prev else None
    if not isinstance(drives, list):
        return {}
    return {d["guid"]: d for d in drives if isinstance(d, dict) and isinstance(d.get("guid"), str)}


def _age_days(last_backup_at: str | None, generated_at: str) -> int | None:
    last, now = parse_utc_iso(last_backup_at or ""), parse_utc_iso(generated_at)
    if last is None or now is None:
        return None
    return max(0, (now - last).days)


def build(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    st: Status,
    cfg: Config,
    generated_by: str,
    prev: dict[str, Any] | None,
    updates: dict[str, DriveUpdate] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """The document for ``st`` and the registry, merged with ``prev``."""
    now = generated_at or now_utc_iso()
    updates = updates or {}
    old = _previous_drives(prev)
    pools: list[dict[str, Any]] = []
    for pool, space in st.spaces.items():
        entry: dict[str, Any] = {
            "name": pool,
            "space": space.to_json() if space else None,
            "level": (cap_level(space.capacity_pct) or "ok") if space else None,
        }
        listed = st.datasets.get(pool)
        if space is not None and listed is not None:
            entry["datasets"] = [d.to_json() for d in listed]
        pools.append(entry)
    drives: list[dict[str, Any]] = []
    for name, info in sorted(cfg.known_drives.items()):
        before = old.get(info.guid, {})
        up = updates.get(info.guid, DriveUpdate())
        policy = cfg.drive_policy(name)
        last_run = before.get("last_run")
        if up.last_run is not None and not (
            up.unless_started_at
            and isinstance(last_run, dict)
            and last_run.get("started_at") == up.unless_started_at
        ):
            last_run = up.last_run.to_json()
        drives.append(
            {
                "name": name,
                "guid": info.guid,
                "drive_id": info.drive_id or "<unknown>",
                "autoeject": info.autoeject,
                "retention": {
                    "window_days": policy.window_days,
                    "max_age_days": policy.max_age_days,
                    "margin_pct": policy.margin_pct,
                },
                "last_backup_at": info.last_backup_at
                if parse_utc_iso(info.last_backup_at or "")
                else None,
                "age_days": _age_days(info.last_backup_at, now),
                "last_backup_point": up.last_backup_point or before.get("last_backup_point"),
                "last_run": last_run,
                "space": up.space.to_json() if up.space else before.get("space"),
            }
        )
    return {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "zark_version": VERSION,
        "generated_at": now,
        "generated_by": generated_by,
        "registry": str(cfg.drives_file_path),
        "thresholds": {
            "capacity_warning_pct": CAP_WARNING,
            "capacity_critical_pct": CAP_CRITICAL,
            "rpo_warning_days": RPO_WARNING_DAYS,
            "stale_after_s": STALE_AFTER_S,
        },
        "source_pools": pools,
        "drives": drives,
        "notices": [
            {"level": n.level, "code": n.code, "subject": n.subject, "text": n.text}
            for n in notices(st)
        ],
    }


def _lock(path: Path, wait: float) -> int | None:
    """The descriptor of ``path`` locked LOCK_EX, None after ``wait`` seconds."""
    _ = statusdirs.ensure(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.1)


def write_atomic(path: Path, text: str, *, durable: bool) -> None:
    """Replace ``path`` with ``text``, 0644, through a dot-named temporary
    file in its directory (docs/STATUS-FILES.md, "Atomic write"); ``durable``
    fsyncs the file and the directory (status.json; tmpfs needs neither).
    The temporary file never survives an error."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fchmod(f.fileno(), 0o644)
            if durable:
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if durable:
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def write(  # pylint: disable=too-many-arguments,too-many-return-statements
    st: Status,
    cfg: Config,
    generated_by: str,
    log: Log,
    *,
    updates: dict[str, DriveUpdate] | None = None,
    path: Path | None = None,
    lock_path: Path | None = None,
) -> bool:
    """Read, merge and write status.json; True when written. Never raises
    for a write that fails: it logs a warning."""
    if sh.is_live_usb():
        return False
    if cfg.load_error is not None:
        # An empty drive list would drop every drive's carried-forward fields (M7R-1).
        log.warn(unusable_registry(cfg.load_error))
        return False
    path = path or STATUS_PATH
    try:
        fd = _lock(lock_path or LOCK_PATH, LOCK_WAIT_S)
    except OSError as e:
        _warn(log, f"cannot take {lock_path or LOCK_PATH}: {e}; not written")
        return False
    if fd is None:
        _warn(log, f"{lock_path or LOCK_PATH} still held after {LOCK_WAIT_S:.0f} s; not written")
        return False
    try:
        prev = read(path)
        if prev is None and path.exists():
            _warn(log, "the previous file is unreadable; nothing carried forward")
        version = prev.get("schema_version") if prev else None
        if isinstance(version, int) and version > SCHEMA_VERSION:
            # A newer zark wrote it; an older one (a stick) would drop what it does not know.
            _warn(log, f"written by a newer zark (schema {version}); left as it is")
            return False
        doc = build(st, cfg, generated_by, prev, updates)
        _ = statusdirs.ensure(path.parent)
        write_atomic(path, json.dumps(doc, indent=2) + "\n", durable=True)
        return True
    except (OSError, ValueError) as e:
        _warn(log, f"could not write {path}: {e}")
        return False
    finally:
        os.close(fd)


def refresh(
    cfg: Config,
    log: Log,
    generated_by: str,
    updates: dict[str, DriveUpdate] | None = None,
) -> Status:
    """One read of the pools, the registry and origin (:func:`status.gather`)
    gives the MOTD file and status.json, so the two never disagree (M7-13
    13a). Failures are logged, never fatal."""
    st = status.gather(cfg, log)
    try:
        status.write_motd(status.motd_text(st))
    except OSError as e:
        log.warn(f"Could not write {status.MOTD_FILE}: {e}")
    _ = write(st, cfg, generated_by, log, updates=updates)
    return st
