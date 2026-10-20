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
The restore plan of ``zark recover``: the point the operator chooses, what
each dataset is restored from and with which mount properties, the monthly
bases sent first, and the restore table shown before anything is written.
"""

from dataclasses import dataclass, field
from pathlib import Path

from lib import drive_marks, sh, survey
from lib.backup_layout import (
    list_datasets as _list_datasets,
    root_is_empty,
    zark_props as _zark_props,
)
from lib.exclusions import PointMark, covering
from lib.log import Log
from lib.mount_props import (
    BPOOL_CONTAINERS,
    CREATED_CONTAINERS,
    MountProps,
    choose,
    effective_mountpoint,
    parse_list_cache,
    receive_options,
    rpool_root_mountpoint,
)
from lib.replication import ANCHOR_RE, POINT_PREFIX, shared_kind
from lib.restore_points import Point, Snap, logical, point_name, resolve

PROBE_MNT = "/run/zark/probe"


@dataclass
class RestoreRow:  # pylint: disable=too-many-instance-attributes
    """One dataset of the restore table."""

    rel: str  # dataset relative to the backup pool (rpool/var, bpool/BOOT/x)
    snap: Snap | None  # resolved snapshot; None = not restored
    referenced: int = 0
    props: MountProps | None = None
    options: list[str] = field(default_factory=list)
    effective: str = ""  # effective mountpoint after receive
    note: str = ""  # why the dataset is not restored
    # The newest monthly before the point, restored first so that origin
    # shares it with every drive in rotation (M3P.13, M4b-6), and what it adds.
    base: Snap | None = None
    base_bytes: int = 0
    # Excluded from backups at this point (M5b-13 13a): created empty, and on
    # an exclusion's top its flags (``backups`` or ``both``).
    empty: bool = False
    flags: str = ""

    @property
    def restored(self) -> bool:
        """True when this dataset will be received."""
        return self.snap is not None and not self.note


@dataclass
class RestorePlan:  # pylint: disable=too-many-instance-attributes
    """Everything decided before the internal disk is touched."""

    pool: str
    device: str
    be: str
    point: Point
    rows: list[RestoreRow]
    keystore_snap: str
    keystore_bytes: int
    hostid: str = ""  # 8 hex digits, big-endian as zgenhostid expects
    rpool_mountpoint: str = "/"
    rpool_mountpoint_source: str = "ubuntu"

    def row(self, rel: str) -> RestoreRow | None:
        """Row of ``rel``, if any."""
        return next((r for r in self.rows if r.rel == rel), None)

    @property
    def rpool_rows(self) -> list[RestoreRow]:
        """Restored rows under rpool, parents first."""
        return [r for r in self.rows if r.rel.startswith("rpool/") and r.restored]

    @property
    def skipped(self) -> list[RestoreRow]:
        """Rows that will not be restored (nor created empty)."""
        return [r for r in self.rows if not r.restored and not r.empty]

    @property
    def empty_rows(self) -> list[RestoreRow]:
        """Rows created empty, parents first (M5b-13)."""
        return [r for r in self.rows if r.empty]

    @property
    def rpool_bytes(self) -> int:
        """Bytes received into rpool (datasets, their monthly bases, keystore)."""
        rows = self.rpool_rows
        return sum(r.referenced + r.base_bytes for r in rows) + self.keystore_bytes

    @property
    def bpool_bytes(self) -> int:
        """Bytes received into bpool (with its monthly base)."""
        rows = [r for r in self.rows if r.rel.startswith("bpool/") and r.restored]
        return sum(r.referenced + r.base_bytes for r in rows)

    def drop_bases(self) -> int:
        """Restore the point only, everywhere; how many bases were dropped."""
        dropped = sum(1 for r in self.rows if r.base is not None)
        for r in self.rows:
            r.base, r.base_bytes = None, 0
        return dropped


def list_snapshots(pool: str, log: Log) -> list[Snap]:
    """Every snapshot on the drive with its completion mark, read strictly
    (M3R-9): a failed read of the marks would turn the drive into a legacy
    one and pick the default point from org.zark:last-point alone."""
    try:
        snaps = drive_marks.drive_snapshots(pool, strict=True)
    except survey.ReadError as e:
        log.fatal(
            f"Cannot read the snapshots of {pool}: {e}",
            causes=["The drive did not answer a zfs listing; nothing was written"],
            solutions=[f"Check the drive: zpool status {pool}", "Run recover again"],
        )
    return snaps


def _referenced(snapshots: list[str]) -> dict[str, int]:
    """``referenced`` in bytes of each full snapshot name."""
    if not snapshots:
        return {}
    r = sh.run("zfs get -Hp -o name,value referenced " + " ".join(snapshots))
    out: dict[str, int] = {}
    for line in r.lines if r.ok else []:
        fields = line.split("\t")
        if len(fields) == 2 and fields[1].isdigit():
            out[fields[0]] = int(fields[1])
    return out


def _mount_snapshot(full_snap: str) -> bool:
    """Mount a snapshot read-only at PROBE_MNT (snapshots mount as legacy)."""
    _ = sh.run(f"mkdir -p {PROBE_MNT}")
    return sh.run(f"mount -t zfs -o ro {full_snap} {PROBE_MNT}").ok


def _umount_probe() -> None:
    _ = sh.run(f"umount {PROBE_MNT}")


def _probe_be(full_snap: str, log: Log) -> tuple[str, str, str]:
    """Read origin's zfs-list.cache files and hostid from the BE snapshot.

    Returns ``(rpool cache text, bpool cache text, hostid hex)``, each
    empty when absent. The snapshot is mounted read-only in a private
    directory, never over ``/`` (hallazgo 9).
    """
    if not _mount_snapshot(full_snap):
        log.warn(f"Could not mount {full_snap} to read origin metadata")
        return "", "", ""
    try:
        root = Path(PROBE_MNT)
        caches: list[str] = []
        for pool in ("rpool", "bpool"):
            f = root / "etc/zfs/zfs-list.cache" / pool
            caches.append(f.read_text(encoding="utf-8") if f.is_file() else "")
        hostid = ""
        hf = root / "etc/hostid"
        if hf.is_file():
            raw = hf.read_bytes()
            if len(raw) >= 4:
                hostid = f"{int.from_bytes(raw[:4], 'little'):08x}"
        return caches[0], caches[1], hostid
    finally:
        _umount_probe()


def _empty_root(full_snap: str) -> bool | None:
    """True when the snapshot's root directory has no entries; None if unreadable."""
    return root_is_empty(full_snap, PROBE_MNT, zfsutil=False)


