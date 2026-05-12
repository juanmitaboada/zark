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
Divergent dataset detection and auto-repair shared between
``commands/backup.py`` (silent path) and ``commands/repair_divergent.py``
(interactive path).

A "divergent dataset" is one that exists on both source and target with
no common snapshot. This happens when the drive has been disconnected
longer than the source's sanoid retention policy, so the shared snapshot
chain has been purged in source. syncoid then aborts with "Cowardly
refusing to destroy your existing target".

The 64MB safety limit matches the threshold syncoid itself uses to flag
"did you mistakenly run zfs create on the target?" — datasets under it
almost always have no real data, while those above usually do. zark
silently auto-repairs only the safe-to-destroy ones; anything larger
must be reviewed by the user via ``zark repair-divergent``.
"""

from dataclasses import dataclass

from lib import sh
from lib.log import Log
from lib.zfs import ZFS

# Datasets larger than this won't be auto-destroyed even if they're
# divergent. See module docstring for rationale.
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
    only, not its descendants. That's exactly what we want — divergence
    is a per-dataset property.
    """
    r = sh.run(f"zfs list -H -o name -t snapshot {dataset}")
    if not r.ok:
        return set()
    return {line.split("@", 1)[1] for line in r.lines if "@" in line}


def find_divergent(
    zfs: ZFS,
    source_pool: str,
    target_pool: str,
    log: Log,
) -> list[DivergentDataset]:
    """Walk the target pool's datasets, compare snapshot sets with their
    source counterparts, and return the ones with no overlap.

    A dataset is considered *missing on source* (and skipped) if the source
    counterpart doesn't exist — that's a different class of problem that
    this function doesn't try to solve.
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


def auto_repair_under_64mb(
    zfs: ZFS,
    source_pool: str,
    target_pool: str,
    log: Log,
) -> tuple[bool, list[DivergentDataset]]:
    """Detect divergent datasets and silently destroy the ones under 64MB.

    Returns ``(ok, too_big)``:
      - ``ok``: True if every divergent dataset was either successfully
        destroyed or there were none. False if at least one dataset
        exceeds the 64MB safety limit (the caller should abort and
        suggest interactive ``zark repair-divergent``).
      - ``too_big``: the list of divergent datasets that exceed the limit.
        Empty if ``ok`` is True.

    Datasets that fail to destroy (e.g. they're held by some other
    process) are logged as warnings but don't fail the whole operation —
    the caller's subsequent syncoid retry will fail clearly if they're
    really blocking progress.
    """
    divergent = find_divergent(zfs, source_pool, target_pool, log)
    if not divergent:
        return True, []

    too_big = [d for d in divergent if d.used_bytes > SIZE_LIMIT_BYTES]
    if too_big:
        return False, too_big

    log.info(f"Auto-repair: destroying {len(divergent)} divergent dataset(s) under 64MB...")
    for d in divergent:
        r = sh.run(f"zfs destroy -r {d.target}", log=log)
        if r.ok:
            log.ok(f"  destroyed {d.target}  ({d.used_human})")
        else:
            log.warn(f"  failed to destroy {d.target} (rc={r.returncode})")
    return True, []


def is_divergence_error(stdout: str) -> bool:
    """Return True if syncoid's stdout indicates a divergence abort.

    syncoid emits "Cowardly refusing to destroy your existing target"
    when source and target have no common snapshot. We check stdout
    rather than stderr because that's where syncoid prints this
    particular error.
    """
    s = stdout.lower()
    return "cowardly refusing" in s or "no snapshots matching" in s
