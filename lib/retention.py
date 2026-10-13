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
Destination retention: which snapshots on a backup drive may go.

Pure functions over the drive's listing (no shell), like
:mod:`lib.replication`. The executor is in :mod:`lib.engine`.

Policy, per drive (M3 decision 1):
  1. inside the window (``window_days``) everything is kept: backup points
     and the sanoid snapshots that travelled with them;
  2. outside it, only the newest complete backup point of each calendar
     month (UTC, by ``creation``), with no limit in time;
  3. always kept: the newest complete backup point, and the newest snapshot
     of every live dataset (I-E: the base of its next incremental);
  4. ``max_age_days``, when set, removes anything older, except rule 3.

What the policy never touches:
  * history from before the drive's first backup point (a drive written by
    zark 1.x; M3 decision 6c), and snapshots whose names neither zark nor
    sanoid wrote;
  * a snapshot that is the origin of a clone or carries a hold (ZFS refuses
    to destroy either: module/zfs/dsl_destroy.c:79-86), given as ``pinned``;
  * anything named in ``protected`` (the bases a running backup will use).

A backup point is decided once, for the whole drive, from its newest
snapshot's ``creation``, so it goes whole or stays whole; only I-E can keep
one dataset's snapshot of a point that goes, and the point then reads as
incomplete (its mark records how many datasets it had, M2V-4). A point is
judged by its run's mark, and a month prefers a point no dataset skipped
since (M3F-5 5b): see :func:`lib.restore_points.retention_points`.

Archived lineages and kept orphans are not live (M3 decision 7a): no I-E,
and a subtree left with nothing is destroyed whole.

When the drive is short of space (M3 decision 4), :func:`space_candidates`
lists the monthly points outside the window, oldest first, for a plan the
user confirms.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lib.replication import POINT_RE, Dest, Ref, anchor_disk, is_archived

DAY = 86400
AUTOSNAP_RE = re.compile(r"^autosnap_")


@dataclass(frozen=True)
class Policy:
    """A drive's retention settings (M3 decision 1)."""

    window_days: int = 30
    max_age_days: int = 0  # 0: off
    margin_pct: int = 10  # free space a backup tries to leave, % of the drive


@dataclass(frozen=True)
class Removal:
    """One snapshot that may go, and why."""

    rel: str  # dataset that stores it, relative to the pool (archived name included)
    name: str
    reason: str

    @property
    def target(self) -> str:
        """``rel@name``."""
        return f"{self.rel}@{self.name}"


@dataclass
class Prune:
    """What the policy removes from a drive."""

    snapshots: list[Removal] = field(default_factory=list)
    datasets: list[tuple[str, str]] = field(default_factory=list)  # (top, why), destroyed whole
    kept: list[tuple[str, str]] = field(default_factory=list)  # (rel@name, why) worth reporting

    @property
    def empty(self) -> bool:
        """True when nothing goes."""
        return not self.snapshots and not self.datasets


def is_live(rel: str, d: Dest) -> bool:
    """A dataset I-E protects: neither an archived lineage nor a kept orphan."""
    return not is_archived(rel) and not d.orphan.startswith("kept@")


def _kind(name: str) -> str:
    """Kind of snapshot: a backup point, an intermediate (sanoid's, or another
    drive's bpool anchor), or another name."""
    if POINT_RE.match(name):
        return "point"
    if AUTOSNAP_RE.match(name) or anchor_disk(name):
        return "intermediate"
    return "other"


