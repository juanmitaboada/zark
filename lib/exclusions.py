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
Datasets left out of backups or of local snapshots (D7 to D12).

An exclusion is declared on an origin dataset as a ZFS user property set
there (M5b-3 3a): ``org.zark:replicate=no`` leaves the dataset out of every
backup, ``org.zark:snapshot=no`` out of sanoid's snapshots (D8). Only values
set on the dataset itself count (``zfs get -s local``), and an exclusion
covers the dataset and everything below it (M5b-4 4a): ZFS inheritance does
not decide, so a child cannot be included alone.

Never excluded (D10, M5b-5): the boot environments (``rpool/ROOT`` and
below), the boot pool, the keystore, the structural containers, and a
dataset whose subtree holds any of them; zvols and ``.archived-`` names are
left out already. A value found on such a dataset (set by hand) is ignored
and reported at every run.

A user property changes no mount, so it can be set with the keystore zvol
present (the chase.c:648 rule is about mount properties).
"""

import hashlib
import json
import shlex
from dataclasses import dataclass, field

from lib import sh
from lib.backup_layout import zark_props
from lib.config import zark_command
from lib.drive_marks import drive_snapshots, snapshot_marks
from lib.log import Log
from lib.replication import (
    EXCLUDED_KEPT_PROP,
    KEYSTORE,
    STRUCTURAL,
    Origin,
    anchor_disk,
    is_archived,
    is_bpool,
)
from lib.restore_points import COMPLETE_PROP, Snap, complete_points
from lib.survey import ReadError, checked, read_origin

REPLICATE = "org.zark:replicate"
SNAPSHOT = "org.zark:snapshot"
# On every snapshot of a backup point on a drive: what that point left out
# (M5b-7 7a), so recover recreates it and never restores an older copy.
POINT_MARK = "org.zark:excluded"
# On a drive's copy of an excluded dataset the user chose to keep (M5b-10).
KEPT = EXCLUDED_KEPT_PROP
# A user property value holds at most 8191 bytes (OpenZFS 2.4.1
# include/sys/fs/zfs.h:86, module/zfs/dsl_prop.c:963-970).
MARK_LIMIT = 8191
# A record longer than one value is written in parts (M5bR-1, M5bF-6): the
# mark holds a pointer, the parts are POINT_MARK.1 to .n. 64 parts in one
# zfs set fit well within execve's argument limit (M5bF-7).
MAX_PARTS = 64

# What `zark exclude` calls each flag.
BACKUPS, SNAPSHOTS, BOTH = "backups", "snapshots", "both"


def _q(word: str) -> str:
    return shlex.quote(word)


def under(rel: str, top: str) -> bool:
    """True for ``top`` and everything below it."""
    return rel == top or rel.startswith(f"{top}/")


def covering(rel: str, tops: frozenset[str] | set[str]) -> str:
    """The exclusion that covers ``rel`` (its nearest top), or ""."""
    best = ""
    for top in tops:
        if under(rel, top) and len(top) > len(best):
            best = top
    return best


def never_excluded(rel: str, origin: dict[str, Origin] | None = None) -> str:
    """Why ``rel`` can never be excluded, "" when it can (D10, M5b-5 5a)."""
    o = (origin or {}).get(rel)
    rules = (
        (
            rel == "rpool/ROOT" or rel.startswith("rpool/ROOT/"),
            "a boot environment is never excluded",
        ),
        (is_bpool(rel), "the boot pool is never excluded"),
        (under(rel, KEYSTORE), "the keystore is never excluded"),
        (
            rel in STRUCTURAL,
            "a structural container is not replicated itself: exclude its children instead",
        ),
        (o is not None and o.kind == "volume", "a zvol is never backed up already"),
        (is_archived(rel), "zark leaves .archived- names out already"),
    )
    why = next((text for hit, text in rules if hit), "")
    if why:
        return why
    held = next(
        (x for x in sorted(origin or {}) if x != rel and under(x, rel) and never_excluded(x)), ""
    )
    return f"its subtree holds {held}" if held else ""


@dataclass(frozen=True)
class Exclusion:
    """One declared exclusion: a dataset and its subtree."""

    top: str
    replicate: bool  # left out of backups
    snapshot: bool  # left out of sanoid's snapshots

    @property
    def flags(self) -> str:
        """``backups``, ``snapshots`` or ``both``."""
        if self.replicate and self.snapshot:
            return BOTH
        return BACKUPS if self.replicate else SNAPSHOTS


def _minimal(rels: set[str]) -> frozenset[str]:
    """The datasets of ``rels`` that no other one of them covers."""
    return frozenset(r for r in rels if not covering(r, rels - {r}))


@dataclass
class Declared:
    """The exclusions read from origin, and the values zark ignores.

    ``tops`` holds every dataset an exclusion was set on (the user's own
    words, for ``zark exclude list``); each flag covers its subtree, so a
    dataset set inside one already covered for the same flag changes nothing.
    """

    tops: dict[str, Exclusion] = field(default_factory=dict)
    ignored: list[tuple[str, str]] = field(default_factory=list)  # (dataset, why)

    @property
    def no_backup(self) -> frozenset[str]:
        """The subtree tops left out of backups."""
        return _minimal({t for t, e in self.tops.items() if e.replicate})

    @property
    def no_snapshot(self) -> frozenset[str]:
        """The subtree tops left out of sanoid's snapshots."""
        return _minimal({t for t, e in self.tops.items() if e.snapshot})

    def flags_of(self, top: str) -> str:
        """``backups``, ``snapshots`` or ``both`` for a top of :attr:`no_backup`."""
        return BOTH if covering(top, set(self.no_snapshot)) else BACKUPS


