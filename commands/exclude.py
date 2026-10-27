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
zark exclude - leave datasets out of backups or of local snapshots (D7, D8).

  zark exclude list
        what is excluded, where each exclusion is set, the size of its
        subtree, and any value zark ignores (and why)
  zark exclude add <dataset> backups|snapshots|both
        backups:   the dataset and everything below it are not sent to any
                   drive (a copy a drive already holds stays there; the next
                   backup to it asks whether to keep or destroy it)
        snapshots: sanoid takes no snapshots of them, and removes the ones
                   it took at its next prune (sanoid.conf is rewritten)
  zark exclude remove <dataset> backups|snapshots|both
        undo it: the next backup sends the dataset again (an incremental
        when the drive kept its copy), sanoid snapshots it again

The exclusions are ZFS user properties on the dataset itself
(org.zark:replicate=no, org.zark:snapshot=no), so every copy of zark sees
them and they move with the dataset. Never excluded: the boot environments,
the boot pool, the keystore and the structural containers (D10).
"""

import shlex
from typing import NoReturn

from commands.setup import configure_sanoid
from lib import exclusions, sh, survey
from lib.config import Config, zark_command
from lib.exclusions import BACKUPS, BOTH, REPLICATE, SNAPSHOT, SNAPSHOTS
from lib.log import Log
from lib.replication import Origin, anchor_disk
from lib.zfs import ZFS

USAGE = (
    f"Usage: {zark_command()} exclude list"
    + " | add <dataset> backups|snapshots|both"
    + " | remove <dataset> backups|snapshots|both"
)
_PROPS = {BACKUPS: (REPLICATE,), SNAPSHOTS: (SNAPSHOT,), BOTH: (REPLICATE, SNAPSHOT)}


def _q(word: str) -> str:
    return shlex.quote(word)


def _gib(n: int) -> str:
    return sh.humanize_bytes(n)


def _refuse(log: Log, message: str, hint: str = "") -> NoReturn:
    """A refusal: nothing was changed, so no FATAL banner and no pause."""
    log.error(message)
    if hint:
        log.info(f"  {hint}")
    raise SystemExit(1)


def _read(log: Log) -> tuple[dict[str, Origin], exclusions.Declared]:
    """Origin and its exclusions, read strictly."""
    pools = survey.source_pools()
    try:
        origin = survey.read_origin(pools, strict=True)
        declared = exclusions.read(pools, origin, strict=True)
    except survey.ReadError as e:
        log.fatal(f"Cannot read the source pools: {e}")
    return origin, declared


def _list(log: Log) -> None:
    origin, declared = _read(log)
    if not declared.tops and not declared.ignored:
        log.info("Nothing is excluded: every dataset is backed up and snapshotted.")
        return
    if declared.tops:
        log.info("Excluded (each one with everything below it):")
        width = max(len(t) for t in declared.tops)
        for top, ex in sorted(declared.tops.items()):
            size = _gib(exclusions.subtree_referenced(top, origin))
            note = ""
            if ex.flags == SNAPSHOTS:
                note = "  (still backed up, as backup points only)"
            log.info(f"  {top:<{width}}  {ex.flags:<9}  {size}{note}")
    for rel, why in declared.ignored:
        log.warn(f"  ignored: {rel}: {why}")


def _drained_snapshots(rels: list[str], origin: dict[str, Origin]) -> int:
    return sum(1 for rel in rels for s in origin[rel].snaps if s.name.startswith("autosnap_"))


def _unanchored_drives(rels: list[str], origin: dict[str, Origin]) -> list[str]:
    """Registered drives with no zark 2 anchor on one of ``rels``: their base
    there is a sanoid snapshot, which the drain removes (as M4d-32 warns)."""
    drives = {i.guid: n for n, i in Config.load().known_drives.items()}
    out: set[str] = set()
    for rel in rels:
        o = origin[rel]
        anchored = {anchor_disk(r.name) for r in [*o.bookmarks, *o.snaps]} - {""}
        out |= {name for guid, name in drives.items() if guid not in anchored}
    return sorted(out)


def _add(dataset: str, what: str, log: Log) -> None:
    origin, _declared = _read(log)  # read strictly: a broken value is reported first
    if dataset not in origin:
        _refuse(log, f"{dataset} is not a dataset of the source pools", USAGE)
    why = exclusions.never_excluded(dataset, origin)
    if why:
        _refuse(log, f"{dataset} cannot be excluded: {why}")
    rels = exclusions.subtree(dataset, origin)
    log.info(f"{dataset} and everything below it ({len(rels)} dataset(s)):")
    for rel in rels:
        log.info(f"  {rel}  ({_gib(origin[rel].referenced)})")
    if what in (BACKUPS, BOTH):
        log.info(
            "Left out of every backup from the next one on; a copy a drive already "
            + "holds stays there, and that drive's next backup asks whether to keep it."
        )
    if what in (SNAPSHOTS, BOTH):
        n = _drained_snapshots(rels, origin)
        log.warn(
            f"sanoid takes no more snapshots of them, and removes the {n} it took "
            + "at its next prune."
        )
        late = _unanchored_drives(rels, origin)
        if late:
            log.warn(
                f"{', '.join(late)} has no zark 2 anchor there: its base may be one of "
                + "those snapshots. Back up to it first."
            )
    if not log.ask(f"Exclude {dataset} ({what})?", default=False):
        log.info("Nothing changed")
        return
    props = " ".join(f"{p}=no" for p in _PROPS[what])
    r = sh.run(f"zfs set {props} {_q(dataset)}", log=log)
    if not r.ok:
        log.fatal(f"Could not set {props} on {dataset}: {r.stderr.strip()}")
    log.ok(f"{dataset} excluded ({what})")
    if what in (SNAPSHOTS, BOTH):
        _sanoid(log)


def _next_backups(dataset: str, origin: dict[str, Origin], log: Log) -> None:
    """What each registered drive's next backup does with ``dataset``, from
    the anchors origin holds, without importing a drive (M5b-11 11a)."""
    anchored = {anchor_disk(b.name) for b in origin[dataset].bookmarks} - {""}
    for name, info in sorted(Config.load().known_drives.items()):
        if info.guid in anchored:
            # Without importing the drive this is all that is known (M5bP.2).
            log.info(
                f"  {name}: it has an anchor here: an incremental if it still holds {dataset} "
                + "as it was then; otherwise its next backup asks"
            )
        else:
            log.info(
                f"  {name}: in full (no anchor of it for this drive; if it holds an older "
                + "copy, its next backup asks what to do with it)"
            )


def _remove(dataset: str, what: str, log: Log) -> None:
    origin, declared = _read(log)
    ex = declared.tops.get(dataset)
    wanted = [p for p in _PROPS[what] if ex and (ex.replicate if p == REPLICATE else ex.snapshot)]
    if not wanted:
        top = exclusions.covering(dataset, set(declared.tops) - {dataset})
        if top:
            _refuse(
                log,
                f"{dataset} is excluded with {top} ({declared.tops[top].flags}): "
                + "only the whole subtree can be included again",
                f"{zark_command()} exclude remove {top} {what}",
            )
        log.info(f"{dataset} is not excluded ({what}): nothing to do")
        return
    for prop in wanted:
        r = sh.run(f"zfs inherit {prop} {_q(dataset)}", log=log)
        if not r.ok:
            log.fatal(f"Could not clear {prop} on {dataset}: {r.stderr.strip()}")
    log.ok(f"{dataset} included again ({what})")
    if REPLICATE in wanted:
        log.info("The next backup to each drive:")
        _next_backups(dataset, origin, log)
    if SNAPSHOT in wanted:
        _sanoid(log)


def _sanoid(log: Log) -> None:
    """sanoid.conf follows the snapshot flag (M5b-6), as setup writes it."""
    if not configure_sanoid(ZFS(log), log):
        log.warn(f"sanoid.conf not written: run {zark_command()} setup")


def run(args: list[str]) -> None:
    """``zark exclude list|add|remove``."""
    log = Log()
    if args in ([], ["list"]):
        _list(log)
        return
    if len(args) == 3 and args[0] in ("add", "remove") and args[2] in _PROPS:
        (_add if args[0] == "add" else _remove)(args[1].strip("/"), args[2], log)
        return
    log.error(USAGE)
    raise SystemExit(2)
