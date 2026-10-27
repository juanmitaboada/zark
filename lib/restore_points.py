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
Restore points of a backup pool, and per-dataset resolution of a point.

Pure functions over snapshot metadata (no shell), so they are tested
against the frozen Phase 0 stick manifest.

Ordering rules (P0-8):
  * across datasets, time comes from ``creation`` (preserved by receive);
    ``createtxg`` on a destination is the *receive* order and is only used
    to break ties within one dataset;
  * names are never used for ordering: sanoid names mix local time and UTC.

A restore point is one snapshot run: snapshots of one name family
(``autosnap_``, ``syncoid_<host>_``, ``prepare_``, or a manual name)
grouped by ``creation``; a zark backup point is one ``zfs snapshot`` call
with a unique name, so its snapshots are grouped by that name instead (two
backups a minute apart are two points). A run spans several seconds for sanoid and
many minutes for a syncoid replication. A new point starts when a
dataset reappears more than ``REPEAT_GAP`` seconds after its first
snapshot in the current point, or - except for syncoid, which snapshots
one dataset at a time - when ``REPEAT_GAP`` seconds pass with no snapshot
(so a dataset created between two sanoid runs is not filed into the
earlier one).
Only runs that include the boot environment root are offered.

Resolution (hallazgo 3): for the chosen point every dataset uses its own
snapshot from that run; a dataset absent from the run uses its newest
snapshot whose ``creation`` is not after the end of the run. Never a later
one: a dataset with nothing at or before the point is reported, not
silently restored from the future. A zark backup point is taken of every
replicated dataset at once, so a dataset without the point's snapshot
either no longer existed (its lineage ends before the point: it is not
restored, so a dataset destroyed in origin is not brought back) or was
missed by a run that did not reach it (it has later snapshots: it is
resolved like any other absent dataset, from its newest earlier snapshot).
The same evidence applies to any chosen point, sanoid's included: a dataset
missing from a backup point taken at or before it, with nothing after that
backup point, was destroyed in origin and is not restored (sanoid's own runs
prove nothing, ``rpool/ROOT`` children are outside its recursion).

Archived lineages (``<dataset>.archived-YYYYMMDD[-N]``, left by a backup that
had to resend a dataset in full) are read as earlier snapshots of the same
dataset, so points older than the archive stay restorable; ``Snap.source``
names where such a snapshot is actually stored.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lib.replication import POINT_RE

# Set on every snapshot of a backup point whose run brought every expected
# dataset to it; the value is how many datasets that run brought there, so a
# point that later loses part of its snapshots (a prune, a rollback, a
# destroy) is no longer complete. Read with `-s local` only: user properties
# are inherited, so a value on a filesystem would show on all its snapshots.
# Drives written by 2.0.0-rc2 carry "yes", without the count.
COMPLETE_PROP = "org.zark:point-complete"
LEGACY_MARK = "yes"

# Two snapshots of one dataset further apart than this belong to different
# runs (sanoid takes all of a dataset's buckets within a few seconds).
REPEAT_GAP = 300


_ARCHIVED = re.compile(r"\.archived-[^/]*")


def logical(rel: str) -> str:
    """Dataset name with any ``.archived-...`` suffix removed from its components."""
    return _ARCHIVED.sub("", rel)


@dataclass(frozen=True)
class Snap:  # pylint: disable=too-many-instance-attributes
    """One snapshot, with ``dataset`` relative to the backup pool."""

    dataset: str
    name: str
    guid: str
    createtxg: int
    creation: int
    source: str = ""  # stored under this archived lineage, when not "dataset"
    mark: str = ""  # its own COMPLETE_PROP value, "" when not set on it

    @property
    def stored(self) -> str:
        """Dataset that actually holds the snapshot on the backup."""
        return self.source or self.dataset

    @property
    def family(self) -> str:
        """Name family: the name up to its first digit (``autosnap_``...)."""
        return re.sub(r"\d.*$", "", self.name) or self.name


