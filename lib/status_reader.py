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
The reference reader of zark's status files, schema version 1
(``docs/STATUS-FILES.md``, "Reading the files" and "For third-party
readers").

It reads ``/run/zark/progress.json`` and ``/var/lib/zark/status.json``,
classifies each one (orphan, stalled and stale rules), gives the indicator
state, and computes the figures the contract defines. It never writes, takes
no lock and runs no command.

This file depends on the standard library only, so another program may copy
it as it is or import it from ``/usr/share/zark/lib``. The names in
``__all__`` keep their meaning and signature within schema version 1; new
names may be added. Its tests are the fixtures in ``tests/fixtures/status/``
and their ``expected.json``, which every reader shares.
"""

import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "ATTENTION_RESULTS",
    "BOOT_ID_PATH",
    "BY_ID",
    "PROC",
    "PROGRESS_PATH",
    "SCHEMA_VERSION",
    "STALLED_AFTER_S",
    "STATUS_PATH",
    "FileRead",
    "Indicator",
    "ProgressClass",
    "Reading",
    "StatusClass",
    "age_days",
    "byte_fraction",
    "capacity_level",
    "chart_total",
    "classify_progress",
    "classify_status",
    "DriveContents",
    "drive_connected",
    "drive_contents",
    "drive_margin_bytes",
    "eta_seconds",
    "file_key",
    "indicator",
    "own_share",
    "parse_timestamp",
    "pool_metadata_bytes",
    "paths",
    "process_alive",
    "retention_next_is_floor",
    "read_all",
    "read_progress",
    "read_status",
    "reserved_bytes",
]

SCHEMA_VERSION = 1
PROGRESS_PATH = Path("/run/zark/progress.json")
STATUS_PATH = Path("/var/lib/zark/status.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
PROC = Path("/proc")
BY_ID = Path("/dev/disk/by-id")
STALLED_AFTER_S = 30
ATTENTION_RESULTS = ("incomplete", "not_verified", "interrupted")

FileState = Literal["absent", "ok", "unreadable", "newer"]
ProgressClass = Literal["idle", "running", "stalled", "orphan", "unknown"]
StatusClass = Literal["ok", "stale", "newer", "unreadable", "absent"]
Indicator = Literal["waiting", "running", "attention", "edge", "overdue", "none", "ok"]
Doc = dict[str, Any]

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Fields a reader needs before it can apply its rules; a file without them
# is treated as unreadable rather than half-read.
_PROGRESS_NEEDS: tuple[tuple[str, tuple[type, ...]], ...] = (
    ("pid", (int,)),
    ("pid_start_ticks", (int,)),
    ("boot_id", (str,)),
    ("updated_at", (str,)),
    ("waiting_for_input", (bool,)),
)
_STATUS_NEEDS: tuple[tuple[str, tuple[type, ...]], ...] = (
    ("generated_at", (str,)),
    ("thresholds", (dict,)),
    ("source_pools", (list,)),
    ("drives", (list,)),
    ("notices", (list,)),
)


@dataclass(frozen=True)
class FileRead:
    """One read of one file.

    ``state``: ``absent``; ``ok``; ``unreadable`` (does not parse, not an
    object, another ``kind``, or a needed field missing); ``newer`` (a
    ``schema_version`` above :data:`SCHEMA_VERSION`, not interpreted).
    ``doc`` is the parsed object only when ``state`` is ``ok``. ``key`` is
    (inode, mtime in ns, size) of what was read, for change detection.
    """

    state: FileState
    doc: Doc | None = None
    error: str = ""
    key: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class Reading:
    """Both files, read and classified at ``now``."""

    now: datetime
    progress: FileRead
    status: FileRead
    progress_class: ProgressClass
    status_class: StatusClass
    indicator: Indicator


def paths(root: Path = Path("/")) -> tuple[Path, Path]:
    """``progress.json`` and ``status.json`` under ``root`` (the fixtures'
    layout mirrors the real one)."""
    return (
        root / PROGRESS_PATH.relative_to("/"),
        root / STATUS_PATH.relative_to("/"),
    )


def parse_timestamp(value: object) -> datetime | None:
    """A contract timestamp (``2026-10-24T09:15:02Z``) as an aware UTC
    datetime; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _missing(doc: Mapping[str, Any], needs: tuple[tuple[str, tuple[type, ...]], ...]) -> str:
    for name, types in needs:
        value = doc.get(name)
        # bool is an int to Python; a count or a pid is never one.
        if not isinstance(value, types) or (bool not in types and isinstance(value, bool)):
            return name
    return ""


# One return per rule of "Reading the files", in the contract's order.
def _read(  # pylint: disable=too-many-return-statements
    path: Path, kind: str, needs: tuple[tuple[str, tuple[type, ...]], ...]
) -> FileRead:
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            data = f.read()
    except FileNotFoundError:
        return FileRead("absent")
    except OSError as e:
        return FileRead("unreadable", error=f"cannot read: {e.strerror or e}")
    key = (st.st_ino, st.st_mtime_ns, st.st_size)
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return FileRead("unreadable", error="not valid JSON", key=key)
    if not isinstance(doc, dict):
        return FileRead("unreadable", error="not a JSON object", key=key)
    if doc.get("kind") != kind:
        return FileRead("unreadable", error=f"kind is not {kind}", key=key)
    version = doc.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return FileRead("unreadable", error="no valid schema_version", key=key)
    if version > SCHEMA_VERSION:
        return FileRead("newer", error=f"schema_version {version}", key=key)
    name = _missing(doc, needs)
    if name:
        return FileRead("unreadable", error=f"{name} missing or of the wrong type", key=key)
    return FileRead("ok", doc=doc, key=key)


def file_key(path: Path) -> tuple[int, int, int] | None:
    """(inode, mtime in ns, size) of ``path``, None when it is absent: a
    reader polls this every second and reads again when it changes. A file
    that cannot be examined gives (0, 0, -1), so it is read again each time."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return (0, 0, -1)
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def read_progress(path: Path = PROGRESS_PATH) -> FileRead:
    """Read ``progress.json`` whole, ``kind`` first, then ``schema_version``."""
    return _read(path, "zark-progress", _PROGRESS_NEEDS)


def read_status(path: Path = STATUS_PATH) -> FileRead:
    """Read ``status.json`` whole, ``kind`` first, then ``schema_version``."""
    return _read(path, "zark-status", _STATUS_NEEDS)


def _start_ticks(stat_line: str) -> int | None:
    """Field 22 (``starttime``) of a ``/proc/<pid>/stat`` line: the 20th after
    the last ``)``, since the process name may hold spaces or parentheses."""
    fields = stat_line[stat_line.rfind(")") + 1 :].split()
    if stat_line.rfind(")") < 0 or len(fields) < 20 or not fields[19].isdigit():
        return None
    return int(fields[19])


def process_alive(
    doc: Mapping[str, Any],
    *,
    proc: Path = PROC,
    boot_id_path: Path = BOOT_ID_PATH,
) -> bool:
    """Whether the process a ``progress.json`` names still runs (orphan rules
    3 to 5). A check whose file cannot be read is skipped, as rule 5 says for
    ``hidepid``: an unreadable check never makes a live run an orphan."""
    try:
        boot_id = boot_id_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        boot_id = ""
    if boot_id and doc.get("boot_id") != boot_id:
        return False
    pid = doc.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not (proc / str(pid)).exists():
        return False
    try:
        line = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    ticks = _start_ticks(line)
    return ticks is None or ticks == doc.get("pid_start_ticks")


def classify_progress(
    read: FileRead,
    now: datetime,
    alive: Callable[[Mapping[str, Any]], bool] = process_alive,
) -> ProgressClass:
    """``progress.json``'s class: orphan detection, first match wins."""
    if read.state == "absent":
        return "idle"
    if read.state != "ok" or read.doc is None:
        return "unknown"
    updated = parse_timestamp(read.doc.get("updated_at"))
    if updated is None:
        return "unknown"
    if not alive(read.doc):
        return "orphan"
    if (now - updated).total_seconds() > STALLED_AFTER_S:
        return "stalled"
    return "running"


