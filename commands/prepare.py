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
zark prepare — Prepare a new blank drive for backup.

Asks every question first, creates the pool (no encryption of its own —
the raw send brings rpool's), brings every replicated dataset to one backup
point with zark's engine (point only, no history), sends the keystore,
reads the pool back and only then registers the drive. Leaves nothing in
origin but this drive's anchors.
"""

import socket
import time
from datetime import UTC, datetime
from pathlib import Path

from lib import engine, sh
from lib.cleanup import Cleanup, flush_device_cache, prompt_eject_or_attach
from lib.config import VERSION, Config, DriveInfo, now_utc_iso
from lib.drives import validate_external_block_device
from lib.health import check_device, render_report
from lib.log import Log
from lib.mount import warn_rpool_mountpoint_lost
from lib.replication import DatasetPlan, State, point_name
from lib.zfs import ZFS, backup_altroot


def run(
    args: list[str],
):  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    """Main entry point for the prepare command."""
    log = Log()
    cfg = Config.load()
    cfg.check_registry(log, fatal=True)
    zfs = ZFS(log)

    target_arg = args[0] if args else ""
    ident = validate_external_block_device(target_arg, log, command="prepare")
    target_dev = ident.disk

    log.banner("PREPARE NEW BACKUP DRIVE")
    warn_rpool_mountpoint_lost(log)
    engine.require_bookmark_v2(log)

    # ── Verify rpool keystore accessible ─────────────────────────────────
    if not Path("/run/keystore/rpool/system.key").exists():
        log.fatal(
            "/run/keystore/rpool/system.key not found",
            solutions=["Run from the system whose backup you want to create"],
        )

    # ── Show drive info ──────────────────────────────────────────────────
    log.info(f"Device:    {target_dev}")
    log.info(f"Drive ID:  {ident.by_id or '-'}")
    log.info(f"Model:     {ident.model}")
    log.info(f"Serial:    {ident.serial}")
    log.info(f"Size:      {ident.size}")
    log.info(f"Transport: {ident.transport}")
    if not ident.by_id:
        log.fatal(
            f"{target_dev} has no stable /dev/disk/by-id name",
            causes=["zark registers drives by their by-id name; it never registers <unknown>"],
            solutions=[
                "Use an enclosure/driver that exposes a by-id name",
                "Check: ls -l /dev/disk/by-id/",
            ],
        )

    # ── Non-destructive risk check ───────────────────────────────────────
    # Flag bridges/transports correlated with the FUA cache-flush lie BEFORE
    # any work begins, so the operator can apply mitigations (see
    # docs/HARDWARE.md) up front. This is a risk signal, not a verdict: with
    # the usb-storage quirk in place such a bridge works fine, so we warn and
    # ask rather than abort. The full raw send below, plus the read-back at
    # the end, is what actually proves the drive survives write load.
    report = check_device(target_dev)
    render_report(report, log)
    if report.has_risk and not log.ask(
        "Risk factors detected — proceed with preparation anyway?",
    ):
        log.info("Aborted — see docs/HARDWARE.md")
        return

    # ── Check drive is empty ─────────────────────────────────────────────
    existing_parts = sh.run(f"lsblk -no NAME {target_dev} | tail -n +2").output
    sigs = sh.run(f"blkid {target_dev}").output
    if existing_parts or sigs:
        log.fatal(
            "Drive is NOT empty — has partitions or signatures",
            solutions=[f"Wipe first: sudo ./zark purge {target_dev}"],
        )

    log.ok("Drive appears empty ✓")

    base_drive_id = ident.by_id

    # ── Stale registry entries for this same drive ───────────────────────
    # The drive is blank, so any entry pointing at its drive_id describes a
    # pool that no longer exists (e.g. purged by hand, or prepared again).
    stale = [n for n, info in cfg.known_drives.items() if info.drive_id == base_drive_id]
    replace: set[str] = set()
    if stale:
        log.warn(f"Registry entries for this same drive (stale): {', '.join(stale)}")
        if log.ask(f"Remove {', '.join(stale)} from the registry when preparing?", default=True):
            replace = set(stale)

    # ── Pool name ────────────────────────────────────────────────────────
    default_pool = "backup"
    counter = 1
    while (default_pool in cfg.known_drives and default_pool not in replace) or zfs.pool_exists(
        default_pool
    ):
        counter += 1
        default_pool = f"backup{counter}"

    new_pool = log.ask_input("Pool name for this backup drive", default_pool)
    if not new_pool.isidentifier():
        # Not echoed: a mistyped answer here may be a passphrase.
        log.fatal("Invalid pool name — letters, digits and _ only, not starting with a digit")
    if new_pool in cfg.known_drives and new_pool not in replace:
        other = cfg.known_drives[new_pool].drive_id
        log.fatal(
            f"Pool name '{new_pool}' is registered for another drive ({other})",
            solutions=[
                "Choose another pool name",
                f"Or, if that drive is gone: sudo zark registry forget {new_pool}",
            ],
        )
    if zfs.pool_exists(new_pool):
        log.fatal(f"A pool named '{new_pool}' is already imported on this system")

    log.ok(f"Pool name: {new_pool}")

    # Per-drive auto-eject preference, asked now so nothing waits for the
    # operator after the long send (incident §8). When enabled, this drive's
    # eject prompt (here and in later backup/umount runs) gets a 10 s
    # countdown that applies the default automatically. Default no.
    autoeject = log.ask(
        "Enable auto-eject (timed eject prompt) for this drive?",
    )

    # ── Confirmation ─────────────────────────────────────────────────────
    log.info("The pool will be created WITHOUT its own encryption.")
    log.info("Encryption comes from rpool raw send (same key/passphrase).")
    if not log.ask("Proceed with drive preparation?"):
        log.info("Aborted")
        return

    start = time.time()
    cleanup = Cleanup(log)
    cleanup.register()

    # ── Create pool ──────────────────────────────────────────────────────
    log.step(1, 4, f"Creating ZFS pool '{new_pool}'...")

    # -R: the new pool never enters zpool.cache and nothing it will hold can
    # mount over the running system (I-G). The vdev is named by its by-id so
    # later imports and `zpool status` never depend on /dev/sdX (I16).
    altroot = backup_altroot(new_pool)
    _ = sh.run(f"mkdir -p {altroot}")
    r = sh.run(
        "zpool create -f -o ashift=12 -O atime=off -O xattr=sa "
        + f"-O dnodesize=auto -O normalization=formD -m none -R {altroot} "
        + f"{new_pool} {ident.by_id_path}",
        log=log,
    )
    if not r.ok:
        log.fatal(f"Failed to create pool: {r.stderr.strip()}")
    cleanup.track_pool(new_pool)
    log.ok(f"Pool '{new_pool}' created")

    # ── Initial send: the backup point only (decision 2) ─────────────────
    log.step(2, 4, f"Sending a backup point of rpool and bpool → {new_pool}...")
    rpool_used = sh.run("zfs list -H -o used rpool").output
    log.info(f"~{rpool_used} to transfer")
    new_guid = zfs.pool_guid(new_pool)
    eng = engine.Run(new_pool, new_guid, log, point_name(datetime.now(UTC)))

    def decide(plans: list[DatasetPlan], _eng: engine.Run) -> bool:
        return all(p.state in (State.NEW, State.EXCLUDED) for p in plans)

    res = engine.execute(eng, cleanup, decide)
    if res.aborted:
        log.fatal(f"Initial send not started: {res.error or 'the new pool is not empty'}")
    for p in res.plans:
        if p.state is not State.AT_POINT:
            out = res.outcomes.get(p.rel)
            log.warn(f"  {p.rel}: {p.note or (out.error if out else p.state)}")

    prepared_at = now_utc_iso()
    rpool_guid = sh.run("zpool get -H -o value guid rpool").output.strip()
    fields = {
        "format": engine.FORMAT,
        "version": VERSION,
        "prepared-at": prepared_at,
        "origin-host": socket.gethostname(),
        "origin-rpool-guid": rpool_guid,
    }
    if res.ok:
        fields |= {"last-backup-at": prepared_at, "last-point": eng.point}
    _ = engine.write_metadata(new_pool, fields, log)

    # ── Keystore: outside the rpool tree, the last ZFS work before export ─
    log.step(3, 4, "Sending the keystore...")
    keystore_ok = _send_keystore(new_pool, log)
    complete = res.ok and keystore_ok

    # ── Export, read back, register ──────────────────────────────────────
    log.step(4, 4, "Verifying and registering the drive...")
    log.ok(f"Pool GUID: {new_guid}")
    exported = zfs.pool_export(new_pool)
    if exported:
        cleanup.untrack_pool(new_pool)
        flush_device_cache(log)

    if not complete:
        # H16: a drive whose data or keystore did not land is never registered.
        log.banner_error(
            "DRIVE NOT REGISTERED",
            [
                "The initial send did not complete:"
                if not res.ok
                else "The keystore did not land.",
                "See the messages above. The drive has NOT been registered.",
                f"Wipe and retry: sudo zark purge {ident.by_id_path}",
            ],
        )
        raise SystemExit(1)

    rb = zfs.verify_exported_pool_readback(new_pool, device=ident.part1) if exported else None
    if rb is None or not rb.ok:
        lines = (
            rb.describe() if rb else [f"{new_pool} could not be exported, so it was not read back."]
        )
        log.banner_error("DRIVE NOT VERIFIED", [*lines, "", "The drive has NOT been registered."])
        raise SystemExit(1)
    log.ok(f"{new_pool} verified reimportable (ONLINE)")  # I13: the verdict is logged

    for name in replace:
        del cfg.known_drives[name]
    cfg.known_drives[new_pool] = DriveInfo(
        name=new_pool,
        guid=new_guid,
        drive_id=base_drive_id,
        last_backup_at=prepared_at,  # I13: prepare is a full backup
        autoeject=autoeject,
    )
    cfg.save_drives()

    elapsed = int(time.time() - start)
    mins, secs = divmod(elapsed, 60)

    log.banner_ok(
        "DRIVE PREPARED",
        [
            f"Pool:     {log.W}{new_pool}{log.N}  (GUID: {new_guid})",
            f"Drive ID: {log.W}{base_drive_id}{log.N}",
            f"Point:    {log.W}{eng.point}{log.N}",
            f"Duration: {log.W}{mins}m {secs}s{log.N}",
            "",
            f"Registered in: {log.W}{cfg.drives_file_path}{log.N}",
            "",
            f"Run backup: {log.W}sudo ./zark backup{log.N}",
        ],
    )

    # Default to NOT ejecting: the typical workflow after `prepare` is
    # `zark backup` against the drive that was just prepared. Auto-
    # ejecting would force a pointless unplug/replug cycle. Operators
    # who really want to disconnect now can answer "y".
    prompt_eject_or_attach(
        ident.by_id_path,
        new_pool,
        log,
        default_eject=False,
        autoeject=autoeject,
    )


def _send_keystore(new_pool: str, log: Log) -> bool:
    """Send rpool's keystore zvol, then drop the snapshot that carried it (I-B)."""
    snap = f"rpool/keystore@prepare_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    if not sh.run(f"zfs snapshot {snap}", log=log).ok:
        log.error("Could not snapshot the keystore")
        return False
    r = sh.run_pipe(f"zfs send {snap}", f"zfs receive -u {new_pool}/keystore", log=log)
    _ = sh.run(f"zfs destroy {snap}", log=log)
    if not r.ok:
        log.error(f"Keystore send failed: {r.stderr.strip()}")
        return False
    log.ok(f"Keystore sent to {new_pool}/keystore")
    return True