def is_incomplete(p: Point, complete: set[str] | None) -> bool:
    """True for a backup point whose run did not reach every dataset."""
    return p.family == "zark_" and complete is not None and point_name(p) not in complete


def partly_removed(p: Point, complete: set[str] | None, run_complete: set[str] | None) -> bool:
    """True for a backup point whose run was complete but that lost datasets
    on the drive since (a prune, a rollback or a destroy; M3P.5)."""
    return is_incomplete(p, complete) and run_complete is not None and point_name(p) in run_complete


def choose_point(
    points: list[Point],
    log: Log,
    complete: set[str] | None = None,
    run_complete: set[str] | None = None,
) -> Point:
    """Backup points (``zark_``) first-class; other runs as carried intermediates,
    each with how many datasets it holds (redesign §4.2). A backup point of an
    incomplete run is labelled so, and one whose run was complete
    (``run_complete``) but that lost datasets since as PARTLY REMOVED; the
    default is the newest complete backup point (the newest entry when there
    is none)."""
    total = len({ds for p in points for ds in p.members})
    options = []
    default = len(points) - 1
    if complete is not None:
        good = [
            i
            for i, p in enumerate(points)
            if p.family == "zark_" and not is_incomplete(p, complete)
        ]
        default = good[-1] if good else default
    for p in points:
        held = f"{len(p.members)}/{total} datasets"
        if partly_removed(p, complete, run_complete):
            options.append(f"{p.label_utc()}  backup point, PARTLY REMOVED  ({held})")
            continue
        if is_incomplete(p, complete):
            options.append(f"{p.label_utc()}  backup point, INCOMPLETE  ({held})")
            continue
        if p.family == "zark_":
            options.append(f"{p.label_utc()}  backup point  ({held})")
            continue
        kinds = sorted({s.name.split("_")[-1] for m in p.members.values() for s in m})
        detail = "/".join(kinds) if p.family == "autosnap_" else p.family.rstrip("_")
        options.append(f"{p.label_utc()}    carried: {detail}  ({held})")
    question = (
        "Restore point (newest is the default):"
        if default == len(points) - 1
        else "Restore point (newest complete backup point is the default):"
    )
    idx = log.ask_choice(question, options, default)
    return points[idx]


