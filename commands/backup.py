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
zark backup — Run incremental backup to connected drive.

Auto-detects connected known drive, imports pool, loads key,
runs syncoid (raw send), syncs bpool, exports pool.

Refuses to run from live USB (would back up the wrong system).

Divergence handling: when syncoid aborts with "cowardly refusing"
(no common snapshots between source and target), backup invokes the
auto-repair logic in ``lib.repair`` to destroy any divergent datasets
under 64MB and re-run syncoid so it recreates them via initial
replication. Datasets above 64MB abort with a clear message asking
the user to run ``zark repair-divergent`` interactively.
"""

import time
from dataclasses import dataclass
from pathlib import Path

from lib import repair, sh
from lib.cleanup import Cleanup, prompt_eject_or_attach
from lib.config import Config, now_utc_iso
from lib.drives import (
    drive_staleness_days,
    drives_in_danger_zone,
    scan_connected_drives,
    select_drive,
)
from lib.keystore import Keystore
from lib.log import Log
from lib.sanoid_retention import worst_case_retention_days
from lib.zfs import ZFS, PoolInfo, syncoid_exclude_flag

# How many days before retention runs out we start mentioning a drive
# at the end of a successful backup. With 90-day retention, drives
# untouched for 60+ days appear in the "danger zone" list.
_DANGER_ZONE_MARGIN_DAYS = 30


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


def _report_staleness_at_end(
    cfg: Config,
    pool_name: str,
    age_at_start: int | None,
    retention: int | None,
    log: Log,
) -> None:
    """Print staleness reporting *after* the backup completes successfully.

    Two distinct messages, both purely informative (no FATAL):

    1. If the just-backed-up drive was already past the retention horizon
       when we started this run, WARN the operator that it had crossed
       the line — the backup we just finished may have only worked
       because some shared snapshot happened to still exist, and next
       time around it might not. Also explicitly notes that
       ``zark repair-divergent`` does NOT fix staleness — it only fixes
       divergence after a syncoid abort, which is a different problem.

    2. INFO list of *other* known drives whose age has reached the
       danger zone (``≥ retention - 30 days``), so the operator knows
       which drive to grab next. The drive we just backed up is
       intentionally excluded — its age is now zero and listing it
       would be misleading.

    Both messages are no-ops when ``retention`` is None (sanoid.conf
    missing or empty) — staleness reporting is unavailable in that
    case, with no value lost.
    """
    if retention is None:
        return

    # Message 1: drive that was expired at the start of the run.
    if age_at_start is not None and age_at_start > retention:
        log.warn(
            f"Drive '{pool_name}' was {age_at_start} day(s) old when this "
            f"backup started — past the {retention}-day retention horizon",
        )
        log.info(
            "  This run succeeded, but next time the source's sanoid retention",
        )
        log.info(
            "  may have purged the last shared snapshot. Consider rotating to",
        )
        log.info(
            "  a fresher drive, or fully reinitialize this one:",
        )
        log.info(
            "    sudo ./zark purge /dev/sdX  &&  sudo ./zark prepare /dev/sdX",
        )
        log.info(
            "  Note: 'zark repair-divergent' does NOT fix staleness — it only",
        )
        log.info(
            "  helps after syncoid aborts on actual divergent datasets.",
        )

    # Message 2: other drives in the danger zone.
    danger = drives_in_danger_zone(
        cfg.known_drives,
        retention,
        _DANGER_ZONE_MARGIN_DAYS,
        exclude=pool_name,
    )
    if danger:
        log.info("Other drives approaching retention horizon:")
        for name, age in danger:
            remaining = max(0, retention - age)
            log.info(
                f"  {name}: {age} day(s) old, ~{remaining} day(s) left before divergence",
            )


# Free-space margin: 1% of the source's used data, with a 1 GiB floor.
# Lax by design — only fires when the target is essentially full, where
# any incremental will fail. The reactive ENOSPC handler in run() catches
# in-flight exhaustion. This guard avoids starting a long syncoid run
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
       start. This prevents kicking off a long syncoid run that is
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
                "Purge old snapshots on target: sudo ./zark purge",
                "Source has grown — consider a larger backup drive",
            ],
        )


# ── Snapshot policy ─────────────────────────────────────────────────────
#
# Sanoid takes snapshots automatically via its systemd timer (enabled
# by `zark setup`), typically hourly. That means when an operator
# runs `zark backup` they may be replicating state that is up to ~1
# hour old. Since taking a snapshot is cheap (seconds, no I/O on the
# backup drive, idempotent — sanoid won't duplicate within the same
# retention window), backup now invokes `sanoid --take-snapshots`
# before every replication. Result: the backup drive always holds
# the most current state of the source pool.
#
# `--no-snapshot` exists as an escape hatch. Realistic uses are
# narrow (re-running backup after a transient failure when the
# operator already triggered sanoid by hand, or pure paranoia about
# triple-tagging a pool that's tightly bounded by retention) but the
# flag is cheap to keep and lets a script declare its intent
# explicitly.


@dataclass
class BackupArgs:
    """Parsed backup command-line arguments."""

    take_snapshots: bool = True  # default: always take snapshots before syncoid


def _parse_args(args: list[str]) -> BackupArgs:
    """Parse backup's command-line arguments.

    Recognised arguments (all optional, any order):
      --no-snapshot    skip the sanoid snapshot stage and replicate
                       whatever snapshots already exist

    Unknown flags are ignored, consistent with the rest of the
    codebase.
    """
    parsed = BackupArgs()
    if "--no-snapshot" in args:
        parsed.take_snapshots = False
    return parsed


def _take_snapshots(log: Log) -> None:
    """Run `sanoid --take-snapshots` with friendly logging.

    A failure here is non-fatal: sanoid may emit warnings about a few
    datasets while still snapshotting the rest, and we'd rather
    proceed with backup than abort because of a noisy edge case. The
    operator sees the warning in the log either way.
    """
    log.info("Taking fresh snapshots via sanoid...")
    r = sh.run("sanoid --take-snapshots", timeout=300, log=log)
    if r.ok:
        log.ok("Sanoid snapshots taken")
    else:
        log.warn("sanoid --take-snapshots had errors — proceeding with backup anyway")


def run(
    args: list[str],
):  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    """Main backup function."""
    opts = _parse_args(args)
    log = Log()
    cfg = Config.load()
    zfs = ZFS(log)
    cleanup = Cleanup(log)
    cleanup.register()

    log.banner("ZFS BACKUP", f"Source: {cfg.source_pool}")

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

    # ── Find and select drive ────────────────────────────────────────────
    log.step(1, 9, "Scanning for known backup drives...")
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

    # Capture how old the recorded backup is before we overwrite it. The
    # value is reported back to the operator at the end of the run if
    # the drive was already past the retention horizon when we started.
    selected_info = cfg.known_drives.get(pool_name)
    age_at_start: int | None = (
        drive_staleness_days(selected_info) if selected_info is not None else None
    )
    if age_at_start is not None:
        log.dbg(f"Last backup on {pool_name}: {age_at_start} day(s) ago")

    # Worst-case retention horizon from sanoid.conf. Read once per
    # backup run and reused for the staleness reporting at the end and
    # for the --no-snapshot anchor check below.
    retention_days = worst_case_retention_days(log)

    # ── Import pool ──────────────────────────────────────────────────────
    log.step(2, 9, f"Importing pool {pool_name}...")

    device = None
    if drive.drive_id != "<unknown>":
        by_id = Path(f"/dev/disk/by-id/{drive.drive_id}")
        if by_id.exists():
            device = str(by_id)
        # ZFS may auto-partition: check for -part1
        part1 = Path(f"/dev/disk/by-id/{drive.drive_id}-part1")
        if part1.exists():
            device = str(part1)

    if not zfs.pool_import(pool_name, device=device):
        log.fatal(
            f"Cannot import pool {pool_name}",
            solutions=[
                f"Try: zpool import -f {pool_name}",
                "Check: zpool import  (list importable pools)",
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

    # ── Check health ─────────────────────────────────────────────────────
    log.step(3, 9, "Checking pool health...")

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
    log.step(4, 9, "Gathering pool info...")

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
    log.step(5, 9, "Loading encryption key...")

    keystatus = zfs.get_property(f"{pool_name}/rpool", "keystatus")
    ks = Keystore(log)

    if keystatus == "available":
        log.ok("Key already loaded")
        if not log.ask("Key already loaded. Proceed with backup?", default=True):
            log.fatal("Aborted by user")
    else:
        passphrase = log.ask_password(f"Enter passphrase for {pool_name}")

        if not ks.mount(pool_name, passphrase):
            log.fatal(
                "Cannot open keystore",
                causes=["Wrong passphrase", "Keystore not found"],
                solutions=["Re-prepare the drive: sudo ./zark prepare /dev/sdX"],
            )

        cleanup.track_keystore(ks)

        loaded = ks.load_pool_keys(f"{pool_name}/rpool")
        if loaded == 0:
            log.fatal(
                "Cannot load encryption key for backup pool",
                solutions=["Re-prepare the drive"],
            )
        log.ok(f"Encryption key loaded ({loaded} datasets)")

    # ── Take fresh snapshots ─────────────────────────────────────────────
    log.step(6, 9, "Taking fresh snapshots before backup...")
    if opts.take_snapshots:
        _take_snapshots(log)
    else:
        log.info("--no-snapshot: replicating existing snapshots")
        # When --no-snapshot is in effect, this run relies entirely on
        # whatever snapshots already exist on source. If sanoid hasn't
        # run recently enough to leave a fresh anchor that is also
        # present on the target, syncoid will abort. Surface a WARN
        # here so the operator sees the risk before syncoid emits its
        # own (much noisier) error. Best-effort: if the check itself
        # fails, we don't block — syncoid is the authoritative answer.
        anchor = sh.run(
            f"zfs list -H -o name -t snapshot -s creation {cfg.source_pool} | tail -1",
        )
        if not anchor.ok or not anchor.output.strip():
            log.warn(
                "--no-snapshot is set but no recent source snapshot was found; "
                "syncoid may abort with no shared anchor",
            )

    # ── Run syncoid ──────────────────────────────────────────────────────
    log.step(7, 9, "Running syncoid (this may take a while)...")
    log.info("Tip: run 'sudo ./zark monitor' in another terminal for progress")

    _notify("🔄 Backup started", f"Syncing {cfg.source_pool} → {pool_name}...")

    target_pool = f"{pool_name}/rpool"
    start = time.time()

    # The base sync command. Held in a variable because we may need to
    # invoke it twice: once normally, and once after auto-repair if
    # syncoid aborts with "cowardly refusing" due to divergent datasets.
    #
    # ``--no-sync-snap`` tells syncoid not to create its own
    # ``@syncoid_<host>_<ts>`` snapshots and instead anchor every
    # incremental on the most recent snapshot it finds in source.
    # Sanoid (run in step 6) provides those anchors already, so the
    # extra ``syncoid_*`` snapshot is redundant. More importantly,
    # without ``--no-sync-snap`` syncoid runs ``pruneoldsyncsnaps``
    # after each transfer, which destroys *both* the source and
    # target's previous ``@syncoid_*`` snapshots — including ones the
    # other backup drives still depend on. The result was the
    # alternating "could not find any snapshots to destroy / WARNING:
    # zfs destroy ... failed: 256" cascade visible whenever the user
    # rotated between drives. Disabling sync-snap eliminates the bug
    # at the source.
    #
    # syncoid 2.3.0+ uses --exclude-datasets; older releases use
    # --exclude.
    excl = syncoid_exclude_flag()
    rpool_syncoid_cmd = (
        "syncoid --recursive --no-privilege-elevation --no-sync-snap "
        + "--sendoptions=w "
        + f"{excl}=rpool/keystore "
        + f"{cfg.source_pool} {target_pool}"
    )
    r = sh.run(rpool_syncoid_cmd, log=log)

    # If syncoid aborted with "cowardly refusing" (divergent datasets that
    # exist on both sides without a common snapshot), try auto-repair: any
    # divergent dataset under 64MB is destroyed so the next syncoid run
    # recreates it via initial replication. Datasets above the threshold
    # may contain real user data and are not touched — we abort with a
    # clear pointer to the interactive `zark repair-divergent` command.
    if not r.ok and repair.is_divergence_error(r.stdout):
        log.warn("Source and target have no common snapshots on one or more datasets")
        log.info(
            "This usually means the drive has been disconnected longer than "
            "your sanoid retention policy. Attempting auto-repair of small "
            "datasets (< 64MB)...",
        )
        ok, big = repair.auto_repair_under_64mb(zfs, cfg.source_pool, pool_name, log)
        if not ok:
            log.fatal(
                "Auto-repair could not resolve all divergent datasets",
                causes=[
                    f"{len(big)} dataset(s) exceed the 64MB safety limit:",
                    *[f"  {d.target}  used: {d.used_human}" for d in big],
                    "These may contain real data — zark refuses to destroy them silently",
                ],
                solutions=[
                    "Run 'sudo ./zark repair-divergent' for an interactive review",
                ],
            )
        log.info("Re-running syncoid after auto-repair...")
        # Sleep 2s so any new syncoid_* snapshot from the failed attempt
        # doesn't collide with the next one (timestamp resolution: 1 s).
        time.sleep(2)
        r = sh.run(rpool_syncoid_cmd, log=log)

    elapsed = int(time.time() - start)
    mins, secs = divmod(elapsed, 60)

    # syncoid returns non-zero on partial success; check actual transfers
    if not r.ok:
        # Reactive ENOSPC: must be checked BEFORE the "sent > 0 → warn"
        # branch below, otherwise a syncoid that managed to copy the
        # first dataset and then ran out of space would fall through
        # as a "partial success" and continue to bpool sync (which
        # would also ENOSPC). Checking here turns it into a clean fatal.
        if sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout):
            log.fatal(
                f"Backup ran out of space on {pool_name} after {mins}m",
                causes=[
                    f"{pool_name} filled up during transfer",
                    "Incremental was larger than estimated, or target was already nearly full",
                ],
                solutions=[
                    "Purge old snapshots on target: sudo ./zark purge",
                    "Connect a larger backup drive",
                    "Then re-run: sudo ./zark backup (syncoid resumes)",
                ],
            )
        sent = r.stdout.count("Sending") + r.stdout.count("INFO: Sending")
        if sent > 0:
            log.warn(f"Syncoid had warnings but transferred {sent} dataset(s)")
        else:
            log.fatal(
                f"Syncoid failed after {mins}m",
                causes=[
                    "USB interruption",
                    "Out of space",
                    "Snapshot deleted during sync",
                ],
                solutions=[
                    f"Check log: {cfg.log_file}",
                    "Syncoid will resume from where it left off on next run",
                ],
            )

    log.ok(f"rpool synced in {mins}m {secs}s")

    # ── Sync bpool ───────────────────────────────────────────────────────
    log.step(8, 9, "Syncing bpool (kernels + grub)...")

    if zfs.pool_exists("bpool"):
        bpool_syncoid_cmd = (
            "syncoid --recursive --no-privilege-elevation --no-sync-snap "
            + "--preserve-properties "
            + f"bpool {pool_name}/bpool"
        )
        r = sh.run(bpool_syncoid_cmd, log=log)
        if r.ok:
            log.ok("bpool synced")
        elif sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout):
            # bpool ENOSPC: do not auto-recreate. Auto-recreate destroys
            # the stale target and re-sends from scratch — but if rpool
            # already filled the drive, bpool will not fit either, and
            # destroying the stale bpool leaves the user with no boot
            # backup at all. Fatal here, preserving whatever bpool data
            # is still on the target.
            log.fatal(
                f"bpool backup ran out of space on {pool_name}",
                causes=[
                    f"{pool_name} has no room for bpool after rpool sync",
                    "rpool likely consumed the headroom intended for bpool",
                ],
                solutions=[
                    "Purge old snapshots on target: sudo ./zark purge",
                    "Connect a larger backup drive",
                    "Then re-run: sudo ./zark backup",
                ],
            )
        elif "no snapshots matching" in r.stdout.lower():
            # bpool is small (~150MB) and recover regenerates initrd via
            # dracut anyway, so destroying the stale target and resending
            # from scratch is safe and fast. Always auto-recreate.
            log.info(
                "No common snapshots with "
                + f"{pool_name}/bpool — recreating from scratch "
                + "(sanoid purged the shared syncoid snapshot)",
            )
            r = sh.run(f"zfs destroy -r {pool_name}/bpool", log=log)
            if not r.ok:
                log.warn(
                    f"Could not destroy {pool_name}/bpool — "
                    + "bpool backup is stale (recover still works via dracut)",
                )
            else:
                # Sleep 2s before relaunching so the next syncoid generates
                # a fresh timestamp and doesn't collide with the failed run.
                time.sleep(2)
                r = sh.run(bpool_syncoid_cmd, log=log)
                if r.ok:
                    log.ok("bpool resynced from scratch ✓")
                elif sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout):
                    # Same reasoning as above: ENOSPC during a from-scratch
                    # bpool resync means the drive is full. Fatal.
                    log.fatal(
                        f"bpool resync ran out of space on {pool_name}",
                        causes=[f"{pool_name} is full after rpool sync"],
                        solutions=[
                            "Purge old snapshots: sudo ./zark purge",
                            "Connect a larger backup drive",
                        ],
                    )
                else:
                    log.warn(
                        "bpool resync failed — bpool backup is stale "
                        + "(recover still works via dracut, but consider "
                        + "investigating)",
                    )
        else:
            log.warn(
                f"bpool sync failed (rc={r.returncode}) — bpool backup may "
                + "be stale (recover still works via dracut --regenerate-all)",
            )
    else:
        log.dbg("No bpool found — skipping")

    # ── Sync properties ──────────────────────────────────────────────────
    log.step(9, 9, "Syncing ZFS properties...")

    # Find ubuntu dataset name
    ubuntu_ds = sh.run(
        "zfs list -H -o name -r rpool/ROOT "
        + "| grep -v '^rpool/ROOT$' | grep -v '@' | awk -F/ 'NF==3' | head -1",
    ).output
    if ubuntu_ds:  # pylint: disable=too-many-nested-blocks
        # ubuntu_suffix = ubuntu_ds.replace("rpool/ROOT/", "")
        # Sync canmount from source to backup
        r = sh.run(f"zfs get -H -o name,value canmount -r {ubuntu_ds}")
        if r.ok:
            for line in r.lines:
                parts = line.split("\t")
                if len(parts) >= 2 and "@" not in parts[0]:
                    src_ds = parts[0].strip()
                    src_val = parts[1].strip()
                    dst_ds = src_ds.replace("rpool/", f"{pool_name}/rpool/", 1)
                    if zfs.dataset_exists(dst_ds):
                        cur = zfs.get_property(dst_ds, "canmount")
                        if cur != src_val:
                            _ = zfs.set_property(dst_ds, "canmount", src_val)
                            log.dbg(f"Fixed canmount {dst_ds}: {cur} → {src_val}")
        log.ok("Properties synced")

    # ── Unmount keystore and cleanup ─────────────────────────────────────
    ks.umount()
    cleanup.run()

    # ── Persist last_backup_at on the selected drive ─────────────────────
    # Marks the drive as freshly backed up so future staleness reporting
    # has a baseline. Done after cleanup so we don't write the timestamp
    # if cleanup itself failed to export the pool — exporting failures
    # do not alter the data we just transferred but are worth flagging
    # in the recorded state, and this ordering keeps the file simple
    # ("written iff backup truly finished"). A write failure here is a
    # warn, not fatal: the backup itself succeeded; missing the timestamp
    # only weakens the next run's reporting by one cycle.
    info = cfg.known_drives.get(pool_name)
    if info is not None:
        info.last_backup_at = now_utc_iso()
        try:
            cfg.save_drives()
            log.dbg(f"Recorded last_backup_at={info.last_backup_at} for {pool_name}")
        except OSError as e:
            log.warn(f"Could not persist last_backup_at for {pool_name}: {e}")

    # ── Summary ──────────────────────────────────────────────────────────
    dst_info2 = zfs.pool_info(pool_name) if zfs.pool_exists(pool_name) else dst_info

    log.banner_ok(
        "BACKUP COMPLETED",
        [
            f"Duration:       {log.W}{mins}m {secs}s{log.N}",
            f"Used on target: {log.W}{dst_info2.used if dst_info2 else '?'}{log.N}",
            f"Available:      {log.W}{dst_info2.avail if dst_info2 else '?'}{log.N}",
        ],
    )

    # ── Staleness reporting (post-banner, informative only) ──────────────
    _report_staleness_at_end(cfg, pool_name, age_at_start, retention_days, log)

    # By this point cleanup.run() has exported the pool and issued the
    # kernel-side flush (sync + sleep). Ask the operator whether to also
    # power the bridge down now. Default is "yes" because the typical
    # path after a backup is "I'm done, I'll unplug" — Enter ejects. For
    # operators rotating multiple backups in one session, "n" keeps the
    # drive in /dev so the next zark command can use it without
    # unplug/replug.
    prompt_eject_or_attach(device, pool_name, log, default_eject=True)

    _notify("✅ Backup completed", f"Duration: {mins}m {secs}s")
