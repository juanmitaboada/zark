# Copyright 2026 Juanmi Taboada # pylint: disable=too-many-lines
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
zark's replication executor: the shell side of :mod:`lib.replication`.

One run against one backup pool (already imported under I-G by the caller):

  1. ``pause_sanoid`` — stop ``sanoid.timer`` and wait for a running take
     or prune, so nothing inside a ``-I`` range is pruned mid-send.
  2. ``take_point`` — ``zfs snapshot`` of every expected dataset, one call
     per pool (one txg each; a call cannot span pools).
  3. ``survey`` — read origin and destination, plan every dataset.
  4. Decisions the caller asked the user about: ``rollback``, ``archive``,
     ``destroy``, ``keep_orphan``, ``follow_rename``; then ``survey`` runs
     again, the "kept" mark of an orphan origin has again is cleared
     (``clear_orphan_marks``) and the structural containers are created on
     the destination where missing (``ensure_containers``).
  5. ``transfer`` for each transferable dataset, parents first:
     ``zfs send [-w] … | zfs receive -s -u -o canmount=noauto -x mountpoint
     -o org.zark:canmount=… -o org.zark:mountpoint=… <dest>``, never ``-F``.
  6. ``survey`` again: success iff every expected dataset is at the point.
  7. ``anchor`` — per dataset at the point: a bookmark on rpool, the point
     renamed to the disk's anchor on bpool.
  8. ``drop_check`` / ``drop`` — every candidate for removal in origin (this
     run's point, points left by earlier runs, this disk's older anchors) is
     checked against a fresh read of both sides: it goes only when the
     dataset is verified on the backup at the new point, by guid, and its new
     anchor exists; anything that may still be a base stays. Every verdict is
     logged with its reason.

Every ZFS behaviour relied on here was checked in OpenZFS 2.4.1 (see the
M2 plan, §2, and the commit messages).
"""

import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from lib import sh
from lib.cleanup import Cleanup
from lib.identity import disks_under
from lib.log import Log
from lib.replication import (
    POINT_RE,
    STRUCTURAL,
    TRANSFERABLE,
    DatasetPlan,
    Dest,
    Origin,
    Ref,
    State,
    anchor_disk,
    anchor_name,
    archive_name,
    expected,
    is_bpool,
    old_anchors,
    parent,
    plan,
    point_name,
    reached,
    shared_snapshots,
)
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
from lib.retention import Prune, Removal, point_times

SANOID_UNITS = ("sanoid.service", "sanoid-prune.service")
SANOID_WAIT_SECONDS = 600
# Written just before zark stops sanoid.timer, removed once it starts it
# again. /run is emptied at boot, when the enabled timer starts by itself,
# so a marker found with the timer inactive means a zark run that died
# without its cleanup left the timer stopped.
SANOID_MARKER = "/run/zark/sanoid-paused"
# Set on every snapshot of a backup point when it is taken (same sync task):
# the pool GUID of the drive the run was for, so whatever removes a point a
# run left in origin first anchors it for that drive (M4b-4, 8b).
POINT_FOR = "org.zark:point-for"
# One more try for an anchor that failed (M3R-5); later runs retry it too.
ANCHOR_RETRY_SECONDS = 2.0
# Names per grouped `zfs destroy` (M3P.26): keeps each command line short.
DESTROY_GROUP = 100


def _q(word: str) -> str:
    return shlex.quote(word)


def _parse_refs(lines: list[str], strip: str) -> dict[str, tuple[list[Ref], list[Ref]]]:
    """``name guid createtxg creation`` lines → {rel: (snapshots, bookmarks)}."""
    out: dict[str, tuple[list[Ref], list[Ref]]] = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) < 4:
            continue
        full = fields[0]
        sep = "@" if "@" in full else "#" if "#" in full else ""
        if not sep or not full.startswith(strip):
            continue
        ds, name = full[len(strip) :].split(sep, 1)
        try:
            ref = Ref(name, fields[1], int(fields[2]), int(fields[3]))
        except ValueError:
            continue
        snaps, bms = out.setdefault(ds, ([], []))
        (snaps if sep == "@" else bms).append(ref)
    for snaps, bms in out.values():
        snaps.sort(key=lambda r: r.createtxg)
        bms.sort(key=lambda r: r.createtxg)
    return out


def source_pools() -> list[str]:
    """Origin pools zark replicates that exist here: rpool, and bpool if present."""
    return [p for p in ("rpool", "bpool") if sh.run(f"zpool list -H -o name {p}").ok]


class ReadError(Exception):
    """A ``zfs list`` needed for a safe decision failed."""


def _checked(r: sh.RunResult, strict: bool) -> sh.RunResult:
    if strict and not r.ok:
        raise ReadError(f"could not read: {r.command or 'zfs list'}")
    return r


def read_origin(pools: list[str], strict: bool = False) -> dict[str, Origin]:
    """Origin datasets with kind, mount properties, snapshots and bookmarks.

    ``strict`` raises :class:`ReadError` when a listing fails instead of
    returning what could be read.
    """
    origin: dict[str, Origin] = {}
    for pool in pools:
        r = sh.run(
            "zfs list -Hp -t filesystem,volume "
            + f"-o name,type,canmount,mountpoint,referenced,origin -r {pool}",
        )
        r = _checked(r, strict)
        for line in r.lines if r.ok else []:
            f = line.split("\t")
            if len(f) >= 6:
                ref = int(f[4]) if f[4].isdigit() else 0
                clone_of = "" if f[5] in ("-", "") else f[5]
                origin[f[0]] = Origin(f[0], f[1], f[2], f[3], ref, clone_of=clone_of)
        r = sh.run(
            f"zfs list -Hp -t snapshot,bookmark -o name,guid,createtxg,creation -r {pool}",
        )
        r = _checked(r, strict)
        for rel, (snaps, bms) in _parse_refs(r.lines if r.ok else [], "").items():
            if rel in origin:
                origin[rel].snaps, origin[rel].bookmarks = snaps, bms
    return origin


def read_dest(pool: str, strict: bool = False) -> dict[str, Dest]:
    """Destination datasets under ``<pool>/rpool`` and ``<pool>/bpool``, by origin name.

    ``strict`` raises :class:`ReadError` when the pool does not list (an
    exported pool is not an empty one) or a listing of an existing root fails;
    a missing ``<pool>/rpool`` or ``<pool>/bpool`` is accepted only then.
    """
    dest: dict[str, Dest] = {}
    strip = f"{pool}/"
    if strict:
        _ = _checked(sh.run(f"zfs list -H -o name {_q(pool)}"), strict)
    for root in (f"{pool}/rpool", f"{pool}/bpool"):
        if not sh.run(f"zfs list -H -o name {root}").ok:
            continue
        r = sh.run(
            "zfs list -Hp -t filesystem,volume "
            + f"-o name,type,receive_resume_token,{_ORPHAN},used,written -r {root}",
        )
        r = _checked(r, strict)
        for line in r.lines if r.ok else []:
            f = line.split("\t")
            if len(f) >= 6 and f[0].startswith(strip):
                rel = f[0][len(strip) :]
                used = int(f[4]) if f[4].isdigit() else 0
                written = int(f[5]) if f[5].isdigit() else 0
                token = "" if f[2] in ("-", "") else f[2]
                orphan = "" if f[3] in ("-", "") else f[3]
                dest[rel] = Dest(
                    rel, kind=f[1], token=token, orphan=orphan, used=used, written=written
                )
        r = _checked(
            sh.run(f"zfs list -Hp -t snapshot -o name,guid,createtxg,creation -r {root}"),
            strict,
        )
        for rel, (snaps, _) in _parse_refs(r.lines if r.ok else [], strip).items():
            if rel in dest:
                dest[rel].snaps = snaps
    return dest


_ORPHAN = "org.zark:orphan"


def bookmark_feature(pool: str = "rpool") -> str:
    """``feature@bookmark_v2`` state on ``pool`` (enabled/active/disabled/"")."""
    r = sh.run(f"zpool get -H -o value feature@bookmark_v2 {pool}")
    return r.output.strip() if r.ok else ""


def require_bookmark_v2(log: Log) -> None:
    """Refuse without bookmark_v2 on rpool (M2 decision 4a; `zark setup` enables it)."""
    state = bookmark_feature("rpool")
    if state not in ("enabled", "active"):
        log.fatal(
            f"rpool feature@bookmark_v2 is {state or 'unknown'}",
            causes=["zark's backup anchors are rpool bookmarks, which need this feature"],
            solutions=["Run: sudo zark setup  (it explains the change and asks first)"],
        )


def _importable_bpools() -> list[tuple[str, list[str]]]:
    """(id, vdev paths) of every pool named bpool that ``zpool import`` lists.

    ``zpool import`` without a pool name only lists what could be imported
    (cmd/zpool/zpool_main.c:3986-4017); ZPOOL_VDEV_NAME_PATH=1 makes it print
    each vdev by its full path (lib/libzfs/libzfs_pool.c:4521).
    """
    r = sh.run("env LC_ALL=C ZPOOL_VDEV_NAME_PATH=1 zpool import")
    found: list[tuple[str, list[str]]] = []
    for block in re.split(r"^\s*pool: ", r.stdout, flags=re.MULTILINE)[1:]:
        lines = block.splitlines()
        if not lines or lines[0].strip() != "bpool":
            continue
        m = re.search(r"^\s*id: (\d+)", block, re.MULTILINE)
        devices = [w for ln in lines[1:] for w in ln.split()[:1] if w.startswith("/dev/")]
        found.append((m.group(1) if m else "?", devices))
    return found


def _rpool_disks() -> set[str]:
    """Whole disks under the imported rpool's vdevs (empty when it is not imported)."""
    r = sh.run("zpool list -vHP rpool")
    disks: set[str] = set()
    for line in r.lines[1:] if r.ok else []:
        path = line.strip().split("\t")[0].split(" ")[0]
        if path.startswith("/dev/"):
            disks |= disks_under(path)
    return disks


