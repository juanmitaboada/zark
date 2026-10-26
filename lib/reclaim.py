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
Snapshots removed from a backup drive: what they would free, and their
destroy by exact name, grouped per dataset (retention and ``zark prune``).
"""

import shlex
import time

from lib import sh
from lib.log import Log
from lib.retention import Prune, Removal


def _q(word: str) -> str:
    return shlex.quote(word)


# How long to wait for a pool to free what was removed (M4d-33, M5a-11).
FREEING_WAIT_SEC = 60


def freeing(pool: str) -> int:
    """Bytes ``pool`` still frees in the background after destroys, 0 when
    it cannot be read (``zpool get freeing`` is ``$FREE``'s used bytes,
    OpenZFS 2.4.1 module/zfs/spa.c:516-519)."""
    r = sh.run(f"zpool get -Hp -o value freeing {pool}")
    return int(r.output) if r.ok and r.output.isdigit() else 0


def wait_freed(pools: list[str], log: Log) -> list[str]:
    """Wait, at most :data:`FREEING_WAIT_SEC`, until each pool has freed what
    was destroyed on it; the pools still freeing at the cap.

    A destroy returns before its blocks are free: they sit in the pool's
    ``$FREE`` until its free pass has run, and a write that needs more than
    ``available`` meanwhile is refused with ENOSPC (OpenZFS 2.4.1
    module/zfs/dsl_destroy.c:414-420, dsl_dir.c:1338-1363).
    """
    end = time.monotonic() + FREEING_WAIT_SEC
    busy = [p for p in pools if freeing(p)]
    if busy:
        log.info(f"Waiting for {', '.join(busy)} to free what was removed...")
    while busy and time.monotonic() < end:
        time.sleep(1)
        busy = [p for p in busy if freeing(p)]
    return busy


# Names per grouped `zfs destroy` (M3P.26): keeps each command line short.
DESTROY_GROUP = 100


def _under_any(rel: str, tops: list[str]) -> bool:
    return any(rel == t or rel.startswith(f"{t}/") for t in tops)


def _dry_run_command(ds: str, snaps: list[str]) -> str:
    """The only spelling of the dry run: ``-n`` is part of the constant, so
    no caller can drop it (M9a, plan section 5)."""
    spec = ",".join(dict.fromkeys(snaps))
    return f"zfs destroy -nvp {_q(f'{ds}@{spec}')}"


def dry_run(ds: str, snaps: list[str]) -> int | None:
    """Bytes destroying these snapshots of ``ds`` together would free, as ZFS
    measures it: one dry run over the exact names, comma separated
    (``zfs destroy -nvp ds@a,b,c``, cmd/zfs/zfs_main.c:1756-1779; only a ``%``
    makes a range, lib/libzfs/libzfs_iter.c:429-460). None when the command
    fails or prints no ``reclaim`` line (a clone origin among them, every
    name missing)."""
    out = sh.run(_dry_run_command(ds, snaps))
    for line in out.lines if out.ok else []:
        f = line.split("\t")
        if len(f) == 2 and f[0] == "reclaim" and f[1].isdigit():
            return int(f[1])
    return None


def _by_dataset(names: list[str]) -> dict[str, list[str]]:
    by_ds: dict[str, list[str]] = {}
    for full in names:
        ds, _, snap = full.partition("@")
        by_ds.setdefault(ds, []).append(snap)
    return by_ds


def reclaim_names(names: list[str]) -> int:
    """Bytes destroying these snapshots (full ``ds@snap`` names) would free:
    :func:`dry_run` per dataset, summed. 0 for what cannot be measured (the
    screens of ``zark prune`` and the space plan show it as freeing
    nothing; :func:`measure` is the variant that says so)."""
    return sum(dry_run(ds, snaps) or 0 for ds, snaps in sorted(_by_dataset(names).items()))


def _used(name: str) -> int | None:
    used = sh.run(f"zfs list -Hp -o used {_q(name)}")
    return int(used.output.strip()) if used.ok and used.output.strip().isdigit() else None


def reclaim(pool: str, removals: list[Removal], tops: list[str] | None = None) -> int:
    """Bytes the removals on drive ``pool`` would free (:func:`reclaim_names`),
    plus ``used`` of each subtree destroyed whole."""
    tops = tops or []
    total = reclaim_names([f"{pool}/{r.target}" for r in removals if not _under_any(r.rel, tops)])
    return total + sum(_used(f"{pool}/{top}") or 0 for top in tops)


def measure(
    pool: str,
    removals: list[Removal],
    tops: list[str],
    deadline: float | None = None,
    tally: list[int] | None = None,
) -> int | None:
    """What :func:`reclaim` gives, exactly or not at all: None when any dry
    run or ``used`` could not be read, or when ``deadline`` (a
    :func:`time.monotonic` value) passes before the last command starts.
    The status file's figures use it: null when not measured, never a
    guess (contract, "Drive contents"). Each dry run appends 1 to
    ``tally``, for the pass's log line."""
    by_ds = _by_dataset([f"{pool}/{r.target}" for r in removals if not _under_any(r.rel, tops)])
    total = 0
    for ds, snaps in sorted(by_ds.items()):
        if deadline is not None and time.monotonic() >= deadline:
            return None
        got = dry_run(ds, snaps)
        if tally is not None:
            tally.append(1)
        if got is None:
            return None
        total += got
    for top in tops:
        got = _used(f"{pool}/{top}")
        if got is None:
            return None
        total += got
    return total