@dataclass
class Point:
    """A restore point: one snapshot run of one family."""

    family: str
    members: dict[str, list[Snap]] = field(default_factory=dict)
    label: int = 0  # creation of the boot environment root's snapshot

    @property
    def end(self) -> int:
        """Latest creation in the run."""
        return max(s.creation for snaps in self.members.values() for s in snaps)

    def label_utc(self) -> str:
        """Point time as ``YYYY-MM-DD HH:MM:SS UTC``."""
        return datetime.fromtimestamp(self.label, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_snapshots(
    lines: list[str],
    pool: str,
    marks: dict[str, str] | None = None,
) -> list[Snap]:
    """Parse ``zfs list -Hp -t snapshot -o name,guid,createtxg,creation`` lines;
    ``marks`` maps a full snapshot name to its own (local) COMPLETE_PROP."""
    snaps: list[Snap] = []
    marks = marks or {}
    prefix = f"{pool}/"
    for line in lines:
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 4 or "@" not in fields[0]:
            continue
        full, name = fields[0].split("@", 1)
        if not full.startswith(prefix):
            continue
        rel = full[len(prefix) :]
        source = rel if logical(rel) != rel else ""
        try:
            snaps.append(
                Snap(
                    logical(rel),
                    name,
                    fields[1],
                    int(fields[2]),
                    int(fields[3]),
                    source,
                    marks.get(fields[0], ""),
                ),
            )
        except ValueError:
            continue
    return snaps


def cluster(snaps: list[Snap]) -> list[Point]:
    """Group snapshots into runs, per family (see module docstring)."""
    points: list[Point] = []
    by_family: dict[str, list[Snap]] = {}
    for s in snaps:
        by_family.setdefault(s.family, []).append(s)
    for family, members in sorted(by_family.items()):
        points += _by_name(m for m in members if POINT_RE.match(m.name))
        members = [m for m in members if not POINT_RE.match(m.name)]
        # syncoid snapshots one dataset at a time, just before sending it, so
        # its run can have long gaps between datasets; every other family
        # (sanoid, zark's atomic points, manual -r snapshots) takes a whole
        # run within seconds, so a long gap starts a new run there. A syncoid
        # run starts with the pool's root dataset, which repeats from the
        # previous run, so a dataset new in that run cannot slip into the
        # previous point.
        sequential = family.startswith("syncoid_")
        current: Point | None = None
        first_seen: dict[str, int] = {}
        last = 0
        for s in sorted(members, key=lambda x: (x.creation, x.dataset, x.createtxg)):
            repeated = s.dataset in first_seen and s.creation - first_seen[s.dataset] > REPEAT_GAP
            gap = not sequential and s.creation - last > REPEAT_GAP
            if current is None or repeated or gap:
                current = Point(family=family)
                points.append(current)
                first_seen = {}
            first_seen.setdefault(s.dataset, s.creation)
            current.members.setdefault(s.dataset, []).append(s)
            last = s.creation
    return points


def _by_name(snaps: Iterable[Snap]) -> list[Point]:
    """One point per backup point name (atomic, so time is never needed)."""
    points: dict[str, Point] = {}
    for s in snaps:
        point = points.setdefault(s.name, Point(family=s.family))
        point.members.setdefault(s.dataset, []).append(s)
    return list(points.values())


def restore_points(snaps: list[Snap], be_root: str) -> list[Point]:
    """Runs that include ``be_root``, oldest first, labelled by its snapshot."""
    offered: list[Point] = []
    for p in cluster(snaps):
        if be_root in p.members:
            p.label = max(s.creation for s in p.members[be_root])
            offered.append(p)
    return sorted(offered, key=lambda p: (p.label, p.family))


def _newest(snaps: list[Snap]) -> Snap:
    return max(snaps, key=lambda s: (s.creation, s.createtxg))


def point_name(point: Point) -> str:
    """Snapshot name of a backup point (every member shares it)."""
    return next(iter(point.members.values()))[0].name


def resolve(
    point: Point,
    snaps: list[Snap],
    datasets: list[str],
    complete: set[str] | None = None,
) -> dict[str, Snap | None]:
    """Snapshot of each dataset for ``point``; None when it has none at or before it.

    ``complete`` names the backup points whose run reached every dataset;
    only those are evidence that a lineage ended (an incomplete one lacks
    whatever its run missed). None counts every backup point.
    """
    end = point.end
    # Every complete backup point at or before this one, as (time, datasets it holds).
    backups = [
        (bp.end, set(bp.members))
        for bp in _by_name(s for s in snaps if POINT_RE.match(s.name))
        if bp.end <= end and (complete is None or point_name(bp) in complete)
    ]
    out: dict[str, Snap | None] = {}
    for ds in datasets:
        own = point.members.get(ds)
        if own:
            out[ds] = _newest(own)
            continue
        mine = [s for s in snaps if s.dataset == ds]
        if ended(mine, backups):
            out[ds] = None  # its lineage ends before the point
            continue
        earlier = [s for s in mine if s.creation <= end]
        out[ds] = _newest(earlier) if earlier else None
    return out


def ended(mine: list[Snap], backups: list[tuple[int, set[str]]]) -> bool:
    """True when a backup point lacks the dataset and nothing of it follows."""
    if not mine:
        return False
    ds = mine[0].dataset
    last = max(s.creation for s in mine)
    return any(t >= last and ds not in held for t, held in backups)


def legacy_complete(snaps: list[Snap], last_point: str) -> set[str]:
    """Backup points of a drive written before completion was recorded on it.

    Only a complete run wrote ``org.zark:last-point``, so a backup point
    newer than it is incomplete. One at or before it counts as complete
    unless a dataset continues across it without it (a snapshot of that
    dataset before and after the point, none of the point): the run that
    took the point did not reach that dataset. Zark point names are UTC
    and fixed-width, so they compare in time order.
    """
    if not last_point:
        return set()
    points = {s.name for s in snaps if POINT_RE.match(s.name) and s.name <= last_point}
    return points - skipped_points(snaps)


def skipped_points(snaps: list[Snap]) -> set[str]:
    """Backup points some dataset skipped: that dataset has a snapshot on the
    drive before and after the point's time but none of the point, so the
    run that took it did not reach the dataset, or the dataset's snapshot of
    it was removed later (a rollback, a hand destroy) while the dataset
    stayed. A dataset destroyed whole has no snapshot at all and skips
    nothing. Archived lineages count as their dataset."""
    points: dict[str, list[Snap]] = {}
    for s in snaps:
        if POINT_RE.match(s.name):
            points.setdefault(s.name, []).append(s)
    by_ds: dict[str, list[int]] = {}
    for s in snaps:
        by_ds.setdefault(s.dataset, []).append(s.creation)
    out: set[str] = set()
    for name, members in points.items():
        held = {m.dataset for m in members}
        when = max(m.creation for m in members)
        if any(ds not in held and min(times) < when < max(times) for ds, times in by_ds.items()):
            out.add(name)
    return out


def point_marks(snaps: list[Snap]) -> dict[str, list[Snap]]:
    """Backup point name → its snapshots, for the points with any mark."""
    marked = {s.name for s in snaps if s.mark and POINT_RE.match(s.name)}
    out: dict[str, list[Snap]] = {}
    for s in snaps:
        if s.name in marked:
            out.setdefault(s.name, []).append(s)
    return out


def is_complete(members: list[Snap]) -> bool:
    """True when every snapshot of a point carries its mark and as many
    datasets as the mark records still hold the point (archived lineages
    count as their dataset). An rc2 mark (``yes``) carries no count."""
    marks = {s.mark for s in members}
    if len(marks) != 1:
        return False
    mark = marks.pop()
    if mark == LEGACY_MARK:
        return True
    return mark.isdigit() and int(mark) == len({s.dataset for s in members})


@dataclass(frozen=True)
class PointLoss:
    """One backup point once a destroy took part of its snapshots."""

    targets: list[str]  # ``<pool>/<stored>@<name>`` of the snapshots left
    lost: frozenset[str]  # datasets (archived lineages as theirs) that no longer hold it
    count: int  # its completion count, lowered by them


def point_losses(pool: str, snaps: list[Snap], tops: list[str]) -> dict[str, PointLoss]:
    """For every backup point the destroy of the trees ``tops`` (paths as
    stored on the drive) takes snapshots from: the snapshots it keeps, the
    datasets it loses and its count lowered by them, so that
    :func:`is_complete` still finds it whole (M5bV-4, M5b-10b). A point
    marked before the count (``yes``), one left with no snapshot or a count
    of 0 is not in the result."""

    def gone(s: Snap) -> bool:
        return any(s.stored == t or s.stored.startswith(f"{t}/") for t in tops)

    out: dict[str, PointLoss] = {}
    for name in sorted({s.name for s in snaps if gone(s) and s.mark.isdigit()}):
        members = [s for s in snaps if s.name == name]
        left = [s for s in members if not gone(s)]
        lost = frozenset({s.dataset for s in members} - {s.dataset for s in left})
        count = int(next(s.mark for s in members if s.mark.isdigit())) - len(lost)
        if not lost or not left or count <= 0:
            continue
        out[name] = PointLoss([f"{pool}/{s.stored}@{name}" for s in left], lost, count)
    return out


def run_complete_points(snaps: list[Snap], last_point: str) -> set[str]:
    """Backup points whose run reached every dataset: any snapshot of the
    point carries a mark, or, on a drive with no mark at all,
    :func:`legacy_complete`. Unlike :func:`complete_points`, a point stays
    in this set after part of its snapshots went, so retention keeps
    judging it by its run: a dataset destroyed on the drive later (a
    confirmed DESTROY) must not turn the older points into incomplete
    ones the policy would then remove."""
    marked = point_marks(snaps)
    return set(marked) if marked else legacy_complete(snaps, last_point)


@dataclass(frozen=True)
class Complete:
    """What retention knows about a drive's backup points (M3F-5 5b):
    ``run`` are the points whose run was complete, ``whole`` those of them
    no dataset skipped since (:func:`skipped_points`). The policy prefers a
    whole point as a month's point, and falls back to a run-complete one."""

    run: set[str]
    whole: set[str]
    first: str = ""  # org.zark:first-point: where 1.x history ends (M3P.25)


def retention_points(snaps: list[Snap], last_point: str) -> Complete:
    """:class:`Complete` for retention: the run-complete points, and those
    of them still whole."""
    run = run_complete_points(snaps, last_point)
    return Complete(run, run - skipped_points(snaps))


def complete_points(snaps: list[Snap], last_point: str) -> set[str]:
    """Names of the complete backup points on a drive: by their marks, or,
    on a drive with no mark at all, :func:`legacy_complete`."""
    marked = point_marks(snaps)
    if not marked:
        return legacy_complete(snaps, last_point)
    return {name for name, members in marked.items() if is_complete(members)}