def require_boot_pool(log: Log) -> None:
    """Refuse while a pool named bpool can be imported here but is not (M2R-4).

    With bpool not imported ``/boot`` is not mounted either, so the pool's
    absence from ``zpool list`` is all there is to see; a run would then
    leave the kernels and initrds out and still call itself complete.

    Each importable bpool is named with its id and devices (M2V-2, M4a-6):
    one with a disk in common with rpool is this system's and is imported by
    id; any other belongs to another disk and must not be imported, since it
    would mount over ``/boot``; one whose devices cannot be read is named as
    such, with no advice about its disk. The advice never includes exporting
    bpool: on a running system that cannot succeed and leaves ``/boot``
    unmounted (P6).
    """
    if sh.run("zpool list -H -o name bpool").ok:
        return
    candidates = _importable_bpools()
    if not candidates:
        return
    mine = _rpool_disks()
    own: list[str] = []
    unknown = False
    causes = ["Without this system's bpool the backup would leave out /boot"]
    solutions: list[str] = []
    for pool_id, devices in candidates:
        disks = set().union(*(disks_under(d) for d in devices)) if devices else set()
        where = ", ".join(devices)
        if not disks:
            unknown = True
            causes.append(f"bpool id {pool_id}: no readable device; cannot tell whose it is")
            solutions.append("Look at it without importing: sudo zpool import (it only lists)")
        elif disks & mine:
            own.append(pool_id)
            causes.append(f"bpool id {pool_id} on {where}: on rpool's disk, not imported")
        else:
            causes.append(f"bpool id {pool_id} on {where}: another disk's")
            solutions.append(
                f"Disconnect the disk holding bpool id {pool_id}; do not import it: "
                + "it would mount over /boot"
            )
    if len(own) == 1:
        solutions.insert(
            0, f"Import this system's bpool, then run again: sudo zpool import {own[0]}"
        )
    elif own:
        solutions.insert(0, "More than one bpool on rpool's disk: import the right one by id")
    elif not unknown:
        causes.append("rpool's disk holds no bpool: this system has none of its own")
        solutions.append("zark needs Ubuntu's ZFS layout, with bpool on rpool's disk")
    log.fatal("bpool is not imported", causes=causes, solutions=solutions)


@dataclass
class Outcome:
    """Result of one dataset's transfer."""

    rel: str
    ok: bool
    error: str = ""
    enospc: bool = False


@dataclass(frozen=True)
class Verdict:
    """Whether one snapshot or bookmark in origin may be destroyed, and why."""

    target: str  # "ds@snap" or "ds#bookmark"
    drop: bool
    reason: str
    owner: str = ""  # a point left by another drive's run: anchor it for that drive first