def _absent_note(ds: str, point: Point, snaps: list[Snap]) -> str:
    """Why ``resolve`` gave ``ds`` nothing for ``point``."""
    earlier = [s for s in snaps if s.dataset == ds and s.creation <= point.end]
    if not earlier:
        return "no snapshot at or before this point"
    last = max(earlier, key=lambda s: (s.creation, s.createtxg))
    return f"destroyed in origin before this point (last snapshot {last.name})"


def _log_archived(stored: dict[str, str], log: Log) -> None:
    archived = sorted(ds for ds in stored if logical(ds) != ds)
    if archived:
        log.info(
            f"Archived lineages on this backup ({len(archived)}), used for points before them:"
        )
        for ds in archived:
            log.info(f"  {ds}")


def build_plan(  # pylint: disable=too-many-locals,too-many-branches
    pool: str,
    device: str,
    be: str,
    point: Point,
    snaps: list[Snap],
    log: Log,
    complete: set[str] | None = None,
    excluded: PointMark | None = None,
) -> RestorePlan:
    """Resolve every dataset for ``point`` and decide its mount properties.
    What the point left out (``excluded``, its org.zark:excluded mark) is
    never restored from an earlier snapshot: it is created empty."""
    excluded = excluded or PointMark()
    stored = _list_datasets(pool)
    # Archived lineages count as earlier snapshots of their dataset (decision 19).
    types: dict[str, str] = {}
    for ds in sorted(stored, key=lambda d: logical(d) != d):
        types.setdefault(logical(ds), stored[ds])
    _log_archived(stored, log)
    wanted = [
        ds
        for ds in sorted(set(types) | set(excluded.datasets))
        if (ds.startswith("rpool/") and ds not in CREATED_CONTAINERS) or ds == f"bpool/BOOT/{be}"
    ]
    resolved = resolve(point, [s for s in snaps if s.dataset in wanted], wanted, complete)

    be_row_snap = resolved.get(f"rpool/ROOT/{be}")
    cache_r, cache_b, hostid = ("", "", "")
    if be_row_snap:
        cache_r, cache_b, hostid = _probe_be(f"{pool}/{be_row_snap.stored}@{be_row_snap.name}", log)
    cache = {**parse_list_cache(cache_r), **parse_list_cache(cache_b)}
    zark = _zark_props(pool)
    children = {ds for ds in types for other in types if other.startswith(f"{ds}/")}

    root_mp, root_src = rpool_root_mountpoint(cache)
    effective: dict[str, str] = {"rpool": root_mp, **CREATED_CONTAINERS, **BPOOL_CONTAINERS}
    claimed: dict[str, str] = {}  # guessed mountpoint → dataset that got it
    rows: list[RestoreRow] = []
    for ds in wanted:
        if excluded.covers(ds):
            rows.append(_empty_row(ds, excluded, zark, effective))
            continue
        snap = resolved.get(ds)
        row = RestoreRow(rel=ds, snap=snap)
        parent = ds.rsplit("/", 1)[0]
        leaf = ds.rsplit("/", 1)[-1]
        if types[ds] == "volume":
            row.note = "zvol (only the keystore zvol is restored)"
        elif snap is None:
            row.note = _absent_note(ds, point, snaps)
        elif parent not in effective:
            row.note = f"parent {parent} is not restored"
        if row.note:
            rows.append(row)
            continue
        assert snap is not None
        full = f"{pool}/{snap.stored}@{snap.name}"
        needs_probe = ds not in zark and ds not in cache and ds in children
        empty = bool(needs_probe and _empty_root(full))
        row.props = choose(ds, be, zark=zark, cache=cache, empty_with_children=empty)
        target = effective_mountpoint(row.props, effective[parent], leaf)
        if row.props.source in ("ubuntu", "inferred") and row.props.canmount == "on":
            if target in claimed:
                # e.g. a second home_* from a reinstall: never stack two
                # datasets on one mountpoint by guesswork.
                log.warn(f"{ds}: {target} already taken by {claimed[target]} - noauto")
                row.props = MountProps("noauto", row.props.mountpoint, row.props.source)
            else:
                claimed[target] = ds
        row.options = receive_options(row.props, effective[parent], leaf)
        row.effective = effective_mountpoint(row.props, effective[parent], leaf)
        effective[ds] = row.effective
        rows.append(row)

    refs = _referenced(
        [f"{pool}/{r.snap.stored}@{r.snap.name}" for r in rows if r.restored and r.snap],
    )
    for r in rows:
        if r.restored and r.snap:
            r.referenced = refs.get(f"{pool}/{r.snap.stored}@{r.snap.name}", -1)
    _plan_bases(pool, rows, snaps, log)

    ks = sh.run(f"zfs list -Hp -t snapshot -o name,createtxg -s createtxg {pool}/keystore")
    ks_snap = ks.lines[-1].split("\t")[0] if ks.ok and ks.lines else ""
    ks_bytes = _referenced([ks_snap]).get(ks_snap, -1) if ks_snap else -1
    return RestorePlan(
        pool,
        device,
        be,
        point,
        rows,
        ks_snap,
        ks_bytes,
        hostid,
        rpool_mountpoint=root_mp,
        rpool_mountpoint_source=root_src,
    )