def _values(lines: list[str]) -> dict[str, dict[str, str]]:
    """``name property value`` lines → {dataset: {property: value}}."""
    values: dict[str, dict[str, str]] = {}
    for line in lines:
        f = line.split("\t")
        if len(f) == 3 and f[1] in (REPLICATE, SNAPSHOT) and "@" not in f[0] and "#" not in f[0]:
            values.setdefault(f[0], {})[f[1]] = f[2]
    return values


def parse(lines: list[str], origin: dict[str, Origin]) -> Declared:
    """``name property value`` lines of ``zfs get -H -s local`` into the
    declared exclusions, origin's datasets naming what is checked."""
    values = _values(lines)
    out = Declared()
    for rel, props in sorted(values.items()):
        bad = [f"{p}={v}" for p, v in sorted(props.items()) if v not in ("no", "yes")]
        if bad:
            out.ignored.append((rel, f"{', '.join(bad)}: only 'no' excludes"))
        rep, snap = props.get(REPLICATE) == "no", props.get(SNAPSHOT) == "no"
        if not (rep or snap):
            continue
        why = never_excluded(rel, origin)
        if why:
            out.ignored.append((rel, why))
            continue
        out.tops[rel] = Exclusion(rel, rep, snap)
    flag_sets = {REPLICATE: set(out.no_backup), SNAPSHOT: set(out.no_snapshot)}
    for rel, props in sorted(values.items()):
        for prop, value in sorted(props.items()):
            top = covering(rel, flag_sets[prop] - {rel})
            if value == "yes" and top:
                out.ignored.append(
                    (rel, f"{prop}=yes inside {top}, which excludes its whole subtree")
                )
    return out


def read(pools: list[str], origin: dict[str, Origin], strict: bool = False) -> Declared:
    """The exclusions declared in ``pools`` (one ``zfs get`` per pool). ``-t
    filesystem,volume``: without it ``zfs get -r`` walks every snapshot and
    bookmark of the pool too (OpenZFS cmd/zfs/zfs_iter.c:97-98, :150-160)."""
    lines: list[str] = []
    for pool in pools:
        r = checked(
            sh.run(
                "zfs get -H -s local -t filesystem,volume -o name,property,value "
                + f"{REPLICATE},{SNAPSHOT} -r {_q(pool)}"
            ),
            strict,
        )
        lines += r.lines if r.ok else []
    return parse(lines, origin)


