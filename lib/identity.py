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
Device identity: the single way every command names a physical disk.

Any device argument (``/dev/sdX``, ``/dev/sdX1``, ``/dev/disk/by-id/...``,
``/dev/disk/by-path/...``) is canonicalised once with ``realpath`` and
``lsblk`` into a :class:`DiskIdentity`: the whole-disk kernel node, its
stable ``/dev/disk/by-id`` name, the hardware facts shown to the operator,
and the ZFS label read from the disk itself (``zdb -l``). Commands match a
disk against the registry through :func:`match_registry` (pool GUID from
the on-disk label first, then ``drive_id``), never through the kernel name.

:func:`protected_disks` answers "which disks must a destructive command
never touch": disks holding a vdev of an imported pool, a mounted
filesystem (the live USB, the ESP, a LUKS container) or active swap.
"""

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from lib.config import DriveInfo
from lib.sh import part, run

BY_ID_DIR = "/dev/disk/by-id"

# by-id prefixes in order of preference. wwn-/scsi-/nvme-eui. names come
# last: USB-SATA bridges can report a bogus WWN shared across enclosures
# (wwn-0x5000000000000001 on both Micron drives).
_BY_ID_RANK = ("usb-", "ata-", "nvme-", "mmc-", "virtio-", "dm-", "md-", "")
_BY_ID_WEAK = ("wwn-", "scsi-", "nvme-eui.", "nvme-nvme.")
_PART_SUFFIX_RE = re.compile(r"-part\d+$")
_LSBLK_PAIR_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


class IdentityError(Exception):
    """A device argument could not be resolved to a whole disk."""


@dataclass(frozen=True)
class PoolLabel:
    """The ZFS label found on a partition (``zdb -l``)."""

    name: str
    guid: str
    hostid: str = ""
    hostname: str = ""


@dataclass(frozen=True)
class DiskIdentity:  # pylint: disable=too-many-instance-attributes
    """A physical disk, identified once and shared by every command."""

    disk: str  # whole-disk kernel node, e.g. /dev/sdb
    by_id: str  # preferred /dev/disk/by-id basename of the whole disk, or ""
    model: str = ""
    serial: str = ""
    size: str = ""
    transport: str = ""
    label: PoolLabel | None = None
    aliases: tuple[str, ...] = ()  # every whole-disk by-id name of this disk

    @property
    def name(self) -> str:
        """Kernel name of the whole disk (``sdb``)."""
        return Path(self.disk).name

    @property
    def by_id_path(self) -> str:
        """Absolute by-id path of the whole disk, or "" when there is none."""
        return f"{BY_ID_DIR}/{self.by_id}" if self.by_id else ""

    @property
    def part1(self) -> str:
        """First partition, by its by-id name when one exists (ZFS vdev)."""
        if self.by_id:
            return f"{BY_ID_DIR}/{self.by_id}-part1"
        return part(self.disk, 1)


def _lsblk_fields(node: str) -> dict[str, str]:
    """``lsblk -dn -P`` for one node as a dict (empty on failure)."""
    r = run(f"lsblk -dn -P -o NAME,TYPE,PKNAME,MODEL,SERIAL,SIZE,TRAN {shlex.quote(node)}")
    if not r.ok or not r.output:
        return {}
    line = r.output.splitlines()[0]
    return {k: v.replace('\\"', '"').strip() for k, v in _LSBLK_PAIR_RE.findall(line)}


def by_id_names(disk: str) -> list[str]:
    """All /dev/disk/by-id entries (whole-disk only) that point at ``disk``."""
    target = Path(disk).name
    r = run(f"find {BY_ID_DIR}/ -maxdepth 1 -type l -printf '%f\\t%l\\n'")
    if not r.ok:
        return []
    names: list[str] = []
    for line in r.lines:
        fields = line.strip().split("\t")
        if len(fields) != 2:
            continue
        name, link = fields
        if Path(link).name == target and not _PART_SUFFIX_RE.search(name):
            names.append(name)
    return sorted(names)


def preferred_by_id(names: list[str]) -> str:
    """Pick the most stable by-id name; weak (WWN-like) names only as a last resort."""
    strong = [n for n in names if not n.startswith(_BY_ID_WEAK)]
    pool = strong or list(names)
    if not pool:
        return ""

    def rank(n: str) -> tuple[int, str]:
        for i, prefix in enumerate(_BY_ID_RANK):
            if n.startswith(prefix):
                return (i, n)
        return (len(_BY_ID_RANK), n)

    return min(pool, key=rank)


def read_pool_label(device: str) -> PoolLabel | None:
    """Read the ZFS label on ``device`` with ``zdb -l`` (read-only), or None."""
    r = run(f"zdb -l {shlex.quote(device)}")
    if not r.ok:
        return None
    found: dict[str, str] = {}
    for line in r.lines:
        m = re.match(r"^\s+(name|pool_guid|hostid|hostname):\s+'?([^']*)'?\s*$", line)
        if m and m.group(1) not in found:
            found[m.group(1)] = m.group(2)
    if "name" not in found or "pool_guid" not in found:
        return None
    return PoolLabel(
        name=found["name"],
        guid=found["pool_guid"],
        hostid=found.get("hostid", ""),
        hostname=found.get("hostname", ""),
    )


def whole_disk(device: str) -> str:
    """Whole-disk node for any device path (disk, partition, by-id), or ""."""
    node = os.path.realpath(device)
    info = _lsblk_fields(node)
    if info.get("TYPE") == "part" and info.get("PKNAME"):
        return f"/dev/{info['PKNAME']}"
    if info.get("TYPE") == "disk":
        return node
    return ""


def resolve_disk(device: str, *, allow_partition: bool = False) -> DiskIdentity:
    """Canonicalise a device argument into a :class:`DiskIdentity`.

    Raises :class:`IdentityError` when the path is not a disk, or is a
    partition and ``allow_partition`` is False (destructive commands work
    on whole disks only).
    """
    node = os.path.realpath(device)
    info = _lsblk_fields(node)
    if not info:
        raise IdentityError(f"{device}: not a block device known to lsblk")
    if info.get("TYPE") == "part":
        parent = f"/dev/{info.get('PKNAME', '')}"
        if not allow_partition:
            raise IdentityError(
                f"{device} is a partition of {parent}; pass the whole disk instead",
            )
        node = parent
        info = _lsblk_fields(node)
    if info.get("TYPE") != "disk":
        raise IdentityError(f"{device}: type '{info.get('TYPE', '?')}' is not a disk")

    aliases = tuple(by_id_names(node))
    by_id = preferred_by_id(list(aliases))
    ident = DiskIdentity(
        disk=node,
        by_id=by_id,
        model=info.get("MODEL", ""),
        serial=info.get("SERIAL", ""),
        size=info.get("SIZE", ""),
        transport=info.get("TRAN", ""),
    )
    label_dev = ident.part1 if Path(ident.part1).exists() else ""
    label = read_pool_label(label_dev) if label_dev else None
    if label is None:
        label = read_pool_label(node)
    return DiskIdentity(
        disk=ident.disk,
        by_id=ident.by_id,
        model=ident.model,
        serial=ident.serial,
        size=ident.size,
        transport=ident.transport,
        label=label,
        aliases=aliases,
    )


def match_registry(ident: DiskIdentity, drives: dict[str, DriveInfo]) -> list[str]:
    """Registry entries that describe ``ident``: by on-disk pool GUID, then by
    drive_id against every by-id alias of the disk (a 1.0.12 entry may carry
    a weak wwn- alias)."""
    names: list[str] = []
    if ident.label:
        names += [n for n, info in drives.items() if info.guid == ident.label.guid]
    ids = set(ident.aliases) | ({ident.by_id} if ident.by_id else set())
    names += [n for n, info in drives.items() if info.drive_id in ids]
    return list(dict.fromkeys(names))


def disks_under(device: str) -> set[str]:
    """Whole disks beneath a device (partition, dm/LUKS, md), via ``lsblk -s``."""
    r = run(f"lsblk -nrs -o NAME,TYPE {shlex.quote(device)}")
    if not r.ok:
        return set()
    disks: set[str] = set()
    for line in r.lines:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "disk":
            disks.add(f"/dev/{fields[0]}")
    return disks


def protected_disks(*, own_pool: str | None = None) -> dict[str, str]:
    """Disks a destructive command must never touch, mapped to the reason.

    ``own_pool`` exempts the vdevs of the pool the command is itself
    operating on (``purge`` of an imported backup pool).
    """
    reasons: dict[str, str] = {}

    pools = run("zpool list -H -o name")
    for pool in pools.lines if pools.ok else []:
        pool = pool.strip()
        if not pool or pool == own_pool:
            continue
        vdevs = run(f"zpool list -vHP {shlex.quote(pool)}")
        for line in vdevs.lines if vdevs.ok else []:
            first = line.strip().split("\t")[0].split()[0] if line.strip() else ""
            if first.startswith("/dev/"):
                for d in disks_under(first):
                    reasons.setdefault(d, f"holds a vdev of imported pool '{pool}'")

    mounts = run("findmnt -rn -o SOURCE,TARGET")
    for line in mounts.lines if mounts.ok else []:
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("/dev/"):
            for d in disks_under(fields[0]):
                reasons.setdefault(d, f"has a mounted filesystem ({fields[1]})")

    swaps = run("swapon --show=NAME --noheadings --raw")
    for line in swaps.lines if swaps.ok else []:
        dev = line.strip()
        if dev.startswith("/dev/"):
            for d in disks_under(dev):
                reasons.setdefault(d, "holds active swap")
    return reasons