@dataclass
class Run:  # pylint: disable=too-many-instance-attributes
    """One replication run of origin into backup pool ``pool``."""

    pool: str
    disk_guid: str
    log: Log
    point: str
    pools: list[str] = field(default_factory=source_pools)
    sleep: Callable[[float], None] = time.sleep
    origin: dict[str, Origin] = field(default_factory=dict)
    dest: dict[str, Dest] = field(default_factory=dict)
    started: set[str] = field(default_factory=set)  # datasets a receive began on
    _timer_was_active: bool = False

    # ── sanoid ───────────────────────────────────────────────────────────

    def pause_sanoid(self) -> bool:
        """Stop sanoid.timer and wait for a running take/prune. False on timeout.

        The flag that makes :meth:`resume_sanoid` start the timer again is
        set, and the marker written, before the timer is stopped, so any
        ending from here on (Ctrl-C in the wait included) restores it.
        """
        state = sh.run("systemctl is-active sanoid.timer").output.strip()
        marker = Path(SANOID_MARKER)
        left = state != "active" and marker.exists()
        self._timer_was_active = state == "active" or left
        if left:
            self.log.warn(
                "sanoid.timer was left stopped by an earlier zark run; "
                + "this run starts it again when it ends"
            )
        if state == "active":
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                _ = marker.write_text(self.point + "\n", encoding="ascii")
            except OSError as e:
                self.log.warn(f"Could not write {marker}: {e}")
            _ = sh.run("systemctl stop sanoid.timer", log=self.log)
        waited = 0
        while any(
            sh.run(f"systemctl is-active {u}").output.strip() in ("active", "activating")
            for u in SANOID_UNITS
        ):
            if waited >= SANOID_WAIT_SECONDS:
                return False
            if waited == 0:
                self.log.info(
                    f"Waiting for sanoid to finish (up to {SANOID_WAIT_SECONDS // 60} min)..."
                )
            self.sleep(5)
            waited += 5
        self.log.ok("sanoid paused" if self._timer_was_active else "sanoid timer not active")
        return True

    def resume_sanoid(self) -> None:
        """Start sanoid.timer again if this run stopped it (or found it left
        stopped by an earlier run); the marker goes once it is running."""
        if not self._timer_was_active:
            return
        self._timer_was_active = False
        if not sh.run("systemctl start sanoid.timer", log=self.log).ok:
            self.log.warn(f"Could not start sanoid.timer; {SANOID_MARKER} stays for the next run")
            return
        try:
            Path(SANOID_MARKER).unlink(missing_ok=True)
        except OSError as e:
            self.log.warn(f"Could not remove {SANOID_MARKER}: {e}")

    # ── point ────────────────────────────────────────────────────────────

    def take_point(self) -> bool:
        """Snapshot every expected dataset as ``self.point``, one call per pool (I-C)."""
        try:
            self.origin = read_origin(self.pools, strict=True)
        except ReadError as e:
            self.log.error(f"Cannot take {self.point}: {e}")
            return False
        wanted = expected(self.origin)
        for pool in self.pools:
            names = [rel for rel in wanted if rel == pool or rel.startswith(f"{pool}/")]
            if not names:
                continue
            r = sh.run(
                f"zfs snapshot -o {POINT_FOR}={_q(self.disk_guid)} "
                + " ".join(_q(f"{n}@{self.point}") for n in names),
                log=self.log,
            )
            if not r.ok:
                self.log.error(f"Cannot take {self.point} on {pool}: {r.stderr.strip()}")
                return False
        self.log.ok(f"Backup point {self.point} taken ({len(wanted)} datasets)")
        return True

    def survey(self) -> list[DatasetPlan]:
        """Read both sides and plan every dataset.

        Strict: what origin "has" decides what is expected and what the drive
        holds only for itself, so a listing that fails raises
        :class:`ReadError` rather than shrinking either side. So does a pool
        tree the drive holds whose origin pool is not imported.
        """
        self.origin = read_origin(self.pools, strict=True)
        self.dest = read_dest(self.pool, strict=True)
        for pool in ("rpool", "bpool"):
            held = any(rel == pool or rel.startswith(f"{pool}/") for rel in self.dest)
            if held and pool not in self.pools:
                raise ReadError(
                    f"{pool} is not imported, and {self.pool} holds its backup "
                    + f"(import it first: sudo zpool import {pool})"
                )
        return plan(self.origin, self.dest, self.point)

    # ── sizes ────────────────────────────────────────────────────────────

    def _raw(self, rel: str) -> str:
        return "" if is_bpool(rel) else "-w "

    def _send_args(self, p: DatasetPlan, full: bool = False) -> list[str]:
        """``zfs send`` argument strings (one per stream) for a plan."""
        rel, pt = p.rel, f"{p.rel}@{self.point}"
        raw = self._raw(rel)
        if p.state is State.RESUME:
            return [f"-t {_q(self.dest[rel].token)}"]
        if p.state is State.NEW and p.clone_from and not full:
            # -I from the clone's own origin, named in full (fromorigin,
            # cmd/zfs/zfs_main.c:5101-5114).
            return [f"{raw}-I {_q(p.base)} {_q(pt)}"]
        if full or p.state in (State.NEW, State.DIVERGED):
            return [f"{raw}{_q(pt)}"]
        if p.state is State.VIA_BOOKMARK or p.base.startswith("#"):
            first = f"{rel}{p.first}" if p.first else self._after_bookmark(rel, p.base[1:])
            out = [f"-w -i {_q(rel + p.base)} {_q(first)}"]
            if first != pt:
                out.append(f"-w -I {_q(first)} {_q(pt)}")
            return out
        if p.state in (State.INCREMENTAL, State.ROLLBACK, State.MODIFIED):
            return [f"{raw}-I {_q(rel + p.base)} {_q(pt)}"] if p.base else []
        return []

    def _after_bookmark(self, rel: str, bookmark: str) -> str:
        """First origin snapshot after a bookmark (the point at worst)."""
        o = self.origin[rel]
        bm = next((b for b in o.bookmarks if b.name == bookmark), None)
        later = [s for s in o.snaps if bm is None or s.createtxg > bm.createtxg]
        return f"{rel}@{later[0].name}" if later else f"{rel}@{self.point}"

    def estimate(self, p: DatasetPlan, full: bool = False) -> int:
        """Bytes a transfer would send (``zfs send -nvP``); 0 when unknown."""
        args_list = self._send_args(p, full)
        if p.state is State.NEW and p.clone_from and not full:
            # M3 decision 7a: `-nvP -I <origin> <clone>@<point>` reports the
            # clone's full size: with no previous snapshot zfs_send_space gets
            # no `from` and fromorigin is ignored (OpenZFS 2.4.1,
            # lib/libzfs/libzfs_sendrecv.c:1237-1255), while the real send
            # passes it (dump_ioctl, :1282). `-i` goes through zfs_send_one,
            # which estimates from the origin (cmd/zfs/zfs_main.c:5026-5063).
            # The send itself stays `-I` (_send_args).
            args_list = [f"{self._raw(p.rel)}-i {_q(p.base)} {_q(f'{p.rel}@{self.point}')}"]
        total = 0
        for args in args_list:
            r = sh.run(f"zfs send -nvP {args}")
            for line in (r.stdout + "\n" + r.stderr).splitlines():
                fields = line.split("\t")
                if len(fields) == 2 and fields[0] == "size" and fields[1].isdigit():
                    total += int(fields[1])
        return total

    # ── decisions (each one asked to the user by the caller) ─────────────

    def _dst(self, rel: str) -> str:
        return f"{self.pool}/{rel}"

    def rollback(self, p: DatasetPlan) -> bool:
        """ROLLBACK option b: ``zfs rollback -r`` to the newest common snapshot."""
        r = sh.run(f"zfs rollback -r {_q(self._dst(p.rel) + p.common)}", log=self.log)
        self._report(r, f"rolled back {p.rel} to {p.common}, destroying {len(p.newer)} snapshot(s)")
        return r.ok

    def discard_changes(self, p: DatasetPlan) -> bool:
        """MODIFIED, when chosen: ``zfs rollback`` to the newest snapshot,
        without ``-r`` (no snapshot is destroyed; works without the keys,
        checked on eli). A dataset that also holds partial receive state
        (M2V-1) loses it first with ``zfs receive -A``, which destroys only
        ``<ds>/%recv`` (cmd/zfs/zfs_main.c:5262-5301), so the rollback never
        meets that clone."""
        if p.partial:
            if not self.abort_partial(p.rel):
                self.log.error(
                    f"  {p.rel}: could not discard the partial transfer; nothing changed"
                )
                return False
            self.log.ok(f"Discarded the partial transfer of {p.rel} on the backup")
        r = sh.run(f"zfs rollback {_q(self._dst(p.rel) + p.common)}", log=self.log)
        self._report(
            r,
            f"restored {p.rel} on the backup to {p.common} "
            + "(undid what a read-write mount changed; no snapshot destroyed)",
        )
        return r.ok

    def archive(self, p: DatasetPlan, now: datetime | None = None) -> str:
        """Rename the destination lineage aside; return its new name ("" on failure)."""
        new = archive_name(p.rel, set(self.dest), now or datetime.now(UTC))
        r = sh.run(f"zfs rename {_q(self._dst(p.rel))} {_q(self._dst(new))}", log=self.log)
        self._report(r, f"archived {p.rel} as {new}")
        if r.ok:
            self.dest[new] = self.dest.pop(p.rel, Dest(new))
        return new if r.ok else ""

    def branch(self, p: DatasetPlan, now: datetime | None = None) -> bool:
        """ROLLBACK option a (M3R-1, M4b-5): keep the drive's newer snapshots
        in an archive and continue from the common one, destroying nothing
        and resending nothing.

        The lineage is renamed aside, ``zfs clone -u`` of its common snapshot
        takes its place and ``zfs promote`` gives it the snapshots up to that
        one, so the archive keeps only the newer ones, as a clone of
        ``<ds>@<common>``. Promote is allowed with the keys unloaded when the
        archive is its own encryption root, as every raw-received lineage is
        (zfs-2.4.1 module/zfs/dsl_crypt.c:1695-1720, 1745-1791; staged on eli,
        M4b-5). The children, which the rename took along, go back under the
        live dataset. The next survey plans an incremental from the common
        snapshot (raw: it is the live dataset's newest).
        """
        children = sorted(rel for rel in self.dest if parent(rel) == p.rel)
        new = self.archive(p, now)
        if not new:
            return False
        live, kept = self._dst(p.rel), self._dst(new)
        for cmd in (f"zfs clone -u {_q(kept + p.common)} {_q(live)}", f"zfs promote {_q(live)}"):
            r = sh.run(cmd, log=self.log)
            if not r.ok:
                self.log.error(f"  {p.rel}: {cmd} failed: {r.stderr.strip()}")
                return False
        for child in children:
            moved = new + child[len(p.rel) :]
            r = sh.run(f"zfs rename {_q(self._dst(moved))} {_q(self._dst(child))}", log=self.log)
            if not r.ok:
                self.log.error(f"  {child}: could not move it back from {new}: {r.stderr.strip()}")
                return False
        self.log.ok(
            f"Kept the {len(p.newer)} newer snapshot(s) of {p.rel} in {new}; "
            + f"{p.rel} continues from {p.common.lstrip('@')}"
        )
        return True

    def destroy(self, p: DatasetPlan) -> bool:
        """Destroy a destination tree (DIVERGED, ORPHAN, ROLLBACK when chosen)."""
        r = sh.run(f"zfs destroy -r {_q(self._dst(p.rel))}", log=self.log)
        self._report(r, f"destroyed {self._dst(p.rel)} on the backup")
        return r.ok

    def keep_orphan(self, p: DatasetPlan, stamp: str) -> bool:
        """Remember that the user keeps this orphan (not asked again)."""
        r = sh.run(f"zfs set {_ORPHAN}={_q('kept@' + stamp)} {_q(self._dst(p.rel))}", log=self.log)
        self._report(r, f"keeping {p.rel} (only on backup)")
        return r.ok

    def clear_orphan_marks(self) -> list[str]:
        """Drop the "kept" mark of datasets origin has again; return them.

        A kept orphan that reappears in origin (a recover of an earlier
        point) is replicated again; left marked, a later removal in origin
        would be kept without asking.
        """
        cleared: list[str] = []
        for rel, d in sorted(self.dest.items()):
            if not d.orphan or rel not in self.origin:
                continue
            r = sh.run(f"zfs inherit {_ORPHAN} {_q(self._dst(rel))}", log=self.log)
            if r.ok:
                d.orphan = ""
                cleared.append(rel)
                self.log.info(f"  {rel}: back in origin, no longer kept as an orphan")
            else:
                self.log.warn(f"  {rel}: could not clear {_ORPHAN}: {r.stderr.strip()}")
        return cleared

    def follow_rename(self, p: DatasetPlan) -> bool:
        """Rename an orphan to the name its lineage now has in origin."""
        r = sh.run(
            f"zfs rename {_q(self._dst(p.rel))} {_q(self._dst(p.rename_to))}",
            log=self.log,
        )
        self._report(r, f"renamed {p.rel} → {p.rename_to} on the backup")
        return r.ok

    def ensure_containers(self) -> list[str]:
        """Create the structural containers missing on the destination
        (``canmount=off``, ``mountpoint=none``, unencrypted: a raw receive
        below them is allowed, module/zfs/dmu_recv.c:723-746). Set at create
        time, as recover does, never with ``zfs set``. Returns the failures."""
        failed: list[str] = []
        for rel in STRUCTURAL:  # parents first
            if rel not in self.origin or rel in self.dest:
                continue
            if parent(rel) and parent(rel) not in self.dest:
                failed.append(rel)
                continue
            r = sh.run(
                f"zfs create -o canmount=off -o mountpoint=none {_q(self._dst(rel))}",
                log=self.log,
            )
            self._report(r, f"created container {self._dst(rel)}")
            if r.ok:
                self.dest[rel] = Dest(rel)
            else:
                failed.append(rel)
        return failed

    def _report(self, r: sh.RunResult, what: str) -> None:
        if r.ok:
            # Only the first letter: capitalize() would lowercase the dataset names.
            self.log.ok(what[:1].upper() + what[1:])
        else:
            self.log.error(f"Failed: {what}: {r.stderr.strip()}")

    # ── transfer ─────────────────────────────────────────────────────────

    def _recv(self, rel: str, resume: bool, clone_from: str = "") -> str:
        dst = _q(self._dst(rel))
        if resume:
            return f"zfs receive -s -u {dst}"
        o = self.origin[rel]
        # A clone of the drive's copy of its origin; the kernel refuses an
        # origin whose guid is not the stream's (ENODEV, dmu_recv.c:791-796).
        clone = [f"-o origin={_q(self._dst(clone_from))}"] if clone_from else []
        props = [
            *clone,
            "-o canmount=noauto",
            "-x mountpoint",
            f"-o org.zark:canmount={_q(o.canmount)}",
            f"-o org.zark:mountpoint={_q(o.mountpoint)}",
        ]
        return f"zfs receive -s -u {' '.join(props)} {dst}"

    def transfer(self, p: DatasetPlan) -> Outcome:
        """Bring one transferable dataset to the point (RESUME brings it to
        the partial snapshot only; survey again and transfer the rest)."""
        if p.state is State.AT_POINT:
            return Outcome(p.rel, True)
        if p.state not in TRANSFERABLE:
            return Outcome(p.rel, False, f"not transferable ({p.state})")
        resume = p.state is State.RESUME
        self.started.add(p.rel)
        clone_from = p.clone_from if p.state is State.NEW else ""
        for args in self._send_args(p):
            r = sh.run_pipe(f"zfs send {args}", self._recv(p.rel, resume, clone_from), log=self.log)
            if not r.ok:
                err = pipe_message(r) or f"rc={r.returncode}"
                enospc = sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout)
                return Outcome(p.rel, False, err, enospc)
        return Outcome(p.rel, True)

    def resume_impossible(self, rel: str) -> str:
        """Why a partial receive can never be resumed, "" when it may be.

        libzfs refuses a resume whose stream snapshot or incremental source
        no longer exists before any stream starts, dry run included
        (lib/libzfs/libzfs_sendrecv.c:1857-1890); any other failure (I/O, a
        USB reset) leaves the partial state worth keeping.
        """
        d = self.dest.get(rel)
        if d is None or not d.token:
            return ""
        r = sh.run(f"zfs send -nvP -t {_q(d.token)}")
        if r.ok:
            return ""
        msg = last_message(r.stderr)
        gone = ("no longer exists", "is no longer the same snapshot")
        return msg if any(g in msg for g in gone) else ""

    def discard_stale_partials(self, plans: list[DatasetPlan]) -> list[str]:
        """Before any question: discard the partial receives that can never
        resume (:meth:`resume_impossible`); return their datasets.

        ``receive -A`` touches no snapshot and the state is of no use, so
        nothing is lost even when the user then aborts. Found only at
        transfer time, the dataset's new state (often ROLLBACK) would wait
        for another run (seen on eli after a recover, N7).
        """
        gone: list[str] = []
        for p in plans:
            if p.state is not State.RESUME:
                continue
            why = self.resume_impossible(p.rel)
            if why and self.abort_partial(p.rel):
                self.log.warn(f"  {p.rel}: partial receive discarded: {why}")
                gone.append(p.rel)
        return gone

    def abort_partial(self, rel: str) -> bool:
        """``zfs receive -A``: discard partial receive state (no snapshot is touched)."""
        r = sh.run(f"zfs receive -A {_q(self._dst(rel))}", log=self.log)
        return r.ok

    # ── after the transfers ──────────────────────────────────────────────

    def anchor(self, at_point: list[str]) -> list[str]:
        """Anchor every dataset that reached the point; return the failures.

        A failed anchor is tried once more (M3R-5); one that still fails is
        retried by :meth:`anchor_held` at the end of every later run of this
        drive. Older anchors are not removed here: :meth:`drop_check` decides.
        """
        failed: list[str] = []
        for rel in at_point:
            if rel not in self.origin:
                continue
            if not _anchor_point(rel, self.point, self.disk_guid, self.log):
                self.sleep(ANCHOR_RETRY_SECONDS)
                if not _anchor_point(rel, self.point, self.disk_guid, self.log):
                    failed.append(rel)
        return failed

    def anchor_held(self) -> list[str]:
        """Bookmark, as this drive's anchor, the newest snapshot the drive
        holds of every rpool dataset when origin still has it and no bookmark
        of this drive does; return the datasets anchored (M4b-4, 8b).

        Run at the end of every run, normal or not: the datasets that reached
        the point of an interrupted run, a ``-I`` stream cut after a carried
        snapshot (M2R-2 b), an anchor that failed before (M3R-5, M2P.7). bpool
        cannot take bookmarks (I-F): a cut there still needs ROLLBACK.
        Nothing is done when either side cannot be read.
        """
        try:
            origin = read_origin(self.pools, strict=True)
            dest = read_dest(self.pool, strict=True)
        except ReadError as e:
            self.log.warn(f"Anchors not checked: {e}")
            return []
        done: list[str] = []
        for rel in expected(origin):
            d, o = dest.get(rel), origin[rel]
            newest = d.newest if d is not None else None
            if is_bpool(rel) or newest is None or o.snap_by_guid(newest.guid) is None:
                continue
            snap = o.snap_by_guid(newest.guid)
            assert snap is not None
            # Named after the moment it marks, not this run's point: only the
            # point's own anchor lets drop_check supersede the older ones.
            name = anchor_name(
                self.disk_guid, point_name(datetime.fromtimestamp(snap.creation, UTC))
            )
            mine = [b for b in o.bookmarks if anchor_disk(b.name) == self.disk_guid]
            if any(b.guid == newest.guid for b in mine) or any(b.name == name for b in mine):
                continue
            r = sh.run(
                f"zfs bookmark {_q(f'{rel}@{snap.name}')} {_q(f'{rel}#{name}')}", log=self.log
            )
            if r.ok:
                self.log.info(f"  {rel}: anchored {snap.name} for {self.pool} (its newest there)")
                done.append(rel)
            else:
                self.log.warn(f"  {rel}: could not anchor {snap.name}: {r.stderr.strip()}")
        return done

    def shared_with_origin(self) -> dict[str, list[Ref]] | None:
        """What the drive shares with origin now, besides its anchors (M3
        decision 5e); None when either side cannot be read."""
        try:
            origin = read_origin(self.pools, strict=True)
            dest = read_dest(self.pool, strict=True)
        except ReadError as e:
            self.log.warn(f"Could not read what {self.pool} shares with origin: {e}")
            return None
        return shared_snapshots(origin, dest)

    def drop_check(self) -> list[Verdict]:  # pylint: disable=too-many-locals,too-many-branches
        """Decide, against a fresh read of both sides, what may go from origin.

        Candidates are this run's point, points left by earlier runs and this
        disk's older anchors; another disk's anchors are never candidates.
        Nothing goes when either side cannot be read.
        """
        try:
            origin = read_origin(self.pools, strict=True)
            dest = read_dest(self.pool, strict=True)
            owners = point_owners(self.pools)
        except ReadError as e:
            self.log.warn(f"Nothing removed in origin: {e}")
            return []
        gone = self._gone_from(dest)
        if gone:
            # The drive changed under the run: no verdict can rest on it.
            self.log.warn(f"Nothing removed in origin: {self.pool}/{gone} is no longer listed")
            return []
        wanted = set(expected(origin))
        keep = anchor_name(self.disk_guid, self.point)
        verdicts: list[Verdict] = []
        for rel, o in sorted(origin.items()):
            d = dest.get(rel)
            on_backup = {s.guid for s in d.snaps} if d else set()
            sep = "@" if is_bpool(rel) else "#"
            new = o.snaps if is_bpool(rel) else o.bookmarks
            anchor = next((a for a in new if a.name == keep), None)
            point_snap = next((s for s in o.snaps if s.name == self.point), None)
            # Verified on the backup, by guid, and anchored.
            verified = anchor is not None and anchor.guid in on_backup
            partial = d is not None and bool(d.token)
            for s in o.snaps:
                if not POINT_RE.match(s.name):
                    continue
                target = f"{rel}@{s.name}"
                if rel not in wanted:
                    verdicts.append(Verdict(target, True, "dataset not replicated"))
                elif partial:
                    verdicts.append(Verdict(target, False, "partial receive to resume"))
                elif s is point_snap and s.guid not in on_backup:
                    verdicts.append(Verdict(target, True, "not on the backup, nothing to resume"))
                elif verified:
                    owner = owners.get(target, "")
                    other = owner if owner != self.disk_guid and s is not point_snap else ""
                    verdicts.append(
                        Verdict(target, True, f"on the backup, anchored as {keep}", other)
                    )
                else:
                    verdicts.append(Verdict(target, False, "may be the base of the next run"))
            for name in old_anchors([a.name for a in new], self.disk_guid, keep):
                target = f"{rel}{sep}{name}"
                if verified:
                    verdicts.append(Verdict(target, True, f"superseded by {keep}"))
                else:
                    verdicts.append(Verdict(target, False, "new anchor not verified"))
        return verdicts

    def _gone_from(self, dest: dict[str, Dest]) -> str:
        """First dataset of the last survey that a fresh read of the drive lacks."""
        return next((rel for rel in sorted(self.dest) if rel not in dest), "")

    def drop(self, verdicts: list[Verdict]) -> int:
        """Destroy what :meth:`drop_check` allowed, one by one by exact name (F5)."""
        dropped = 0
        for v in verdicts:
            if not v.drop:
                self.log.info(f"  kept {v.target}: {v.reason}")
                continue
            if v.owner:
                # A point another drive's run left (no cleanup ran): anchored
                # for that drive first, so its removal costs it no rollback.
                dropped += release_point(v.target, v.owner, self.log)
                continue
            r = sh.run(f"zfs destroy {_q(v.target)}", log=self.log)
            if r.ok:
                self.log.info(f"  removed {v.target}: {v.reason}")
                dropped += 1
            else:
                self.log.warn(f"  could not remove {v.target}: {r.stderr.strip()}")
        return dropped

    def undo_point(self) -> None:
        """Cleanup action for a run that did not end normally (8b, M4b-4).

        The backup pool is still imported here. Every dataset the drive holds
        at the point is anchored, as at the end of a normal run, and so is the
        newest snapshot the drive holds of every other rpool dataset
        (:meth:`anchor_held`). The point then goes wherever no partial receive
        needs it (the next run resumes one), or wherever the drive cannot be
        read and no receive began (the rule before 8b).
        """
        try:
            dest = read_dest(self.pool, strict=True)
        except ReadError as e:
            self.log.warn(f"Anchors not checked after the interruption: {e}")
            dest = None
        at_point: list[str] = []
        partial: set[str] = set()
        if dest is not None:
            for rel in expected(self.origin):
                d = dest.get(rel)
                own = next((s for s in self.origin[rel].snaps if s.name == self.point), None)
                if d is not None and d.token:
                    partial.add(rel)
                elif d is not None and own is not None and any(x.guid == own.guid for x in d.snaps):
                    at_point.append(rel)
            # Anchored (renamed on bpool): no longer a point to remove.
            renamed = {rel for rel in at_point if is_bpool(rel)}
            failed = set(self.anchor(at_point))
            _ = self.anchor_held()
            keep = partial | {rel for rel in failed if not is_bpool(rel)} | (renamed & failed)
            gone = renamed - failed
        else:
            keep, gone = set(self.started), set()
        for rel in expected(self.origin):
            if rel in keep or rel in gone:
                continue
            _ = sh.run(f"zfs destroy {_q(f'{rel}@{self.point}')}")
        self.resume_sanoid()


