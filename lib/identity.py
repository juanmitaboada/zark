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
disk against the registry through :func:`recognise` (the pool GUID from the
on-disk label first, then the disk's own by-id name; an enclosure's name
proves only the enclosure), never through the kernel name.

:func:`protected_disks` answers "which disks must a destructive command
never touch": disks holding a vdev of an imported pool, a mounted
filesystem (the live USB, the ESP, a LUKS container) or active swap.
"""

import os
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lib.config import DriveInfo
from lib.sh import part, run

BY_ID_DIR = "/dev/disk/by-id"
SYS_BLOCK = "/sys/class/block"

# by-id prefixes in order of preference. wwn-/scsi-/nvme-eui. names come
# last: USB-SATA bridges can report a bogus WWN shared across enclosures
# (wwn-0x5000000000000001 on both Micron drives).
_BY_ID_RANK = ("usb-", "ata-", "nvme-", "mmc-", "virtio-", "dm-", "md-", "")
_BY_ID_WEAK = ("wwn-", "scsi-", "nvme-eui.", "nvme-nvme.")
# What a by-id name is made from (M16-1): the disk's own IDENTIFY or controller
# data (it follows the disk between enclosures), the transport or bridge (it
# follows the enclosure), or something not to rely on (_BY_ID_WEAK).
_DISK_PREFIXES = ("ata-", "nvme-", "mmc-", "virtio-")
_BUS_PREFIXES = ("usb-", "ieee1394-")
_PART_SUFFIX_RE = re.compile(r"-part\d+$")
_LSBLK_PAIR_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


class Kind(Enum):
    """What a /dev/disk/by-id name identifies."""

    DISK = "disk"  # ata-, nvme-, mmc-, virtio-: the disk itself
    BUS = "bus"  # usb-, ieee1394-: the enclosure or bridge it sits behind
    WEAK = "weak"  # wwn-, scsi-, nvme-eui.: may be shared by distinct disks
    OTHER = "other"  # dm-, md-, anything else


def classify(name: str) -> Kind:
    """The kind of a by-id basename. WEAK is tested first: ``nvme-eui.`` and
    ``nvme-nvme.`` begin with ``nvme-`` but are not a disk's own name."""
    if name.startswith(_BY_ID_WEAK):
        return Kind.WEAK
    if name.startswith(_DISK_PREFIXES):
        return Kind.DISK
    if name.startswith(_BUS_PREFIXES):
        return Kind.BUS
    return Kind.OTHER


def _best(names: set[str], kind: Kind, order: tuple[str, ...]) -> str:
    """The name of ``kind`` that ranks first in ``order``; ties by name."""
    picked = [n for n in names if classify(n) is kind]
    if not picked:
        return ""
    return min(picked, key=lambda n: (next(i for i, p in enumerate(order) if n.startswith(p)), n))


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
    def disk_id(self) -> str:
        """The disk's own by-id name (``ata-...``), or "" when it has none."""
        return _best(set(self.aliases) | {self.by_id}, Kind.DISK, _DISK_PREFIXES)

    @property
    def bus_id(self) -> str:
        """The enclosure's by-id name (``usb-...``), or "" when it has none."""
        return _best(set(self.aliases) | {self.by_id}, Kind.BUS, _BUS_PREFIXES)

    @property
    def part1(self) -> str:
        """First partition, by its by-id name when one exists (ZFS vdev)."""
        if self.by_id:
            return f"{BY_ID_DIR}/{self.by_id}-part1"
        return part(self.disk, 1)


def registry_names(ident: DiskIdentity) -> tuple[str, str]:
    """(drive_id, enclosure_id) to register ``ident`` under (M16-1): the
    disk's own name and the enclosure's when the disk has a name of its own;
    otherwise the preferred name alone, which is all that identifies it."""
    if ident.disk_id:
        return ident.disk_id, ident.bus_id
    return ident.by_id, ""


