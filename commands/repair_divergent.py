"""
zark repair-divergent — Repair backup datasets with no common snapshots.

A "divergent dataset" is one that exists on both the source pool (e.g.
``rpool/var``) and the backup pool (e.g. ``blue/rpool/var``) but with no
shared snapshot in common. This happens when:

  - sanoid purged the last shared snapshot during a long backup gap, and
  - the dataset has no autosnap policy (covered after v1.0.7-test5, but
    legacy backups created before that may still have the issue), or
  - syncoid's own ``zfs destroy`` cleaned up the shared snapshot at end-of-
    sync and there's no overlap until the next run.

When this happens, syncoid prints "CRITICAL ERROR: Target X exists but has
no snapshots matching with Y" and refuses to touch the target. The dataset
on the backup is then frozen in time forever — captured but never updated.

This command finds those datasets, refuses to destroy any larger than 64MB
(the threshold syncoid itself uses to flag "did you mistakenly run zfs
create"), and after confirmation destroys the small-and-safe ones so the
next ``zark backup`` recreates them via initial replication.
"""

from dataclasses import dataclass

from lib import sh
from lib.cleanup import Cleanup
from lib.config import Config
from lib.drives import scan_connected_drives, select_drive
from lib.log import Log
from lib.zfs import ZFS

# Datasets larger than this won't be auto-destroyed even if they're
# divergent. The 64MB number matches the threshold syncoid itself uses
# to flag "did you mistakenly run zfs create on the target?" — datasets
# under it almost always have no real data, while those above usually do.
SIZE_LIMIT_BYTES = 64 * 1024 * 1024


@dataclass
class DivergentDataset:
    """A dataset on the backup pool that has no common snapshot with its
    source counterpart. ``used_bytes`` is the destination's used size, the
    quantity that would be lost if we destroyed it."""

    source: str  # e.g. "rpool/var"
    target: str  # e.g. "blue/rpool/var"
    used_bytes: int  # size on the target
    used_human: str  # human-readable form for logging


def _used_bytes(dataset: str) -> int:
    """Return the dataset's used size in bytes, or -1 if unavailable."""
    r = sh.run(f"zfs get -H -p -o value used {dataset}")
    if not r.ok or not r.output.strip():
        return -1
    try:
        return int(r.output.strip())
    except ValueError:
        return -1


def _used_human(dataset: str) -> str:
    """Human-readable size from `zfs get used` (e.g. '8K', '475G')."""
    r = sh.run(f"zfs get -H -o value used {dataset}")
    return r.output.strip() if r.ok else "?"


def _snapshot_set(dataset: str) -> set[str]:
    """Return the set of snapshot names (just the @suffix) for one dataset.

    Note: `zfs list -t snapshot <dataset>` lists snapshots of THAT dataset
    only, not its descendants. That's exactly what we want — divergence is
    a per-dataset property.
    """
    r = sh.run(f"zfs list -H -o name -t snapshot {dataset}")
    if not r.ok:
        return set()
    return {line.split("@", 1)[1] for line in r.lines if "@" in line}


def _find_divergent(
    zfs: ZFS,
    source_pool: str,
    target_pool: str,
    log: Log,
) -> list[DivergentDataset]:
    """Walk the target pool's datasets, compare snapshot sets with their
    source counterparts, and return the ones with no overlap.

    A dataset is considered *missing on source* (and skipped) if the source
    counterpart doesn't exist — that's a different class of problem that
    this command doesn't try to solve.
    """
    divergent: list[DivergentDataset] = []
    for ds in zfs.list_datasets(target_pool, recursive=True):
        if ds.type != "filesystem":
            continue  # zvols are handled by prepare/recover, not syncoid

        target_name = ds.name
        # Strip the target_pool/ prefix to get the source-relative name.
        # e.g. "blue/rpool/var" → "rpool/var"
        if not target_name.startswith(target_pool + "/"):
            continue  # the target_pool itself, no source counterpart
        relative = target_name[len(target_pool) + 1 :]
        if not relative.startswith(source_pool):
            continue  # not under the mirrored namespace

        source_name = relative
        if not zfs.dataset_exists(source_name):
            log.dbg(f"  {target_name}: source {source_name} missing — skipping")
            continue

        target_snaps = _snapshot_set(target_name)
        source_snaps = _snapshot_set(source_name)

        if not target_snaps:
            # Empty dataset with no snapshots at all is a different bug;
            # don't flag it as divergent.
            continue

        if target_snaps & source_snaps:
            continue  # overlap exists, all good

        used = _used_bytes(target_name)
        divergent.append(
            DivergentDataset(
                source=source_name,
                target=target_name,
                used_bytes=used,
                used_human=_used_human(target_name),
            ),
        )
    return divergent


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

    # User chose: auto-detect if only one, prompt if several.
    # `select_drive` already does both (line 291: "Auto-selects if only one option").
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
    divergent = _find_divergent(zfs, cfg.source_pool, pool_name, log)

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
        # Release any holds (placed by --use-hold) before destroy, otherwise
        # zfs destroy fails with "dataset is busy". Same pattern as backup.py.
        n = zfs.release_all_holds(d.target)
        if n:
            log.dbg(f"  {d.target}: released {n} hold(s)")
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