def _anchor_point(rel: str, point: str, disk_guid: str, log: Log) -> bool:
    """Make ``rel@point`` drive ``disk_guid``'s anchor: a bookmark on rpool;
    on bpool, where I-F forbids bookmarks, the snapshot renamed."""
    keep = anchor_name(disk_guid, point)
    if is_bpool(rel):
        cmd = f"zfs rename {_q(f'{rel}@{point}')} {_q(f'{rel}@{keep}')}"
        return sh.run(cmd, log=log).ok
    cmd = f"zfs bookmark {_q(f'{rel}@{point}')} {_q(f'{rel}#{keep}')}"
    if sh.run(cmd, log=log).ok:
        return True
    # A run stopped between its bookmark and the destroy (power loss) left
    # both: the anchor is there already when the guids match.
    r = sh.run(f"zfs get -Hp -o value guid {_q(f'{rel}@{point}')} {_q(f'{rel}#{keep}')}", log=log)
    return r.ok and len(r.lines) == 2 and r.lines[0] == r.lines[1]


def point_owners(pools: list[str]) -> dict[str, str]:
    """``rel@point`` → the pool GUID of the drive its run was for
    (:data:`POINT_FOR`), read from the snapshots themselves; strict
    (:class:`ReadError` when a listing fails). Points taken before rc5 carry
    no owner and are not listed."""
    out: dict[str, str] = {}
    for pool in pools:
        for name, value in snapshot_marks(pool, strict=True, prop=POINT_FOR).items():
            # A bpool point renamed to an anchor keeps the property: not a point.
            if value.isdigit() and POINT_RE.match(name.partition("@")[2]):
                out[name] = value
    return out