def destroy_snapshots(targets: list[tuple[str, str]], log: Log) -> tuple[int, list[str]]:
    """Destroy ``(ds@snap, reason)`` by exact name, each logged with its
    reason; returns how many went and what could not (M3P.26, M4b-10).

    The snapshots of one dataset go in one ``zfs destroy ds@a,b,c`` of at
    most :data:`DESTROY_GROUP` names: the kernel checks them all and destroys
    none when one fails (zfs-2.4.1 module/zfs/dsl_destroy.c:589-601), and the
    names of a failed group are then destroyed one by one, so what is kept and
    what failed stay exact. Never a ``%`` range (redesign §7).
    """
    count, failed = 0, []
    groups: dict[str, list[tuple[str, str]]] = {}
    singles: list[tuple[str, str]] = []
    for target, reason in targets:
        ds, sep, name = target.partition("@")
        if sep and "#" not in target:
            groups.setdefault(ds, []).append((name, reason))
        else:
            singles.append((target, reason))
    for ds, items in groups.items():
        for i in range(0, len(items), DESTROY_GROUP):
            chunk = items[i : i + DESTROY_GROUP]
            if len(chunk) == 1:
                singles.append((f"{ds}@{chunk[0][0]}", chunk[0][1]))
                continue
            for name, reason in chunk:
                log.file(f"  to remove {ds}@{name}: {reason}")
            res = sh.run(f"zfs destroy {_q(ds + '@' + ','.join(n for n, _ in chunk))}", log=log)
            if res.ok:
                count += len(chunk)
                for name, reason in chunk:
                    log.info(f"  removed {ds}@{name}: {reason}")
                continue
            log.file(f"  {ds}: {len(chunk)} snapshot(s) not removed together, one by one now")
            singles += [(f"{ds}@{name}", reason) for name, reason in chunk]
    for target, reason in singles:
        res = sh.run(f"zfs destroy {_q(target)}", log=log)
        if res.ok:
            count += 1
            log.info(f"  removed {target}: {reason}")
        else:
            failed.append(target)
            log.warn(f"  could not remove {target}: {res.stderr.strip()}")
    return count, failed


def subtree_datasets(top: str) -> list[tuple[str, bool]] | None:
    """The datasets at and below ``top`` from the drive's own listing, each
    with whether it holds a partial receive (its ``%recv`` child never lists:
    module/zcommon/zfs_comutil.c:232-244); None when the listing fails."""
    r = sh.run(f"zfs list -H -o name,receive_resume_token -t filesystem,volume -r {_q(top)}")
    if not r.ok:
        return None
    out: list[tuple[str, bool]] = []
    for line in r.lines:
        f = line.split("\t")
        if len(f) == 2:
            out.append((f[0], f[1] not in ("", "-")))
    return out


def apply_prune(pool: str, prune: Prune, log: Log) -> tuple[int, list[str]]:
    """Destroy what the plan allows, each snapshot by its exact name, grouped
    per dataset (:func:`destroy_snapshots`), every one logged with its reason
    (never a % range, redesign §7); a subtree destroyed whole names every snapshot and every dataset
    it takes first, from the drive's own listing, and is left alone when
    that listing fails (M3R-11, M3F-8 8a). Returns how many went and what
    could not be destroyed."""
    tops = [t for t, _ in prune.datasets]
    count, failed = destroy_snapshots(
        [(f"{pool}/{r.target}", r.reason) for r in prune.snapshots if not _under_any(r.rel, tops)],
        log,
    )
    for top, why in prune.datasets:
        below = subtree_datasets(f"{pool}/{top}")
        if below is None:
            failed.append(f"{pool}/{top}")
            log.warn(f"  could not remove {pool}/{top}: its datasets could not be listed")
            continue
        planned = {f"{pool}/{r.rel}" for r in prune.snapshots if _under_any(r.rel, [top])}
        for r in prune.snapshots:
            if _under_any(r.rel, [top]):
                log.file(f"  {pool}/{r.target} goes with {pool}/{top}")
        for ds, partial in below:
            if ds not in planned:
                log.file(f"  {ds} (no snapshot) goes with {pool}/{top}")
            if partial:
                log.file(f"  {ds} and its partial receive go with {pool}/{top}")
        res = sh.run(f"zfs destroy -r {_q(f'{pool}/{top}')}", log=log)
        if res.ok:
            count += sum(1 for r in prune.snapshots if _under_any(r.rel, [top]))
            log.info(f"  removed {pool}/{top} and everything below it: {why}")
        else:
            failed.append(f"{pool}/{top}")
            log.warn(f"  could not remove {pool}/{top}: {res.stderr.strip()}")
    return count, failed
