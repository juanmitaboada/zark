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
zark repair-divergent — Interactive review and repair of divergent backup datasets.

A "divergent dataset" is one that exists on both source and target with
no shared snapshot. See ``lib/repair.py`` for the full rationale and the
detection/repair primitives this command uses.

When ``zark backup`` hits divergence, it auto-repairs datasets under
64 MB silently and aborts on anything larger. This command picks up
where backup leaves off: for each divergent dataset above 64 MB it
shows a context block (size, snapshot dates, source counterpart
status, child datasets) and asks the operator to ``[d]estroy``,
``[s]kip``, or ``[a]bort all``. ``destroy`` on a dataset over 1 GiB
gates a second prompt that requires typing the literal string
``DESTROY`` to proceed — a deliberate friction layer because
``zfs destroy -r`` on a multi-GiB dataset is irreversible and pure
``y/n`` confirmations have a documented history of operator misclicks.

Datasets at or below 64 MB are still auto-destroyed silently here too:
they're below the threshold syncoid uses to flag mistaken targets, and
matching backup's behavior keeps the two flows symmetric.

Mid-flight ``zfs destroy`` failures (busy zvol, lock contention) prompt
the operator once for the policy to apply to the *rest* of the session:
abort, continue, or keep current state and abort. The choice persists
so that a noisy storage system doesn't trigger a prompt per failure.
"""

from datetime import datetime
from typing import Literal

from lib import repair, sh
from lib.cleanup import Cleanup
from lib.config import Config
from lib.drives import drive_staleness_days, scan_connected_drives, select_drive
from lib.log import Log
from lib.repair import SIZE_LIMIT_BYTES, DivergentDataset
from lib.sanoid_retention import worst_case_retention_days
from lib.zfs import ZFS

# Datasets larger than this require typing the literal string ``DESTROY``
# at the secondary confirmation prompt. The threshold is intentionally
# generous: typical container datasets that show up as divergent weigh
# 100s of MB at most (small filesystems holding metadata), so 1 GiB
# cleanly separates "negligible metadata" from "data the operator should
# pause to think about". Hardcoded — no flag — by explicit user request.
DOUBLE_CONFIRM_BYTES = 1 * 1024**3

# Sentinel return type for the per-dataset action prompt.
_Action = Literal["destroy", "skip", "abort"]

# Sentinel return type for the mid-flight failure policy prompt. The
# choice is sticky for the rest of the session (no re-prompt per
# failure) by design — the user already picked the policy once.
_FailurePolicy = Literal["abort", "continue", "keep_state_abort"]


# ─── Snapshot inspection helpers (used only by interactive prompt) ───────────
#
# Reads from ``zfs list -t snapshot`` on the target. Cheap and idempotent.
# Source comparison reuses the snapshot set computation from lib.repair to
# avoid duplicating the parsing logic.


def _snapshot_creation_dates(dataset: str) -> list[tuple[str, datetime]]:
    """Return [(snapshot_name, creation_dt), ...] sorted oldest first.

    Snapshots that fail to parse are dropped silently — the prompt
    block degrades to fewer fields rather than crashing on a malformed
    creation column.
    """
    r = sh.run(f"zfs list -H -p -o name,creation -t snapshot -s creation {dataset}")
    out: list[tuple[str, datetime]] = []
    if not r.ok:
        return out
    for line in r.lines:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        try:
            ts = int(parts[1].strip())
        except ValueError:
            continue
        out.append((name, datetime.fromtimestamp(ts)))
    return out


def _shared_snapshot_with_source(
    target_dataset: str,
    source_dataset: str,
) -> str | None:
    """Return the most recent snapshot suffix shared between target and
    source, or ``None`` if there is no overlap (the divergent case).

    The "@" suffix is what syncoid uses to anchor incrementals; we
    surface it in the prompt block so the user can correlate to
    ``zfs list -t snapshot`` output if they decide to inspect manually.
    """
    # pylint: disable=protected-access
    target_set = repair._snapshot_set(target_dataset)
    source_set = repair._snapshot_set(source_dataset)
    common = target_set & source_set
    if not common:
        return None
    # Pick the lexicographically last shared snapshot. Both sanoid and
    # syncoid use ISO-style timestamps in the suffix so lex order
    # matches chronological order.
    return sorted(common)[-1]


def _children_summary(zfs: ZFS, dataset: str) -> tuple[int, str]:
    """Return ``(child_count, aggregated_used_string)`` for descendants."""
    count = 0
    # We piggyback on the existing ZFS facade for child enumeration to
    # avoid yet another sh.run call here. ``list_datasets(recursive=True)``
    # yields the dataset itself plus descendants; subtract 1 so we count
    # only children.
    total_used: list[str] = []
    for child in zfs.list_datasets(dataset, recursive=True):
        if child.name == dataset:
            continue
        count += 1
        total_used.append(child.used or "")
    aggregated = ", ".join(u for u in total_used[:3] if u)
    if count > 3:
        aggregated += f", … (+{count - 3} more)"
    return count, aggregated or "(empty)"


def _hint_for(target: str, source_exists: bool, children: int) -> str:
    """Best-effort one-line classification of the divergence pattern."""
    del target  # reserved for future heuristics
    if not source_exists:
        return "no source counterpart — orphan on target"
    if children > 0:
        return "container dataset — leaves may still be in sync"
    return "leaf dataset — likely sanoid retention rotated past target"


# ─── Interactive prompts ─────────────────────────────────────────────────────


def _prompt_action(log: Log) -> _Action:
    """Per-dataset action prompt: destroy / skip / abort all.

    ``ask_choice`` is reused so the prompt looks like the rest of zark's
    interactive flows. Default is ``skip`` because it's the only fully
    reversible choice — the operator can come back later with more
    context.
    """
    options = [
        "destroy this dataset",
        "skip this dataset (review later)",
        "abort all — leave everything as-is",
    ]
    idx = log.ask_choice(
        "What would you like to do with this dataset?",
        options,
        default=1,  # skip
    )
    return ("destroy", "skip", "abort")[idx]


def _prompt_double_confirm(log: Log, dataset: str, used: str) -> bool:
    """Secondary confirmation for destroying datasets over 1 GiB.

    Requires typing the literal string ``DESTROY`` (case-sensitive).
    Anything else returns False — including ``yes``, ``y``, the
    dataset name, or empty input. The string is deliberately one
    the operator must commit to typing rather than fat-finger.
    """
    answer = log.ask_input(
        f"This will destroy {dataset} ({used}). "
        "Type DESTROY (uppercase) to confirm, anything else cancels",
    )
    return answer.strip() == "DESTROY"


def _prompt_failure_policy(log: Log) -> _FailurePolicy:
    """Mid-flight failure policy prompt — asked once, reused for the
    rest of the session.

    Three choices intentionally mapped to the three reasonable
    operator stances:
      - ``abort``: the failure indicates something I want to look at
        before continuing.
      - ``continue``: noise, keep going on the others.
      - ``keep_state_abort``: stop now, leave the partial state, I want
        to inspect what was destroyed before any more changes happen.
        Distinct from ``abort`` only in that there's no ambiguity for
        the operator about whether the prompt itself counts as part of
        "before continuing".
    """
    options = [
        "continue — try the remaining datasets",
        "abort — stop further destroys, keep cleanup",
        "keep state and abort — stop and leave the pool exactly as it is",
    ]
    idx = log.ask_choice(
        "How should I handle the rest of this run?",
        options,
        default=1,  # abort
    )
    return ("continue", "abort", "keep_state_abort")[idx]


# ─── Per-dataset display ─────────────────────────────────────────────────────


def _print_dataset_block(
    log: Log,
    zfs: ZFS,
    d: DivergentDataset,
) -> None:
    """Render the multi-line context block shown before each prompt.

    Information density tuned to "what would I check by hand before
    running zfs destroy?" — sizes, snapshot dates on each side, the
    most recent shared snapshot if any, child datasets summary, hint.
    Kept narrow enough to fit a typical 80-column terminal.
    """
    snaps = _snapshot_creation_dates(d.target)
    n_snaps = len(snaps)
    oldest = snaps[0][1].strftime("%Y-%m-%d") if snaps else "—"
    newest = snaps[-1][1].strftime("%Y-%m-%d") if snaps else "—"

    source_exists = zfs.dataset_exists(d.source)
    shared = _shared_snapshot_with_source(d.target, d.source) if source_exists else None
    n_children, children_used = _children_summary(zfs, d.target)
    hint = _hint_for(d.target, source_exists, n_children)

    log.info("")
    log.info(f"  {log.W}{d.target}{log.N}")
    log.info(f"    used:                   {d.used_human} ({d.used_bytes:,} bytes)")
    log.info(f"    target snapshots:       {n_snaps} (oldest {oldest} — newest {newest})")
    if source_exists:
        log.info(f"    last shared with src:   {shared if shared else 'none'}")
    else:
        log.info("    source counterpart:     does NOT exist (orphan on target)")
    if n_children > 0:
        log.info(f"    child datasets:         {n_children} ({children_used})")
    log.info(f"    hint:                   {hint}")
    log.info("")


# ─── Destruction loop ────────────────────────────────────────────────────────


def _destroy_loop(  # pylint: disable=too-many-branches,too-many-locals
    log: Log,
    zfs: ZFS,
    divergent: list[DivergentDataset],
) -> tuple[list[str], list[str], bool]:
    """Walk every divergent dataset, prompting and destroying.

    Datasets ≤ 64 MB are auto-destroyed without prompting (matches
    backup's silent path). Anything larger goes through the per-dataset
    prompt with ``[d]estroy / [s]kip / [a]bort all`` and, for
    > 1 GiB destroys, the typed-``DESTROY`` confirmation.

    Returns ``(destroyed, skipped, aborted_by_user)``.
    """
    destroyed: list[str] = []
    skipped: list[str] = []
    aborted = False
    failure_policy: _FailurePolicy | None = None  # asked once, then sticky

    auto_small = [d for d in divergent if d.used_bytes <= SIZE_LIMIT_BYTES]
    big = [d for d in divergent if d.used_bytes > SIZE_LIMIT_BYTES]

    if auto_small:
        log.info(
            f"Auto-destroying {len(auto_small)} dataset(s) ≤ 64 MB silently "
            "(matches backup's threshold)…",
        )
        for d in auto_small:
            r = sh.run(f"zfs destroy -r {d.target}", log=log)
            if r.ok:
                log.ok(f"  destroyed {d.target}  ({d.used_human})")
                destroyed.append(d.target)
            else:
                log.warn(f"  failed to destroy {d.target} (rc={r.returncode})")
                skipped.append(d.target)

    if not big:
        return destroyed, skipped, aborted

    log.info("")
    log.info(
        f"{len(big)} dataset(s) exceed 64 MB. Reviewing each in turn — "
        "type 'a' at any prompt to abort the whole run.",
    )

    for d in big:
        if aborted:
            skipped.append(d.target)
            continue

        _print_dataset_block(log, zfs, d)
        action = _prompt_action(log)

        if action == "skip":
            log.info(f"  skipped {d.target}")
            skipped.append(d.target)
            continue

        if action == "abort":
            log.warn("Aborted by user — remaining datasets will not be touched")
            aborted = True
            skipped.append(d.target)
            continue

        # action == "destroy"
        if d.used_bytes > DOUBLE_CONFIRM_BYTES:
            if not _prompt_double_confirm(log, d.target, d.used_human):
                log.info(f"  cancelled — {d.target} NOT destroyed")
                skipped.append(d.target)
                continue

        r = sh.run(f"zfs destroy -r {d.target}", log=log)
        if r.ok:
            log.ok(f"  destroyed {d.target}  ({d.used_human})")
            destroyed.append(d.target)
            continue

        # Mid-flight failure: prompt once for the rest-of-session policy.
        log.warn(f"  failed to destroy {d.target} (rc={r.returncode})")
        if r.stderr:
            log.dbg(f"    stderr: {r.stderr.strip()}")
        skipped.append(d.target)

        if failure_policy is None:
            log.info(
                "A destroy operation failed. Choose what should happen with "
                "any further failures in this run.",
            )
            failure_policy = _prompt_failure_policy(log)

        if failure_policy == "continue":
            continue
        if failure_policy == "abort":
            log.warn("Stopping further destroys (cleanup will still run)")
            aborted = True
            continue
        # keep_state_abort
        log.warn("Stopping immediately and leaving the pool state untouched")
        aborted = True

    return destroyed, skipped, aborted


# ─── Final summary ───────────────────────────────────────────────────────────


def _print_summary(
    log: Log,
    destroyed: list[str],
    skipped: list[str],
    *,
    aborted: bool,
) -> None:
    """Final banner. Different recommendation based on whether anything
    was skipped — re-running ``zark backup`` will recreate the destroyed
    datasets via initial replication, so that's the next step almost
    always."""
    lines: list[str] = []
    lines.append(f"Destroyed:  {len(destroyed)} dataset(s)")
    for name in destroyed:
        lines.append(f"  • {name}")
    if skipped:
        lines.append(f"Skipped:    {len(skipped)} dataset(s)")
        for name in skipped:
            lines.append(f"  • {name}")
    lines.append("")
    if destroyed:
        lines.append("Next: sudo ./zark backup  → recreates destroyed datasets")
        lines.append("      via initial replication.")
    if skipped:
        lines.append("Skipped datasets remain divergent. Re-run repair-divergent")
        lines.append("after deciding what to do with them, or rotate to a")
        lines.append("different drive.")
    if aborted:
        log.banner_error("REPAIR ABORTED", lines)
    elif skipped and not destroyed:
        log.banner_error("REPAIR — NOTHING DESTROYED", lines)
    else:
        log.banner_ok("REPAIR COMPLETE", lines)


# ─── Entry point ─────────────────────────────────────────────────────────────


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
        # When the drive is fine on snapshots but old on staleness, give
        # the operator the heads-up here. They came to repair-divergent
        # presumably because of a backup issue, and the answer for them
        # is "this command can't help, but here's what you can do".
        info = cfg.known_drives.get(pool_name)
        age = drive_staleness_days(info) if info is not None else None
        retention = worst_case_retention_days(log)
        if age is not None and retention is not None:
            danger_threshold = max(0, retention - 30)
            if age >= danger_threshold:
                remaining = max(0, retention - age)
                log.info("")
                log.info(
                    f"Note: drive '{pool_name}' was last backed up {age} day(s) "
                    f"ago. With current sanoid retention of {retention} day(s),",
                )
                log.info(
                    f"~{remaining} day(s) remain before container datasets start "
                    "diverging. repair-divergent does not address staleness;",
                )
                log.info(
                    "rotate to a fresher drive or run a fresh backup soon.",
                )
        cleanup.run()
        return

    # Quick visual table for the operator before we go dataset-by-dataset.
    log.info("Divergent datasets found:")
    log.info("")
    width = max(len(d.target) for d in divergent)
    for d in divergent:
        marker = "⚠ " if d.used_bytes > SIZE_LIMIT_BYTES else "  "
        log.info(f"  {marker}{d.target:<{width}}  used: {d.used_human}")
    log.info("")

    # ── 4. Walk each dataset interactively ───────────────────────────────
    log.step(4, 4, "Reviewing and destroying...")
    destroyed, skipped, aborted = _destroy_loop(log, zfs, divergent)

    cleanup.run()
    _print_summary(log, destroyed, skipped, aborted=aborted)
