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
Anchors and backup points in origin, outside a run.

A drive's anchors (a bookmark per rpool dataset, a renamed snapshot on
bpool), the points runs that did not end normally left in origin and the
drive each one names, and their removal when a drive leaves the registry.
"""

import shlex

from lib import sh
from lib.config import zark_command
from lib.drive_marks import snapshot_marks
from lib.log import Log
from lib.replication import POINT_RE, Origin, anchor_disk, anchor_name, is_bpool
from lib.survey import read_origin, source_pools


def _q(word: str) -> str:
    return shlex.quote(word)


# Set on every snapshot of a backup point when it is taken (same sync task):
# the pool GUID of the drive the run was for, so whatever removes a point a
# run left in origin first anchors it for that drive (M4b-4, 8b).
POINT_FOR = "org.zark:point-for"


def anchor_point(rel: str, point: str, disk_guid: str, log: Log) -> bool:
    """Make ``rel@point`` drive ``disk_guid``'s anchor: a bookmark on rpool;
    on bpool, where I-F forbids bookmarks, the snapshot renamed."""
    keep = anchor_name(disk_guid, point)
    if is_bpool(rel):
        cmd = f"zfs rename {_q(f'{rel}@{point}')} {_q(f'{rel}@{keep}')}"
        return sh.run(cmd, log=log).ok
    cmd = f"zfs bookmark {_q(f'{rel}@{point}')} {_q(f'{rel}#{keep}')}"
    if sh.run(cmd, log=log).ok:
        return True
    # A run stopped between its bookmark and the destroy (power loss) left
    # both: the anchor is there already when the guids match.
    r = sh.run(f"zfs get -Hp -o value guid {_q(f'{rel}@{point}')} {_q(f'{rel}#{keep}')}", log=log)
    return r.ok and len(r.lines) == 2 and r.lines[0] == r.lines[1]


def point_owners(pools: list[str]) -> dict[str, str]:
    """``rel@point`` → the pool GUID of the drive its run was for
    (:data:`POINT_FOR`), read from the snapshots themselves; strict
    (:class:`ReadError` when a listing fails). Points taken before rc5 carry
    no owner and are not listed."""
    out: dict[str, str] = {}
    for pool in pools:
        for name, value in snapshot_marks(pool, strict=True, prop=POINT_FOR).items():
            # A bpool point renamed to an anchor keeps the property: not a point.
            if value.isdigit() and POINT_RE.match(name.partition("@")[2]):
                out[name] = value
    return out


def release_point(target: str, owner: str, log: Log) -> bool:
    """Take a point a run left in origin out of the way for drive ``owner``
    (8b, M4b-4): on rpool it is bookmarked as that drive's anchor, then
    destroyed; on bpool it is renamed to that drive's anchor. True when the
    point is gone; when the anchor cannot be made the point stays."""
    rel, _, point = target.partition("@")
    if not anchor_point(rel, point, owner, log):
        log.warn(f"  kept {target}: could not anchor it for drive {owner}")
        return False
    if is_bpool(rel):
        log.info(f"  {target}: renamed to drive {owner}'s anchor")
        return True
    r = sh.run(f"zfs destroy {_q(target)}", log=log)
    if r.ok:
        log.info(f"  removed {target}: anchored for drive {owner} first")
    else:
        log.warn(f"  could not remove {target}: {r.stderr.strip()}")
    return r.ok


def anchored_disks(origin: dict[str, Origin]) -> set[str]:
    """Pool GUIDs that hold anchors in origin (bookmarks or bpool snapshots)."""
    disks: set[str] = set()
    for o in origin.values():
        for ref in [*o.bookmarks, *o.snaps]:
            g = anchor_disk(ref.name)
            if g:
                disks.add(g)
    return disks


def disk_anchors(origin: dict[str, Origin], disk_guid: str) -> list[str]:
    """Every anchor of one disk in origin, as ``ds#bookmark`` / ``ds@snapshot``."""
    names: list[str] = []
    for rel, o in sorted(origin.items()):
        names += [f"{rel}#{b.name}" for b in o.bookmarks if anchor_disk(b.name) == disk_guid]
        names += [f"{rel}@{s.name}" for s in o.snaps if anchor_disk(s.name) == disk_guid]
    return names


def drop_disk_anchors(disk_guid: str, pools: list[str], log: Log) -> tuple[int, list[str]]:
    """Remove every anchor of one disk from origin, by exact name; return how
    many went and the names that could not be destroyed (each one logged)."""
    count, failed = 0, []
    for name in disk_anchors(read_origin(pools), disk_guid):
        r = sh.run(f"zfs destroy {_q(name)}", log=log)
        if r.ok:
            count += 1
        else:
            failed.append(name)
            log.warn(f"  could not remove {name}: {r.stderr.strip()}")
    return count, failed


def drop_anchors_of(disk_guid: str, log: Log) -> str:
    """Drop a drive's anchors here when rpool is imported (purge, prepare;
    M2R-6). Returns what is left to do, "" when nothing."""
    pools = source_pools()
    retry = f"{zark_command()} registry forget {disk_guid}"
    if "rpool" not in pools:
        return f"Its anchors stay in origin: on the installed system run {retry}"
    count, failed = drop_disk_anchors(disk_guid, pools, log)
    if count:
        log.ok(f"{count} anchor(s) of drive {disk_guid} dropped from origin")
    if failed:
        return f"{len(failed)} anchor(s) of drive {disk_guid} could not be dropped: {retry}"
    return ""


def leftover_points(origin: dict[str, Origin]) -> list[str]:
    """Backup points left in origin by runs that did not end normally."""
    return [
        f"{rel}@{s.name}" for rel, o in sorted(origin.items()) for s in o.snaps if is_point(s.name)
    ]


def is_point(name: str) -> bool:
    """True for a backup point name."""
    return bool(POINT_RE.match(name))
