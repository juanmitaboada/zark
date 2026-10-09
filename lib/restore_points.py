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
snapshot in the current point, or — except for syncoid, which snapshots
one dataset at a time — when ``REPEAT_GAP`` seconds pass with no snapshot
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

# Two snapshots of one dataset further apart than this belong to different
# runs (sanoid takes all of a dataset's buckets within a few seconds).
REPEAT_GAP = 300


_ARCHIVED = re.compile(r"\.archived-[^/]*")


def logical(rel: str) -> str:
    """Dataset name with any ``.archived-…`` suffix removed from its components."""
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


def parse_snapshots(lines: list[str], pool: str) -> list[Snap]:
    """Parse ``zfs list -Hp -t snapshot -o name,guid,createtxg,creation`` lines."""
    snaps: list[Snap] = []
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
                Snap(logical(rel), name, fields[1], int(fields[2]), int(fields[3]), source),
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


def resolve(point: Point, snaps: list[Snap], datasets: list[str]) -> dict[str, Snap | None]:
    """Snapshot of each dataset for ``point``; None when it has none at or before it."""
    end = point.end
    # Every backup point at or before this one, as (time, datasets it holds).
    backups = [
        (bp.end, set(bp.members))
        for bp in _by_name(s for s in snaps if POINT_RE.match(s.name))
        if bp.end <= end
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
