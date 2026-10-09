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
zark backup — Back up this system to a connected, registered drive.

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

from lib import engine, sh
from lib.cleanup import Cleanup, prompt_eject_or_attach
from lib.config import VERSION, Config, now_utc_iso
from lib.drives import (
    backup_device,
    drive_staleness_days,
    scan_connected_drives,
    select_drive,
)
from lib.identity import by_id_names, preferred_by_id, whole_disk
from lib.keystore import Keystore, open_keystore
from lib.log import Log
from lib.mount import warn_kernel_named_vdevs, warn_rpool_mountpoint_lost
from lib.replication import TRANSFERABLE, DatasetPlan, State, point_name
from lib.zfs import ZFS, PoolInfo

# Remediation for a full backup drive. `zark purge` erases the whole drive,
# so it is never the answer to "out of space" (hallazgo 8).
_FREE_SPACE_HINT = (
    "Free space on the drive: destroy old destination snapshots by exact name, "
    "keeping each dataset's newest one (never a % range) — "
    "zfs list -t snapshot -o name,used -s creation -r <pool>"
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


def _report_rpo(cfg: Config, pool_name: str, log: Log) -> None:
    """Days since every other registered drive's last backup (decision 15).

    Bookmark-anchored drives never lose their anchor through origin
    retention, so there is no divergence deadline to warn about: only how
    old each drive's newest point is.
    """
    for name, info in sorted(cfg.known_drives.items()):
        if name == pool_name:
            continue
        age = drive_staleness_days(info)
        if age is None:
            log.info(f"  {name}: no backup recorded")
        else:
            log.info(f"  {name}: {age} day(s) since its last backup")


TOTAL_STEPS = 8

# Free-space margin: 1% of the source's used data, with a 1 GiB floor.
# Lax by design — only fires when the target is essentially full, where
# any incremental will fail. The reactive ENOSPC handler in run() catches
# in-flight exhaustion. This guard avoids starting a long transfer
# that is mathematically guaranteed to fail.
_ENOSPC_GUARD_FLOOR_BYTES = 1024**3  # 1 GiB
_ENOSPC_GUARD_PCT_OF_SOURCE = 100  # divisor: used_bytes // 100 == 1%


def _check_target_space(
    src_info: PoolInfo | None,
    dst_info: PoolInfo | None,
    pool_name: str,
    source_pool: str,
    log: Log,
) -> None:
    """Coherence + preventive ENOSPC checks before starting a backup.

    Two distinct checks:

    1. Coherence (warn only): the backup invariant is that the destination
       drive is at least as large as the source pool. A smaller target
       will eventually run out of space even with a perfect retention
       policy. Warn — the operator may know what they're doing (testing
       on a small loop, compression headroom, etc.).

    2. Preventive ENOSPC (fatal): if the target's free space is below
       1% of the source's used data (with a 1 GiB floor), refuse to
       start. This prevents kicking off a long transfer that is
       guaranteed to ENOSPC mid-stream.

    Either check is silently skipped when the corresponding PoolInfo
    is None or carries zero-valued bytes (transient zpool list failure,
    or a pool we couldn't measure for any reason). The reactive ENOSPC
    handler still catches in-flight exhaustion in those cases.
    """
    if src_info is None or dst_info is None:
        return

    # Check 1 — coherence warn
    if (
        dst_info.size_bytes > 0
        and src_info.size_bytes > 0
        and dst_info.size_bytes < src_info.size_bytes
    ):
        log.warn(
            f"Target {pool_name} ({dst_info.size}) is smaller than source "
            f"{source_pool} ({src_info.size}) — backup may eventually fail",
        )

    # Check 2 — preventive ENOSPC fatal
    if src_info.used_bytes <= 0:
        return  # cannot compute threshold, defer to reactive handler

    threshold = max(
        src_info.used_bytes // _ENOSPC_GUARD_PCT_OF_SOURCE,
        _ENOSPC_GUARD_FLOOR_BYTES,
    )
    if dst_info.avail_bytes < threshold:
        log.fatal(
            f"Insufficient free space on {pool_name}",
            causes=[
                f"Source {source_pool}: used={src_info.used} "
                f"({sh.humanize_bytes(src_info.used_bytes)})",
                f"Target {pool_name}: avail={dst_info.avail} "
                f"({sh.humanize_bytes(dst_info.avail_bytes)})",
                f"Required margin: ≥ {sh.humanize_bytes(threshold)} (1% of source, min 1 GiB)",
            ],
            solutions=[
                _FREE_SPACE_HINT,
                "Source has grown — consider a larger backup drive",
            ],
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


def _decide(  # noqa: C901 # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    plans: list[DatasetPlan],
    eng: engine.Run,
    pool_name: str,
    dst_info: PoolInfo | None,
    log: Log,
) -> bool:
    """Show the plan, ask every question before the first byte is sent,
    check space, then apply the user's choices. False aborts the run."""
    sizes = {p.rel: eng.estimate(p) for p in plans if p.state in TRANSFERABLE}
    counts: dict[str, int] = {}
    for p in plans:
        counts[str(p.state)] = counts.get(str(p.state), 0) + 1
    log.info("Plan: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    for p in plans:
        if p.state is State.EXCLUDED:
            log.warn(f"  {p.rel}: NOT BACKED UP ({p.note})")
        elif p.state is State.VIA_BOOKMARK:
            log.info(f"  {p.rel}: from this drive's bookmark")
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
        )
        if pick == 1:
            actions.append(partial(eng.keep_orphan, p, datetime.now(UTC).strftime("%Y-%m-%d")))
        elif pick == 2:
            actions.append(partial(eng.destroy, p))
            confirmed_any = True
        elif pick == 3:
            actions.append(partial(eng.follow_rename, p))

    # Only an older common snapshot left (§7 migration; recovered older point).
    rollbacks = [p for p in plans if p.state is State.ROLLBACK]
    if rollbacks:
        pre_v2 = engine.read_metadata(pool_name).get("format") != engine.FORMAT
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
            shown = ", ".join(n.lstrip("@") for n in p.newer[:3]) + (
                " …" if len(p.newer) > 3 else ""
            )
            log.info(f"  {p.rel}: common {p.common.lstrip('@')}; newer on the backup: {shown}")
        tops = _tops([p.rel for p in rollbacks])
        before = dict(sizes)
        full = sum(resend(t) for t in tops)
        archived_sizes, sizes = sizes, before
        opts = ["abort the backup (nothing changes)"]
        fits = sum(archived_sizes.values()) <= avail
        if fits:
            below = sum(len(_subtree(plans, t)) for t in tops) - len(tops)
            opts.append(
                f"archive {', '.join(tops)}"
                + (f" (and {below} dataset(s) below)" if below else "")
                + f" and resend in full ({_gib(full)})",
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
            sizes = archived_sizes
            moved.update(tops)
            actions += [partial(eng.archive, p) for p in rollbacks if p.rel in tops]
        else:
            # After the rollback each one is an incremental from its common snapshot.
            sizes.update({p.rel: eng.estimate(p) for p in rollbacks})
            actions += [partial(eng.rollback, p) for p in rollbacks]
            confirmed_any = True

    # Nothing in common (D17). A dataset below one already archived or
    # destroyed goes with it and is not asked about.
    for p in plans:
        if p.state is not State.DIVERGED or _under(p.rel, moved):
            continue
        before = dict(sizes)
        full_size = resend(p.rel)
        below = len(_subtree(plans, p.rel)) - 1
        extra = f" (and {below} dataset(s) below)" if below else ""
        opts = ["skip it this time (the backup will be INCOMPLETE)"]
        fits = sum(sizes.values()) <= avail
        if fits:
            opts.append(f"archive the old lineage{extra} and resend in full ({_gib(full_size)})")
        opts.append(
            f"destroy it{extra} on the backup and resend in full ({_gib(p.used)} destroyed)"
        )
        log.warn(f"  {p.rel}: no snapshot in common with the backup ({p.note})")
        pick = _ask_confirmed(
            log,
            f"{p.rel} has diverged:",
            opts,
            {len(opts) - 1: ("DESTROY", f"destroy {pool_name}/{p.rel} and everything below it")},
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
    if dst_info and needed > avail:
        log.fatal(
            f"Not enough space on {pool_name} for this backup",
            causes=[f"Needs about {_gib(needed)}, {_gib(avail)} available"],
            solutions=[_FREE_SPACE_HINT, "Connect a larger backup drive"],
        )
    if confirmed_any:
        log.ok("Applying the confirmed changes on the backup")
    for act in actions:
        _ = act()
    return True


def _subtree(plans: list[DatasetPlan], top: str) -> list[DatasetPlan]:
    """Planned datasets at or below ``top`` that a rename or destroy of it moves."""
    return [
        p
        for p in plans
        if (p.rel == top or p.rel.startswith(f"{top}/"))
        and p.state not in (State.ORPHAN, State.EXCLUDED)
    ]


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
    log: Log, question: str, opts: list[str], confirm: dict[int, tuple[str, str]]
) -> int:
    """Numbered choice whose destructive options need a typed word.

    A word that does not match is no answer: the same question is asked
    again, so a typo can neither destroy nor silently pick another option.
    """
    while True:
        pick = log.ask_choice(question, opts, 0)
        if pick not in confirm or _typed(log, *confirm[pick]):
            return pick
        log.info(f"Not confirmed ({confirm[pick][0]} was not typed) — choose again")


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
        else:
            mark = f"NOT at the point: {out.error or p.state}"
        line = f"  {p.rel:<48} {mark}"
        if p.state is State.AT_POINT:
            log.info(line)
        else:
            log.warn(line)


def _write_metadata(pool_name: str, source_pool: str, point: str, log: Log) -> None:
    """Decision 17a. The point and its time are recorded only for a complete run."""
    rpool_guid = sh.run(f"zpool get -H -o value guid {source_pool}").output.strip()
    fields = {
        "format": engine.FORMAT,
        "version": VERSION,
        "origin-host": socket.gethostname(),
        "origin-rpool-guid": rpool_guid,
        "origin-machine-id": engine.machine_id(),
    }
    if point:
        fields |= {"last-backup-at": now_utc_iso(), "last-point": point}
    _ = engine.write_metadata(pool_name, fields, log)


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
    started = time.time()

    # ── Refuse live USB ──────────────────────────────────────────────────
    if _detect_live_usb():
        log.fatal(
            "Live USB detected — backup blocked",
            causes=[
                "Running backup from live USB would back up the wrong system",
                "It corrupts ZFS mountpoints on the backup drive",
            ],
            solutions=["Boot into your installed system and run backup from there"],
        )

    warn_rpool_mountpoint_lost(log)
    warn_kernel_named_vdevs(log)
    engine.require_bookmark_v2(log)

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
    engine.check_drive_owner(pool_name, log)
    _heal_drive_id(cfg, pool_name, device, log)

    # ── Check health ─────────────────────────────────────────────────────
    log.step(3, TOTAL_STEPS, "Checking pool health...")

    for pool in (cfg.source_pool, pool_name):
        health = zfs.pool_health(pool)
        if health == "ONLINE":
            log.ok(f"{pool}: ONLINE")
        elif health == "DEGRADED":
            log.warn(f"{pool}: DEGRADED")
            if not log.ask(f"{pool} is DEGRADED. Continue anyway?"):
                log.fatal(
                    f"Aborted — {pool} is DEGRADED",
                    solutions=[f"Run: zpool status {pool}"],
                )
        else:
            log.fatal(f"{pool} is {health}", solutions=[f"Run: zpool status {pool}"])

    # ── Pool info summary ────────────────────────────────────────────────
    log.step(4, TOTAL_STEPS, "Gathering pool info...")

    src_info = zfs.pool_info(cfg.source_pool)
    dst_info = zfs.pool_info(pool_name)

    _check_target_space(src_info, dst_info, pool_name, cfg.source_pool, log)

    if dst_info and dst_info.pct_used >= 90:
        log.warn(f"Target is {dst_info.pct_used}% full — consider pruning snapshots")
        if not log.ask(f"Target is {dst_info.pct_used}% full. Continue?"):
            log.fatal("Aborted — disk too full")
    elif dst_info and dst_info.pct_used >= 80:
        log.warn(f"Target is {dst_info.pct_used}% full — consider pruning soon")

    log.info(
        f"Source: {cfg.source_pool}  used={src_info.used if src_info else '?'}  "
        + f"avail={src_info.avail if src_info else '?'}",
    )
    log.info(
        f"Target: {pool_name}  used={dst_info.used if dst_info else '?'}  "
        + f"({dst_info.pct_used if dst_info else '?'}%)  "
        + f"avail={dst_info.avail if dst_info else '?'}",
    )

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

    def decide(plans: list[DatasetPlan], run_: engine.Run) -> bool:
        return _decide(plans, run_, pool_name, dst_info, log)

    log.step(7, TOTAL_STEPS, "Replicating (this may take a while)...")
    res = engine.execute(eng, cleanup, decide)
    if res.aborted:
        log.fatal(f"Backup not started: {res.error or 'aborted'}")

    _show_table(res, eng.point, log)
    used_after = zfs.pool_info(pool_name)  # P0-4: after the transfer
    _write_metadata(pool_name, cfg.source_pool, eng.point if res.ok else "", log)

    # ── Export and read back ─────────────────────────────────────────────
    ks.umount()
    cleanup.run()
    elapsed = int(time.time() - started)  # P0-2: the whole run
    mins, secs = divmod(elapsed, 60)

    log.step(8, TOTAL_STEPS, f"Verifying {pool_name} is reimportable...")
    if pool_name not in cleanup.exported_pools():
        # H17: an export that failed is a failed backup, never a skipped check.
        log.banner_error(
            "BACKUP NOT VERIFIED",
            [
                f"{pool_name} could not be exported, so it was not read back.",
                "Do NOT disconnect the drive. Check: zpool status; then",
                f"  sudo zpool export {pool_name}",
            ],
        )
        _notify("❌ Backup NOT verified", f"{pool_name} could not be exported")
        raise SystemExit(1)
    rb = zfs.verify_exported_pool_readback(pool_name, device=device)
    if not rb.ok:
        log.banner_error("BACKUP NOT VERIFIED", [*rb.describe(), "", "Do NOT rely on this backup."])
        _notify("❌ Backup NOT verified", f"{pool_name} failed its read-back")
        raise SystemExit(1)
    log.ok(f"{pool_name} verified reimportable (ONLINE)")

    if not res.ok:
        missing = [
            p.rel
            for p in res.plans
            if p.state not in (State.AT_POINT, State.ORPHAN, State.EXCLUDED)
        ]
        log.banner_error(
            "BACKUP INCOMPLETE",
            [
                f"{len(missing)} dataset(s) did not reach {eng.point}:",
                *[f"  {rel}" for rel in missing[:15]],
                *(
                    [f"  …and {len(missing) - 15} more (see the table above)"]
                    if len(missing) > 15
                    else []
                ),
                "",
                "The drive keeps everything it had. Run backup again;",
                "an interrupted transfer resumes where it stopped.",
            ],
        )
        _notify("❌ Backup incomplete", f"{len(missing)} dataset(s) not backed up")
        raise SystemExit(1)

    # ── Persist last_backup_at (only for a complete, verified backup) ────
    info = cfg.known_drives.get(pool_name)
    if info is not None:
        info.last_backup_at = now_utc_iso()
        try:
            cfg.save_drives()
            log.dbg(f"Recorded last_backup_at={info.last_backup_at} for {pool_name}")
        except OSError as e:
            log.warn(f"Could not persist last_backup_at for {pool_name}: {e}")

    log.banner_ok(
        "BACKUP COMPLETED",
        [
            f"Point:          {log.W}{eng.point}{log.N}",
            f"Duration:       {log.W}{mins}m {secs}s{log.N}",
            f"Used on target: {log.W}{used_after.used if used_after else '?'}{log.N}",
            f"Available:      {log.W}{used_after.avail if used_after else '?'}{log.N}",
        ],
    )
    _report_rpo(cfg, pool_name, log)

    prompt_eject_or_attach(
        device,
        pool_name,
        log,
        default_eject=True,
        autoeject=cfg.drive_autoeject(pool_name),
    )

    _notify("✅ Backup completed", f"Duration: {mins}m {secs}s")