def classify_status(read: FileRead, now: datetime) -> StatusClass:
    """``status.json``'s class; stale against ``thresholds.stale_after_s``."""
    if read.state in ("absent", "newer"):
        return "absent" if read.state == "absent" else "newer"
    doc = read.doc if read.state == "ok" else None
    generated = parse_timestamp(doc.get("generated_at")) if doc is not None else None
    limit = doc["thresholds"].get("stale_after_s") if doc is not None else None
    if generated is None or not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return "unreadable"
    return "stale" if (now - generated).total_seconds() > limit else "ok"


def _dicts(value: object) -> list[Mapping[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _attention(doc: Mapping[str, Any]) -> bool:
    if any(n.get("level") == "critical" for n in _dicts(doc.get("notices"))):
        return True
    for drive in _dicts(doc.get("drives")):
        run = drive.get("last_run")
        if isinstance(run, dict) and run.get("result") in ATTENTION_RESULTS:
            return True
    return False


def _overdue(doc: Mapping[str, Any]) -> bool:
    return any(
        n.get("code") == "backup_age" and n.get("level") == "warning"
        for n in _dicts(doc.get("notices"))
    )


# One return per row of the contract's table, first match wins.
def indicator(  # pylint: disable=too-many-return-statements
    progress_class: ProgressClass,
    progress_doc: Mapping[str, Any] | None,
    status_class: StatusClass,
    status_doc: Mapping[str, Any] | None,
) -> Indicator:
    """The indicator state from both files, first match wins."""
    if progress_class in ("running", "stalled"):
        if progress_doc is not None and progress_doc.get("waiting_for_input") is True:
            return "waiting"
        return "running"
    readable = status_class in ("ok", "stale") and status_doc is not None
    if readable and status_doc is not None and _attention(status_doc):
        return "attention"
    if status_class in ("newer", "unreadable", "stale") or progress_class in ("orphan", "unknown"):
        return "edge"
    if readable and status_doc is not None and _overdue(status_doc):
        return "overdue"
    if status_class == "absent":
        return "none"
    return "ok"


def read_all(
    root: Path = Path("/"),
    now: datetime | None = None,
    alive: Callable[[Mapping[str, Any]], bool] = process_alive,
) -> Reading:
    """Read both files under ``root`` and classify them at ``now`` (the
    reader's clock when None)."""
    when = now or datetime.now(UTC)
    progress_path, status_path = paths(root)
    progress = read_progress(progress_path)
    status = read_status(status_path)
    pc = classify_progress(progress, when, alive)
    sc = classify_status(status, when)
    return Reading(
        now=when,
        progress=progress,
        status=status,
        progress_class=pc,
        status_class=sc,
        indicator=indicator(pc, progress.doc, sc, status.doc),
    )


# ── derived figures ──────────────────────────────────────────────────────


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def reserved_bytes(space: Mapping[str, Any]) -> int:
    """Space reserved by ZFS: ``max(0, pool_free_bytes - available_bytes)``."""
    free = _int(space.get("pool_free_bytes")) or 0
    avail = _int(space.get("available_bytes")) or 0
    return max(0, free - avail)


def chart_total(space: Mapping[str, Any]) -> int:
    """The total of a usage chart: used + available + reserved."""
    used = _int(space.get("used_bytes")) or 0
    avail = _int(space.get("available_bytes")) or 0
    return used + avail + reserved_bytes(space)


def own_share(dataset: Mapping[str, Any]) -> int:
    """A dataset's own share: usedbydataset + usedbysnapshots +
    usedbyrefreservation. The shares of a pool's datasets do not overlap;
    their sum is a little less than the root's ``used_bytes`` (see
    :func:`pool_metadata_bytes`)."""
    return sum(
        _int(dataset.get(k)) or 0
        for k in ("usedbydataset_bytes", "usedbysnapshots_bytes", "usedbyrefreservation_bytes")
    )


def pool_metadata_bytes(pool: str, datasets: Sequence[Mapping[str, Any]]) -> int:
    """What the root's ``used_bytes`` holds beyond the datasets' own shares:
    the pool's metadata in directories ``zfs list`` never shows (``$MOS``
    among them), and children's unused ``reservation`` where one is set
    (OpenZFS 2.4.1 module/zfs/dsl_pool.c:512, :798-812). 0 when the listing
    has no root."""
    root = next((d for d in datasets if d.get("name") == pool), None)
    if root is None:
        return 0
    used = _int(root.get("used_bytes")) or 0
    return max(0, used - sum(own_share(d) for d in datasets))


def capacity_level(pct: int, thresholds: Mapping[str, Any]) -> Literal["ok", "warning", "critical"]:
    """A pool's level from its capacity and the writer's thresholds."""
    critical = _int(thresholds.get("capacity_critical_pct")) or 90
    warning = _int(thresholds.get("capacity_warning_pct")) or 80
    if pct >= critical:
        return "critical"
    if pct >= warning:
        return "warning"
    return "ok"


def byte_fraction(transfer: Mapping[str, Any]) -> float | None:
    """``bytes_done`` over ``bytes_total``, kept below 1 while the transfer
    runs; None when either is null (not measured, never a zero)."""
    done = _int(transfer.get("bytes_done"))
    total = _int(transfer.get("bytes_total"))
    if done is None or total is None or total <= 0:
        return None
    return min(max(done, 0) / total, 0.999)


def eta_seconds(transfer: Mapping[str, Any]) -> int | None:
    """Seconds left, only when ``bytes_total`` and ``rate_bytes_per_s`` are
    non-null, the rate above 0 and the estimate not partial."""
    total = _int(transfer.get("bytes_total"))
    rate = _int(transfer.get("rate_bytes_per_s"))
    done = _int(transfer.get("bytes_done"))
    if total is None or rate is None or done is None or rate <= 0:
        return None
    if transfer.get("bytes_total_partial") is not False:
        return None
    return math.ceil(max(0, total - done) / rate)


def age_days(last_backup_at: object, now: datetime) -> int | None:
    """Whole days since ``last_backup_at`` at ``now``; never negative."""
    when = parse_timestamp(last_backup_at)
    if when is None:
        return None
    return max(0, int((now - when).total_seconds() // 86400))


def drive_connected(drive_id: object, by_id: Path = BY_ID) -> bool:
    """Whether ``/dev/disk/by-id/<drive_id>`` exists (a link, followed or not)."""
    if not isinstance(drive_id, str) or drive_id in ("", ".", "..") or "/" in drive_id:
        return False
    return os.path.lexists(by_id / drive_id)


# ── drive contents (optional in schema 1: "Drive contents") ─────────────


@dataclass(frozen=True)
class DriveContents:  # pylint: disable=too-many-instance-attributes
    """What a drive's tab shows from its five optional fields, as the
    fixtures' ``expected.json`` names it. A field the drive entry lacks
    leaves out only what it feeds (None)."""

    points: int | None
    months: dict[str, int] | None
    next_points: int | None
    next_reclaim_bytes: int | None
    available_after_policy_bytes: int | None
    datasets: int | None
    # R9.2: per `before` month, what removing every older candidate frees;
    # a value is None when not measured, never 0.
    frees_if_older: dict[str, int | None] | None = None


_CONTENTS = ("datasets", "points", "non_point_snapshots", "retention_next", "reclaim_by_age")


def drive_margin_bytes(drive: Mapping[str, Any]) -> int | None:
    """The free space a backup tries to leave on the drive:
    ``(used + available) * margin_pct / 100``, rounded down; None without a
    ``space`` or a ``retention.margin_pct``."""
    space, retention = drive.get("space"), drive.get("retention")
    if not isinstance(space, dict) or not isinstance(retention, dict):
        return None
    pct = _int(retention.get("margin_pct"))
    used, avail = _int(space.get("used_bytes")), _int(space.get("available_bytes"))
    if pct is None or used is None or avail is None:
        return None
    return (used + avail) * pct // 100


def drive_contents(drive: Mapping[str, Any]) -> DriveContents | None:
    """The drive contents, or None when all five fields are absent (or there
    is no ``space`` to date them: they never come without it)."""
    if not isinstance(drive.get("space"), dict) or not any(k in drive for k in _CONTENTS):
        return None
    points = drive.get("points")
    months: dict[str, int] | None = None
    if isinstance(points, list):
        months = {}
        for point in points:
            when = parse_timestamp(point.get("created_at")) if isinstance(point, dict) else None
            if when is not None:
                key = when.strftime("%Y-%m")
                months[key] = months.get(key, 0) + 1
    nxt = drive.get("retention_next")
    nxt = nxt if isinstance(nxt, dict) else None
    reclaim = _int(nxt.get("reclaim_bytes")) if nxt else None
    avail = _int(drive["space"].get("available_bytes"))
    datasets = drive.get("datasets")
    by_age = drive.get("reclaim_by_age")
    frees: dict[str, int | None] | None = None
    if isinstance(by_age, list):
        frees = {
            e["before"]: _int(e.get("reclaim_bytes"))
            for e in by_age
            if isinstance(e, dict) and isinstance(e.get("before"), str)
        }
    return DriveContents(
        points=len(points) if isinstance(points, list) else None,
        months=months,
        next_points=len(nxt["points"]) if nxt and isinstance(nxt.get("points"), list) else None,
        next_reclaim_bytes=reclaim,
        available_after_policy_bytes=avail + reclaim
        if reclaim is not None and avail is not None
        else None,
        datasets=len(datasets) if isinstance(datasets, list) else None,
        frees_if_older=frees,
    )


def retention_next_is_floor(drive: Mapping[str, Any], now: datetime) -> bool:
    """Whether ``retention_next`` is only a lower bound: a window shorter than
    its horizon, or its horizon passed since ``space.measured_at``."""
    nxt, space, retention = drive.get("retention_next"), drive.get("space"), drive.get("retention")
    if not isinstance(nxt, dict) or not isinstance(space, dict):
        return False
    horizon = _int(nxt.get("horizon_days")) or 0
    window = _int(retention.get("window_days")) if isinstance(retention, dict) else None
    measured = parse_timestamp(space.get("measured_at"))
    passed = measured is not None and (now - measured).total_seconds() > horizon * 86400
    return passed or (window is not None and window < horizon)
