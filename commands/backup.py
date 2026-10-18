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
zark backup - Back up this system to a connected, registered drive.

Imports the drive under I-G, takes one backup point ``zark_<UTC>`` of every
replicated dataset and brings each of them to it with zark's own send/receive
loop (lib/engine.py): ``-I`` from the drive's newest snapshot, or from this
drive's bookmark when origin no longer has it. Nothing on the drive is ever
destroyed unless the user chose it for that dataset in the questions asked
before the first byte is sent (ROLLBACK, DIVERGED, datasets only on the
drive). Success means every expected dataset reached the point; the
per-dataset table goes to the terminal and to zark.log.

Refuses to run from live USB (would back up the wrong system).
"""

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from lib import anchors, certificates, drive_marks, engine, forensics, reclaim, sh, status, survey
from lib.cleanup import Cleanup, prompt_eject_or_attach
from lib.config import VERSION, Config, now_utc_iso
from lib.drives import (
    backup_device,
    drive_staleness_days,
    no_anchor_lines,
    record_backup,
    scan_connected_drives,
    select_drive,
)
from lib.identity import by_id_names, preferred_by_id, whole_disk
from lib.keystore import Keystore, open_keystore
from lib.log import Log
from lib.mount import warn_kernel_named_vdevs, warn_rpool_mountpoint_lost
from lib.origin_prune import inventory
from lib.replication import (
    TRANSFERABLE,
    DatasetPlan,
    Origin,
    Ref,
    State,
    is_archived,
    point_name,
)
from lib.retention import (
    Policy,
    Prune,
    plan_prune,
    point_removals,
    space_candidates,
    whole_datasets,
)
from lib.zfs import ZFS, PoolInfo

# Remediation for a full backup drive. `zark purge` erases the whole drive,
# so it is never the answer to "out of space" (hallazgo 8).
_FREE_SPACE_HINT = (
    "Free space on the drive: its retention removes old backup points (see "
    "`sudo zark registry retention <pool>`); never destroy snapshots by a % range"
)


def _detect_live_usb() -> bool:
    """Return True if running from a live USB environment."""
    r = sh.run("cat /proc/cmdline")
    if r.ok and any(k in r.output for k in ("boot=casper", "boot=live", "live-media")):
        return True
    if sh.run("test -d /rofs").ok or sh.run("test -d /cow").ok:
        return True
    if not sh.run("zpool list rpool").ok:
        return True
    return False


def _notify(title: str, message: str):
    """Desktop notification (best-effort)."""
    user = sh.run("who | grep -m1 '(:0)' | awk '{print $1}'").output
    if user:
        _ = sh.run(
            f"sudo -u {user} DISPLAY=:0 "
            + f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u {user})/bus "
            + f"notify-send '{title}' '{message}' --icon=drive-harddisk",
        )


def _report_rpo(
    cfg: Config,
    pool_name: str,
    log: Log,
    anchored: set[str] | None = None,
    origin: dict[str, Origin] | None = None,
) -> None:
    """Days since every other registered drive's last backup (decision 15).

    A bookmark-anchored drive never loses its anchor through origin
    retention, so for it there is only how old its newest point is. A drive
    with no anchor in origin (``anchored`` names the GUIDs that have some)
    depends on the snapshots it shares with the source: what it can expect
    is checked against ``origin`` (M3 decision 5e; hallazgo 21 without a
    record).
    """
    others = [i for n, i in sorted(cfg.known_drives.items()) if n != pool_name]
    lost = [i for i in others if anchored is not None and i.guid not in anchored]
    lines = dict(zip((i.name for i in lost), no_anchor_lines(lost, origin or {}, log), strict=True))
    for info in others:
        age = drive_staleness_days(info)
        if age is None:
            log.info(f"  {info.name}: no backup recorded")
        else:
            log.info(f"  {info.name}: {age} day(s) since its last backup")
        if info.name in lines:
            log.warn(f"  {lines[info.name]}")


TOTAL_STEPS = 8


def _check_target_space(
    src_info: PoolInfo | None,
    dst_info: PoolInfo | None,
    pool_name: str,
    source_pool: str,
    log: Log,
) -> None:
    """Coherence check before a backup (warn only): the destination drive
    should be at least as large as the source pool, or it will run out of
    space whatever its retention. Whether this backup fits is decided by
    the estimate, after the questions (M3 decision 4)."""
    if src_info is None or dst_info is None:
        return
    if 0 < dst_info.size_bytes < src_info.size_bytes:
        log.warn(
            f"Target {pool_name} ({dst_info.size}) is smaller than source "
            f"{source_pool} ({src_info.size}) - backup may eventually fail",
        )


@dataclass
class BackupArgs:
    """Parsed backup command-line arguments."""

    # ``--no-snapshot`` predates the backup point, which now replaces the
    # pre-backup sanoid run; the flag is accepted and has no effect.
    take_snapshots: bool = True


def _parse_args(args: list[str]) -> BackupArgs:
    """Parse backup's command-line arguments (unknown flags are ignored)."""
    parsed = BackupArgs()
    if "--no-snapshot" in args:
        parsed.take_snapshots = False
    return parsed


def _gib(n: int) -> str:
    return sh.humanize_bytes(n)


