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
zark prune - reclaim space in origin or on a connected backup drive.

Interactive; nothing goes without a confirmation, every snapshot is
destroyed by its exact name (never a % range) and logged with its reason.

Origin (rpool, bpool; M3 decisions 9a-11a): what each kind of snapshot
holds and what removing it frees, then
  * sanoid's snapshots older than N days (typed PRUNE); sanoid.conf is not
    changed, so sanoid keeps taking and pruning by its own policy;
  * legacy residues: syncoid_* snapshots no registered drive can depend on
    and the keystore's prepare_* snapshots (yes/no);
  * backup points left in origin by an interrupted run, or by a complete
    run whose anchor failed (typed PRUNE);
  * a drive's bpool anchors (typed PRUNE); that drive's next backup then
    asks to roll those datasets back to a shared sanoid snapshot (its newest
    backup point goes on them) or to archive them and resend them in full.
There is no floor imposed by the backups: rpool's anchors are bookmarks.

A drive (imported under I-G, no passphrase needed): what its retention
policy removes now (yes/no), then, beyond the policy, the oldest monthly
backup points and the history from before its first zark 2 backup point
(typed PRUNE). The drive's newest complete point and every live dataset's
newest snapshot (I-E) are never offered.
"""

import re
import time
from collections.abc import Callable
from functools import partial

from lib import anchors, drive_marks, reclaim, sh, status, survey
from lib.cleanup import Cleanup, prompt_eject_or_attach
from lib.config import Config
from lib.drives import ConnectedDrive, backup_device, scan_connected_drives
from lib.log import Log
from lib.origin_prune import Inventory, Item, inventory, older_than
from lib.replication import Dest
from lib.restore_points import Complete
from lib.retention import (
    DAY,
    Policy,
    Prune,
    Removal,
    first_point_time,
    is_live,
    plan_prune,
    point_removals,
    point_times,
    space_candidates,
    whole_datasets,
)
from lib.zfs import ZFS

WORD = "PRUNE"
# What zark 1.x, or sanoid, writes to a dataset: zark 2 moves only its points.
LEGACY_NAME = re.compile(r"^(autosnap|syncoid)_")
HISTORY_WHY = "older than the drive's first backup point, removed by hand"


def _gib(n: int) -> str:
    return sh.humanize_bytes(n)


def _when(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _typed(log: Log, what: str) -> bool:
    answer = log.ask_text(
        f"    Type {WORD} to {what}: ", accept=(WORD,), label=f"Type {WORD} to {what}"
    )
    if answer != WORD:
        log.info(f"Not confirmed ({WORD} was not typed) - nothing removed")
    return answer == WORD


def _capacity(log: Log, settle: bool = False) -> None:
    """Each source pool's fill level; ``settle``: after removals, once the
    pool has freed them. A destroy frees in the background, so `zpool list`
    right after it lags (78.9G shown on carmen, 105G a minute later)."""
    pools = survey.source_pools()
    busy: list[str] = reclaim.wait_freed(pools, log) if settle else []
    for pool in pools:
        r = sh.run(f"zpool list -Hp -o name,capacity,size,free {pool}")
        f = r.output.strip().split("\t") if r.ok else []
        if len(f) == 4 and f[2].isdigit() and f[3].isdigit():
            line = f"{pool}: {f[1]}% full ({_gib(int(f[3]))} free of {_gib(int(f[2]))})"
            if pool in busy:
                line += "; still freeing in the background, the free space will grow"
            log.info(line)


def _summary(inv: Inventory, drives: dict[str, str], log: Log) -> None:
    def line(label: str, items: list[Item]) -> None:
        if items:
            oldest = min(i.creation for i in items)
            freed = reclaim.reclaim_names([i.target for i in items])
            log.info(
                f"  {label:34} {len(items):5}  oldest {_when(oldest)}"
                + f"  all of them free {_gib(freed)}"
            )

    log.info("Snapshots in origin, by kind:")
    for kind in ("frequently", "hourly", "daily", "weekly", "monthly", "yearly"):
        line(f"sanoid {kind}", inv.autosnaps.get(kind, []))
    line("legacy residues (removable)", inv.residues)
    if inv.residues_kept:
        log.info(f"  {'legacy residues a drive may need':34} {len(inv.residues_kept):5}")
    line("backup points left in origin", inv.points)
    for guid, items in sorted(inv.anchors.items()):
        line(f"bpool anchors of {drives.get(guid, guid)}", items)


def _remove(items: list[Item], why: str, log: Log) -> None:
    count, failed = reclaim.destroy_snapshots([(i.target, i.reason or why) for i in items], log)
    log.ok(f"{count} snapshot(s) removed")
    if failed:
        log.warn(f"{len(failed)} could not be removed (see above)")


def _by_age(inv: Inventory, log: Log) -> None:
    raw = log.ask_input("Keep sanoid snapshots of how many days?", "30")
    if not raw.isdigit():
        log.warn("Not a number of days - nothing removed")
        return
    items = older_than(inv, int(time.time()) - int(raw) * DAY)
    if not items:
        log.info(f"No sanoid snapshot is older than {raw} days")
        return
    # M4d-32: a drive with no zark 2 anchor on a dataset the cut touches has
    # a sanoid snapshot there as its base; its next backup would roll back.
    # One line per drive naming the datasets (M4dR-3): the drive may have
    # had zark 2 backups and skipped these, not only be a 1.x drive.
    touched = {i.target.partition("@")[0] for i in items}
    lacking: dict[str, list[str]] = {}
    for rel in sorted(touched):
        for name in inv.unanchored.get(rel, []):
            lacking.setdefault(name, []).append(rel)
    for name, rels in sorted(lacking.items()):
        shown = ", ".join(rels[:3]) + (f" and {len(rels) - 3} more" if len(rels) > 3 else "")
        log.warn(
            f"{name} has no zark 2 anchor on {shown}: its base there is a sanoid "
            + "snapshot this can remove; back up to it first"
        )
    freed = reclaim.reclaim_names([i.target for i in items])
    log.warn(
        f"{len(items)} sanoid snapshot(s) older than {raw} days; "
        + f"removing them frees {_gib(freed)}"
    )
    log.file("  " + ", ".join(i.target for i in items))
    if _typed(log, f"remove {len(items)} sanoid snapshot(s)"):
        _remove(items, f"sanoid snapshot older than {raw} days", log)


def _residues(inv: Inventory, log: Log) -> None:
    for i in inv.residues_kept:
        log.info(f"  kept {i.target}: {i.reason}")
    freed = reclaim.reclaim_names([i.target for i in inv.residues])
    for i in inv.residues:
        log.info(f"  {i.target}: {i.reason}")
    if log.ask(f"Remove these {len(inv.residues)} residue(s) ({_gib(freed)})?", default=False):
        _remove(inv.residues, "legacy residue", log)


def _points(inv: Inventory, log: Log) -> None:
    try:
        owners = anchors.point_owners(survey.source_pools())
    except survey.ReadError as e:
        log.warn(f"Could not read which drive each point is for: {e}")
        owners = {}
    for i in inv.points:
        owner = owners.get(i.target, "")
        log.info(f"  {i.target}" + (f"  (for drive {owner})" if owner else ""))
    # A point names its drive since rc5 (8b): it is anchored for that drive
    # before it goes, so that drive keeps a base; a partial receive still
    # needs the snapshot itself. Older points carry no owner (eli R8.7, 8a; M3R-5).
    log.warn(
        "A point that names its drive is first anchored for it (on bpool it becomes that "
        + "drive's anchor), so that drive does not roll back; without a named drive, a "
        + "drive that received part of the point rolls those datasets back at its next "
        + "backup (it asks first). Either way, a drive whose transfer was interrupted "
        + "discards it and sends it again"
    )
    if _typed(log, f"remove {len(inv.points)} backup point(s)"):
        named = [i for i in inv.points if owners.get(i.target)]
        gone = sum(anchors.release_point(i.target, owners[i.target], log) for i in named)
        if named:
            log.ok(f"{gone} point snapshot(s) anchored for their drive and taken out")
        rest = [i for i in inv.points if not owners.get(i.target)]
        if rest:
            _remove(rest, "backup point left in origin", log)


def _anchors(inv: Inventory, drives: dict[str, str], log: Log) -> None:
    guids = sorted(inv.anchors)
    pick = log.ask_choice(
        "Whose bpool anchors?",
        ["none", *(f"{drives.get(g, g)} ({len(inv.anchors[g])} snapshot(s))" for g in guids)],
    )
    if pick == 0:
        return
    guid = guids[pick - 1]
    items = inv.anchors[guid]
    name = drives.get(guid, guid)
    freed = reclaim.reclaim_names([i.target for i in items])
    # The drive's newest snapshot on bpool is its backup point, so the only
    # common snapshots left are sanoid's: a rollback, never incremental (M3R-1).
    log.warn(
        f"Without them, {name}'s next backup finds only sanoid's snapshots in common on "
        + "these bpool datasets, or nothing: it asks to roll them back (the drive's newest "
        + "backup point goes on them) or to archive them and resend in full (a few hundred "
        + f"MB). Frees {_gib(freed)}."
    )
    if _typed(log, f"remove {name}'s {len(items)} bpool anchor(s)"):
        _remove(items, f"bpool anchor of drive {name}", log)


def _prune_origin(cfg: Config, log: Log) -> None:
    """The origin menu, until the user is done."""
    drives = {info.guid: name for name, info in cfg.known_drives.items()}
    acted = False
    while True:
        _capacity(log, settle=acted)
        try:
            origin = survey.read_origin(survey.source_pools(), strict=True)
        except survey.ReadError as e:
            log.fatal(f"Cannot read origin: {e}")
        inv = inventory(origin, drives)
        _summary(inv, drives, log)
        actions: list[tuple[str, Callable[[], None] | None]] = [("done", None)]
        if inv.autosnaps:
            actions.append(
                ("remove sanoid snapshots older than N days", partial(_by_age, inv, log))
            )
        if inv.residues:
            actions.append(("remove the legacy residues", partial(_residues, inv, log)))
        if inv.points:
            label = (
                "remove backup points left in origin (interrupted runs, a run whose anchor "
                + "failed, or a recovery)"
            )
            actions.append((label, partial(_points, inv, log)))
        if inv.anchors:
            actions.append(("remove a drive's bpool anchors", partial(_anchors, inv, drives, log)))
        pick = log.ask_choice("What to remove in origin?", [label for label, _ in actions])
        act = actions[pick][1]
        if act is None:
            return
        act()
        acted = True


def _legacy_history(dest: dict[str, Dest], pins: dict[str, str], first: str = "") -> Prune:
    """sanoid's and syncoid's snapshots from before the drive's first backup
    point (decision 6c: a drive written by zark 1.x, or a dataset that came
    with older snapshots, M4bF-12), every live dataset's newest kept (I-E)
    and pinned ones left. Backup points are dated by their newest snapshot,
    as the policy does, so no part of the first point is taken for history;
    other names are left. An archived lineage or kept orphan left with
    nothing goes whole, as with the policy (decision 7a): its head would
    keep its data with no snapshot recover could reach."""
    times = point_times(dest)
    start = first_point_time(times, first)
    out: list[Removal] = []
    for rel, d in sorted(dest.items()):
        newest = d.newest if is_live(rel, d) else None
        for s in d.snaps:
            if not LEGACY_NAME.match(s.name) or (newest and s.guid == newest.guid):
                continue
            if f"{rel}@{s.name}" in pins:
                continue
            if start is None or s.creation < start:
                out.append(Removal(rel, s.name, HISTORY_WHY))
    return Prune(snapshots=out, datasets=whole_datasets(dest, out))


def _reread(pool: str, log: Log) -> dict[str, Dest]:
    """The drive's listing after a removal; fatal when it cannot be read, as
    every read that decides a removal here is (M3R-8)."""
    try:
        dest = survey.read_dest(pool, strict=True)
    except survey.ReadError as e:
        log.fatal(f"Cannot read {pool}: {e}")
    return dest


def _prune_drive(cfg: Config, drive: ConnectedDrive, log: Log) -> None:  # pylint: disable=too-many-locals
    """Retention now, then beyond it, on one connected drive."""
    pool = drive.name
    zfs = ZFS(log)
    cleanup = Cleanup(log)
    cleanup.register()
    device = backup_device(drive)
    if not zfs.import_backup_pool(pool, device, guid=drive.guid):
        log.fatal(f"Cannot import pool {pool}", solutions=["Reconnect the drive and run again"])
    cleanup.track_pool(pool)
    drive_marks.check_drive_owner(pool, log)
    policy = cfg.drive_policy(pool)
    now = int(time.time())
    try:
        dest = survey.read_dest(pool, strict=True)
        complete = drive_marks.drive_complete(pool)
        pins = drive_marks.drive_pins(pool, strict=True)
    except survey.ReadError as e:
        log.fatal(f"Cannot read {pool}: {e}")
    prune = plan_prune(
        dest, complete.run, policy, now, whole=complete.whole, pinned=pins, first=complete.first
    )
    for target, why in prune.kept:  # as backup's retention reports them (M3X.5)
        log.info(f"  kept {pool}/{target}: {why}")
    if not prune.empty:
        freed = reclaim.reclaim(pool, prune.snapshots, [t for t, _ in prune.datasets])
        log.info(
            f"{pool}'s retention (window {policy.window_days} days) removes "
            + f"{len(prune.snapshots)} snapshot(s) now, freeing {_gib(freed)}"
        )
        if log.ask(f"Apply {pool}'s retention now?", default=False):
            _ = reclaim.apply_prune(pool, prune, log)
            dest = _reread(pool, log)
            prune = Prune()
    else:
        log.ok(f"{pool}'s retention removes nothing now")
    _beyond(pool, dest, complete, pins, policy, now, prune, log)
    cleanup.run()
    prompt_eject_or_attach(device, pool, log, default_eject=False)


def _beyond(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    pool: str,
    dest: dict[str, Dest],
    complete: Complete,
    pins: dict[str, str],
    policy: Policy,
    now: int,
    prune: Prune,
    log: Log,
) -> None:
    """The oldest monthly points, then the history from before the first
    point, each on a typed PRUNE."""
    # A point none of whose snapshots can go (I-E, pins) frees nothing: not
    # offered, as backup's space plan already skips it (M3 decision 6a).
    names = [
        name
        for name in space_candidates(dest, complete.run, policy, now, prune, complete.whole)
        if point_removals(dest, name, pinned=pins)
    ]
    if names:
        log.info(f"Beyond the policy, the oldest monthly backup points on {pool}:")
        removals: list[Removal] = []
        for i, name in enumerate(names, 1):
            removals += point_removals(dest, name, pinned=pins)
            # 7a (M3R-3): a lineage these removals leave empty goes whole, and
            # its `used` counts in what the removal frees.
            tops = [t for t, _ in whole_datasets(dest, removals)]
            freed = reclaim.reclaim(pool, removals, tops)
            log.info(f"  {i}) {name}  (with the ones above: {_gib(freed)})")
        raw = log.ask_input("How many of the oldest to remove? (0: none)", "0")
        n = min(int(raw), len(names)) if raw.isdigit() else 0
        chosen = [r for name in names[:n] for r in point_removals(dest, name, pinned=pins)]
        plan = Prune(snapshots=chosen, datasets=whole_datasets(dest, chosen))
        for top, _why in plan.datasets:
            log.info(f"  {pool}/{top} and everything below it go too (nothing of it would be left)")
        if chosen and _typed(log, f"remove the {n} oldest backup point(s) from {pool}"):
            _ = reclaim.apply_prune(pool, plan, log)
            dest = _reread(pool, log)
    legacy = _legacy_history(dest, pins, complete.first)
    if not legacy.empty:
        tops = [t for t, _ in legacy.datasets]
        freed = reclaim.reclaim(pool, legacy.snapshots, tops)
        whole = f", and {len(tops)} archived or kept dataset(s) left empty" if tops else ""
        log.info(
            f"{pool} holds {len(legacy.snapshots)} snapshot(s) from before its first backup "
            + f"point{whole}; removing them frees {_gib(freed)}"
        )
        if _typed(log, f"remove the history from before {pool}'s first backup point"):
            _ = reclaim.apply_prune(pool, legacy, log)


def run(args: list[str]) -> None:
    """Main entry point for 'zark prune'."""
    _ = args
    log = Log()
    cfg = Config.load()
    cfg.check_registry(log, fatal=True)
    log.banner("PRUNE", "Reclaim space in origin or on a backup drive")
    drives = [d for d in scan_connected_drives(cfg, log) if d.known]
    targets: list[str] = []
    if "rpool" in survey.source_pools():
        targets.append("origin (rpool, bpool)")
    targets += [f"backup drive {d.name}" for d in drives]
    if not targets:
        log.fatal(
            "Nothing to prune here",
            causes=["rpool is not imported and no registered drive is connected"],
        )
    pick = log.ask_choice("Prune what?", targets)
    if targets[pick].startswith("origin"):
        _prune_origin(cfg, log)
    else:
        _prune_drive(cfg, drives[pick - (len(targets) - len(drives))], log)
    status.refresh_quietly(cfg)