def _month(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def point_times(dest: dict[str, Dest]) -> dict[str, int]:
    """Backup point name → its newest snapshot's ``creation`` on the drive."""
    out: dict[str, int] = {}
    for d in dest.values():
        for s in d.snaps:
            if POINT_RE.match(s.name):
                out[s.name] = max(out.get(s.name, 0), s.creation)
    return out


def monthly_points(
    times: dict[str, int], complete: set[str], whole: set[str] | None = None
) -> set[str]:
    """The newest complete backup point of each calendar month (UTC): the
    newest whole one when the month has any, else its newest run-complete
    one (M3F-5 5b), so a rollback or a hand destroy that leaves a point
    partial never empties a month."""
    whole = complete if whole is None else whole
    best: dict[str, tuple[bool, int, str]] = {}
    for name, ts in times.items():
        if name in complete:
            month = _month(ts)
            best[month] = max(best.get(month, (False, 0, "")), (name in whole, ts, name))
    return {name for _w, _ts, name in best.values()}


def newest_complete(
    times: dict[str, int], complete: set[str], whole: set[str] | None = None
) -> str:
    """The newest complete backup point (recover's default: the newest whole
    one, else the newest run-complete one), "" when there is none."""
    whole = complete if whole is None else whole
    done = [(name in whole, ts, name) for name, ts in times.items() if name in complete]
    return max(done)[2] if done else ""


def first_point_time(times: dict[str, int]) -> int | None:
    """``creation`` of the drive's oldest backup point; None on a drive without one."""
    return min(times.values()) if times else None


def plan_prune(  # pylint: disable=too-many-arguments,too-many-locals
    dest: dict[str, Dest],
    complete: set[str],
    policy: Policy,
    now: int,
    *,
    whole: set[str] | None = None,
    pinned: dict[str, str] | None = None,
    protected: set[str] | None = None,
) -> Prune:
    """What the policy removes (see the module docstring).

    ``dest`` is the drive's listing by dataset relative to the pool;
    ``complete`` the run-complete backup points and ``whole`` those of them
    no dataset skipped (lib.restore_points; None: all of them);
    ``pinned`` maps ``rel@name`` to why ZFS would refuse to destroy it;
    ``protected`` names ``rel@name`` that must stay for this run.
    """
    pinned = pinned or {}
    protected = protected or set()
    times = point_times(dest)
    start = first_point_time(times)
    out = Prune()
    if start is None:
        return out  # nothing written by zark 2 yet: 1.x history only
    monthly = monthly_points(times, complete, whole)
    keep_point = newest_complete(times, complete, whole)
    window = now - policy.window_days * DAY
    too_old = now - policy.max_age_days * DAY if policy.max_age_days else None

    for rel, d in sorted(dest.items()):
        newest = d.newest if is_live(rel, d) else None
        for s in d.snaps:
            target = f"{rel}@{s.name}"
            kind = _kind(s.name)
            when = times.get(s.name, s.creation) if kind == "point" else s.creation
            why = _keep_reason(s, kind, when, newest, keep_point, start, target, protected)
            if why:
                continue
            if target in pinned:
                if when < window:
                    out.kept.append((target, pinned[target]))
                continue
            if too_old is not None and when < too_old:
                out.snapshots.append(Removal(rel, s.name, f"older than {policy.max_age_days} days"))
                continue
            if when >= window:
                continue
            reason = _outside_window(s.name, kind, monthly, complete)
            if reason:
                out.snapshots.append(Removal(rel, s.name, reason))
    out.datasets = whole_datasets(dest, out.snapshots)
    return out


def _keep_reason(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    s: Ref,
    kind: str,
    when: int,
    newest: Ref | None,
    keep_point: str,
    start: int,
    target: str,
    protected: set[str],
) -> str:
    """Why a snapshot is never removed by the policy, "" when it may be."""
    if kind == "other":
        return "not written by zark or sanoid"
    if when < start:
        return "from before the drive's first backup point (1.x history)"
    if newest is not None and s.guid == newest.guid:
        return "newest snapshot of a live dataset (I-E)"
    if s.name == keep_point:
        return "newest complete backup point"
    if target in protected:
        return "base of this run"
    return ""


def _outside_window(name: str, kind: str, monthly: set[str], complete: set[str]) -> str:
    """Why a snapshot older than the window goes, "" when it stays."""
    if kind == "intermediate":
        return "intermediate older than the window"
    if name in monthly:
        return ""
    if name not in complete:
        return "incomplete backup point older than the window"
    return "older than the window, not its month's newest complete point"


def whole_datasets(dest: dict[str, Dest], removals: list[Removal]) -> list[tuple[str, str]]:
    """Archived lineages and kept orphans that ``removals`` leave with no
    snapshot, by subtree top: destroyed whole (M3 decision 7a)."""
    going: dict[str, int] = {}
    for r in removals:
        going[r.rel] = going.get(r.rel, 0) + 1
    empty = {
        rel
        for rel, d in dest.items()
        if not is_live(rel, d) and len(d.snaps) == going.get(rel, 0) and d.snaps
    }
    out: list[tuple[str, str]] = []
    for top in sorted(empty):
        if any(top.startswith(f"{t}/") for t, _ in out):
            continue
        below = [rel for rel in dest if rel == top or rel.startswith(f"{top}/")]
        if all(rel in empty or not dest[rel].snaps for rel in below):
            what = "archived lineage" if is_archived(top) else "kept orphan"
            out.append((top, f"{what} with nothing left that the policy keeps"))
    return out


def space_candidates(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    dest: dict[str, Dest],
    complete: set[str],
    policy: Policy,
    now: int,
    prune: Prune,
    whole: set[str] | None = None,
) -> list[str]:
    """Backup points a space plan may remove beyond the policy, oldest first:
    the monthly points outside the window, never the newest complete one.
    History from before the first backup point is never offered here."""
    times = point_times(dest)
    window = now - policy.window_days * DAY
    keep_point = newest_complete(times, complete, whole)
    gone = {r.name for r in prune.snapshots}
    names = [
        name
        for name in monthly_points(times, complete, whole)
        if times[name] < window and name != keep_point and name not in gone
    ]
    return sorted(names, key=lambda n: times[n])


def point_removals(
    dest: dict[str, Dest],
    name: str,
    *,
    pinned: dict[str, str] | None = None,
    protected: set[str] | None = None,
) -> list[Removal]:
    """Every snapshot of one backup point that may go, keeping I-E, pinned
    and protected ones (space plan)."""
    pinned = pinned or {}
    protected = protected or set()
    out: list[Removal] = []
    for rel, d in sorted(dest.items()):
        newest = d.newest if is_live(rel, d) else None
        for s in d.snaps:
            target = f"{rel}@{s.name}"
            if s.name != name or target in pinned or target in protected:
                continue
            if newest is not None and s.guid == newest.guid:
                continue
            out.append(Removal(rel, s.name, "oldest monthly point, to free space"))
    return out
