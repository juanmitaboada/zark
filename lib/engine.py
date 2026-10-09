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

import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lib import sh
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
    reached,
)

SANOID_UNITS = ("sanoid.service", "sanoid-prune.service")
SANOID_WAIT_SECONDS = 600


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
        raise ReadError(r.command or "zfs list")
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
            + f"-o name,type,canmount,mountpoint,referenced -r {pool}",
        )
        r = _checked(r, strict)
        for line in r.lines if r.ok else []:
            f = line.split("\t")
            if len(f) >= 5:
                ref = int(f[4]) if f[4].isdigit() else 0
                origin[f[0]] = Origin(f[0], f[1], f[2], f[3], ref)
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

    ``strict`` raises :class:`ReadError` when a listing of an existing root fails.
    """
    dest: dict[str, Dest] = {}
    strip = f"{pool}/"
    for root in (f"{pool}/rpool", f"{pool}/bpool"):
        if not sh.run(f"zfs list -H -o name {root}").ok:
            continue
        r = sh.run(
            "zfs list -Hp -t filesystem,volume "
            + f"-o name,type,receive_resume_token,{_ORPHAN},used -r {root}",
        )
        r = _checked(r, strict)
        for line in r.lines if r.ok else []:
            f = line.split("\t")
            if len(f) >= 5 and f[0].startswith(strip):
                rel = f[0][len(strip) :]
                used = int(f[4]) if f[4].isdigit() else 0
                token = "" if f[2] in ("-", "") else f[2]
                orphan = "" if f[3] in ("-", "") else f[3]
                dest[rel] = Dest(rel, kind=f[1], token=token, orphan=orphan, used=used)
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
        """Stop sanoid.timer and wait for a running take/prune. False on timeout."""
        state = sh.run("systemctl is-active sanoid.timer").output.strip()
        self._timer_was_active = state == "active"
        if self._timer_was_active:
            _ = sh.run("systemctl stop sanoid.timer", log=self.log)
        waited = 0
        while any(
            sh.run(f"systemctl is-active {u}").output.strip() in ("active", "activating")
            for u in SANOID_UNITS
        ):
            if waited >= SANOID_WAIT_SECONDS:
                return False
            self.sleep(5)
            waited += 5
        self.log.ok("sanoid paused" if self._timer_was_active else "sanoid timer not active")
        return True

    def resume_sanoid(self) -> None:
        """Start sanoid.timer again if this run stopped it."""
        if self._timer_was_active:
            _ = sh.run("systemctl start sanoid.timer", log=self.log)
            self._timer_was_active = False

    # ── point ────────────────────────────────────────────────────────────

    def take_point(self) -> bool:
        """Snapshot every expected dataset as ``self.point``, one call per pool (I-C)."""
        self.origin = read_origin(self.pools)
        wanted = expected(self.origin)
        for pool in self.pools:
            names = [rel for rel in wanted if rel == pool or rel.startswith(f"{pool}/")]
            if not names:
                continue
            r = sh.run(
                "zfs snapshot " + " ".join(_q(f"{n}@{self.point}") for n in names), log=self.log
            )
            if not r.ok:
                self.log.error(f"Cannot take {self.point} on {pool}: {r.stderr.strip()}")
                return False
        self.log.ok(f"Backup point {self.point} taken ({len(wanted)} datasets)")
        return True

    def survey(self) -> list[DatasetPlan]:
        """Read both sides and plan every dataset."""
        self.origin = read_origin(self.pools)
        self.dest = read_dest(self.pool)
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
        if full or p.state in (State.NEW, State.DIVERGED):
            return [f"{raw}{_q(pt)}"]
        if p.state is State.VIA_BOOKMARK or p.base.startswith("#"):
            first = f"{rel}{p.first}" if p.first else self._after_bookmark(rel, p.base[1:])
            out = [f"-w -i {_q(rel + p.base)} {_q(first)}"]
            if first != pt:
                out.append(f"-w -I {_q(first)} {_q(pt)}")
            return out
        if p.state in (State.INCREMENTAL, State.ROLLBACK):
            return [f"{raw}-I {_q(rel + p.base)} {_q(pt)}"]
        return []

    def _after_bookmark(self, rel: str, bookmark: str) -> str:
        """First origin snapshot after a bookmark (the point at worst)."""
        o = self.origin[rel]
        bm = next((b for b in o.bookmarks if b.name == bookmark), None)
        later = [s for s in o.snaps if bm is None or s.createtxg > bm.createtxg]
        return f"{rel}@{later[0].name}" if later else f"{rel}@{self.point}"

    def estimate(self, p: DatasetPlan, full: bool = False) -> int:
        """Bytes a transfer would send (``zfs send -nvP``); 0 when unknown."""
        total = 0
        for args in self._send_args(p, full):
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

    def archive(self, p: DatasetPlan, now: datetime | None = None) -> str:
        """Rename the destination lineage aside; return its new name ("" on failure)."""
        new = archive_name(p.rel, set(self.dest), now or datetime.now(UTC))
        r = sh.run(f"zfs rename {_q(self._dst(p.rel))} {_q(self._dst(new))}", log=self.log)
        self._report(r, f"archived {p.rel} as {new}")
        if r.ok:
            self.dest[new] = self.dest.pop(p.rel, Dest(new))
        return new if r.ok else ""

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
            self.log.ok(what.capitalize())
        else:
            self.log.error(f"Failed: {what}: {r.stderr.strip()}")

    # ── transfer ─────────────────────────────────────────────────────────

    def _recv(self, rel: str, resume: bool) -> str:
        dst = _q(self._dst(rel))
        if resume:
            return f"zfs receive -s -u {dst}"
        o = self.origin[rel]
        props = [
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
        for args in self._send_args(p):
            r = sh.run_pipe(f"zfs send {args}", self._recv(p.rel, resume), log=self.log)
            if not r.ok:
                err = (
                    r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"rc={r.returncode}"
                )
                enospc = sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout)
                return Outcome(p.rel, False, err, enospc)
        return Outcome(p.rel, True)

    def abort_partial(self, rel: str) -> bool:
        """``zfs receive -A``: discard partial receive state (no snapshot is touched)."""
        r = sh.run(f"zfs receive -A {_q(self._dst(rel))}", log=self.log)
        return r.ok

    # ── after the transfers ──────────────────────────────────────────────

    def anchor(self, at_point: list[str]) -> list[str]:
        """Anchor every dataset that reached the point; return the failures.

        Older anchors are not removed here: :meth:`drop_check` decides.
        """
        failed: list[str] = []
        keep = anchor_name(self.disk_guid, self.point)
        for rel in at_point:
            if rel not in self.origin:
                continue
            if is_bpool(rel):
                # I-F: bpool keeps a per-disk snapshot instead of a bookmark.
                cmd = f"zfs rename {_q(f'{rel}@{self.point}')} {_q(f'{rel}@{keep}')}"
            else:
                cmd = f"zfs bookmark {_q(f'{rel}@{self.point}')} {_q(f'{rel}#{keep}')}"
            if not sh.run(cmd, log=self.log).ok:
                failed.append(rel)
        return failed

    def drop_check(self) -> list[Verdict]:  # pylint: disable=too-many-locals
        """Decide, against a fresh read of both sides, what may go from origin.

        Candidates are this run's point, points left by earlier runs and this
        disk's older anchors; another disk's anchors are never candidates.
        Nothing goes when either side cannot be read.
        """
        try:
            origin = read_origin(self.pools, strict=True)
            dest = read_dest(self.pool, strict=True)
        except ReadError as e:
            self.log.warn(f"Nothing removed in origin: could not read {e}")
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
                elif verified:
                    verdicts.append(Verdict(target, True, f"on the backup, anchored as {keep}"))
                elif s is point_snap and s.guid not in on_backup:
                    verdicts.append(Verdict(target, True, "not on the backup, nothing to resume"))
                else:
                    verdicts.append(Verdict(target, False, "may be the base of the next run"))
            for name in old_anchors([a.name for a in new], self.disk_guid, keep):
                target = f"{rel}{sep}{name}"
                if verified:
                    verdicts.append(Verdict(target, True, f"superseded by {keep}"))
                else:
                    verdicts.append(Verdict(target, False, "new anchor not verified"))
        return verdicts

    def drop(self, verdicts: list[Verdict]) -> int:
        """Destroy what :meth:`drop_check` allowed, one by one by exact name (F5)."""
        dropped = 0
        for v in verdicts:
            if not v.drop:
                self.log.info(f"  kept {v.target}: {v.reason}")
                continue
            if sh.run(f"zfs destroy {_q(v.target)}", log=self.log).ok:
                self.log.info(f"  removed {v.target}: {v.reason}")
                dropped += 1
        return dropped

    def undo_point(self) -> None:
        """Cleanup action for an interrupted run: drop the point where no
        receive began (where one did, the next run resumes it)."""
        for rel in expected(self.origin):
            if rel in self.started:
                continue
            _ = sh.run(f"zfs destroy {_q(f'{rel}@{self.point}')}")
        self.resume_sanoid()


def write_metadata(pool: str, fields: dict[str, str], log: Log) -> bool:
    """Record ``org.zark:<key>=<value>`` on the backup pool's root dataset
    (M2 decision 17a): readable without the passphrase, and a user property
    mounts nothing, so the zvol rule does not apply."""
    pairs = " ".join(_q(f"org.zark:{k}={v}") for k, v in sorted(fields.items()) if v)
    r = sh.run(f"zfs set {pairs} {_q(pool)}", log=log)
    if not r.ok:
        log.warn(f"Could not record zark metadata on {pool}: {r.stderr.strip()}")
    return r.ok


def read_metadata(pool: str) -> dict[str, str]:
    """``org.zark:*`` user properties set on the backup pool's root dataset."""
    r = sh.run(f"zfs get -H -s local -o property,value all {_q(pool)}")
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