def subtree(top: str, origin: dict[str, Origin]) -> list[str]:
    """``top`` and every origin dataset below it, parents first."""
    return sorted(rel for rel in origin if under(rel, top))


def subtree_referenced(top: str, origin: dict[str, Origin]) -> int:
    """Bytes a first full send of the subtree would carry (D12, M5b-12 12a)."""
    return sum(origin[rel].referenced for rel in subtree(top, origin))


def point_mark(declared: Declared, origin: dict[str, Origin]) -> str:
    """The value recorded on a point's snapshots (M5b-7 7a, M5b-8): every
    top with its flags, and every dataset of a subtree left out of the
    backup with origin's canmount and mountpoint at the point. A subtree
    excluded from snapshots only is listed too (``snapshots``): a raw send
    carries no user property, so recover sets it again on what it restores.
    ``{}``-shaped when nothing is excluded, so a drive written before rc10
    reads differently."""
    tops = {t: declared.flags_of(t) for t in sorted(declared.no_backup)}
    for top in sorted(declared.no_snapshot):
        _ = tops.setdefault(top, SNAPSHOTS)
    datasets = {
        rel: (origin[rel].canmount, origin[rel].mountpoint)
        for top in sorted(declared.no_backup)
        for rel in subtree(top, origin)
    }
    return _dump(tops, datasets)


def _dump(tops: dict[str, str], datasets: dict[str, tuple[str, str]]) -> str:
    data = {"tops": tops, "datasets": {rel: list(v) for rel, v in datasets.items()}}
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def part_prop(i: int) -> str:
    """The user property holding part ``i`` (from 1) of a record in parts."""
    return f"{POINT_MARK}.{i}"


