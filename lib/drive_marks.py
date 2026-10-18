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
What zark records on a backup drive and reads back from it.

The drive's metadata (``org.zark:*`` on its root dataset) and its owner
check; the completion marks of backup points, read from the snapshots
themselves; and the pins (holds, clone origins) that keep a snapshot from
being destroyed.
"""

import shlex
from dataclasses import replace
from pathlib import Path

from lib import sh
from lib.log import Log
from lib.replication import State, plan
from lib.restore_points import (
    COMPLETE_PROP,
    LEGACY_MARK,
    Complete,
    Snap,
    legacy_complete,
    parse_snapshots,
    point_marks,
    retention_points,
)
from lib.retention import point_times
from lib.survey import ReadError, checked, read_dest, read_origin, source_pools


def _q(word: str) -> str:
    return shlex.quote(word)


def write_metadata(pool: str, fields: dict[str, str], log: Log) -> bool:
    """Record ``org.zark:<key>=<value>`` on the backup pool's root dataset
    (M2 decision 17a): readable without the passphrase, and a user property
    mounts nothing, so the zvol rule does not apply."""
    pairs = " ".join(_q(f"org.zark:{k}={v}") for k, v in sorted(fields.items()) if v)
    r = sh.run(f"zfs set {pairs} {_q(pool)}", log=log)
    if not r.ok:
        log.warn(f"Could not record zark metadata on {pool}: {r.stderr.strip()}")
    return r.ok


def record_first_point(pool: str, log: Log) -> None:
    """Record ``org.zark:first-point`` on a drive that lacks it (M3P.25,
    M4b-9): its oldest backup point still there, before any retention of this
    run can remove it. prepare records its own point; a drive whose first
    points were already removed gets the best value left. Nothing is written
    when the drive cannot be read or holds no point."""
    try:
        if read_metadata(pool, strict=True).get("first-point"):
            return
        times = point_times(read_dest(pool, strict=True))
    except ReadError as e:
        log.warn(f"First backup point not recorded: {e}")
        return
    if times:
        _ = write_metadata(pool, {"first-point": min(times, key=lambda n: times[n])}, log)


def read_metadata(pool: str, strict: bool = False) -> dict[str, str]:
    """``org.zark:*`` user properties set on the backup pool's root dataset.

    ``strict`` raises :class:`ReadError` when ``zfs get`` fails, instead of
    returning nothing (which reads as a drive without metadata).
    """
    r = checked(sh.run(f"zfs get -H -s local -o property,value all {_q(pool)}"), strict)
    out: dict[str, str] = {}
    for line in r.lines if r.ok else []:
        f = line.split("\t")
        if len(f) == 2 and f[0].startswith("org.zark:"):
            out[f[0][len("org.zark:") :]] = f[1]
    return out


FORMAT = "2"  # org.zark:format of a backup made with points and anchors


# The system's identity across a recover: rpool is created anew (new GUID),
# but /etc/machine-id lives in the boot environment recover restores.
MACHINE_ID_PATH = "/etc/machine-id"


def machine_id() -> str:
    """This system's /etc/machine-id, "" when it cannot be read."""
    try:
        return Path(MACHINE_ID_PATH).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""


# States that mean the drive and this system share a snapshot or bookmark
# (or a partial receive of one): an unowned drive is accepted on these (M4a-7).
SHARED_STATES = frozenset(
    {
        State.AT_POINT,
        State.RESUME,
        State.INCREMENTAL,
        State.VIA_BOOKMARK,
        State.ROLLBACK,
        State.MODIFIED,
    },
)


