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
What `zark prune` can remove in origin, by kind (M3 decisions 9a–11a).

Pure functions over the origin listing (no shell). There is no floor
imposed by the backups: rpool's anchors are bookmarks, which no snapshot
destroy touches (redesign F5), and an incremental from a bookmark needs
only one origin snapshot after it, which the next backup's own point is.

Kinds:
  * sanoid's snapshots (``autosnap_*``), by type, removed by age;
  * legacy residues: ``syncoid_*`` snapshots left by zark 1.x, removable on
    a dataset only when every registered drive has an anchor there (one
    without may still have it as its newest common snapshot, redesign §7),
    and ``rpool/keystore@prepare_*``, which nothing uses as a base;
  * backup points left by interrupted runs (``zark_<UTC>``);
  * bpool's per-drive anchor snapshots (``zark_<GUID>_<UTC>``).
"""

import re
from dataclasses import dataclass, field

from lib.replication import KEYSTORE, POINT_RE, Origin, anchor_disk, is_bpool

AUTOSNAP_TYPE = re.compile(r"^autosnap_.*_(frequently|hourly|daily|weekly|monthly|yearly)$")
SYNCOID = re.compile(r"^syncoid_")
KEYSTORE_PREPARE = re.compile(r"^prepare_")


@dataclass(frozen=True)
class Item:
    """One origin snapshot that `zark prune` may offer."""

    target: str  # "ds@snap"
    creation: int
    reason: str = ""  # removable: why it may go; kept: why it stays


@dataclass
class Inventory:
    """Origin snapshots `zark prune` deals with, by kind."""

    autosnaps: dict[str, list[Item]] = field(default_factory=dict)  # type → items
    residues: list[Item] = field(default_factory=list)  # removable legacy residues
    residues_kept: list[Item] = field(default_factory=list)  # a drive may depend on them
    points: list[Item] = field(default_factory=list)  # left by interrupted runs
    anchors: dict[str, list[Item]] = field(default_factory=dict)  # drive GUID → bpool anchors


def inventory(origin: dict[str, Origin], drives: dict[str, str]) -> Inventory:
    """Classify origin's snapshots. ``drives`` maps each registered drive's
    pool GUID to its name."""
    inv = Inventory()
    for rel, o in sorted(origin.items()):
        anchored = {anchor_disk(r.name) for r in [*o.bookmarks, *o.snaps]} - {""}
        for s in o.snaps:
            target = f"{rel}@{s.name}"
            if m := AUTOSNAP_TYPE.match(s.name):
                inv.autosnaps.setdefault(m.group(1), []).append(Item(target, s.creation))
            elif POINT_RE.match(s.name):
                inv.points.append(Item(target, s.creation, "left by an interrupted run"))
            elif guid := anchor_disk(s.name):
                if is_bpool(rel):
                    inv.anchors.setdefault(guid, []).append(Item(target, s.creation))
            elif rel == KEYSTORE and KEYSTORE_PREPARE.match(s.name):
                inv.residues.append(Item(target, s.creation, "keystore snapshot left by prepare"))
            elif SYNCOID.match(s.name):
                missing = sorted(name for guid, name in drives.items() if guid not in anchored)
                if missing:
                    why = f"{', '.join(missing)} has no anchor on {rel}: it may be its base"
                    inv.residues_kept.append(Item(target, s.creation, why))
                else:
                    why = "syncoid residue; every registered drive has an anchor here"
                    inv.residues.append(Item(target, s.creation, why))
    return inv


def older_than(inv: Inventory, cutoff: int) -> list[Item]:
    """sanoid's snapshots created before ``cutoff`` (seconds since the epoch)."""
    return sorted(
        (i for items in inv.autosnaps.values() for i in items if i.creation < cutoff),
        key=lambda i: (i.creation, i.target),
    )