def check_drive_owner(pool: str, log: Log) -> None:
    """Refuse a drive whose metadata names another system (2026-10-08).

    A drive without ``org.zark:origin-machine-id`` (written by zark <=
    2.0.0-rc2) is accepted as before.
    """
    meta = read_metadata(pool)
    theirs = meta.get("origin-machine-id", "")
    if not theirs:
        return
    mine = machine_id()
    if mine and mine == theirs:
        log.ok(f"{pool} belongs to this system (machine-id matches)")
        return
    when = meta.get("last-backup-at") or meta.get("prepared-at") or "?"
    log.fatal(
        f"{pool} is the backup of another system — nothing was changed",
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


def anchored_disks(origin: dict[str, Origin]) -> set[str]:
    """Pool GUIDs that hold anchors in origin (bookmarks or bpool snapshots)."""
    disks: set[str] = set()
    for o in origin.values():
        for ref in [*o.bookmarks, *o.snaps]:
            g = anchor_disk(ref.name)
            if g:
                disks.add(g)
    return disks


def drop_disk_anchors(disk_guid: str, pools: list[str], log: Log) -> int:
    """Remove every anchor of one disk from origin (``registry forget``)."""
    count = 0
    for rel, o in sorted(read_origin(pools).items()):
        names = [f"{rel}#{b.name}" for b in o.bookmarks if anchor_disk(b.name) == disk_guid]
        names += [f"{rel}@{s.name}" for s in o.snaps if anchor_disk(s.name) == disk_guid]
        for name in names:
            if sh.run(f"zfs destroy {_q(name)}", log=log).ok:
                count += 1
    return count


def is_point(name: str) -> bool:
    """True for a backup point name."""
    return bool(POINT_RE.match(name))


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

    @property
    def ok(self) -> bool:
        """I-D: every expected dataset reached the point (and is anchored)."""
        return not self.aborted and bool(self.plans) and reached(self.plans)


Decide = Callable[[list[DatasetPlan], "Run"], bool]

MAX_ROUNDS = 3


def replicate(run: Run, decide: Decide) -> Result:
    """Survey, let ``decide`` apply the user's decisions, transfer, verify,
    anchor and drop the points (steps 3–8 of the module docstring)."""
    res = Result()
    if not decide(run.survey(), run):
        res.aborted = True
        return res
    _ = run.survey()
    _ = run.clear_orphan_marks()
    for rel in run.ensure_containers():
        run.log.error(f"  {rel}: could not create the container on the backup")
    for _ in range(MAX_ROUNDS):
        pending = [p for p in depth_order(run.survey()) if p.state in TRANSFERABLE]
        todo = [p for p in pending if p.state is not State.AT_POINT]
        if not todo:
            break
        failed: set[str] = set()
        again = False
        for p in todo:
            if blocked_by_parent(p.rel, failed):
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
            if p.state is State.RESUME and run.abort_partial(p.rel):
                # The partial stream's snapshot is gone from origin: only the
                # partial state is discarded; the dataset is planned afresh.
                run.log.warn(f"  {p.rel}: stale partial receive discarded")
                again = True
        if res.enospc or not again:
            break
    res.plans = run.survey()
    at_point = [p.rel for p in res.plans if p.state is State.AT_POINT]
    res.anchor_failed = run.anchor(at_point)
    _ = run.drop(run.drop_check())
    return res


def execute(run: Run, cleanup: Cleanup, decide: Decide) -> Result:
    """The whole run: pause sanoid, take the point, :func:`replicate`, resume.

    Until it returns, ``cleanup`` holds undo actions, so an interrupted run
    drops its point where no receive began and resumes sanoid.
    """
    if not run.pause_sanoid():
        run.resume_sanoid()
        return Result(aborted=True, error="sanoid is still running after the wait")
    cleanup.track_action(run.resume_sanoid)
    try:
        if not run.take_point():
            return Result(aborted=True, error=f"could not take {run.point}")
        cleanup.track_action(run.undo_point)
        res = replicate(run, decide)
        cleanup.untrack_action(run.undo_point)
        if res.aborted:
            run.undo_point()
        return res
    finally:
        cleanup.untrack_action(run.resume_sanoid)
        run.resume_sanoid()