def check_drive_owner(pool: str, log: Log) -> None:
    """Refuse a drive whose metadata names another system (2026-10-08).

    A drive without ``org.zark:origin-machine-id`` (written by zark <=
    2.0.0-rc2, or not by zark) is accepted only when it shares something
    with this system (M2R-7, M4a-7); see :func:`_accept_unowned`. One whose
    metadata cannot be read is refused, since unreadable would otherwise
    pass as absent.
    """
    try:
        meta = read_metadata(pool, strict=True)
    except ReadError as e:
        log.fatal(
            f"Cannot read the zark metadata of {pool}: nothing was changed",
            causes=[str(e)],
            solutions=["Check the drive: zpool status " + pool, "Reconnect it and run again"],
        )
    theirs = meta.get("origin-machine-id", "")
    if not theirs:
        _accept_unowned(pool, log)
        return
    mine = machine_id()
    if mine and mine == theirs:
        log.ok(f"{pool} belongs to this system (machine-id matches)")
        return
    when = meta.get("last-backup-at") or meta.get("prepared-at") or "?"
    log.fatal(
        f"{pool} is the backup of another system: nothing was changed",
        causes=[
            f"Drive written for host {meta.get('origin-host', '?')} (last: {when})",
            f"Its machine-id {theirs} is not this system's "
            + (mine or f"(cannot read {MACHINE_ID_PATH})"),
        ],
        solutions=[
            "Connect this system's own backup drive",
            "To reuse this drive for this system, wipe and re-prepare it: "
            + "sudo zark purge <device> && sudo zark prepare <device>",
        ],
    )


def _accept_unowned(pool: str, log: Log) -> None:
    """Accept a drive with no owner recorded only when it shares something.

    Read-only, before keys, questions or the point: the same listings and
    planner as a run. With nothing in common the drive is another system's
    or a blank one zark did not prepare, and a run would offer to destroy
    or replace what it holds; it is refused. The owner is then written by
    this run's metadata (backup's ``_write_metadata``), never by a refusal.
    """
    try:
        origin = read_origin(source_pools(), strict=True)
        dest = read_dest(pool, strict=True)
    except ReadError as e:
        log.fatal(
            f"Cannot read {pool} or this system's pools: nothing was changed",
            causes=[str(e)],
            solutions=[f"Check: zpool status {pool}", "Reconnect it and run again"],
        )
    plans = plan(origin, dest, "")  # no point yet: nothing is AT_POINT
    shared = [p.rel for p in plans if p.state in SHARED_STATES]
    if shared:
        log.info(
            f"{pool}: no owner recorded; shares {len(shared)} dataset(s) with this system, "
            + "accepted (this run records the owner)"
        )
        return
    held = [p.rel for p in plans if p.state in (State.DIVERGED, State.ORPHAN)]
    log.fatal(
        f"{pool} has no owner recorded and nothing in common with this system: "
        + "nothing was changed",
        causes=[
            "No snapshot or bookmark in common with this system: a blank drive, "
            + "one whose anchors in origin are all gone, or another system's backup",
            *([f"It holds {len(held)} dataset(s) this system shares nothing with"] if held else []),
        ],
        solutions=[
            "Connect this system's own backup drive",
            "To use this drive for this system, wipe and prepare it: "
            + "sudo zark purge <device> && sudo zark prepare <device>",
        ],
    )


def log_metadata(pool: str, log: Log, strict: bool = False) -> dict[str, str]:
    """Show who wrote this drive and when (decision 17a); return the values.
    ``strict`` raises :class:`ReadError` when the properties cannot be read
    (recover: an empty read would be taken for a drive without metadata)."""
    meta = read_metadata(pool, strict=strict)
    if not meta:
        log.info(f"{pool}: no zark metadata (written by zark <= 2.0.0-rc1)")
        return meta
    log.info(
        f"{pool}: written by zark {meta.get('version', '?')} on {meta.get('origin-host', '?')}"
        + f", last point {meta.get('last-point', 'none')} ({meta.get('last-backup-at', '?')})",
    )
    return meta


def snapshot_marks(root: str, strict: bool = False, prop: str = COMPLETE_PROP) -> dict[str, str]:
    """A user property (the completion mark by default, M2V-3) as set on the
    snapshots themselves under ``root``: ``-s local`` leaves out what a
    snapshot only inherits from its filesystems (module/zfs/dsl_prop.c:
    1224-1245). ``strict`` raises :class:`ReadError` when the listing fails."""
    r = checked(
        sh.run(f"zfs get -H -p -r -s local -t snapshot -o name,value {prop} {_q(root)}"),
        strict,
    )
    out: dict[str, str] = {}
    for line in r.lines if r.ok else []:
        f = line.split("\t")
        if len(f) == 2 and "@" in f[0]:
            out[f[0]] = f[1]
    return out


