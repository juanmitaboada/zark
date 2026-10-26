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
What a backup drive holds, for its entry in status.json (R9, R9.1, R9.2).

The five optional fields of docs/STATUS-FILES.md, "Drive contents":
``datasets``, ``points``, ``non_point_snapshots``, ``retention_next`` and
``reclaim_by_age``. They are measured while zark has the drive imported, from
zark's own rules (:mod:`lib.retention`, the removal order of ``zark prune``)
and read-only commands: ``zfs list`` and ``zfs destroy -nvp``
(:func:`lib.reclaim.measure`, the only way this module asks ZFS what a
removal frees). Nothing here destroys or writes anything.

A figure that cannot be measured is left out (a field) or null (a
``reclaim_bytes``), never guessed.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lib import drive_marks, reclaim, survey, zfs
from lib.log import Log
from lib.replication import POINT_RE, Dest
from lib.retention import (
    DAY,
    Policy,
    Removal,
    plan_prune,
    point_removals,
    point_times,
    snapshot_kind,
    space_candidates,
    whole_datasets,
)

# How far retention_next moves the clock (contract: 7).
HORIZON_DAYS = 7

# The five fields, in the contract's order; carried with `space` as one group.
CONTENT_FIELDS = ("datasets", "points", "non_point_snapshots", "retention_next", "reclaim_by_age")


@dataclass(frozen=True)
class DriveContents:
    """The drive contents of one measurement; a field is None when it could
    not be read and is then left out of the entry."""

    datasets: list[dict[str, Any]] | None = None
    points: list[dict[str, Any]] | None = None
    non_point_snapshots: dict[str, Any] | None = None
    retention_next: dict[str, Any] | None = None
    reclaim_by_age: list[dict[str, Any]] | None = None

    def to_json(self) -> dict[str, Any]:
        """The fields that were measured, as the drive entry carries them."""
        return {k: getattr(self, k) for k in CONTENT_FIELDS if getattr(self, k) is not None}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_after(ts: int) -> str:
    """``YYYY-MM`` of the UTC month after the one ``ts`` falls in."""
    t = datetime.fromtimestamp(ts, UTC)
    return f"{t.year + t.month // 12:04d}-{t.month % 12 + 1:02d}"


def _points(
    dest: dict[str, Dest],
    times: dict[str, int],
    run: set[str],
    whole: set[str],
    used: dict[str, int],
) -> list[dict[str, Any]] | None:
    """One entry per backup point, oldest first; None when a snapshot's
    ``used`` is missing (listed between the two reads)."""
    holders: dict[str, list[str]] = {}
    for rel, d in dest.items():
        for s in d.snaps:
            if POINT_RE.match(s.name):
                holders.setdefault(s.name, []).append(rel)
    out: list[dict[str, Any]] = []
    for name in sorted(times, key=lambda n: (times[n], n)):
        keys = [f"{rel}@{name}" for rel in holders.get(name, [])]
        if any(k not in used for k in keys):
            return None
        out.append(
            {
                "name": name,
                "created_at": _iso(times[name]),
                "complete": name in run,
                "whole": name in run and name in whole,
                "datasets": len(keys),
                "used_bytes": sum(used[k] for k in keys),
            }
        )
    return out


def _non_points(dest: dict[str, Dest], used: dict[str, int]) -> dict[str, Any] | None:
    """Count and ``used`` of the snapshots that are not points, by kind."""
    out = {k: {"count": 0, "used_bytes": 0} for k in ("intermediate", "other")}
    for rel, d in dest.items():
        for s in d.snaps:
            kind = snapshot_kind(s.name)
            if kind == "point":
                continue
            if f"{rel}@{s.name}" not in used:
                return None
            out[kind]["count"] += 1
            out[kind]["used_bytes"] += used[f"{rel}@{s.name}"]
    return out


@dataclass
class _Pass:
    """The dry runs of one measurement: their deadline (a
    :func:`time.monotonic` value, None: no limit), how many ran, and how
    many figures were left unmeasured."""

    pool: str
    deadline: float | None
    tally: list[int] = field(default_factory=list)
    unmeasured: int = 0

    def measure(self, removals: list[Removal], tops: list[str]) -> int | None:
        """:func:`lib.reclaim.measure` within the deadline, counted."""
        got = reclaim.measure(self.pool, removals, tops, self.deadline, self.tally)
        if got is None:
            self.unmeasured += 1
        return got


