"""
zark explore — Scan for ZFS pools and show their status.

Non-destructive. Shows:
  - Known drives (green): registered and GUID matches
  - GUID changed (yellow): name matches but GUID differs
  - Renamed (yellow): GUID matches but name differs
  - Unknown (yellow): not in known_drives.json

For unknown/changed drives, shows the exact JSON to add.
"""

from lib.config import Config, DriveInfo
from lib.drives import scan_connected_drives
from lib.log import Log

# from lib.zfs import ZFS


def run(
    args: list[str],
):  # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    """Run the explore command."""
    del args  # unused
    log = Log()
    cfg = Config.load()
    # zfs = ZFS(log)

    log.banner("EXPLORE ZFS POOLS", "Scanning all connected ZFS pools")

    # Report config status
    if not cfg.known_drives:
        if cfg.drives_file_path.exists():
            log.warn(f"known_drives.json is empty: {cfg.drives_file_path}")
        else:
            log.warn("No known_drives.json found")
            log.info(f"Expected at: {cfg.drives_file_path}")
            log.info("All drives will be shown as UNKNOWN")
        log.blank()

    # Scan
    log.info("Scanning via blkid and zpool...")
    drives = scan_connected_drives(cfg, log)
    log.blank()

    if not drives:
        log.warn("No external ZFS pools found")
        log.info("Connect a backup drive and run again")
        log.info("Or check: blkid -t TYPE=zfs_member")
        return

    # Display results
    unknown_count = 0
    for d in drives:
        if d.known and not d.guid_changed and not d.renamed:
            log.raw(
                f"  {log.G}✓ KNOWN{log.N}    {log.W}{d.name}{log.N}  "
                + f"(GUID: {d.guid}) [{d.state}]",
            )
            if d.model:
                log.raw(f"           {d.model} {d.size} ({d.transport})")
            log.raw("           Registered correctly ✓")

        elif d.guid_changed:
            unknown_count += 1
            log.raw(f"  {log.Y}⚠ GUID CHANGED{log.N}  {log.W}{d.name}{log.N}  [{d.state}]")
            log.raw("           Drive was re-prepared — GUID changed")
            known = cfg.known_drives.get(d.name)
            if known:
                log.raw(f"           Old GUID: {known.guid}")
            log.raw(f"           New GUID: {d.guid}")
            log.raw("           Update known_drives.json:")
            log.raw(f"           {log.W}{d.registration_json}{log.N}")

        elif d.renamed:
            unknown_count += 1
            log.raw(
                f"  {log.Y}⚠ RENAMED{log.N}   {log.W}{d.name}{log.N}  "
                + f"(GUID: {d.guid}) [{d.state}]",
            )
            # Find old name
            for info in cfg.known_drives.values():
                if info.guid == d.guid:
                    log.raw(f"           Was registered as '{info.name}' — pool renamed")
                    break
            log.raw("           Update known_drives.json:")
            log.raw(f"           {log.W}{d.registration_json}{log.N}")

        else:
            unknown_count += 1
            log.raw(
                f"  {log.Y}? UNKNOWN{log.N}  {log.W}{d.name}{log.N}  "
                + f"(GUID: {d.guid}) [{d.state}]",
            )
            if d.model:
                log.raw(f"           {d.model} {d.size} ({d.transport})")
            log.raw("           Not in known_drives.json — to register, add:")
            log.raw(f"           {log.W}{d.registration_json}{log.N}")

        log.blank()

    # Summary
    if unknown_count == 0:
        log.ok("All detected pools are registered")
    else:
        log.warn(f"{unknown_count} pool(s) need attention")
        log.info(f"Edit: {cfg.drives_file_path}")

    # Check for duplicate drive_ids in known_drives (same physical drive, different names)
    drive_id_to_names: dict[str, list[str]] = {}
    for name, info in cfg.known_drives.items():
        drive_id_to_names.setdefault(info.drive_id, []).append(name)
    for (  # pylint: disable=too-many-nested-blocks
        did,
        names,
    ) in drive_id_to_names.items():
        if len(names) > 1:
            log.blank()
            log.warn(f"Duplicate drive_id detected: {did}")
            log.info(f"  These entries share the same physical drive: {', '.join(names)}")
            # Find which name actually exists as a pool
            real_name = None
            for d in drives:
                if d.drive_id == did:
                    real_name = d.name
                    break
            if real_name:
                stale = [n for n in names if n != real_name]
                if stale:
                    log.info(
                        f"  Pool on disk is '{real_name}' — "
                        + f"'{', '.join(stale)}' appear to be stale entries",
                    )
                    if log.ask(f"Remove stale entries ({', '.join(stale)})?", default=True):
                        for s in stale:
                            del cfg.known_drives[s]
                        cfg.save_drives()
                        log.ok(f"Removed {len(stale)} stale entry/entries")

    # Offer auto-registration for unknown drives
    unknowns = [d for d in drives if not d.known or d.guid_changed or d.renamed]
    if unknowns and log.ask("Auto-register these drives in known_drives.json?", default=False):
        for d in unknowns:
            cfg.known_drives[d.name] = DriveInfo(name=d.name, guid=d.guid, drive_id=d.drive_id)
        cfg.save_drives()
        log.ok(f"Saved {len(unknowns)} drive(s) to {cfg.drives_file_path}")

    log.blank()
