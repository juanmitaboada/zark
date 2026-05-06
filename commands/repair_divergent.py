"""
zark repair-divergent — Interactive review and repair of divergent backup datasets.

A "divergent dataset" is one that exists on both source and target with
no shared snapshot. See ``lib/repair.py`` for the full rationale and the
detection/repair primitives this command uses.

When ``zark backup`` hits divergence, it auto-repairs datasets under
64MB silently and aborts on anything larger. This command is the
escape hatch: it lists what was found, optionally prompts, and destroys
under user control. It also handles the case where the user wants to
review even small datasets before letting backup touch them.
"""

from lib import repair, sh
from lib.cleanup import Cleanup
from lib.config import Config
from lib.drives import scan_connected_drives, select_drive
from lib.log import Log
from lib.repair import SIZE_LIMIT_BYTES, DivergentDataset
from lib.zfs import ZFS


def _print_divergent_table(log: Log, items: list[DivergentDataset]) -> None:
    """Show the user exactly what was found, with sizes."""
    log.info("Divergent datasets found:")
    log.info("")
    width = max(len(d.target) for d in items)
    for d in items:
        marker = "⚠ " if d.used_bytes > SIZE_LIMIT_BYTES else "  "
        log.info(f"  {marker}{d.target:<{width}}  used: {d.used_human}")
    log.info("")


def run(  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    args: list[str],
) -> None:
    """Main entry point for zark repair-divergent."""
    del args  # No CLI args yet; auto-detect a single connected drive.
    log = Log()
    cfg = Config.load()
    zfs = ZFS(log)
    cleanup = Cleanup(log)
    cleanup.register()

    log.banner(
        "REPAIR DIVERGENT DATASETS",
        "Reset backup datasets that have no shared snapshots with the source",
    )

    # ── 1. Find and select drive ─────────────────────────────────────────
    log.step(1, 4, "Scanning for known backup drives...")
    drives = scan_connected_drives(cfg, log)
    known = [d for d in drives if d.known]

    if not known:
        log.fatal(
            "No known backup drives detected",
            solutions=[
                "Connect a backup drive and run again",
                "Run: sudo ./zark explore  to inspect available pools",
            ],
        )

    drive = select_drive(known, log, known_only=True)
    if not drive:
        return
    pool_name = drive.name

    # ── 2. Import pool (no mount, no key load — we only inspect snapshots) ─
    log.step(2, 4, f"Importing pool {pool_name}...")
    if not zfs.pool_import(pool_name, no_mount=True):
        log.fatal(
            f"Cannot import pool {pool_name}",
            solutions=[f"Try: zpool import -f {pool_name}"],
        )
    cleanup.track_pool(pool_name)

    actual_guid = zfs.pool_guid(pool_name)
    if actual_guid != drive.guid:
        log.fatal(
            f"GUID mismatch: expected {drive.guid}, got {actual_guid}",
            solutions=["Connect the correct drive"],
        )
    log.ok(f"Pool {pool_name} imported (GUID: {actual_guid} ✓)")

    # ── 3. Find divergent datasets ───────────────────────────────────────
    log.step(3, 4, "Looking for divergent datasets...")
    divergent = repair.find_divergent(zfs, cfg.source_pool, pool_name, log)

    if not divergent:
        log.ok("No divergent datasets found — backup is in sync")
        cleanup.run()
        return

    _print_divergent_table(log, divergent)

    # ── Safety: refuse to touch anything > 64MB ──────────────────────────
    too_big = [d for d in divergent if d.used_bytes > SIZE_LIMIT_BYTES]
    if too_big:
        log.fatal(
            f"{len(too_big)} divergent dataset(s) exceed the 64MB safety limit",
            causes=[
                "These datasets contain real data that would be destroyed",
                "syncoid uses the same threshold to flag potentially mistaken targets",
            ],
            solutions=[
                "Inspect each dataset manually:",
                *[f"  zfs list -t snapshot {d.target}" for d in too_big],
                "If you are certain the data is replaceable, use zfs destroy "
                + "directly. zark refuses to do this for you because the "
                + "operation is irreversible.",
            ],
        )

    # All under 64MB — safe to offer destruction.
    log.warn(
        f"All {len(divergent)} divergent dataset(s) are under "
        + f"{SIZE_LIMIT_BYTES // (1024 * 1024)}MB — safe to recreate",
    )
    log.info(
        "Destroying them lets the next 'zark backup' recreate each one via "
        + "initial replication, restoring proper sync.",
    )

    if not log.ask("Destroy the listed datasets?", default=False):
        log.warn("Aborted by user — no changes made")
        cleanup.run()
        return

    # ── 4. Destroy ───────────────────────────────────────────────────────
    log.step(4, 4, "Destroying divergent datasets...")
    failed: list[str] = []
    for d in divergent:
        r = sh.run(f"zfs destroy -r {d.target}", log=log)
        if r.ok:
            log.ok(f"  destroyed {d.target}")
        else:
            log.warn(f"  failed to destroy {d.target} (rc={r.returncode})")
            failed.append(d.target)

    cleanup.run()

    if failed:
        log.banner_ok(
            "REPAIR PARTIAL",
            [
                f"Destroyed {len(divergent) - len(failed)} of {len(divergent)} dataset(s)",
                f"Failed: {', '.join(failed)}",
                "",
                "Next: run 'sudo ./zark backup' to recreate the destroyed ones",
            ],
        )
    else:
        log.banner_ok(
            "REPAIR COMPLETE",
            [
                f"Destroyed {len(divergent)} divergent dataset(s)",
                "",
                "Next: run 'sudo ./zark backup' to recreate them via initial replication",
            ],
        )