def _retention_next(
    dry: _Pass, dest: dict[str, Dest], times: dict[str, int], plan: dict[str, Any]
) -> dict[str, Any]:
    """The policy at ``now + HORIZON_DAYS``: what it removes and frees."""
    prune = plan_prune(dest, **{**plan, "now": plan["now"] + HORIZON_DAYS * DAY})
    names = {r.name for r in prune.snapshots if POINT_RE.match(r.name)}
    # Nothing goes: 0 is exact, not a guess, and needs no dry run (M9a-7).
    freed = 0 if prune.empty else dry.measure(prune.snapshots, [t for t, _ in prune.datasets])
    return {
        "horizon_days": HORIZON_DAYS,
        "points": sorted(names, key=lambda n: (times.get(n, 0), n)),
        "snapshots": len(prune.snapshots),
        "datasets_whole": len(prune.datasets),
        "reclaim_bytes": freed,
    }


def _reclaim_by_age(
    dry: _Pass,
    dest: dict[str, Dest],
    times: dict[str, int],
    plan: dict[str, Any],
    pins: dict[str, str],
) -> list[dict[str, Any]]:
    """The candidates ``zark prune`` offers beyond the policy, oldest first,
    each with what removing it and every older one frees (cumulative)."""
    prune = plan_prune(dest, **plan)
    names = [
        name
        for name in space_candidates(
            dest, plan["complete"], plan["policy"], plan["now"], prune, plan["whole"]
        )
        if point_removals(dest, name, pinned=pins)
    ]
    out: list[dict[str, Any]] = []
    removals: list[Removal] = []
    stopped = False
    for k, name in enumerate(names, 1):
        removals += point_removals(dest, name, pinned=pins)
        freed = None
        if not stopped:
            # As _beyond does: a lineage these removals leave empty goes whole (M3R-3).
            freed = dry.measure(removals, [t for t, _ in whole_datasets(dest, removals)])
            stopped = freed is None
        else:
            dry.unmeasured += 1
        out.append(
            {
                "before": month_after(times[name]),
                "points": k,
                "last_point": name,
                "reclaim_bytes": freed,
            }
        )
    return out


def measure(  # pylint: disable=too-many-locals
    pool: str, policy: Policy, log: Log, *, now: int | None = None, budget_s: float | None = None
) -> DriveContents:
    """The drive contents of imported drive ``pool``. ``budget_s`` bounds the
    dry runs (a backup's end, M9a-5): what is not reached has
    ``reclaim_bytes`` null, ``retention_next`` first, then ``reclaim_by_age``
    oldest first. Logs one line with the time and the dry runs (M9a-6)."""
    start = time.monotonic()
    rows = zfs.read_datasets(pool)
    datasets = [r.to_json() for r in rows] if rows is not None else None
    if datasets is None:
        log.warn(f"Could not list {pool}'s datasets for the status files")
    try:
        dest = survey.read_dest(pool, strict=True)
        complete = drive_marks.drive_complete(pool)
        pins = drive_marks.drive_pins(pool, strict=True)
        used = survey.read_dest_used(pool)
    except survey.ReadError as e:
        log.warn(f"Could not read what {pool} holds for the status files: {e}")
        return DriveContents(datasets=datasets)
    times = point_times(dest)
    plan: dict[str, Any] = {
        "complete": complete.run,
        "policy": policy,
        "now": int(time.time()) if now is None else now,
        "whole": complete.whole,
        "pinned": pins,
        "first": complete.first,
    }
    # The limit covers the dry runs, the costly part (plan, section 8).
    dry = _Pass(pool, time.monotonic() + budget_s if budget_s is not None else None)
    nxt = _retention_next(dry, dest, times, plan)
    by_age = _reclaim_by_age(dry, dest, times, plan, pins)
    late = f"; {dry.unmeasured} figure(s) not measured" if dry.unmeasured else ""
    secs = time.monotonic() - start
    log.info(f"Measured what {pool} holds in {secs:.1f} s ({len(dry.tally)} dry run(s){late})")
    return DriveContents(
        datasets=datasets,
        points=_points(dest, times, complete.run, complete.whole, used),
        non_point_snapshots=_non_points(dest, used),
        retention_next=nxt,
        reclaim_by_age=by_age,
    )