def _empty_row(
    ds: str, excluded: PointMark, zark: dict[str, tuple[str, str]], effective: dict[str, str]
) -> RestoreRow:
    """A dataset the point left out, created empty with the canmount and
    mountpoint origin had (M5b-8, M5b-13 13a): the point's mark first, then
    what the drive recorded of an earlier copy, else "on" and inherited."""
    parent, leaf = ds.rsplit("/", 1)
    canmount, mountpoint = excluded.datasets.get(ds) or zark.get(ds) or ("on", "")
    props = MountProps(canmount, mountpoint, "excluded")
    row = RestoreRow(rel=ds, snap=None, props=props, empty=True)
    row.options = receive_options(props, effective.get(parent, ""), leaf)
    row.effective = effective_mountpoint(props, effective.get(parent, ""), leaf)
    effective[ds] = row.effective
    top = covering(ds, excluded.left_out)
    row.flags = excluded.tops[top] if top == ds else ""
    return row


def _monthly_base(row: RestoreRow, snaps: list[Snap]) -> Snap | None:
    """The newest sanoid monthly before the row's point in the same lineage
    (an incremental needs both in one dataset); None when there is none."""
    snap = row.snap
    if snap is None or shared_kind(snap.name) == "monthly":
        return None
    older = [
        s
        for s in snaps
        if s.dataset == row.rel
        and s.stored == snap.stored
        and shared_kind(s.name) == "monthly"
        and s.createtxg < snap.createtxg
    ]
    return max(older, key=lambda s: s.createtxg, default=None)