def names_of(disk: str) -> DiskIdentity:
    """The by-id names of a whole disk as a :class:`DiskIdentity`, without
    reading its label: enough to say which names to register it under."""
    names = by_id_names(disk)
    return DiskIdentity(disk=disk, by_id=preferred_by_id(names), aliases=tuple(names))


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


def _sectors(name: str) -> int | None:
    """Size of a block device in 512-byte sectors, from sysfs; None if unreadable."""
    try:
        return int(Path(SYS_BLOCK, name, "size").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def usb_ids(base: str) -> tuple[str, str]:
    """Return (vendor, product) lowercase hex for a USB-backed block device.

    Walks /sys/block/<base> back to the owning USB device and reads
    idVendor/idProduct. Returns ("", "") for non-USB devices or when the
    attributes cannot be found.
    """
    sys_block = Path(f"/sys/block/{base}")
    if not sys_block.exists():
        return "", ""
    # Resolve the real sysfs path and walk up looking for idVendor/idProduct.
    node = sys_block.resolve()
    for _ in range(12):  # bounded walk up the device tree
        vid = node / "idVendor"
        pid = node / "idProduct"
        if vid.exists() and pid.exists():
            try:
                return (
                    vid.read_text().strip().lower(),
                    pid.read_text().strip().lower(),
                )
            except OSError:
                return "", ""
        if node.parent == node:
            break
        node = node.parent
    return "", ""


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
    if _sectors(Path(node).name) == 0:
        # A card reader or enclosure with nothing in it: zpool create would
        # only fail later, with ENOMEDIUM (M1 checklist F3).
        raise IdentityError(f"{device}: no medium (size 0): insert the card or disk")

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


class Evidence(Enum):
    """What makes a registry entry describe a disk, strongest first (M16-2)."""

    LABEL = "label"  # the pool GUID on the disk is the entry's
    DISK = "disk"  # the entry's drive_id is the disk's own name
    BUS = "bus"  # only the enclosure's name matches: not proof of the disk


@dataclass(frozen=True)
class Match:
    """A registry entry that describes a disk, and why."""

    name: str
    evidence: Evidence


def _evidence(info: DriveInfo, ident: DiskIdentity, aliases: set[str]) -> Evidence | None:
    """Why ``info`` describes ``ident``, or None. The pool label is the
    authority; a disk's own name proves the same disk, or another one when
    both sides have one; an enclosure's name proves only the enclosure; a
    weak name (``wwn-``) proves nothing."""
    if ident.label and info.guid == ident.label.guid:
        return Evidence.LABEL
    own = classify(info.drive_id) is Kind.DISK
    if own and info.drive_id in aliases:
        return Evidence.DISK
    if own and ident.disk_id:
        return None  # two disk names that differ: another disk in the same enclosure
    for name in (info.drive_id, info.enclosure_id):
        if name in aliases and classify(name) is Kind.BUS:
            return Evidence.BUS
    return None


def recognise(ident: DiskIdentity, drives: dict[str, DriveInfo]) -> list[Match]:
    """Registry entries that describe ``ident``, in registry order, each with
    its evidence. Callers act on :class:`Evidence`: ``BUS`` is asked about."""
    aliases = set(ident.aliases) | ({ident.by_id} if ident.by_id else set())
    found: list[Match] = []
    for name, info in drives.items():
        evidence = _evidence(info, ident, aliases)
        if evidence is not None:
            found.append(Match(name, evidence))
    return found


def enclosure_mates(ident: DiskIdentity, drives: dict[str, DriveInfo]) -> list[str]:
    """Registered drives that share an enclosure name with ``ident`` and are
    not it: the disk's own names differ, so they are other disks that went
    through the same enclosure. For messages only (M16-2)."""
    aliases = set(ident.aliases) | ({ident.by_id} if ident.by_id else set())
    mates: list[str] = []
    for name, info in drives.items():
        if _evidence(info, ident, aliases) is not None:
            continue
        if any(
            n in aliases and classify(n) is Kind.BUS for n in (info.drive_id, info.enclosure_id)
        ):
            mates.append(name)
    return mates


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