def release_point(target: str, owner: str, log: Log) -> bool:
    """Take a point a run left in origin out of the way for drive ``owner``
    (8b, M4b-4): on rpool it is bookmarked as that drive's anchor, then
    destroyed; on bpool it is renamed to that drive's anchor. True when the
    point is gone; when the anchor cannot be made the point stays."""
    rel, _, point = target.partition("@")
    if not _anchor_point(rel, point, owner, log):
        log.warn(f"  kept {target}: could not anchor it for drive {owner}")
        return False
    if is_bpool(rel):
        log.info(f"  {target}: renamed to drive {owner}'s anchor")
        return True
    r = sh.run(f"zfs destroy {_q(target)}", log=log)
    if r.ok:
        log.info(f"  removed {target}: anchored for drive {owner} first")
    else:
        log.warn(f"  could not remove {target}: {r.stderr.strip()}")
    return r.ok


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
    r = _checked(sh.run(f"zfs get -H -s local -o property,value all {_q(pool)}"), strict)
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


def log_metadata(pool: str, log: Log) -> dict[str, str]:
    """Show who wrote this drive and when (decision 17a); return the values."""
    meta = read_metadata(pool)
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
    r = _checked(
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
        r = _checked(
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


# ── destination retention (M3) ───────────────────────────────────────────


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
        r = _checked(
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


def _under_any(rel: str, tops: list[str]) -> bool:
    return any(rel == t or rel.startswith(f"{t}/") for t in tops)


def reclaim_names(names: list[str]) -> int:
    """Bytes destroying these snapshots (full ``ds@snap`` names) would free,
    measured by ZFS: one dry run per dataset over the exact names, comma
    separated (``zfs destroy -nvp ds@a,b,c``, cmd/zfs/zfs_main.c:1756-1779;
    only a ``%`` makes a range, lib/libzfs/libzfs_iter.c:429-460). 0 for
    what cannot be measured."""
    by_ds: dict[str, list[str]] = {}
    for full in names:
        ds, _, snap = full.partition("@")
        by_ds.setdefault(ds, []).append(snap)
    total = 0
    for ds, snaps in sorted(by_ds.items()):
        spec = ",".join(dict.fromkeys(snaps))
        out = sh.run(f"zfs destroy -nvp {_q(f'{ds}@{spec}')}")
        for line in out.lines if out.ok else []:
            f = line.split("\t")
            if len(f) == 2 and f[0] == "reclaim" and f[1].isdigit():
                total += int(f[1])
    return total


def reclaim(pool: str, removals: list[Removal], tops: list[str] | None = None) -> int:
    """Bytes the removals on drive ``pool`` would free (:func:`reclaim_names`),
    plus ``used`` of each subtree destroyed whole."""
    tops = tops or []
    total = reclaim_names([f"{pool}/{r.target}" for r in removals if not _under_any(r.rel, tops)])
    for top in tops:
        used = sh.run(f"zfs list -Hp -o used {_q(f'{pool}/{top}')}")
        if used.ok and used.output.strip().isdigit():
            total += int(used.output.strip())
    return total


def destroy_snapshots(targets: list[tuple[str, str]], log: Log) -> tuple[int, list[str]]:
    """Destroy ``(ds@snap, reason)`` by exact name, each logged with its
    reason; returns how many went and what could not (M3P.26, M4b-10).

    The snapshots of one dataset go in one ``zfs destroy ds@a,b,c`` of at
    most :data:`DESTROY_GROUP` names: the kernel checks them all and destroys
    none when one fails (zfs-2.4.1 module/zfs/dsl_destroy.c:589-601), and the
    names of a failed group are then destroyed one by one, so what is kept and
    what failed stay exact. Never a ``%`` range (redesign §7).
    """
    count, failed = 0, []
    groups: dict[str, list[tuple[str, str]]] = {}
    singles: list[tuple[str, str]] = []
    for target, reason in targets:
        ds, sep, name = target.partition("@")
        if sep and "#" not in target:
            groups.setdefault(ds, []).append((name, reason))
        else:
            singles.append((target, reason))
    for ds, items in groups.items():
        for i in range(0, len(items), DESTROY_GROUP):
            chunk = items[i : i + DESTROY_GROUP]
            if len(chunk) == 1:
                singles.append((f"{ds}@{chunk[0][0]}", chunk[0][1]))
                continue
            for name, reason in chunk:
                log.file(f"  to remove {ds}@{name}: {reason}")
            res = sh.run(f"zfs destroy {_q(ds + '@' + ','.join(n for n, _ in chunk))}", log=log)
            if res.ok:
                count += len(chunk)
                for name, reason in chunk:
                    log.info(f"  removed {ds}@{name}: {reason}")
                continue
            log.file(f"  {ds}: {len(chunk)} snapshot(s) not removed together, one by one now")
            singles += [(f"{ds}@{name}", reason) for name, reason in chunk]
    for target, reason in singles:
        res = sh.run(f"zfs destroy {_q(target)}", log=log)
        if res.ok:
            count += 1
            log.info(f"  removed {target}: {reason}")
        else:
            failed.append(target)
            log.warn(f"  could not remove {target}: {res.stderr.strip()}")
    return count, failed


def subtree_datasets(top: str) -> list[tuple[str, bool]] | None:
    """The datasets at and below ``top`` from the drive's own listing, each
    with whether it holds a partial receive (its ``%recv`` child never lists:
    module/zcommon/zfs_comutil.c:232-244); None when the listing fails."""
    r = sh.run(f"zfs list -H -o name,receive_resume_token -t filesystem,volume -r {_q(top)}")
    if not r.ok:
        return None
    out: list[tuple[str, bool]] = []
    for line in r.lines:
        f = line.split("\t")
        if len(f) == 2:
            out.append((f[0], f[1] not in ("", "-")))
    return out


def apply_prune(pool: str, prune: Prune, log: Log) -> tuple[int, list[str]]:
    """Destroy what the plan allows, each snapshot by its exact name, grouped
    per dataset (:func:`destroy_snapshots`), every one logged with its reason
    (never a % range, redesign §7); a subtree destroyed whole names every snapshot and every dataset
    it takes first, from the drive's own listing, and is left alone when
    that listing fails (M3R-11, M3F-8 8a). Returns how many went and what
    could not be destroyed."""
    tops = [t for t, _ in prune.datasets]
    count, failed = destroy_snapshots(
        [(f"{pool}/{r.target}", r.reason) for r in prune.snapshots if not _under_any(r.rel, tops)],
        log,
    )
    for top, why in prune.datasets:
        below = subtree_datasets(f"{pool}/{top}")
        if below is None:
            failed.append(f"{pool}/{top}")
            log.warn(f"  could not remove {pool}/{top}: its datasets could not be listed")
            continue
        planned = {f"{pool}/{r.rel}" for r in prune.snapshots if _under_any(r.rel, [top])}
        for r in prune.snapshots:
            if _under_any(r.rel, [top]):
                log.file(f"  {pool}/{r.target} goes with {pool}/{top}")
        for ds, partial in below:
            if ds not in planned:
                log.file(f"  {ds} (no snapshot) goes with {pool}/{top}")
            if partial:
                log.file(f"  {ds} and its partial receive go with {pool}/{top}")
        res = sh.run(f"zfs destroy -r {_q(f'{pool}/{top}')}", log=log)
        if res.ok:
            count += sum(1 for r in prune.snapshots if _under_any(r.rel, [top]))
            log.info(f"  removed {pool}/{top} and everything below it: {why}")
        else:
            failed.append(f"{pool}/{top}")
            log.warn(f"  could not remove {pool}/{top}: {res.stderr.strip()}")
    return count, failed


def anchored_disks(origin: dict[str, Origin]) -> set[str]:
    """Pool GUIDs that hold anchors in origin (bookmarks or bpool snapshots)."""
    disks: set[str] = set()
    for o in origin.values():
        for ref in [*o.bookmarks, *o.snaps]:
            g = anchor_disk(ref.name)
            if g:
                disks.add(g)
    return disks


def disk_anchors(origin: dict[str, Origin], disk_guid: str) -> list[str]:
    """Every anchor of one disk in origin, as ``ds#bookmark`` / ``ds@snapshot``."""
    names: list[str] = []
    for rel, o in sorted(origin.items()):
        names += [f"{rel}#{b.name}" for b in o.bookmarks if anchor_disk(b.name) == disk_guid]
        names += [f"{rel}@{s.name}" for s in o.snaps if anchor_disk(s.name) == disk_guid]
    return names


def drop_disk_anchors(disk_guid: str, pools: list[str], log: Log) -> tuple[int, list[str]]:
    """Remove every anchor of one disk from origin, by exact name; return how
    many went and the names that could not be destroyed (each one logged)."""
    count, failed = 0, []
    for name in disk_anchors(read_origin(pools), disk_guid):
        r = sh.run(f"zfs destroy {_q(name)}", log=log)
        if r.ok:
            count += 1
        else:
            failed.append(name)
            log.warn(f"  could not remove {name}: {r.stderr.strip()}")
    return count, failed


def drop_anchors_of(disk_guid: str, log: Log) -> str:
    """Drop a drive's anchors here when rpool is imported (purge, prepare;
    M2R-6). Returns what is left to do, "" when nothing."""
    pools = source_pools()
    retry = f"sudo zark registry forget {disk_guid}"
    if "rpool" not in pools:
        return f"Its anchors stay in origin: on the installed system run {retry}"
    count, failed = drop_disk_anchors(disk_guid, pools, log)
    if count:
        log.ok(f"{count} anchor(s) of drive {disk_guid} dropped from origin")
    if failed:
        return f"{len(failed)} anchor(s) of drive {disk_guid} could not be dropped: {retry}"
    return ""


def leftover_points(origin: dict[str, Origin]) -> list[str]:
    """Backup points left in origin by runs that did not end normally."""
    return [
        f"{rel}@{s.name}" for rel, o in sorted(origin.items()) for s in o.snaps if is_point(s.name)
    ]


def is_point(name: str) -> bool:
    """True for a backup point name."""
    return bool(POINT_RE.match(name))


def last_message(stderr: str) -> str:
    """The last error message of a zfs command, on one line.

    libzfs prints ``<action>: <reason>`` and some reasons span two lines
    ("destination %s has been modified\\nsince most recent snapshot",
    lib/libzfs/libzfs_sendrecv.c:5159-5174), so the message runs from the
    last line that starts with "cannot " to the end.
    """
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    starts = [i for i, ln in enumerate(lines) if ln.startswith("cannot ")]
    return " ".join(lines[starts[-1] :]) if starts else (lines[-1] if lines else "")


def pipe_message(r: sh.RunResult) -> str:
    """Both sides' messages of a failed ``zfs send | zfs receive``, send's first.

    A send that stops early leaves receive with only "failed to read from
    stream"; the reason is on the send side.
    """
    if not (r.stderr_left or r.stderr_right):
        return last_message(r.stderr)
    sides = (last_message(r.stderr_left), last_message(r.stderr_right))
    return "; ".join(m for m in sides if m)


def depth_order(plans: list[DatasetPlan]) -> list[DatasetPlan]:
    """Parents before children (a NEW child needs its parent on the backup)."""
    return sorted(plans, key=lambda p: (p.rel.count("/"), p.rel))


def blocked_by_parent(rel: str, failed: set[str]) -> bool:
    """True when an ancestor of ``rel`` failed in this run."""
    up = parent(rel)
    while up:
        if up in failed:
            return True
        up = parent(up)
    return False


@dataclass
class Result:
    """What a replication run achieved."""

    plans: list[DatasetPlan] = field(default_factory=list)  # final survey
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    anchor_failed: list[str] = field(default_factory=list)
    aborted: bool = False
    enospc: bool = False
    error: str = ""
    unverified: str = ""  # why the state after the transfers could not be read

    @property
    def ok(self) -> bool:
        """I-D: every expected dataset reached the point (and is anchored)."""
        return not self.aborted and not self.unverified and bool(self.plans) and reached(self.plans)


Decide = Callable[[list[DatasetPlan], "Run"], bool]

MAX_ROUNDS = 3


def replicate(run: Run, decide: Decide) -> Result:
    """Survey, let ``decide`` apply the user's decisions, transfer, verify,
    anchor and drop the points (steps 3–8 of the module docstring)."""
    res = Result()
    try:
        plans = run.survey()
        if run.discard_stale_partials(plans):
            # Planned afresh, so what they now need is asked with the rest.
            plans = run.survey()
    except ReadError as e:
        res.aborted, res.error = True, str(e)
        return res
    if not decide(plans, run):
        res.aborted = True
        return res
    try:
        _transfer(run, res)
        res.plans = run.survey()
    except ReadError as e:
        # After the user's choices were applied: the run is not a success,
        # and nothing in origin is anchored or removed on a guess.
        res.unverified = str(e)
        run.log.error(f"Cannot check the backup: {e}")
        return res
    at_point = [p.rel for p in res.plans if p.state is State.AT_POINT]
    res.anchor_failed = run.anchor(at_point)
    _ = run.anchor_held()
    _ = run.drop(run.drop_check())
    return res


def _transfer(run: Run, res: Result) -> None:
    """Every transfer round; ``res.plans`` holds the latest survey."""
    res.plans = run.survey()
    _ = run.clear_orphan_marks()
    for rel in run.ensure_containers():
        run.log.error(f"  {rel}: could not create the container on the backup")
    for _ in range(MAX_ROUNDS):
        res.plans = run.survey()
        pending = [p for p in depth_order(res.plans) if p.state in TRANSFERABLE]
        todo = [p for p in pending if p.state is not State.AT_POINT]
        if not todo:
            break
        failed: set[str] = set()
        again = False
        for p in todo:
            # Only a NEW child needs its parent on the drive (M2X.1, M4b-12).
            if p.state is State.NEW and blocked_by_parent(p.rel, failed):
                res.outcomes[p.rel] = Outcome(p.rel, False, "parent did not reach the point")
                failed.add(p.rel)
                continue
            run.log.info(f"  {p.rel}: {p.state}")
            out = run.transfer(p)
            res.outcomes[p.rel] = out
            if out.ok:
                again = again or p.state is State.RESUME
                continue
            failed.add(p.rel)
            run.log.error(f"  {p.rel}: {out.error}")
            if out.enospc:
                res.enospc = True
                break
            if p.state is State.RESUME:
                why = run.resume_impossible(p.rel)
                if why and run.abort_partial(p.rel):
                    # Only the partial state goes; the dataset is planned afresh.
                    run.log.warn(f"  {p.rel}: partial receive discarded: {why}")
                    again = True
                elif not why:
                    out.error += "; partial receive kept, run backup again"
                    run.log.info(f"  {p.rel}: partial receive kept for the next run")
        if res.enospc or not again:
            break


def execute(run: Run, cleanup: Cleanup, decide: Decide) -> Result:
    """The whole run: pause sanoid, take the point, :func:`replicate`, resume.

    Until it returns, ``cleanup`` holds undo actions, so an interrupted run
    drops its point where no receive began and resumes sanoid.
    """
    cleanup.track_action(run.resume_sanoid)
    try:
        if not run.pause_sanoid():
            return Result(aborted=True, error="sanoid is still running after the wait")
        # Tracked first: rpool's point is taken before bpool's, so a failure
        # or an interrupt between the two calls leaves rpool's to undo.
        cleanup.track_action(run.undo_point)
        if not run.take_point():
            cleanup.untrack_action(run.undo_point)
            run.undo_point()
            return Result(aborted=True, error=f"could not take {run.point}")
        res = replicate(run, decide)
        cleanup.untrack_action(run.undo_point)
        if res.aborted:
            run.undo_point()
        return res
    finally:
        cleanup.untrack_action(run.resume_sanoid)
        run.resume_sanoid()
