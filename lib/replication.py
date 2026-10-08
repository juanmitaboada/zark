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
zark's replication planner: what each dataset needs to reach a backup point.

Pure functions over snapshot and bookmark metadata (no shell), so every
invariant of the engine is tested without ZFS.

Model (redesign §4, M2 decisions):
  * A backup point ``zark_<UTC>`` is one ``zfs snapshot`` call per pool
    (a call cannot span pools), taken just before planning.
  * The incremental base is always the destination's **newest** snapshot,
    found in origin by guid as a snapshot (``-I``) or, on rpool only, as a
    bookmark (``-i #bm`` to the first later snapshot, then ``-I``). The
    receive never uses ``-F``: the kernel then refuses any other base.
  * Anything else — an older common snapshot only (ROLLBACK), nothing in
    common (DIVERGED), a dataset that origin no longer has (ORPHAN) — is a
    user decision, never an automatic destroy (invariant I-A).
  * Anchors: rpool datasets get a bookmark per disk
    ``#zark_<pool GUID>_<UTC>``; bpool never gets bookmarks (I-F) and keeps
    one snapshot per disk ``@zark_<pool GUID>_<UTC>`` instead.
  * The structural containers (pool roots, ``rpool/ROOT``, ``rpool/USERDATA``,
    ``bpool/BOOT``) are not replicated: they hold no data, recover creates
    them anew (so after a recover their snapshots never match the backup's),
    and the engine only makes sure they exist on the destination. One that
    could hold data is listed as not backed up.

Dataset names here are relative to the pool root on both sides: origin
``rpool/var`` is ``<backup pool>/rpool/var`` on the destination.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

POINT_PREFIX = "zark_"
_STAMP = r"\d{4}-\d\d-\d\d_\d\d:\d\d:\d\dZ"
POINT_RE = re.compile(rf"^zark_{_STAMP}$")
ANCHOR_RE = re.compile(rf"^zark_(\d+)_({_STAMP})$")
ARCHIVE_MARK = ".archived-"
ORPHAN_PROP = "org.zark:orphan"
KEYSTORE = "rpool/keystore"
# Created, never received, by recover (lib/mount_props.py CREATED_CONTAINERS
# and BPOOL_CONTAINERS, plus the two pool roots).
STRUCTURAL = ("rpool", "rpool/ROOT", "rpool/USERDATA", "bpool", "bpool/BOOT")
# A structural container referencing more than this holds data of its own.
STRUCTURAL_DATA_LIMIT = 8 * 1024 * 1024


def point_name(now: datetime) -> str:
    """``zark_YYYY-MM-DD_HH:MM:SSZ`` for ``now`` (converted to UTC)."""
    return f"{POINT_PREFIX}{now.astimezone(UTC):%Y-%m-%d_%H:%M:%S}Z"


def anchor_name(disk_guid: str, point: str) -> str:
    """Per-disk anchor name (bookmark on rpool, snapshot on bpool) for a point."""
    return f"{POINT_PREFIX}{disk_guid}_{point[len(POINT_PREFIX) :]}"


def anchor_disk(name: str) -> str:
    """Pool GUID an anchor belongs to, or "" if ``name`` is not an anchor."""
    m = ANCHOR_RE.match(name)
    return m.group(1) if m else ""


def is_bpool(rel: str) -> bool:
    """True for the bpool tree."""
    return rel == "bpool" or rel.startswith("bpool/")


def parent(rel: str) -> str:
    """Parent dataset name ("" for a pool root)."""
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def is_archived(rel: str) -> bool:
    """True for an archived lineage or anything below one."""
    return any(ARCHIVE_MARK in part for part in rel.split("/"))


@dataclass(frozen=True)
class Ref:
    """A snapshot (``@name``) or bookmark (``#name``) of one dataset."""

    name: str
    guid: str
    createtxg: int
    creation: int


@dataclass
class Origin:
    """An origin dataset: its kind, mount properties, snapshots and bookmarks."""

    rel: str
    kind: str = "filesystem"  # "filesystem" | "volume"
    canmount: str = ""
    mountpoint: str = ""
    referenced: int = 0
    snaps: list[Ref] = field(default_factory=list)  # by createtxg
    bookmarks: list[Ref] = field(default_factory=list)

    def snap_by_guid(self, guid: str) -> Ref | None:
        """Origin snapshot with that guid."""
        return next((s for s in self.snaps if s.guid == guid), None)

    def bookmark_by_guid(self, guid: str) -> Ref | None:
        """Origin bookmark with that guid (several may share it; any will do)."""
        return next((b for b in self.bookmarks if b.guid == guid), None)


@dataclass
class Dest:
    """A destination dataset: snapshots, partial receive state and markers."""

    rel: str
    kind: str = "filesystem"
    token: str = ""  # receive_resume_token, "" when none
    orphan: str = ""  # value of org.zark:orphan, "" when unset
    used: int = 0
    snaps: list[Ref] = field(default_factory=list)  # by createtxg

    @property
    def newest(self) -> Ref | None:
        """The destination's newest snapshot."""
        return self.snaps[-1] if self.snaps else None


class State(StrEnum):
    """What a dataset needs."""

    AT_POINT = "at point"  # already holds the point
    RESUME = "resume"  # partial receive to finish first
    NEW = "new"  # full send of the point
    INCREMENTAL = "incremental"  # -I from the destination's newest
    VIA_BOOKMARK = "via bookmark"  # -i #bm to F, then -I F→point
    ROLLBACK = "rollback"  # only an older common snapshot left (decision 7)
    DIVERGED = "diverged"  # nothing in common (decision 8)
    ORPHAN = "only on backup"  # destination only (decision 18)
    EXCLUDED = "excluded"  # zvols (decision 10), structural containers with data


# States the engine can transfer without asking anything.
TRANSFERABLE = frozenset(
    {State.AT_POINT, State.RESUME, State.NEW, State.INCREMENTAL, State.VIA_BOOKMARK},
)


@dataclass
class DatasetPlan:  # pylint: disable=too-many-instance-attributes
    """The plan for one dataset."""

    rel: str
    state: State
    base: str = ""  # origin "@snap" or "#bookmark" the transfer starts (ROLLBACK: would start) from
    first: str = ""  # VIA_BOOKMARK: first origin snapshot after the bookmark
    common: str = ""  # ROLLBACK: newest common destination snapshot ("@name")
    newer: list[str] = field(default_factory=list)  # ROLLBACK: dest snapshots it destroys
    rename_to: str = ""  # ORPHAN whose lineage continues under this origin name
    kept: bool = False  # ORPHAN already marked "kept"
    used: int = 0  # destination bytes (ORPHAN, DIVERGED, ROLLBACK)
    note: str = ""


def expected(origin: dict[str, Origin]) -> list[str]:
    """Datasets a backup must bring to the point: rpool tree minus the keystore,
    other zvols and the structural containers, plus the bpool tree minus its
    structural containers, parents first."""
    return sorted(
        rel
        for rel, o in origin.items()
        if rel != KEYSTORE
        and not rel.startswith(f"{KEYSTORE}/")
        and o.kind != "volume"
        and rel not in STRUCTURAL
    )


def excluded(origin: dict[str, Origin]) -> list[tuple[str, str]]:
    """(dataset, why) left out on purpose and worth reporting: volumes other
    than the keystore (which travels apart), and structural containers that
    hold or could hold data of their own."""
    out = [
        (rel, "zvol, not backed up")
        for rel, o in origin.items()
        if o.kind == "volume" and rel != KEYSTORE
    ]
    for rel in STRUCTURAL:
        o = origin.get(rel)
        if o is None:
            continue
        if o.referenced > STRUCTURAL_DATA_LIMIT:
            out.append((rel, "structural container holding data, not backed up"))
        elif o.canmount in ("on", "noauto") and o.mountpoint.startswith("/"):
            out.append((rel, f"structural container mountable at {o.mountpoint}, not backed up"))
    return sorted(out)


def plan_dataset(o: Origin, d: Dest | None, point: str) -> DatasetPlan:  # pylint: disable=too-many-return-statements
    """State of one expected dataset (see the module docstring)."""
    rel = o.rel
    if d is None:
        return DatasetPlan(rel, State.NEW, base="")
    if d.token:
        return DatasetPlan(rel, State.RESUME)
    newest = d.newest
    if newest is None:
        return DatasetPlan(rel, State.DIVERGED, used=d.used, note="no snapshot on backup")
    own = o.snap_by_guid(newest.guid)
    if own is not None:
        if own.name == point:
            return DatasetPlan(rel, State.AT_POINT)
        return DatasetPlan(rel, State.INCREMENTAL, base=f"@{own.name}")
    if not is_bpool(rel):
        bm = o.bookmark_by_guid(newest.guid)
        if bm is not None:
            later = [s for s in o.snaps if s.createtxg > bm.createtxg]
            if later:
                return DatasetPlan(
                    rel, State.VIA_BOOKMARK, base=f"#{bm.name}", first=f"@{later[0].name}"
                )
    # The newest is gone from origin: look for an older common snapshot.
    for i in range(len(d.snaps) - 2, -1, -1):
        s = d.snaps[i]
        own_old = o.snap_by_guid(s.guid)
        bm_old = None if own_old or is_bpool(rel) else o.bookmark_by_guid(s.guid)
        if own_old or bm_old:
            base = f"@{own_old.name}" if own_old else f"#{bm_old.name}" if bm_old else ""
            return DatasetPlan(
                rel,
                State.ROLLBACK,
                base=base,
                common=f"@{s.name}",
                newer=[f"@{x.name}" for x in d.snaps[i + 1 :]],
                used=d.used,
            )
    return DatasetPlan(rel, State.DIVERGED, used=d.used, note="no common snapshot or bookmark")


def _guids(o: Origin) -> set[str]:
    return {s.guid for s in o.snaps} | {b.guid for b in o.bookmarks}


def plan(origin: dict[str, Origin], dest: dict[str, Dest], point: str) -> list[DatasetPlan]:
    """Plan every expected dataset, plus one ORPHAN entry per destination tree
    origin no longer has (its descendants follow it). Archived lineages are
    not planned. Volumes left out are listed as EXCLUDED."""
    live = {rel: d for rel, d in dest.items() if not is_archived(rel)}
    wanted = expected(origin)
    plans = [plan_dataset(origin[rel], live.get(rel), point) for rel in wanted]
    plans += [DatasetPlan(rel, State.EXCLUDED, note=why) for rel, why in excluded(origin)]

    orphans = sorted(rel for rel in live if rel not in origin)
    tops = [rel for rel in orphans if parent(rel) not in orphans]
    new_names = {p.rel for p in plans if p.state is State.NEW}
    renames = _renames(tops, new_names, origin, live)
    for rel in tops:
        d = live[rel]
        plans.append(
            DatasetPlan(
                rel,
                State.ORPHAN,
                rename_to=renames.get(rel, ""),
                kept=d.orphan.startswith("kept@"),
                used=d.used,
            ),
        )
    return plans


def _renames(
    tops: list[str],
    new_names: set[str],
    origin: dict[str, Origin],
    live: dict[str, Dest],
) -> dict[str, str]:
    """Orphan → new origin name, when origin holds the orphan's newest
    snapshot (or, on rpool, a bookmark of it) under another name: a rename in
    origin. Offered only when the new parent already exists on the backup,
    so the rename is a plain ``zfs rename`` there."""
    out: dict[str, str] = {}
    for rel in tops:
        newest = live[rel].newest
        if newest is None:
            continue
        for name in sorted(new_names):
            o = origin[name]
            if is_bpool(name) != is_bpool(rel):
                continue
            hit = o.snap_by_guid(newest.guid) or (
                not is_bpool(name) and o.bookmark_by_guid(newest.guid)
            )
            if hit and parent(name) in live and name not in out.values():
                out[rel] = name
                break
    return out


def reached(plans: list[DatasetPlan]) -> bool:
    """I-D: every expected dataset is at the point."""
    return all(
        p.state is State.AT_POINT for p in plans if p.state not in (State.ORPHAN, State.EXCLUDED)
    )


def old_anchors(names: list[str], disk_guid: str, keep: str) -> list[str]:
    """This disk's anchors other than ``keep`` (another disk's are never touched)."""
    return [n for n in names if anchor_disk(n) == disk_guid and n != keep]


def leftover_points(names: list[str], current: str) -> list[str]:
    """Origin points left by an interrupted run (anchors are not points)."""
    return [n for n in names if POINT_RE.match(n) and n != current]


def archive_name(rel: str, taken: set[str], now: datetime) -> str:
    """``<rel>.archived-YYYYMMDD``, with ``-N`` when that name is taken."""
    base = f"{rel}{ARCHIVE_MARK}{now.astimezone(UTC):%Y%m%d}"
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name
