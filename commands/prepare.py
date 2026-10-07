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

Creates ZFS pool (no encryption — raw send brings its own),
does initial raw send from rpool, sends keystore, sends bpool,
registers the drive in known_drives.json.
"""

import time
from pathlib import Path

from lib import sh
from lib.cleanup import flush_device_cache, prompt_eject_or_attach
from lib.config import Config, DriveInfo
from lib.drives import validate_external_block_device
from lib.health import check_device, render_report
from lib.log import Log
from lib.mount import warn_rpool_mountpoint_lost
from lib.zfs import ZFS, backup_altroot, syncoid_exclude_flag


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

    # ── Confirmation ─────────────────────────────────────────────────────
    log.info("The pool will be created WITHOUT its own encryption.")
    log.info("Encryption comes from rpool raw send (same key/passphrase).")
    if not log.ask("Proceed with drive preparation?"):
        log.info("Aborted")
        return

    start = time.time()

    # ── Create pool ──────────────────────────────────────────────────────
    log.step(1, 3, f"Creating ZFS pool '{new_pool}'...")

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
    log.ok(f"Pool '{new_pool}' created")

    # ── Initial raw send ─────────────────────────────────────────────────
    log.step(2, 3, f"Initial raw send from rpool → {new_pool}/rpool...")

    rpool_used = sh.run("zfs list -H -o used rpool").output
    log.info(f"~{rpool_used} to transfer")

    # Syncoid raw send (excludes keystore — sent separately).
    # syncoid 2.3.0+ uses --exclude-datasets; older Ubuntu releases
    # (22.04 - 25.10, sanoid 2.1.0 - 2.2.0-2) only know --exclude.
    excl = syncoid_exclude_flag()
    r = sh.run(
        "syncoid --recursive --no-privilege-elevation --sendoptions=w --recvoptions=u "
        + f"{excl}=rpool/keystore "
        + f"rpool {new_pool}/rpool",
        log=log,
    )
    if r.ok:
        log.ok("rpool synced ✓")
    else:
        log.warn("rpool sync had warnings — check log")

    # Send keystore separately (outside rpool tree to avoid encryption dependency)
    log.info("Sending keystore...")
    snap_ts = sh.run("date '+%Y%m%d_%H%M%S'").output
    _ = sh.run(f"zfs snapshot rpool/keystore@prepare_{snap_ts}")
    r = sh.run_pipe(
        f"zfs send rpool/keystore@prepare_{snap_ts}",
        f"zfs receive -u -F {new_pool}/keystore",
    )
    if r.ok:
        log.ok(f"Keystore synced to {new_pool}/keystore ✓")
    else:
        log.warn("Keystore sync had errors")

    # Sync bpool.
    if zfs.pool_exists("bpool"):
        log.info("Syncing bpool (kernels + grub)...")
        r = sh.run(
            f"syncoid --recursive --no-privilege-elevation --recvoptions=u bpool {new_pool}/bpool",
            log=log,
        )
        if r.ok:
            log.ok("bpool synced ✓")
        else:
            log.warn("bpool sync had warnings")

    # Fix keylocation
    log.info("Configuring keystore location...")
    _ = zfs.set_property(
        f"{new_pool}/rpool",
        "keylocation",
        "file:///run/keystore/rpool/system.key",
    )

    # Sync mountpoints from origin
    log.info("Syncing mountpoints from origin...")
    r = sh.run("zfs list -H -o name,mountpoint rpool")
    if r.ok:
        for line in r.lines:
            parts = line.split("\t")
            if len(parts) >= 2:
                ds, mp = parts[0].strip(), parts[1].strip()
                if mp in ("none", "-", "legacy"):
                    continue
                dst = f"{new_pool}/{ds}"
                if zfs.dataset_exists(dst):
                    _ = zfs.set_property(dst, "mountpoint", mp)
    log.ok("Mountpoints synced ✓")

    # ── Register and export ──────────────────────────────────────────────
    log.step(3, 3, "Registering drive...")

    new_guid = zfs.pool_guid(new_pool)
    log.ok(f"Pool GUID: {new_guid}")

    _ = zfs.pool_export(new_pool)
    flush_device_cache(log)

    # ── Read-back verification ───────────────────────────────────────────
    # prepare has just written the entire rpool raw send (real write load
    # with transaction churn) and exported. That is exactly the workload
    # that exposes a bridge lying about FUA, so verify the pool re-imports
    # ONLINE before we register it as a trusted backup target. If it fails,
    # do NOT register the drive — the prepared pool is not trustworthy even
    # though every step above reported success.
    if not zfs.verify_exported_pool_readback(new_pool, device=ident.part1).ok:
        log.banner_error(
            "DRIVE NOT VERIFIED",
            [
                "The pool was created and filled but could NOT be",
                "re-imported afterwards — the signature of a USB-SATA",
                "bridge that lies about cache flushing (FUA).",
                "",
                "The drive has NOT been registered.",
                "What to do:",
                "  → See docs/HARDWARE.md (UAS quirk for known bridges)",
                "  → Address the enclosure, then re-run prepare",
            ],
        )
        return

    # Per-drive auto-eject preference. When enabled, this drive's eject
    # prompt (here and in future backup/umount/... runs) gets a 10 s
    # countdown that applies the default automatically — handy for
    # unattended rotation. Default no: the prompt waits for the operator.
    autoeject = log.ask(
        "Enable auto-eject (timed eject prompt) for this drive?",
    )

    # Auto-register in known_drives.json (replacing this drive's stale entries)
    for name in replace:
        del cfg.known_drives[name]
    cfg.known_drives[new_pool] = DriveInfo(
        name=new_pool,
        guid=new_guid,
        drive_id=base_drive_id,
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