def drive_snapshots(pool: str, strict: bool = False) -> list[Snap]:
    """Every snapshot under ``<pool>/rpool`` and ``<pool>/bpool``, archived
    lineages folded into their dataset, with its own completion mark.
    ``strict`` raises :class:`ReadError` when a listing of a root that
    exists fails."""
    snaps: list[Snap] = []
    for root in (f"{pool}/rpool", f"{pool}/bpool"):
        if strict and not sh.run(f"zfs list -H -o name {_q(root)}").ok:
            continue
        r = checked(
            sh.run(f"zfs list -Hp -t snapshot -o name,guid,createtxg,creation -r {root}"), strict
        )
        if r.ok:
            snaps += parse_snapshots(r.lines, pool, snapshot_marks(root, strict))
    return snaps


def _counts_for_old_marks(pool: str, point: str) -> dict[str, list[str]]:
    """Mark value → snapshots, for the points marked before the count
    existed (see :func:`record_complete`)."""
    snaps = drive_snapshots(pool)
    marked = point_marks(snaps)
    if marked:
        old = {n for n, members in marked.items() if all(s.mark == LEGACY_MARK for s in members)}
    else:
        old = legacy_complete(snaps, read_metadata(pool).get("last-point", ""))
    out: dict[str, list[str]] = {}
    for name in sorted(old - {point}):
        members = [s for s in snaps if s.name == name]
        held = str(len({s.dataset for s in members}))
        out.setdefault(held, []).extend(f"{pool}/{s.stored}@{s.name}" for s in members)
    return out


def record_complete(pool: str, point: str, rels: list[str], log: Log) -> bool:
    """Mark ``point`` complete on the drive: every one of its snapshots gets
    ``COMPLETE_PROP=<number of datasets>`` (M2R-1, M2V-4; a user property,
    nothing mounts).

    Points marked before the count existed are brought to it once: an rc2
    mark (``yes``), or, on a drive with no mark at all, the backup points
    :func:`legacy_complete` accepts while ``org.zark:last-point`` still names
    the previous complete run. Each gets the number of datasets that hold it
    now.
    """
    by_value: dict[str, list[str]] = {str(len(rels)): [f"{pool}/{rel}@{point}" for rel in rels]}
    for value, targets in _counts_for_old_marks(pool, point).items():
        by_value.setdefault(value, []).extend(targets)
    ok = True
    for value, targets in sorted(by_value.items()):
        if not targets:
            continue
        names = " ".join(_q(t) for t in dict.fromkeys(targets))
        r = sh.run(f"zfs set {COMPLETE_PROP}={value} {names}", log=log)
        if not r.ok:
            ok = False
            log.warn(
                f"Could not mark complete points on {pool}: {r.stderr.strip()} "
                + "(recover will list them as incomplete)"
            )
    return ok


def drive_complete(pool: str) -> Complete:
    """The backup points on a drive whose run was complete, and those still
    whole, for retention (:func:`lib.restore_points.retention_points`).
    Read strictly: a mark that cannot be read would make complete points
    look incomplete, and the policy remove them, so a failed listing raises
    :class:`ReadError`."""
    meta = read_metadata(pool, strict=True)
    complete = retention_points(drive_snapshots(pool, strict=True), meta.get("last-point", ""))
    return replace(complete, first=meta.get("first-point", ""))


def drive_pins(pool: str, strict: bool = False) -> dict[str, str]:
    """``rel@name`` → why ZFS would refuse to destroy that snapshot: a clone
    origin (EEXIST) or a hold (EBUSY), module/zfs/dsl_destroy.c:79-86.
    ``strict`` raises :class:`ReadError` when a listing of a root that
    exists fails (M3R-8): a plan made without the pins would be refused
    snapshot by snapshot and misreport what it kept."""
    out: dict[str, str] = {}
    strip = f"{pool}/"
    for root in (f"{pool}/rpool", f"{pool}/bpool"):
        if strict and not sh.run(f"zfs list -H -o name {_q(root)}").ok:
            continue
        r = checked(
            sh.run(f"zfs list -Hp -t snapshot -o name,clones,userrefs -r {_q(root)}"), strict
        )
        for line in r.lines if r.ok else []:
            f = line.split("\t")
            if len(f) < 3 or not f[0].startswith(strip):
                continue
            rel = f[0][len(strip) :]
            if f[1] not in ("", "-"):
                out[rel] = f"origin of a clone on the drive ({f[1]})"
            elif f[2].isdigit() and int(f[2]) > 0:
                out[rel] = "held (zfs hold)"
    return out
