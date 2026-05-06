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
from pathlib import Path

from lib import repair, sh
from lib.cleanup import Cleanup
from lib.config import Config
from lib.drives import scan_connected_drives, select_drive
from lib.keystore import Keystore
from lib.log import Log
from lib.zfs import ZFS


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


def run(
    args: list[str],
):  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    """Main backup function."""
    del args  # Unused; backup doesn't take arguments
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
    log.step(1, 8, "Scanning for known backup drives...")
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

    # ── Import pool ──────────────────────────────────────────────────────
    log.step(2, 8, f"Importing pool {pool_name}...")

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
    log.step(3, 8, "Checking pool health...")

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
    log.step(4, 8, "Gathering pool info...")

    src_info = zfs.pool_info(cfg.source_pool)
    dst_info = zfs.pool_info(pool_name)

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
    log.step(5, 8, "Loading encryption key...")

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

    # ── Run syncoid ──────────────────────────────────────────────────────
    log.step(6, 8, "Running syncoid (this may take a while)...")
    log.info("Tip: run 'sudo ./zark monitor' in another terminal for progress")

    _notify("🔄 Backup started", f"Syncing {cfg.source_pool} → {pool_name}...")

    target_pool = f"{pool_name}/rpool"
    start = time.time()

    # The base sync command. Held in a variable because we may need to
    # invoke it twice: once normally, and once after auto-repair if syncoid
    # aborts with "cowardly refusing" due to divergent datasets.
    rpool_syncoid_cmd = (
        "syncoid --recursive --no-privilege-elevation --sendoptions=w "
        + "--exclude-datasets=rpool/keystore "
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
    log.step(7, 8, "Syncing bpool (kernels + grub)...")

    if zfs.pool_exists("bpool"):
        bpool_syncoid_cmd = (
            "syncoid --recursive --no-privilege-elevation --preserve-properties "
            + f"bpool {pool_name}/bpool"
        )
        r = sh.run(bpool_syncoid_cmd, log=log)
        if r.ok:
            log.ok("bpool synced")
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
    log.step(8, 8, "Syncing ZFS properties...")

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

    _notify("✅ Backup completed", f"Duration: {mins}m {secs}s")