def parts_needed(mark: str) -> int:
    """How many values ``mark`` takes: 1 when it fits in one."""
    return max(1, -(-len(mark) // MARK_LIMIT))


def record_props(mark: str) -> list[tuple[str, str]]:
    """The user properties that hold ``mark`` on a point's snapshots: the
    mark itself when it fits in one value, else a pointer naming its parts
    and the digest of the whole record, then the parts (M5bR-1, M5bF-6).
    The JSON is ASCII (json.dumps escapes the rest), so a cut never splits
    a character."""
    if len(mark) <= MARK_LIMIT:
        return [(POINT_MARK, mark)]
    parts = [mark[i : i + MARK_LIMIT] for i in range(0, len(mark), MARK_LIMIT)]
    digest = hashlib.sha256(mark.encode()).hexdigest()
    pointer = json.dumps(
        {"parts": len(parts), "sha256": digest}, sort_keys=True, separators=(",", ":")
    )
    return [(POINT_MARK, pointer), *((part_prop(i), p) for i, p in enumerate(parts, 1))]


def _pointer(value: str) -> tuple[int, str] | None:
    """(parts, digest) when ``value`` points to a record in parts, (0, "")
    for a pointer that cannot be followed, None for a record of its own."""
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "parts" not in data:
        return None
    n, digest = data.get("parts"), data.get("sha256")
    # bool is an int: a pointer of "true" parts is not one to follow.
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_PARTS:
        return 0, ""
    if not isinstance(digest, str):
        return 0, ""
    return n, digest


def _read_parts(root: str, most: int, strict: bool) -> dict[str, dict[str, str]]:
    """Parts 1 to ``most`` as set on each snapshot under ``root``:
    snapshot -> {property: value}, in one ``zfs get`` of the list."""
    parts: dict[str, dict[str, str]] = {}
    if not most:
        return parts
    names = ",".join(part_prop(i) for i in range(1, most + 1))
    r = checked(
        sh.run(f"zfs get -H -p -r -s local -t snapshot -o name,property,value {names} {_q(root)}"),
        strict,
    )
    for line in r.lines if r.ok else []:
        f = line.split("\t")
        if len(f) == 3 and "@" in f[0]:
            parts.setdefault(f[0], {})[f[1]] = f[2]
    return parts


def point_records(pool: str, strict: bool = False) -> dict[str, str | None]:
    """Every point record on the drive's rpool snapshots, as set on each
    snapshot: a record in parts joined, None when one of its parts is
    missing or the joined record does not match the pointer's digest (a
    failed set can leave some parts written, OpenZFS
    module/zfs/zfs_ioctl.c:2875-2908). Parts a rewrite left beyond the
    pointer's count are never read (M5bF-8). ``strict`` raises
    :class:`ReadError` when a listing fails."""
    root = f"{pool}/rpool"
    marks = snapshot_marks(root, strict, prop=POINT_MARK)
    pointers = {snap: p for snap, v in marks.items() if (p := _pointer(v)) is not None}
    out: dict[str, str | None] = dict(marks)
    if not pointers:
        return out
    parts = _read_parts(root, max(n for n, _ in pointers.values()), strict)
    for snap, (n, digest) in pointers.items():
        pieces = [parts.get(snap, {}).get(part_prop(i)) for i in range(1, n + 1)]
        joined = "".join(p for p in pieces if p is not None)
        whole = n > 0 and None not in pieces
        out[snap] = (
            joined if whole and hashlib.sha256(joined.encode()).hexdigest() == digest else None
        )
    return out


@dataclass
class PointMark:
    """What a backup point left out, read back from the drive."""

    tops: dict[str, str] = field(default_factory=dict)  # top -> flags
    datasets: dict[str, tuple[str, str]] = field(default_factory=dict)  # rel -> (canmount, mp)

    @property
    def left_out(self) -> set[str]:
        """The tops of the subtrees the point left out of the backup."""
        return {t for t, flags in self.tops.items() if flags != SNAPSHOTS}

    @property
    def no_snapshot(self) -> list[str]:
        """The tops excluded from snapshots only, as at the point."""
        return sorted(t for t, flags in self.tops.items() if flags == SNAPSHOTS)

    def covers(self, rel: str) -> bool:
        """True when the point left ``rel`` out."""
        return rel in self.datasets or bool(covering(rel, self.left_out))


def parse_point_mark(value: str) -> PointMark | None:
    """A point's mark, or None when absent or unreadable."""
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tops = data.get("tops", {})
    datasets = data.get("datasets", {})
    if not isinstance(tops, dict) or not isinstance(datasets, dict):
        return None
    out = PointMark()
    for top, flags in tops.items():
        if isinstance(top, str) and flags in (BACKUPS, BOTH, SNAPSHOTS):
            out.tops[top] = flags
    for rel, props in datasets.items():
        if isinstance(rel, str) and isinstance(props, list) and len(props) == 2:
            out.datasets[rel] = (str(props[0]), str(props[1]))
    return out


def read_point_mark(pool: str, point: str, snaps: list[Snap]) -> PointMark | None:
    """What backup point ``point`` left out, from its snapshots on the
    drive, read strictly (raises :class:`ReadError`) as the completion
    marks are (M3R-9, M5b-13 13a). None for a point without the mark (a
    drive written before rc10, or a sanoid point): restored as before.
    A record in parts is joined first; one that is incomplete raises
    :class:`ValueError`, as snapshots that disagree do."""
    records = point_records(pool, strict=True)
    values = {
        records[n]
        for n in (f"{pool}/{s.stored}@{s.name}" for s in snaps if s.name == point)
        if n in records
    }
    if not values:
        return None
    value = values.pop() if len(values) == 1 else None
    parsed = parse_point_mark(value) if value is not None else None
    if parsed is None:
        raise ValueError(
            f"the snapshots of {point} disagree on what it left out, "
            + "or its record is unreadable or incomplete"
        )
    return parsed


def held_lines(pool: str) -> list[str]:
    """For ``zark mount`` (M5b-13b): one line per subtree the drive's newest
    complete point left out while the drive still holds a copy of it. Best
    effort: a drive that cannot be read, or written before rc10, gives none."""
    try:
        snaps = drive_snapshots(pool, strict=True)
        newest = max(complete_points(snaps, ""), default="")
        mark = read_point_mark(pool, newest, snaps) if newest else None
    except (ReadError, ValueError):
        return []
    lines: list[str] = []
    for top in sorted(mark.left_out if mark else set()):
        copies = [s for s in snaps if s.stored == top]
        if copies:
            last = max(copies, key=lambda s: s.createtxg)
            lines.append(
                f"{top}: excluded from backups at {newest}; "
                + f"this drive's last copy of it is {last.name}"
            )
    return lines


def for_run(pools: list[str], log: Log) -> tuple[Declared, str]:
    """What a backup or prepare leaves out, and the mark its point will carry
    on the drive, read before the point is taken (D10: checked at every run).

    A value zark ignores is reported. A mark longer than :data:`MAX_PARTS`
    user properties stops the run before anything is written: recover
    could not recreate what the point leaves out (M5b-7 7a, M5bF-7)."""
    try:
        origin = read_origin(pools, strict=True)
        declared = read(pools, origin, strict=True)
    except ReadError as e:
        log.fatal(f"Cannot read what is excluded from backups: {e}")
    for rel, why in declared.ignored:
        log.warn(f"Exclusion ignored on {rel}: {why}")
    mark = point_mark(declared, origin)
    if (n := parts_needed(mark)) > MAX_PARTS:
        log.fatal(
            f"Too many datasets excluded to record them on the drive ({len(mark)} bytes: "
            + f"{n} parts of {MARK_LIMIT}, at most {MAX_PARTS})",
            causes=["recover reads what a point left out from the drive, to recreate it"],
            solutions=[
                f"Include a large subtree again: {zark_command()} exclude list, then "
                + f"{zark_command()} exclude remove <dataset> backups"
            ],
        )
    return declared, mark


def record(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    pool: str,
    point: str,
    rels: list[str],
    mark: str,
    log: Log,
    extra: tuple[tuple[str, str], ...] = (),
) -> bool:
    """Set the point's mark on its snapshots on the drive, complete run or not
    (M5b-7 7a): user properties, nothing mounts. A record in parts goes in
    the same zfs set as its pointer, as separate arguments: one shell
    string longer than 128 KiB is refused by execve (M5bR-1). ``extra``
    goes in the same zfs set: origin's pools (M6-5)."""
    if not rels:
        return True
    props = [f"{k}={v}" for k, v in (*record_props(mark), *extra)]
    r = sh.run(["zfs", "set", *props, *(f"{pool}/{rel}@{point}" for rel in rels)], log=log)
    if not r.ok:
        also = " and how origin's pools were" if extra else ""
        log.warn(
            f"Could not record what {point} leaves out{also} on {pool}: {r.stderr.strip()} "
            + "(recover restores that point as if nothing were excluded)"
        )
    return r.ok


def keep_held(pool: str, top: str, stamp: str, log: Log) -> bool:
    """The user keeps the drive's copy of an excluded subtree: not asked
    again by backup (M5b-10 10a), as a kept orphan is marked."""
    r = sh.run(f"zfs set {_q(f'{KEPT}=kept@{stamp}')} {_q(f'{pool}/{top}')}", log=log)
    if r.ok:
        log.ok(f"Keeping {pool}/{top} (excluded from backups, not asked again)")
    else:
        log.warn(f"Could not mark {pool}/{top} as kept: {r.stderr.strip()}")
    return r.ok


@dataclass
class _Rewrite:
    """One point's marks once a destroy took part of it (M5b-10b)."""

    targets: list[str]  # the snapshots left of it on the drive
    complete: str  # COMPLETE_PROP, lowered
    mark: str  # POINT_MARK, with what the destroy took


def _rewrites(
    pool: str, top: str, flags: str, snaps: list[Snap], records: dict[str, str | None]
) -> dict[str, _Rewrite]:
    """For every point the destroy of ``top`` takes snapshots from, its marks
    as they must read afterwards: the count lowered by the datasets it no
    longer holds, and those datasets recorded as excluded with the values
    they had on the drive. A point marked before the count (rc2, "yes")
    is left as it is: :func:`drive_marks.record_complete` counts it again.
    A point whose record cannot be read raises :class:`ValueError`: its
    rewrite would lose what it left out (M5bF-9)."""
    values = zark_props(pool)
    out: dict[str, _Rewrite] = {}
    for name in sorted({s.name for s in snaps if under(s.stored, top) and s.mark.isdigit()}):
        members = [s for s in snaps if s.name == name]
        left = [s for s in members if not under(s.stored, top)]
        lost = {s.dataset for s in members} - {s.dataset for s in left}
        count = int(next(s.mark for s in members if s.mark.isdigit())) - len(lost)
        if not lost or not left or count <= 0:
            continue
        stored = [f"{pool}/{s.stored}@{name}" for s in left]
        held = [records[t] for t in stored if t in records]
        pm = parse_point_mark(held[0]) if held and held[0] is not None else None
        if held and pm is None:
            raise ValueError(f"the record of {name} cannot be read")
        pm = pm or PointMark()
        pm.tops[top] = flags
        # Recorded on every received dataset since M2; "on" and the inherited
        # mountpoint when a drive lacks them.
        pm.datasets |= {rel: values.get(rel, ("on", "")) for rel in lost}
        out[name] = _Rewrite(stored, str(count), _dump(pm.tops, pm.datasets))
    return out


def destroy_held(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    pool: str, top: str, flags: str, disk_guid: str, origin: dict[str, Origin], log: Log
) -> bool:
    """Destroy a drive's copy of an excluded subtree (D11, M5b-10).

    ``zfs destroy -r`` of the top, by its exact name and logged. The points
    that held it then get their marks rewritten (10b): their completion
    count lowered and the destroyed datasets added to what they left out,
    so they stay whole and recover recreates those datasets empty. Last,
    this drive's anchors on the subtree go from origin (10c), each by name:
    nothing on the drive can use them now; another drive's are never
    touched."""
    try:
        snaps = drive_snapshots(pool, strict=True)
        rewrites = _rewrites(pool, top, flags, snaps, point_records(pool, strict=True))
    except (ReadError, ValueError) as e:
        log.warn(f"{pool}/{top} not destroyed: cannot read the points that hold it: {e}")
        return False
    r = sh.run(f"zfs destroy -r {_q(f'{pool}/{top}')}", log=log)
    if not r.ok:
        log.error(f"Could not destroy {pool}/{top}: {r.stderr.strip()}")
        return False
    log.ok(f"Destroyed {pool}/{top} and everything below it on the backup")
    # Every point is rewritten, and the anchors dropped, whatever failed before.
    rewritten = [_rewrite(name, rw, top, log) for name, rw in rewrites.items()]
    dropped = _drop_anchors(pool, top, disk_guid, origin, log)
    return all(rewritten) and dropped


def _rewrite(name: str, rw: _Rewrite, top: str, log: Log) -> bool:
    """One point's marks after a destroy (M5b-10b), its record in parts when
    it does not fit in one property, as :func:`record` writes it; a point
    left with its old marks reads PARTLY REMOVED, which is then the truth."""
    if parts_needed(rw.mark) > MAX_PARTS:
        log.warn(f"{name} now reads PARTLY REMOVED: too many datasets to record as excluded")
        return False
    props = [f"{COMPLETE_PROP}={rw.complete}", *(f"{k}={v}" for k, v in record_props(rw.mark))]
    r = sh.run(["zfs", "set", *props, *rw.targets], log=log)
    if not r.ok:
        log.warn(f"{name} now reads PARTLY REMOVED: {r.stderr.strip()}")
        return False
    log.info(f"  {name}: whole without {top}, recorded as excluded there")
    return True


def _drop_anchors(pool: str, top: str, disk_guid: str, origin: dict[str, Origin], log: Log) -> bool:
    """This drive's anchors on the destroyed subtree, by name (M5b-10c)."""
    ok = True
    for rel in subtree(top, origin):
        for b in origin[rel].bookmarks:
            if anchor_disk(b.name) != disk_guid:
                continue
            r = sh.run(f"zfs destroy {_q(f'{rel}#{b.name}')}", log=log)
            if r.ok:
                log.info(f"  removed {rel}#{b.name}: {pool} no longer holds {rel}")
            else:
                ok = False
                log.warn(f"  could not remove {rel}#{b.name}: {r.stderr.strip()}")
    return ok
