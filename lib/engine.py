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
zark's replication executor: the shell side of :mod:`lib.replication`.

One run against one backup pool (already imported under I-G by the caller):

  1. ``pause_sanoid`` - stop ``sanoid.timer`` and wait for a running take
     or prune, so nothing inside a ``-I`` range is pruned mid-send.
  2. ``take_point`` - ``zfs snapshot`` of every expected dataset, one call
     per pool (one txg each; a call cannot span pools).
  3. ``survey`` - read origin and destination, plan every dataset.
  4. Decisions the caller asked the user about: ``rollback``, ``archive``,
     ``destroy``, ``keep_orphan``, ``follow_rename``; then ``survey`` runs
     again, the "kept" mark of an orphan origin has again is cleared
     (``clear_orphan_marks``) and the structural containers are created on
     the destination where missing (``ensure_containers``).
  5. ``transfer`` for each transferable dataset, parents first:
     ``zfs send [-w] ... | zfs receive -s -u -o canmount=noauto -x mountpoint
     -o org.zark:canmount=... -o org.zark:mountpoint=... <dest>``, never ``-F``.
  6. ``survey`` again: success iff every expected dataset is at the point.
  7. ``anchor`` - per dataset at the point: a bookmark on rpool, the point
     renamed to the disk's anchor on bpool.
  8. ``drop_check`` / ``drop`` - every candidate for removal in origin (this
     run's point, points left by earlier runs, this disk's older anchors) is
     checked against a fresh read of both sides: it goes only when the
     dataset is verified on the backup at the new point, by guid, and its new
     anchor exists; anything that may still be a base stays. Every verdict is
     logged with its reason.

Every ZFS behaviour relied on here was checked in OpenZFS 2.4.1 (see the
M2 plan, §2, and the commit messages).
"""

import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lib import sh
from lib.anchors import POINT_FOR, anchor_point, point_owners, release_point
from lib.cleanup import Cleanup
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
from lib.survey import ORPHAN, ReadError, read_dest, read_origin, source_pools

SANOID_UNITS = ("sanoid.service", "sanoid-prune.service")
SANOID_WAIT_SECONDS = 600
# Written just before zark stops sanoid.timer, removed once it starts it
# again. /run is emptied at boot, when the enabled timer starts by itself,
# so a marker found with the timer inactive means a zark run that died
# without its cleanup left the timer stopped.
SANOID_MARKER = "/run/zark/sanoid-paused"
# One more try for an anchor that failed (M3R-5); later runs retry it too.
ANCHOR_RETRY_SECONDS = 2.0


def _q(word: str) -> str:
    return shlex.quote(word)


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
        # A clone gets a property set of its own (canmount is not inherited,
        # and defaults to on): the at-rest protection and the marks go in
        # with the clone, set by the kernel in the same ioctl as `receive -o`
        # (zfs_ioctl.c:3775-3781), never by `zfs set` (M4bR-4, M4bF-9 9a).
        o = self.origin[p.rel]
        marks = (
            f"-o canmount=noauto -o org.zark:canmount={_q(o.canmount)} "
            + f"-o org.zark:mountpoint={_q(o.mountpoint)}"
        )
        clone = f"zfs clone -u {marks} {_q(kept + p.common)} {_q(live)}"
        for cmd in (clone, f"zfs promote {_q(live)}"):
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
        r = sh.run(f"zfs set {ORPHAN}={_q('kept@' + stamp)} {_q(self._dst(p.rel))}", log=self.log)
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
            r = sh.run(f"zfs inherit {ORPHAN} {_q(self._dst(rel))}", log=self.log)
            if r.ok:
                d.orphan = ""
                cleared.append(rel)
                self.log.info(f"  {rel}: back in origin, no longer kept as an orphan")
            else:
                self.log.warn(f"  {rel}: could not clear {ORPHAN}: {r.stderr.strip()}")
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
            if not anchor_point(rel, self.point, self.disk_guid, self.log):
                self.sleep(ANCHOR_RETRY_SECONDS)
                if not anchor_point(rel, self.point, self.disk_guid, self.log):
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
                    if s is not point_snap and s.guid not in on_backup:
                        # e.g. a recovered system's points (M4c-12): this
                        # drive never held them, and no drive is named.
                        why = f"not on {self.pool}" + ("" if other else "; it names no drive")
                    else:
                        why = f"on the backup, anchored as {keep}"
                    verdicts.append(Verdict(target, True, why, other))
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

    def undo_point(self) -> None:  # pylint: disable=too-many-locals
        """Cleanup action for a run that did not end normally (8b, M4b-4).

        The backup pool is still imported here. Every dataset the drive holds
        at the point is anchored, as at the end of a normal run, and so is the
        newest snapshot the drive holds of every other rpool dataset
        (:meth:`anchor_held`). The point then goes wherever no partial receive
        needs it (the next run resumes one), or wherever the drive cannot be
        read and no receive began (the rule before 8b). Every removal is
        named and logged with its reason, and so is what stays (M4bR-1,
        M4bF-4 4a): after an interruption the log is what the user reads.
        """
        try:
            dest = read_dest(self.pool, strict=True)
        except ReadError as e:
            self.log.warn(f"Anchors not checked after the interruption: {e}")
            dest = None
        at_point: list[str] = []
        kept: dict[str, str] = {}  # rel -> why the point stays there
        if dest is not None:
            for rel in expected(self.origin):
                d = dest.get(rel)
                own = next((s for s in self.origin[rel].snaps if s.name == self.point), None)
                if d is not None and d.token:
                    kept[rel] = "partial receive to resume"
                elif d is not None and own is not None and any(x.guid == own.guid for x in d.snaps):
                    at_point.append(rel)
            # Anchored (renamed on bpool): no longer a point to remove.
            renamed = {rel for rel in at_point if is_bpool(rel)}
            failed = set(self.anchor(at_point))
            _ = self.anchor_held()
            kept.update((rel, f"could not anchor it for {self.pool}") for rel in failed)
            gone = renamed - failed
            why = {rel: f"anchored for {self.pool} first" for rel in at_point}
            other = "not on the backup, nothing to resume"
        else:
            kept = dict.fromkeys(self.started, "the drive could not be read and a receive began")
            gone = set()
            why = {}
            other = "the drive could not be read and no receive began"
        # Only where the point exists: a run that failed between rpool's
        # snapshot and bpool's has nothing to remove on bpool.
        try:
            origin = read_origin(self.pools, strict=True)
            present = {
                rel for rel, o in origin.items() if any(s.name == self.point for s in o.snaps)
            }
        except ReadError as e:
            self.log.warn(f"Could not list the points left in origin: {e}")
            present = None
        for rel in expected(self.origin):
            target = f"{rel}@{self.point}"
            if rel in gone or (present is not None and rel not in present):
                continue
            if rel in kept:
                self.log.info(f"  kept {target}: {kept[rel]}")
                continue
            r = sh.run(f"zfs destroy {_q(target)}", log=self.log)
            if r.ok:
                self.log.info(f"  removed {target}: {why.get(rel, other)}")
            else:
                self.log.warn(f"  could not remove {target}: {r.stderr.strip()}")
        self.resume_sanoid()


# ── destination retention (M3) ───────────────────────────────────────────


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
    anchor and drop the points (steps 3-8 of the module docstring)."""
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