def _plan_bases(pool: str, rows: list[RestoreRow], snaps: list[Snap], log: Log) -> None:
    """6a (M3P.13, M4b-6): each restored dataset starts from its newest
    monthly before the point, sent in full, then the point with ``-i``, so
    the recovered system shares that monthly with every drive that carried
    it and their next backups continue instead of resending. The extra space
    is the monthly's ``referenced`` plus what the point wrote since it, less
    the point's own ``referenced``; when it cannot be read the dataset gets
    the point only."""
    without = 0
    for r in rows:
        base = _monthly_base(r, snaps) if r.restored else None
        if base is None:
            without += r.restored
            continue
        assert r.snap is not None
        point = f"{pool}/{r.snap.stored}@{r.snap.name}"
        whole = f"{pool}/{base.stored}@{base.name}"
        ref = _referenced([whole]).get(whole, -1)
        w = sh.run(f"zfs get -Hp -o value written@{base.name} {point}")
        written = int(w.output.strip()) if w.ok and w.output.strip().isdigit() else -1
        if ref < 0 or written < 0 or r.referenced < 0:
            without += 1
            continue
        r.base, r.base_bytes = base, max(ref + written - r.referenced, 0)
    if without:
        log.info(
            f"{without} dataset(s) restored from the point only: no monthly snapshot before it "
            + "on the drive (other drives' next backup of them is a full send)"
        )


def _fmt_delta(seconds: int) -> str:
    """Signed offset from the point, e.g. ``-36m30s``, ``-5h02m``, from 48 h
    on in days (``-38d23h59m``, M3P.10), or ``0``."""
    if seconds == 0:
        return "0"
    sign = "-" if seconds < 0 else "+"
    m, sec = divmod(abs(seconds), 60)
    h, m = divmod(m, 60)
    if h >= 48:
        d, h = divmod(h, 24)
        return f"{sign}{d}d{h:02d}h{m:02d}m"
    return f"{sign}{h}h{m:02d}m" if h else f"{sign}{m}m{sec:02d}s"


def _snapshot_label(name: str, drives: dict[str, str]) -> str:
    """How the table names a snapshot. Another drive's bpool anchor travels
    to this drive in a -I stream as an ordinary snapshot (M2X.5): it holds
    that drive's backup point, so it is shown as such."""
    m = ANCHOR_RE.match(name)
    if m is None:
        return name
    guid, stamp = m.groups()
    return f"{POINT_PREFIX}{stamp} (bpool anchor of drive {drives.get(guid, guid)})"


def show_plan(plan: RestorePlan, log: Log, drives: dict[str, str] | None = None) -> None:
    """The restore table (hallazgo 3): what each dataset will be restored from.
    ``drives`` maps a pool GUID to its registry name."""
    drives = drives or {}
    log.info(f"Restore point: {plan.point.label_utc()} ({plan.point.family.rstrip('_')})")
    log.raw(f"  {'DATASET':44} {'SNAPSHOT':44} {'Δ POINT':>9} {'SIZE':>7}  MOUNT")
    log.raw(
        f"  {'rpool (pool root)':44} {'(created)':44} {'':>9} {'':>7}  "
        + f"off {plan.rpool_mountpoint} [{plan.rpool_mountpoint_source}]",
    )
    for r in plan.rows:
        if r.empty:
            assert r.props is not None
            log.raw(
                f"  {r.rel:44} excluded from backups at this point: recreated empty  "
                + f"{r.props.canmount} {r.effective}"
            )
            continue
        if not r.restored:
            log.raw(f"  {log.Y}{r.rel:44} NOT RESTORED - {r.note}{log.N}")
            continue
        assert r.snap is not None and r.props is not None
        delta = _fmt_delta(r.snap.creation - plan.point.label)
        size = sh.humanize_bytes(r.referenced) if r.referenced >= 0 else "?"
        mark = log.Y if r.props.source == "inferred" else ""
        origin = " (from archive)" if r.snap.source else ""
        label = _snapshot_label(r.snap.name, drives)
        log.raw(
            f"  {r.rel:44} {label:44} {delta:>9} {size:>7}  "
            + f"{mark}{r.props.canmount} {r.effective} [{r.props.source}]{log.N if mark else ''}"
            + origin,
        )
    log.raw(f"  {'keystore':44} {plan.keystore_snap.split('@')[-1]:44} (newest, independent)")