def _space(info: PoolInfo | None) -> str:
    """``used=... (N%)  available=...`` from the pool root's byte counts."""
    if info is None or info.size_bytes <= 0:
        return "used=?  available=?"
    return f"used={_gib(info.used_bytes)} ({info.pct_used}%)  available={_gib(info.avail_bytes)}"


def _decide(  # noqa: C901 # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-arguments,too-many-positional-arguments
    plans: list[DatasetPlan],
    eng: engine.Run,
    pool_name: str,
    dst_info: PoolInfo | None,
    log: Log,
    policy: Policy | None = None,
) -> bool:
    """Show the plan, ask every question before the first byte is sent,
    check space, then apply the user's choices. False aborts the run."""
    sizes = {p.rel: eng.estimate(p) for p in plans if p.state in TRANSFERABLE}
    counts: dict[str, int] = {}
    for p in plans:
        counts[str(p.state)] = counts.get(str(p.state), 0) + 1
    policy = policy or Policy()
    log.info("Plan: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    for p in plans:
        if p.state is State.EXCLUDED:
            log.warn(f"  {p.rel}: NOT BACKED UP ({p.note})")
        elif p.state is State.VIA_BOOKMARK:
            log.info(f"  {p.rel}: from this drive's bookmark")
        elif p.state is State.NEW and p.clone_from:
            log.info(f"  {p.rel}: new clone of {p.base}, sent as a delta from the drive's copy")
    avail = dst_info.avail_bytes if dst_info else 0
    actions: list[Callable[[], object]] = []
    confirmed_any = False
    moved: set[str] = set()  # tops archived or destroyed: their subtrees go with them

    def resend(top: str) -> int:
        """Full sizes of everything a rename or destroy of ``top`` sends again."""
        total = 0
        for q in _subtree(plans, top):
            sizes[q.rel] = eng.estimate(q, full=True)
            total += sizes[q.rel]
        return total

    # Datasets origin no longer has (decision 18).
    for p in plans:
        if p.state is not State.ORPHAN:
            continue
        if p.kept:
            log.info(f"  {p.rel}: only on the backup, kept")
            continue
        opts = [
            "ask again next time",
            "keep it (not asked again)",
            f"destroy it on the backup (frees {_gib(p.used)})",
        ]
        if p.rename_to:
            opts.append(f"follow the rename in origin: {p.rel} → {p.rename_to}")
        pick = _ask_confirmed(
            log,
            f"{p.rel} exists only on the backup ({_gib(p.used)}):",
            opts,
            {2: ("DESTROY", f"destroy {pool_name}/{p.rel} and everything below it")},
            {2: _on_drive(eng, p.rel)},
        )
        if pick == 1:
            actions.append(partial(eng.keep_orphan, p, datetime.now(UTC).strftime("%Y-%m-%d")))
        elif pick == 2:
            actions.append(partial(eng.destroy, p))
            confirmed_any = True
        elif pick == 3:
            actions.append(partial(eng.follow_rename, p))

    # Written on the drive since the newest snapshot (M2F-8): asked once.
    modified = [p for p in plans if p.state is State.MODIFIED]
    if modified:
        log.warn(
            f"{len(modified)} dataset(s) on {pool_name} changed since their newest snapshot "
            + "(a read-write zark mount does this, even with nothing written):",
        )
        for p in modified:
            extra = " (a partial transfer is discarded too)" if p.partial else ""
            log.info(f"  {p.rel}: {_gib(p.used)} written since {p.common.lstrip('@')}{extra}")
        pick = _ask_confirmed(
            log,
            "How should this backup continue?",
            [
                "skip them this time (the backup will be INCOMPLETE)",
                "discard those changes on the backup (no snapshot is destroyed)",
            ],
            {1: ("DISCARD", f"discard the changes in {len(modified)} dataset(s) on {pool_name}")},
        )
        if pick == 1:
            sizes.update({p.rel: eng.estimate(p) for p in modified})
            actions += [partial(eng.discard_changes, p) for p in modified]
            confirmed_any = True

    # Only an older common snapshot left (§7 migration; recovered older point).
    rollbacks = [p for p in plans if p.state is State.ROLLBACK]
    if rollbacks:
        pre_v2 = drive_marks.read_metadata(pool_name).get("format") != drive_marks.FORMAT
        log.warn(
            f"{len(rollbacks)} dataset(s) on {pool_name} hold snapshots newer than anything origin "
            + (
                "still has: first run of this zark on a drive written by an older one."
                if pre_v2
                else "still has (recovered from an older point, or snapshots destroyed in origin)."
            ),
        )
        destroyed = 0
        for p in rollbacks:
            destroyed += len(p.newer)
            names = [n.lstrip("@") for n in p.newer]
            more = len(names) - 3
            shown = ", ".join(names[:3]) + (
                f" ...and {more} more (full list in zark.log)" if more > 0 else ""
            )
            log.info(f"  {p.rel}: common {p.common.lstrip('@')}; newer on the backup: {shown}")
            if more > 0:
                log.file(f"  {p.rel}: every snapshot the rollback destroys: {', '.join(names)}")
        # Either way each one is then an incremental from its common snapshot.
        sizes.update({p.rel: eng.estimate(p) for p in rollbacks})
        opts = ["abort the backup (nothing changes)"]
        # Keeping the newer snapshots costs no space now and resends nothing
        # (M3R-1, M4b-5); it replaces "archive and resend in full" here.
        fits = sum(sizes.values()) <= avail
        if fits:
            opts.append(
                f"keep the {destroyed} newer snapshot(s) in archived lineages and continue "
                + "from the common snapshot (nothing destroyed, nothing resent)"
            )
        opts.append(f"roll back: destroy the {destroyed} newer snapshot(s) listed above")
        pick = _ask_confirmed(
            log,
            "How should this backup continue?",
            opts,
            {len(opts) - 1: ("ROLLBACK", f"destroy {destroyed} snapshot(s) on {pool_name}")},
        )
        if pick == 0:
            return False
        if fits and pick == 1:
            # Parents first: each one's children are back under it before theirs go.
            actions += [partial(eng.branch, p) for p in sorted(rollbacks, key=lambda q: q.rel)]
        else:
            actions += [partial(eng.rollback, p) for p in rollbacks]
            confirmed_any = True

    # Nothing in common (D17). A dataset below one already archived or
    # destroyed goes with it and is not asked about. Several are asked
    # about together first (M3P.27, M4b-11).
    diverged = _tops(
        [p.rel for p in plans if p.state is State.DIVERGED and not _under(p.rel, moved)]
    )
    skipped: set[str] = set()  # with what is below them, by the joint question
    if len(diverged) > 1:
        lineages = [p for p in plans if p.rel in diverged]
        log.warn(
            f"{len(lineages)} dataset(s) on {pool_name} have no snapshot in common with origin:"
        )
        for p in lineages:
            log.info(f"  {p.rel}: {p.note}")
        before = dict(sizes)
        full = sum(resend(t) for t in diverged)
        resent, sizes = sizes, before
        opts = ["skip them this time (the backup will be INCOMPLETE)"]
        fits = sum(resent.values()) <= avail
        if fits:
            opts.append(
                f"archive the {len(lineages)} old lineages and resend in full ({_gib(full)})"
            )
        opts.append(
            f"destroy the {len(lineages)} on the backup and resend in full "
            + f"({_gib(sum(p.used for p in lineages))} destroyed)"
        )
        opts.append("decide one by one")
        destroy = len(opts) - 2
        pick = _ask_confirmed(
            log,
            f"{len(lineages)} datasets have diverged:",
            opts,
            {
                destroy: (
                    "DESTROY",
                    f"destroy {len(lineages)} dataset(s) on {pool_name} and everything below them",
                )
            },
            {destroy: [line for t in diverged for line in _on_drive(eng, t)]},
        )
        if pick == 0:
            skipped.update(diverged)
        elif pick < len(opts) - 1:
            sizes = resent
            moved.update(diverged)
            if fits and pick == 1:
                actions += [partial(eng.archive, p) for p in lineages]
            else:
                actions += [partial(eng.destroy, p) for p in lineages]
                confirmed_any = True
    for p in plans:
        if p.state is not State.DIVERGED or _under(p.rel, moved | skipped):
            continue
        before = dict(sizes)
        full_size = resend(p.rel)
        below = len(_subtree(plans, p.rel)) - 1
        extra = f" (and {below} dataset(s) below)" if below else ""
        held = _on_drive(eng, p.rel)
        on_drive = f" (and {len(held) - 1} dataset(s) below)" if len(held) > 1 else ""
        opts = ["skip it this time (the backup will be INCOMPLETE)"]
        fits = sum(sizes.values()) <= avail
        if fits:
            opts.append(f"archive the old lineage{extra} and resend in full ({_gib(full_size)})")
        opts.append(
            f"destroy it{on_drive} on the backup and resend in full ({_gib(p.used)} destroyed)"
        )
        log.warn(f"  {p.rel}: no snapshot in common with the backup ({p.note})")
        pick = _ask_confirmed(
            log,
            f"{p.rel} has diverged:",
            opts,
            {len(opts) - 1: ("DESTROY", f"destroy {pool_name}/{p.rel} and everything below it")},
            {len(opts) - 1: held},
        )
        if pick == 0:
            sizes = before
            continue
        moved.add(p.rel)
        if fits and pick == 1:
            actions.append(partial(eng.archive, p))
        else:
            actions.append(partial(eng.destroy, p))
            confirmed_any = True

    needed = sum(sizes.values())
    log.info(f"Estimated transfer: {_gib(needed)}; available on {pool_name}: {_gib(avail)}")
    if dst_info:
        actions = [*_make_room(plans, eng, needed, dst_info, policy, log), *actions]
    if confirmed_any:
        log.ok("Applying the confirmed changes on the backup")
    for act in actions:
        _ = act()
    return True


def _make_room(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    plans: list[DatasetPlan],
    eng: engine.Run,
    needed: int,
    dst_info: PoolInfo,
    policy: Policy,
    log: Log,
) -> list[Callable[[], object]]:
    """Free space before the transfer when the drive would end below its
    margin (M3 decision 4, as settled in decision 1).

    First what the retention policy removes anyway, without a question;
    then, if that is not enough, a plan of the oldest monthly points, each
    with what it frees, applied only on a yes (default no). The margin is a
    goal: when only the transfer itself fits, the backup goes on with a
    warning; it stops only when the transfer does not fit. Returns the
    removals to apply before the other confirmed changes.
    """
    pool, avail = eng.pool, dst_info.avail_bytes
    margin = dst_info.size_bytes * policy.margin_pct // 100
    if needed + margin <= avail:
        return []
    try:
        complete, pins = drive_marks.drive_complete(pool), drive_marks.drive_pins(pool, strict=True)
    except survey.ReadError as e:
        log.warn(f"Retention skipped: {e}")
        _check_room(pool, needed, avail, margin, policy, log)
        return []
    # Bases of this run: a ROLLBACK's common snapshot, a new clone's origin.
    bases = {f"{p.rel}{p.common}" for p in plans if p.state is State.ROLLBACK and p.common}
    bases |= {p.clone_from for p in plans if p.state is State.NEW and p.clone_from}
    now = int(time.time())
    prune = plan_prune(
        eng.dest,
        complete.run,
        policy,
        now,
        whole=complete.whole,
        pinned=pins,
        protected=bases,
        first=complete.first,
    )
    tops = [t for t, _ in prune.datasets]
    freed = reclaim.reclaim(pool, prune.snapshots, tops) if not prune.empty else 0
    actions: list[Callable[[], object]] = []
    if not prune.empty:
        log.info(
            f"Retention policy: {len(prune.snapshots)} snapshot(s) on {pool} go now "
            + f"(frees {_gib(freed)})"
        )
        actions.append(partial(reclaim.apply_prune, pool, prune, log))
    chosen: list[tuple[str, int]] = []
    beyond = Prune()
    total = freed
    for name in space_candidates(eng.dest, complete.run, policy, now, prune, complete.whole):
        if needed + margin <= avail + total:
            break
        more = point_removals(eng.dest, name, pinned=pins, protected=bases)
        if not more:
            continue
        beyond.snapshots += more
        # 7a holds here too (M3R-3): an archived lineage or kept orphan these
        # removals (with the policy's, applied first) leave empty goes whole.
        beyond.datasets = [
            d
            for d in whole_datasets(eng.dest, [*prune.snapshots, *beyond.snapshots])
            if d not in prune.datasets
        ]
        new_total = reclaim.reclaim(
            pool, [*prune.snapshots, *beyond.snapshots], [*tops, *(t for t, _ in beyond.datasets)]
        )
        chosen.append((name, new_total - total))
        total = new_total
    room = avail + freed
    if chosen:
        short = (
            f"would be left with less than {policy.margin_pct}% free ({_gib(margin)})" + " after"
            if margin
            else "does not have room for"
        )
        log.warn(f"{pool} {short} this backup; the oldest monthly backup points can go:")
        for name, frees in chosen:
            log.info(f"  {name}  (frees {_gib(frees)})")
        for top, _why in beyond.datasets:
            log.info(f"  {pool}/{top} and everything below it go too (nothing of it would be left)")
        if log.ask(f"Remove these {len(chosen)} backup point(s) from {pool}?", default=False):
            actions.append(partial(reclaim.apply_prune, pool, beyond, log))
            room = avail + total
    _check_room(pool, needed, room, margin, policy, log)
    return actions


def _check_room(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    pool: str,
    needed: int,
    room: int,
    margin: int,
    policy: Policy,
    log: Log,
) -> None:
    """Stop when the transfer does not fit; warn when the margin is not kept."""
    if needed > room:
        log.fatal(
            f"Not enough space on {pool} for this backup",
            causes=[f"Needs about {_gib(needed)}, {_gib(room)} available"],
            solutions=[_FREE_SPACE_HINT, "Connect a larger backup drive"],
        )
    if needed + margin > room:
        log.warn(
            f"{pool} will have less than {policy.margin_pct}% free after this backup: "
            + "the next one will propose removing old backup points again, "
            + "or the drive is too small for this system"
        )


def _apply_retention(pool: str, policy: Policy, log: Log) -> tuple[int, int]:
    """The drive's retention policy after a complete run (M3 decision 3a):
    every removal listed and logged. Returns how many snapshots went and
    what ZFS measured they free; (0, 0) when nothing goes or the drive
    cannot be read."""
    try:
        dest = survey.read_dest(pool, strict=True)
        complete = drive_marks.drive_complete(pool)
        pins = drive_marks.drive_pins(pool, strict=True)
    except survey.ReadError as e:
        log.warn(f"Retention skipped: {e}")
        return 0, 0
    prune = plan_prune(
        dest,
        complete.run,
        policy,
        int(time.time()),
        whole=complete.whole,
        pinned=pins,
        first=complete.first,
    )
    for target, why in prune.kept:
        log.info(f"  kept {pool}/{target}: {why}")
    if prune.empty:
        return 0, 0
    freed = reclaim.reclaim(pool, prune.snapshots, [t for t, _ in prune.datasets])
    log.info(
        f"Retention on {pool} (window {policy.window_days} days): "
        + f"{len(prune.snapshots)} snapshot(s) go, freeing {_gib(freed)}"
    )
    count, failed = reclaim.apply_prune(pool, prune, log)
    if failed:
        log.warn(f"{len(failed)} snapshot(s) could not be removed (see above); kept for next time")
    return count, freed


def _subtree(plans: list[DatasetPlan], top: str) -> list[DatasetPlan]:
    """Planned datasets at or below ``top`` that a rename or destroy of it moves."""
    return [
        p
        for p in plans
        if (p.rel == top or p.rel.startswith(f"{top}/"))
        and p.state not in (State.ORPHAN, State.EXCLUDED)
    ]


def _on_drive(eng: engine.Run, top: str) -> list[str]:
    """What `zfs destroy -r` of ``top`` takes, from the drive's own listing:
    one line per dataset with its snapshot count, archived lineages and
    kept orphans named as such (the plan leaves both out)."""
    lines: list[str] = []
    for rel, d in sorted(eng.dest.items()):
        if rel != top and not rel.startswith(f"{top}/"):
            continue
        mark = ""
        if is_archived(rel):
            mark = "  - archived lineage"
        elif d.orphan.startswith("kept@"):
            mark = "  - kept, only on the backup"
        lines.append(f"    {eng.pool}/{rel}  ({len(d.snaps)} snapshot(s)){mark}")
    return lines


def _under(rel: str, tops: set[str]) -> bool:
    """True when ``rel`` is one of ``tops`` or below one of them."""
    return any(rel == t or rel.startswith(f"{t}/") for t in tops)


def _tops(rels: list[str]) -> list[str]:
    """The datasets in ``rels`` with no ancestor in ``rels`` (a rename moves the rest)."""
    return sorted(r for r in rels if not any(r.startswith(f"{t}/") for t in rels))


def _typed(log: Log, word: str, what: str) -> bool:
    """Typed confirmation for anything that destroys data on the backup."""
    answer = log.ask_text(
        f"    Type {word} to {what}: ", accept=(word,), label=f"Type {word} to {what}"
    )
    return answer == word


def _ask_confirmed(
    log: Log,
    question: str,
    opts: list[str],
    confirm: dict[int, tuple[str, str]],
    takes: dict[int, list[str]] | None = None,
) -> int:
    """Numbered choice whose destructive options need a typed word.

    ``takes`` lists, per destructive option, what it destroys; it is shown
    and logged before the word is asked. A word that does not match is no
    answer: the same question is asked again, so a typo can neither destroy
    nor silently pick another option.
    """
    while True:
        pick = log.ask_choice(question, opts, 0)
        if pick not in confirm:
            return pick
        lines = (takes or {}).get(pick, [])
        if lines:
            log.warn(f"{confirm[pick][0]} takes {len(lines)} dataset(s) on the backup:")
            for line in lines:
                log.warn(line)
        if _typed(log, *confirm[pick]):
            return pick
        log.info(f"Not confirmed ({confirm[pick][0]} was not typed) - choose again")


def _show_table(res: engine.Result, point: str, log: Log) -> None:
    """Per-dataset outcome, in the terminal and zark.log (I-D, I19)."""
    log.info(f"Per-dataset result for {point}:")
    for p in res.plans:
        out = res.outcomes.get(p.rel)
        if p.state is State.AT_POINT:
            mark = "ok"
            if p.rel in res.anchor_failed:
                mark = "ok, but its anchor could not be created (next run may need a decision)"
        elif p.state is State.ORPHAN:
            mark = "only on backup" + (", kept" if p.kept else "")
        elif p.state is State.EXCLUDED:
            mark = f"NOT BACKED UP ({p.note})"
        elif out is None:
            mark = f"NOT at the point: not attempted ({p.state})"
        elif res.unverified and out.ok:
            mark = "transferred, not verified"
        else:
            mark = f"NOT at the point: {out.error or p.state}"
        line = f"  {p.rel:<48} {mark}"
        if p.state is State.AT_POINT:
            log.info(line)
        else:
            log.warn(line)


# Shown in the BACKUP INCOMPLETE banner (M2F-11b).
_INCOMPLETE_SHOWN = 15


def _next_step(p: DatasetPlan, out: engine.Outcome | None, res: engine.Result) -> str:
    """What the next backup does with a dataset that missed the point."""
    failed = out is not None and not out.ok
    steps = (  # first match wins
        (p.state is State.RESUME, "partial transfer kept: the next backup resumes it"),
        (
            p.state in (State.ROLLBACK, State.DIVERGED),
            f"needs a decision ({p.state}): the next backup asks",
        ),
        (p.state is State.MODIFIED, "changed on the drive: the next backup asks again"),
        (
            out is not None and out.enospc,
            "the drive is full: free space, then run backup again",
        ),
        (
            bool(res.unverified) and out is not None and out.ok,
            "transferred, not verified: the next backup checks it",
        ),
        (failed, "failed (error in the table above): the next backup tries again"),
        (res.enospc, "not attempted after the drive filled up"),
    )
    return next((text for hit, text in steps if hit), "not attempted: the next backup sends it")


def _incomplete_lines(res: engine.Result, point: str) -> tuple[list[str], int]:
    """BACKUP INCOMPLETE banner lines, the datasets grouped by what the next
    run does with them, and how many datasets missed the point."""
    missing = [
        p for p in res.plans if p.state not in (State.AT_POINT, State.ORPHAN, State.EXCLUDED)
    ]
    groups: dict[str, list[str]] = {}  # first seen first
    for p in missing:
        groups.setdefault(_next_step(p, res.outcomes.get(p.rel), res), []).append(p.rel)
    lines = [f"The result could not be checked: {res.unverified}", ""] if res.unverified else []
    lines.append(f"{len(missing)} dataset(s) did not reach {point}:")
    shown = 0
    for step, rels in groups.items():
        lines.append(f"  {step} ({len(rels)}):")
        names = rels[: max(_INCOMPLETE_SHOWN - shown, 0)]
        lines += [f"    {rel}" for rel in names]
        shown += len(names)
        if len(names) < len(rels):
            lines.append(f"    ...and {len(rels) - len(names)} more (see the table above)")
    lines += ["", "Run backup again: each group above says what it will do."]
    return lines, len(missing)


def _write_metadata(pool_name: str, source_pool: str, point: str, log: Log) -> None:
    """Decision 17a. The point and its time are recorded only for a complete run."""
    rpool_guid = sh.run(f"zpool get -H -o value guid {source_pool}").output.strip()
    fields = {
        "format": drive_marks.FORMAT,
        "version": VERSION,
        "origin-host": socket.gethostname(),
        "origin-rpool-guid": rpool_guid,
        "origin-machine-id": drive_marks.machine_id(),
    }
    if point:
        fields |= {"last-backup-at": now_utc_iso(), "last-point": point}
    _ = drive_marks.write_metadata(pool_name, fields, log)


def _heal_drive_id(cfg: Config, pool_name: str, device: str | None, log: Log) -> None:
    """Rewrite the registry's drive_id when the verified pool sits on another by-id.

    Runs after the pool GUID matched the registry, so the disk is known to be
    the registered one; a placeholder ("<unknown>") or stale drive_id left by
    1.0.12 is replaced by the by-id this disk actually has.
    """
    info = cfg.known_drives.get(pool_name)
    if info is None or not device:
        return
    disk = whole_disk(device)
    by_id = preferred_by_id(by_id_names(disk)) if disk else ""
    if not by_id or by_id == info.drive_id:
        return
    old = info.drive_id
    info.drive_id = by_id
    cfg.save_drives()
    log.ok(f"Registry: {pool_name} drive_id {old} → {by_id}")


SHOWN_ERRORS = 20


def _check_health(zfs: ZFS, pool: str, log: Log) -> None:
    """Pre-check of one pool (I8, M4a-10).

    ONLINE with zero counters passes. DEGRADED, or ONLINE with non-zero
    READ/WRITE/CKSUM counters, is captured (M4a-9), its counters and first
    permanent errors shown, and the run continues only on an explicit yes.
    Any other health is captured and fatal.
    """
    health = zfs.pool_health(pool)
    if health not in ("ONLINE", "DEGRADED"):
        _ = forensics.capture(pool, f"pre-check: {health}", log)
        log.fatal(f"{pool} is {health}", solutions=[f"Run: zpool status -v {pool}"])
    damage = zfs.pool_damage(pool)
    if health == "ONLINE" and not damage.any:
        log.ok(f"{pool}: ONLINE")
        return
    state = health if health == "DEGRADED" else "ONLINE with errors"
    log.warn(f"{pool}: {state}")
    path = forensics.capture(pool, f"pre-check: {state}", log)
    for line in damage.counters:
        log.warn(f"  errors (READ WRITE CKSUM): {line}")
    if damage.errors:
        log.warn(f"  permanent errors in {len(damage.errors)} object(s):")
        for obj in damage.errors[:SHOWN_ERRORS]:
            log.warn(f"    {obj}")
        rest = len(damage.errors) - SHOWN_ERRORS
        if rest > 0:
            log.warn(f"    ... and {rest} more in {path or f'zpool status -v {pool}'}")
    if not log.ask(f"{pool} is {state}. Continue anyway?", default=False):
        log.fatal(
            f"Aborted: {pool} is {state}",
            solutions=[f"Run: zpool status -v {pool}"],
        )


def run(
    args: list[str],
):  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    """Main backup function."""
    opts = _parse_args(args)
    log = Log()
    cfg = Config.load()
    cfg.check_registry(log, fatal=True)
    zfs = ZFS(log)
    cleanup = Cleanup(log)
    cleanup.register()

    log.banner("ZFS BACKUP", f"Source: {cfg.source_pool}")
    certificates.show_warnings(log)
    started = time.time()

    # ── Refuse live USB ──────────────────────────────────────────────────
    if _detect_live_usb():
        log.fatal(
            "Live USB detected - backup blocked",
            causes=[
                "Running backup from live USB would back up the wrong system",
                "It corrupts ZFS mountpoints on the backup drive",
            ],
            solutions=["Boot into your installed system and run backup from there"],
        )

    warn_rpool_mountpoint_lost(log)
    warn_kernel_named_vdevs(log)
    survey.require_bookmark_v2(log)
    survey.require_boot_pool(log)

    # ── Find and select drive ────────────────────────────────────────────
    log.step(1, TOTAL_STEPS, "Scanning for known backup drives...")
    drives = scan_connected_drives(cfg, log)
    known = [d for d in drives if d.known]

    if not known:
        log.fatal(
            "No known backup drives detected",
            causes=[
                "No drives from known_drives.json are connected",
                "USB cable not properly seated",
                "Drive has different ID than registered",
            ],
            solutions=[
                "Connect a backup drive and run again",
                "Run: sudo ./zark explore  to scan for unknown pools",
                "Run: sudo ./zark prepare /dev/sdX  to register a new drive",
            ],
        )

    drive = select_drive(known, log, known_only=True)
    if not drive:
        return

    pool_name = drive.name
    pool_guid = drive.guid

    selected_info = cfg.known_drives.get(pool_name)
    age_at_start = drive_staleness_days(selected_info) if selected_info is not None else None
    if age_at_start is not None:
        log.info(f"Last backup on {pool_name}: {age_at_start} day(s) ago")

    # ── Import pool ──────────────────────────────────────────────────────
    log.step(2, TOTAL_STEPS, f"Importing pool {pool_name}...")

    device = backup_device(drive)
    if not zfs.import_backup_pool(pool_name, device, guid=pool_guid):
        log.fatal(
            f"Cannot import pool {pool_name}",
            causes=[
                f"Device: {device or 'not found'}",
                "The pool may be imported elsewhere, or the drive not fully connected",
            ],
            solutions=[
                "Check: zpool status; sudo zpool export <pool> if imported by hand",
                "Reconnect the drive and run again",
            ],
        )

    cleanup.track_pool(pool_name)

    # Verify GUID
    actual_guid = zfs.pool_guid(pool_name)
    if actual_guid != pool_guid:
        log.fatal(
            f"GUID mismatch: expected {pool_guid}, got {actual_guid}",
            causes=["Wrong drive connected", "Pool was re-prepared"],
            solutions=[
                f"Update known_drives.json with new GUID: {actual_guid}",
                "Connect the correct drive",
            ],
        )

    log.ok(f"Pool {pool_name} imported (GUID: {actual_guid} ✓)")
    # By content, not only by the registry: a drive of another system is
    # refused before anything is asked or written.
    drive_marks.check_drive_owner(pool_name, log)
    _heal_drive_id(cfg, pool_name, device, log)
    drive_marks.record_first_point(pool_name, log)

    # ── Check health ─────────────────────────────────────────────────────
    log.step(3, TOTAL_STEPS, "Checking pool health...")

    for pool in (cfg.source_pool, pool_name):
        _check_health(zfs, pool, log)

    # ── Pool info summary ────────────────────────────────────────────────
    log.step(4, TOTAL_STEPS, "Gathering pool info...")

    src_info = zfs.pool_info(cfg.source_pool)
    dst_info = zfs.pool_info(pool_name)

    _check_target_space(src_info, dst_info, pool_name, cfg.source_pool, log)

    # One set of figures, the pool root's `used` and `available` (which
    # account for reservations), as the space check after the questions uses.
    log.info(f"Source: {cfg.source_pool}  {_space(src_info)}")
    log.info(f"Target: {pool_name}  {_space(dst_info)}")

    # ── Load encryption key ──────────────────────────────────────────────
    log.step(5, TOTAL_STEPS, "Loading encryption key...")

    # A drive prepared by zark >= 2.0.0-rc2 has an unencrypted <pool>/rpool
    # container, so "every key loaded" is read from the datasets below it.
    ks = Keystore(log)

    if not zfs.datasets_needing_key(f"{pool_name}/rpool"):
        log.ok("Key already loaded")
        if not log.ask("Key already loaded. Proceed with backup?", default=True):
            log.fatal("Aborted by user")
    else:
        if not open_keystore(ks, pool_name, log):
            log.fatal(
                "Cannot open keystore",
                causes=["Wrong passphrase (3 attempts)", "Keystore zvol not found"],
                solutions=[
                    "Retry with the passphrase of the system this drive backs up",
                    "Test the passphrase read-only: sudo ./zark mount",
                ],
            )

        cleanup.track_keystore(ks)

        loaded = ks.load_pool_keys(f"{pool_name}/rpool")
        if loaded == 0:
            log.fatal(
                "Cannot load encryption key for backup pool",
                solutions=["Re-prepare the drive"],
            )
        log.ok(f"Encryption key loaded ({loaded} datasets)")

    # ── Backup point, plan and questions ─────────────────────────────────
    log.step(6, TOTAL_STEPS, "Taking the backup point and planning every dataset...")
    if not opts.take_snapshots:
        log.info("--no-snapshot has no effect: the backup point is taken by zark itself")
    eng = engine.Run(pool_name, pool_guid, log, point_name(datetime.now(UTC)))
    _notify("🔄 Backup started", f"{cfg.source_pool} → {pool_name}")
    log.info("Tip: run 'sudo ./zark monitor' in another terminal for progress")

    policy = cfg.drive_policy(pool_name)

    def decide(plans: list[DatasetPlan], run_: engine.Run) -> bool:
        return _decide(plans, run_, pool_name, dst_info, log, policy)

    log.step(7, TOTAL_STEPS, "Replicating (this may take a while)...")
    res = engine.execute(eng, cleanup, decide)
    if res.aborted:
        log.fatal(f"Backup not started: {res.error or 'aborted'}")

    _show_table(res, eng.point, log)
    used_after = zfs.pool_info(pool_name)  # P0-4: after the transfer
    pruned = (0, 0)
    shared: dict[str, list[Ref]] | None = None
    meta: dict[str, str] = {}
    if res.ok:
        at_point = [p.rel for p in res.plans if p.state is State.AT_POINT]
        _ = drive_marks.record_complete(pool_name, eng.point, at_point, log)
        pruned = _apply_retention(pool_name, policy, log)
        # After retention, which may remove shared intermediates (decision 5e).
        shared = eng.shared_with_origin()
        meta = drive_marks.read_metadata(pool_name)
    _write_metadata(pool_name, cfg.source_pool, eng.point if res.ok else "", log)

    # ── Export and read back ─────────────────────────────────────────────
    ks.umount()
    cleanup.run()
    elapsed = int(time.time() - started)  # P0-2: the whole run
    mins, secs = divmod(elapsed, 60)

    log.step(8, TOTAL_STEPS, f"Verifying {pool_name} is reimportable...")
    # Written before the export, which a read-back needs (M2X.2, M4b-13).
    marked = (
        [
            "",
            f"{pool_name} already records {eng.point} as its last backup point (written",
            "before the export); the registry's last backup was not updated.",
        ]
        if res.ok
        else []
    )
    if pool_name not in cleanup.exported_pools():
        # H17: an export that failed is a failed backup, never a skipped check.
        log.banner_error(
            "BACKUP NOT VERIFIED",
            [
                f"{pool_name} could not be exported, so it was not read back.",
                "Do NOT disconnect the drive. Check: zpool status; then",
                f"  sudo zpool export {pool_name}",
                *marked,
            ],
        )
        _notify("❌ Backup NOT verified", f"{pool_name} could not be exported")
        raise SystemExit(1)
    rb = zfs.verify_exported_pool_readback(pool_name, device=device)
    if not rb.ok:
        log.banner_error(
            "BACKUP NOT VERIFIED", [*rb.describe(), "", "Do NOT rely on this backup.", *marked]
        )
        _notify("❌ Backup NOT verified", f"{pool_name} failed its read-back")
        raise SystemExit(1)
    log.ok(f"{pool_name} verified reimportable (ONLINE)")

    if not res.ok:
        lines, missing = _incomplete_lines(res, eng.point)
        log.banner_error("BACKUP INCOMPLETE", lines)
        _notify("❌ Backup incomplete", f"{missing} dataset(s) not backed up")
        raise SystemExit(1)

    # ── Persist last_backup_at (only for a complete, verified backup) ────
    info = cfg.known_drives.get(pool_name)
    if info is not None:
        info.last_backup_at = now_utc_iso()
        record_backup(info, VERSION, shared, meta)
        try:
            cfg.save_drives()
            log.dbg(f"Recorded last_backup_at={info.last_backup_at} for {pool_name}")
        except OSError as e:
            log.warn(f"Could not persist last_backup_at for {pool_name}: {e}")

    not_backed_up = [p for p in res.plans if p.state is State.EXCLUDED]
    log.banner_ok(
        "BACKUP COMPLETED",
        [
            f"Point:          {log.W}{eng.point}{log.N}",
            f"Duration:       {log.W}{mins}m {secs}s{log.N}",
            f"Used on target: {log.W}{used_after.used if used_after else '?'}{log.N}",
            f"Available:      {log.W}{used_after.avail if used_after else '?'}{log.N}",
            *(
                [f"Retention:      {pruned[0]} old snapshot(s) removed, {_gib(pruned[1])} freed"]
                if pruned[0]
                else []
            ),
            *(
                [f"{log.Y}Not backed up:  {len(not_backed_up)} dataset(s), by design:{log.N}"]
                if not_backed_up
                else []
            ),
            *[f"{log.Y}  {p.rel} ({p.note}){log.N}" for p in not_backed_up],
        ],
    )
    origin: dict[str, Origin] = {}
    try:
        origin = survey.read_origin(survey.source_pools(), strict=True)
        anchored: set[str] | None = anchors.anchored_disks(origin)
    except survey.ReadError as e:
        # Not an empty origin: the report would call every other drive
        # unanchored and promise full resends (M3R-2). Ages only.
        log.warn(
            f"Could not read the source pools: {e}; what the other drives share was not checked"
        )
        anchored = None
    _report_rpo(cfg, pool_name, log, anchored, origin)
    status.refresh_quietly(cfg)
    residues = inventory(origin, {i.guid: n for n, i in cfg.known_drives.items()}).residues
    if residues:
        log.info(
            f"{len(residues)} legacy snapshot(s) in origin no registered drive needs "
            + "(zark 1.x residues): sudo zark prune removes them"
        )

    prompt_eject_or_attach(
        device,
        pool_name,
        log,
        default_eject=True,
        autoeject=cfg.drive_autoeject(pool_name),
    )

    _notify("✅ Backup completed", f"Duration: {mins}m {secs}s")
