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
How origin's pools were at a backup point (M6-5): each pool's features and
their state, its ``compatibility``, and the Ubuntu release and OpenZFS tools
and module that ran it, recorded on every snapshot of the point on the
drive as ``org.zark:pools``, so that recover creates the new pools as
origin had them instead of with every feature of the live's ZFS.

On a system whose ``/boot`` is an ext4 partition (Ubuntu 26.10 and later
on UEFI, M12-1 1a) the record names rpool alone and carries a ``boot``
entry: the file system, its partition, the ESP and the swap as origin had
them, so that recover lays the disk out the same way (M12 plan 5.3). Every
point also records ``written_by``, the zark that wrote it, and an ext4
point ``min_recover``, the oldest zark that recovers it, which recover
checks before erasing anything (M12-8). Both are optional keys of format 1:
zark 2.1.1 reads only ``format``, ``pools``, ``ubuntu``, ``zfs`` and
``kmod``, so a record it reads is the same with or without them. A later
format must keep ``written_by`` and ``min_recover`` as they are here, top
level keys with a version as text: this zark reads both before it checks
``format``, so that it refuses such a point by the zark it names
(M13R-2).

A point without a record (an interrupted run, a carried sanoid snapshot)
is an ext4 point when the drive holds its boot environment's copy of
``/boot`` or carries ``org.zark:origin-layout=ext4``, which prepare sets
at ``zpool create`` and backup before any transfer (M13F-6 6a); recover
refuses it, since only a record says how to lay the disk out.

The states are what ``zpool get`` prints: ``enabled`` or ``active`` for a
feature the tools know, ``inactive`` or ``readonly`` for one they do not,
named ``unsupported@<guid>`` (lib/libzfs/libzfs_pool.c:963-990, :1139-1144
at OpenZFS 2.2.2). Disabled features are left out. The record is one JSON
value, well under a user property's 8191 bytes (module/zfs/dsl_prop.c:
963-970) with every feature of :data:`lib.zfs_features.FEATURES` in both
pools.
"""

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib import platform_facts, sh, zfs_features
from lib.boot_layout import Layout, boot_copy, boot_mount, running_be
from lib.config import VERSION
from lib.drive_marks import snapshot_marks, write_metadata
from lib.log import Log
from lib.restore_points import Snap
from lib.zfs import COMPAT_DIRS

POOLS_PROP = "org.zark:pools"
FORMAT = 1
ORIGIN_POOLS = ("rpool", "bpool")

# The oldest zark that recovers an ext4 point (M12-8).
MIN_RECOVER_EXT4 = "2.2.0"

# The drive's metadata key (``org.zark:origin-layout``) that says an ext4
# system writes to it (M13F-6 6a). Only "ext4" is ever written, so a later
# run of a bpool system cannot clear it (a drive serves one system).
ORIGIN_LAYOUT = "origin-layout"

# The persistent features of the 26.10 installer's /boot (eli's c1-2610
# storage.txt:157 without the transient needs_recovery and orphan_present),
# which are mke2fs.conf's defaults for ext4 in e2fsprogs 1.47.4
# (misc/mke2fs.conf.in:2, :14): recover makes the new /boot with these.
EXT4_FEATURES = (
    "64bit",
    "dir_index",
    "dir_nlink",
    "ext_attr",
    "extent",
    "extra_isize",
    "filetype",
    "flex_bg",
    "has_journal",
    "huge_file",
    "large_file",
    "metadata_csum",
    "metadata_csum_seed",
    "orphan_file",
    "resize_inode",
    "sparse_super",
)


def pools_for(layout: Layout) -> tuple[str, ...]:
    """The pools a system of ``layout`` has."""
    return ORIGIN_POOLS if layout == "bpool" else ("rpool",)


_ON = ("enabled", "active")
# A feature the tools do not know: refcount 0 prints "inactive", else "readonly".
_UNKNOWN_STATE = {"inactive": "enabled", "readonly": "active"}


@dataclass(frozen=True)
class PoolState:
    """One pool as recorded: ``features`` maps a short name, or
    ``unsupported@<guid>``, to ``enabled`` or ``active``."""

    compatibility: str
    features: dict[str, str]


@dataclass(frozen=True)
class Partition:
    """A partition as recorded: its size and GPT type."""

    size_bytes: int
    type_guid: str


@dataclass(frozen=True)
class Esp:
    """The ESP as recorded."""

    size_bytes: int
    uuid: str


@dataclass(frozen=True)
class Swap:
    """The swap partition as recorded, with origin's crypttab line for it."""

    size_bytes: int
    crypttab: str


@dataclass(frozen=True)
class BootRecord:  # pylint: disable=too-many-instance-attributes
    """An ext4 ``/boot`` as origin had it (M12 plan 5.3)."""

    dataset: str  # the copy of /boot in origin's rpool (M12-2 2e)
    fstype: str
    uuid: str
    label: str
    size_bytes: int
    features: tuple[str, ...]
    mount_options: str
    partition: Partition
    esp: Esp | None
    swap: Swap | None


@dataclass(frozen=True)
class PoolRecord:  # pylint: disable=too-many-instance-attributes
    """Origin's pools at a point, and what ran them."""

    ubuntu: str
    zfs: str
    kmod: str
    pools: dict[str, PoolState]
    boot: BootRecord | None = None
    written_by: str = ""
    min_recover: str = ""
    # A ``boot`` entry that is there but unreadable: the point's layout is
    # not known, so recover refuses it rather than take it for a bpool point.
    boot_problem: str = ""


def layout_of(rec: PoolRecord | None) -> Layout:
    """The boot layout of the point ``rec`` describes: ``bpool`` without a
    record or without a ``boot`` entry (every point written by 2.1)."""
    return "ext4" if rec is not None and (rec.boot is not None or rec.boot_problem) else "bpool"


def origin_layout_fields(layout: Layout) -> dict[str, str]:
    """The drive metadata an ext4 system writes before any transfer; none
    for a bpool system (M13F-6 6a)."""
    return {ORIGIN_LAYOUT: "ext4"} if layout == "ext4" else {}


def mark_origin_layout(pool: str, layout: Layout, log: Log) -> None:
    """On an ext4 system, ``org.zark:origin-layout=ext4`` on the drive
    ``pool`` before any transfer (backup; prepare sets it at ``zpool
    create``), so that recover knows a point of the drive without a record
    for an ext4 one (M13F-6 6a). A FATAL when the drive does not take it:
    a point it then held without a record would read as a bpool point."""
    fields = origin_layout_fields(layout)
    if fields and not write_metadata(pool, fields, log):
        log.fatal(
            f"Cannot record on {pool} that this system has /boot on ext4",
            causes=["The drive did not take a zfs set; nothing was sent"],
            solutions=[f"Check the drive: zpool status {pool}", "Run the backup again"],
        )


def point_layout(
    rec: PoolRecord | None, snaps: list[Snap], be: str, meta: dict[str, str]
) -> Layout:
    """The layout of a point on the drive (M13F-6 6a): its record's when it
    has one; else ext4 when the drive holds ``be``'s copy of ``/boot`` or
    its metadata says ext4 (``meta`` is :func:`lib.drive_marks.read_metadata`);
    else bpool, as every point written by 2.1."""
    if rec is not None:
        return layout_of(rec)
    copy = boot_copy(be)
    if meta.get(ORIGIN_LAYOUT) == "ext4" or any(s.dataset == copy for s in snaps):
        return "ext4"
    return "bpool"


def parse_get(text: str) -> PoolState:
    """The pool's state from ``zpool get -H -p -o property,value all <pool>``."""
    compat = "off"
    features: dict[str, str] = {}
    for line in text.splitlines():
        prop, _, value = line.partition("\t")
        value = value.strip()
        if prop == "compatibility":
            compat = value or "off"
        elif prop.startswith("feature@") and value in _ON:
            features[prop.removeprefix("feature@")] = value
        elif prop.startswith("unsupported@") and value in _UNKNOWN_STATE:
            features[prop] = _UNKNOWN_STATE[value]
    return PoolState(compat, features)


def _boot_json(b: BootRecord) -> dict[str, Any]:
    out: dict[str, Any] = {
        "layout": "ext4",
        "dataset": b.dataset,
        "fstype": b.fstype,
        "uuid": b.uuid,
        "label": b.label,
        "size_bytes": b.size_bytes,
        "features": list(b.features),
        "mount_options": b.mount_options,
        "partition": {"size_bytes": b.partition.size_bytes, "type_guid": b.partition.type_guid},
    }
    if b.esp is not None:
        out["esp"] = {"size_bytes": b.esp.size_bytes, "uuid": b.esp.uuid}
    if b.swap is not None:
        out["swap"] = {"size_bytes": b.swap.size_bytes, "crypttab": b.swap.crypttab}
    return out


def dump(rec: PoolRecord) -> str:
    """The record's JSON (ASCII, sorted, compact)."""
    data: dict[str, Any] = {
        "format": FORMAT,
        "ubuntu": rec.ubuntu,
        "zfs": rec.zfs,
        "kmod": rec.kmod,
        "pools": {
            name: {
                "compatibility": p.compatibility,
                "features": dict(sorted(p.features.items())),
            }
            for name, p in sorted(rec.pools.items())
        },
    }
    if rec.boot is not None:
        data["boot"] = _boot_json(rec.boot)
    if rec.written_by:
        data["written_by"] = rec.written_by
    if rec.min_recover:
        data["min_recover"] = rec.min_recover
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def capture(log: Log, layout: Layout = "bpool") -> str:
    """Origin's record for the point a run is about to take, or "" when a
    pool or, on an ext4 system, ``/boot`` cannot be read (a warning naming
    it). A bpool point is then written without one, as a drive written
    before 2.0.0-rc11 is (M6-9); an ext4 point is refused by its caller
    (M13C-5)."""
    states: dict[str, PoolState] = {}
    for pool in pools_for(layout):
        r = sh.run(f"zpool get -H -p -o property,value all {shlex.quote(pool)}")
        if not r.ok:
            log.warn(
                f"Cannot read {pool}'s features: {r.stderr.strip() or 'no output'} "
                + "(the point is recorded without origin's pools: recover then gives the "
                + "new pools every feature of its live)"
            )
            return ""
        states[pool] = parse_get(r.stdout)
    boot: BootRecord | None = None
    if layout == "ext4":
        boot, why = capture_boot()
        if boot is None:
            log.warn(f"Cannot record /boot: {why}")
            return ""
    zv = sh.run("zfs version")
    lines = [ln.strip() for ln in zv.lines] if zv.ok else []
    tools = next((ln for ln in lines if not ln.startswith("zfs-kmod-")), "")
    kmod = next((ln for ln in lines if ln.startswith("zfs-kmod-")), "")
    return dump(
        PoolRecord(
            platform_facts.ubuntu_release(),
            tools,
            kmod,
            states,
            boot=boot,
            written_by=VERSION,
            min_recover=MIN_RECOVER_EXT4 if boot is not None else "",
        )
    )


def require(mark: str, layout: Layout, log: Log) -> None:
    """Refuse an ext4 system's run when :func:`capture` gave no record
    (M13C-5): such a point would read as a bpool point, and recover would
    lay out the wrong disk."""
    if layout != "ext4" or mark:
        return
    log.fatal(
        "Cannot record how this system's pools and /boot are",
        causes=[
            "The warning above names what could not be read",
            "A point without that record would read as a bpool point, and recover "
            + "would lay out the wrong disk",
        ],
        solutions=["Nothing was written: fix what the warning names and run again"],
    )


def _lsblk(device: str) -> tuple[int, str, str] | None:
    """(size in bytes, partition type GUID, PARTUUID) of one block device."""
    r = sh.run(f"lsblk -b -n -d -o SIZE,PARTTYPE,PARTUUID {shlex.quote(device)}")
    fields = r.output.split() if r.ok else []
    if len(fields) < 2 or not fields[0].isdigit():
        return None
    return int(fields[0]), fields[1].lower(), fields[2].lower() if len(fields) > 2 else ""


def _tune2fs(text: str) -> tuple[list[str], int]:
    """(features, size in bytes) from ``tune2fs -l``."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    count, size = fields.get("Block count", ""), fields.get("Block size", "")
    total = int(count) * int(size) if count.isdigit() and size.isdigit() else 0
    return fields.get("Filesystem features", "").split(), total


def _fstab_options(text: str, mountpoint: str) -> str:
    """The options field of ``mountpoint``'s line in an fstab, "" without one."""
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and not f[0].startswith("#") and f[1] == mountpoint:
            return f[3]
    return ""


def _swap_line(text: str) -> tuple[str, str]:
    """(line, PARTUUID) of the crypttab line of a swap on a partition."""
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and not f[0].startswith("#") and "swap" in f[3].split(","):
            if f[1].startswith("PARTUUID="):
                return line.strip(), f[1].removeprefix("PARTUUID=").lower()
    return "", ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def capture_boot(root: str = "") -> tuple[BootRecord | None, str]:
    """The running system's ext4 ``/boot`` as a :class:`BootRecord`, or None
    and what could not be read. ``root`` prefixes ``/etc`` (tests)."""
    mount = boot_mount()
    if mount is None or mount.fstype != "ext4" or not mount.uuid:
        return None, "it is not an ext4 mount with a UUID (findmnt /boot)"
    be = running_be()
    if not be:
        return None, "the running boot environment is not one of rpool/ROOT (findmnt /)"
    src = shlex.quote(mount.source)
    t = sh.run(f"tune2fs -l {src}")
    if not t.ok:
        return None, f"tune2fs -l {mount.source}: {t.stderr.strip() or 'failed'}"
    features, size = _tune2fs(t.stdout)
    part = _lsblk(mount.source)
    if part is None or not size:
        return None, f"the size of {mount.source} (lsblk, tune2fs)"
    label = sh.run(f"blkid -p -s LABEL -o value {src}")
    return (
        BootRecord(
            dataset=boot_copy(be),
            fstype="ext4",
            uuid=mount.uuid,
            label=label.output.strip() if label.ok else "",
            size_bytes=size,
            features=tuple(f for f in EXT4_FEATURES if f in features),
            mount_options=_fstab_options(_read(f"{root}/etc/fstab"), "/boot") or "defaults",
            partition=Partition(part[0], part[1]),
            esp=_esp(),
            swap=_swap(root),
        ),
        "",
    )


def _esp() -> Esp | None:
    """The ESP mounted at ``/boot/efi``, None when it is not mounted."""
    efi = sh.run("findmnt -n -o SOURCE,UUID /boot/efi")
    fields = efi.output.split() if efi.ok else []
    part = _lsblk(fields[0]) if len(fields) >= 2 else None
    return Esp(part[0], fields[1]) if part is not None else None


def _swap(root: str) -> Swap | None:
    """The swap partition crypttab names, None without one."""
    line, partuuid = _swap_line(_read(f"{root}/etc/crypttab"))
    part = _lsblk(f"/dev/disk/by-partuuid/{partuuid}") if partuuid else None
    return Swap(part[0], line) if part is not None else None


class RecordError(ValueError):
    """A record that is there but cannot be read (another format, a
    malformed value, snapshots that disagree), with the versions it names
    when it names them (M13R-2)."""

    def __init__(self, message: str, written_by: str = "", min_recover: str = "") -> None:
        super().__init__(message)
        self.written_by = written_by
        self.min_recover = min_recover


def versions(value: str) -> tuple[str, str]:
    """``written_by`` and ``min_recover`` of a record of any format, as
    :func:`parse` keeps them; ("", "") when the value is no JSON object."""
    try:
        data = json.loads(value)
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return _text(data.get("written_by", "")), _text(data.get("min_recover", ""))


def parse(value: str) -> PoolRecord | None:
    """A record read back, or None when it is not one this zark reads.

    ``written_by`` and ``min_recover`` are read first, before ``format``
    (:func:`versions`), and kept as written, so that one that is not a
    version is refused by :func:`recovers`; a ``boot`` entry that cannot
    be read sets ``boot_problem``."""
    written_by, min_recover = versions(value)
    try:
        data = json.loads(value)
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ValueError("not a record of this format")
        raw = data.get("pools")
        if not isinstance(raw, dict):
            raise ValueError("no pools")
        pools = {str(name): _pool_state(p) for name, p in raw.items()}
        fields = [data.get(k, "") for k in ("ubuntu", "zfs", "kmod")]
        if not all(isinstance(f, str) for f in fields):
            raise ValueError("a version is not text")
    except (TypeError, ValueError):
        return None
    boot: BootRecord | None = None
    problem = ""
    if "boot" in data:
        try:
            boot = _boot(data["boot"])
        except (TypeError, ValueError) as e:
            problem = str(e) or "malformed"
    return PoolRecord(
        fields[0],
        fields[1],
        fields[2],
        pools,
        boot=boot,
        written_by=written_by,
        min_recover=min_recover,
        boot_problem=problem,
    )


def _text(v: object) -> str:
    """A recorded version as text; anything else as its JSON, which is
    then no version (refused where one is needed)."""
    return v if isinstance(v, str) else json.dumps(v)


def _int(v: object) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ValueError("a size is not a positive integer")
    return v


def _str(v: object) -> str:
    if not isinstance(v, str):
        raise ValueError("a field is not text")
    return v


def _obj(v: object) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise ValueError("an entry is not an object")
    return v


def _boot(raw: object) -> BootRecord | None:
    """The ``boot`` entry read back: None for ``{"layout": "bpool"}``;
    :class:`ValueError` when it is not one this zark reads."""
    b = _obj(raw)
    layout = b.get("layout")
    if layout == "bpool":
        return None
    if layout != "ext4" or b.get("fstype") != "ext4":
        raise ValueError(f"layout {layout!r} is not one this zark recovers")
    feats = b.get("features")
    if not isinstance(feats, list) or not all(isinstance(f, str) for f in feats):
        raise ValueError("features is not a list of names")
    part = _obj(b.get("partition"))
    esp = _obj(b["esp"]) if "esp" in b else None
    swap = _obj(b["swap"]) if "swap" in b else None
    return BootRecord(
        dataset=_str(b.get("dataset")),
        fstype="ext4",
        uuid=_str(b.get("uuid")),
        label=_str(b.get("label", "")),
        size_bytes=_int(b.get("size_bytes")),
        features=tuple(feats),
        mount_options=_str(b.get("mount_options", "defaults")),
        partition=Partition(_int(part.get("size_bytes")), _str(part.get("type_guid"))),
        esp=Esp(_int(esp.get("size_bytes")), _str(esp.get("uuid"))) if esp is not None else None,
        swap=(
            Swap(_int(swap.get("size_bytes")), _str(swap.get("crypttab")))
            if swap is not None
            else None
        ),
    )


_ZARK_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-rc\d+)?$")


def zark_version_tuple(text: str) -> tuple[int, int, int] | None:
    """``X.Y.Z`` of a zark version, an ``-rcN`` suffix ignored (an rc of X
    recovers what X does: M12-8); None for anything else."""
    m = _ZARK_VERSION.match(text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def recovers(min_recover: str, version: str = VERSION) -> bool:
    """True when zark ``version`` may recover a point recorded with
    ``min_recover``: an empty one (every bpool point, every 2.1 point)
    always, an unparsable one or an unparsable ``version`` never."""
    if not min_recover:
        return True
    need, have = zark_version_tuple(min_recover), zark_version_tuple(version)
    return need is not None and have is not None and have >= need


def _pool_state(p: object) -> PoolState:
    """One pool of a record read back; :class:`ValueError` when malformed."""
    if not isinstance(p, dict):
        raise ValueError("a pool is not an object")
    compat, feats = p.get("compatibility"), p.get("features")
    if not isinstance(compat, str) or not isinstance(feats, dict):
        raise ValueError("a pool lacks its compatibility or features")
    if not all(isinstance(k, str) and v in _ON for k, v in feats.items()):
        raise ValueError("a feature state is not enabled or active")
    return PoolState(compat, dict(feats))


def read(pool: str, point: str, snaps: list[Snap]) -> PoolRecord | None:
    """Origin's pools at ``point``, from its snapshots on the drive, read
    strictly (raises :class:`lib.survey.ReadError`), as recover reads the
    point's exclusion record. None for a point without one (written before
    2.0.0-rc11); :class:`RecordError` when the snapshots disagree or the
    value is not a record, naming the first ``min_recover`` among the
    values that this zark does not meet (M13R-2)."""
    marks = snapshot_marks(f"{pool}/rpool", strict=True, prop=POOLS_PROP)
    values = {
        marks[n]
        for n in (f"{pool}/{s.stored}@{s.name}" for s in snaps if s.name == point)
        if n in marks
    }
    if not values:
        return None
    rec = parse(next(iter(values))) if len(values) == 1 else None
    if rec is None:
        named = [versions(v) for v in sorted(values)]
        blocking = [v for v in named if not recovers(v[1])]
        written_by, min_recover = (blocking or named)[0]
        raise RecordError(
            f"the snapshots of {point} disagree on origin's pools, or their record is unreadable",
            written_by,
            min_recover,
        )
    return rec


def describe(rec: PoolRecord, pool: str) -> str:
    """One line for recover's plan."""
    p = rec.pools.get(pool)
    n = len(p.features) if p else 0
    ran = ", ".join(
        x
        for x in (
            f"Ubuntu {rec.ubuntu}" if rec.ubuntu else "",
            f"OpenZFS {rec.zfs.removeprefix('zfs-')}" if rec.zfs else "",
            f"module {rec.kmod.removeprefix('zfs-kmod-')}" if rec.kmod else "",
        )
        if x
    )
    return f"{pool}: {n} feature(s) as origin had them" + (f" ({ran})" if ran else "")


@dataclass(frozen=True)
class Create:
    """How recover creates one pool from origin's record (M6-6)."""

    features: tuple[str, ...]  # enabled at create, by name
    compatibility: str  # set at create, "" for none
    lacking_active: tuple[str, ...]  # origin had them active; this ZFS lacks them
    lacking_enabled: tuple[str, ...]  # only enabled in origin; left out
    compat_missing: str  # origin's compatibility, not installed on this live

    def options(self) -> str:
        """The ``zpool create`` options: no default feature, only these."""
        feats = [f"-o feature@{f}=enabled" for f in self.features]
        compat = (
            [f"-o compatibility={shlex.quote(self.compatibility)}"] if self.compatibility else []
        )
        return " ".join(["-d", *feats, *compat])


# Root properties a pool's create refuses without a feature, and the kernel
# then destroys the new pool (module/zfs/zfs_ioctl.c:4966-4977, :5028-5043,
# :1527-1531 at OpenZFS 2.4.1; :4630-4641, :4692-4707, :1503-1507 at 2.2.2).
NEEDS_FEATURE = {"compression=lz4": "lz4_compress", "dnodesize=auto": "large_dnode"}


def root_options(spec: Create | None, props: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """The ``-O`` options for ``props`` on a pool created as ``spec`` (None:
    every feature of this ZFS), and the ones left to the default because
    ``spec`` does not enable the feature they need (M9b-9 9a)."""
    left = tuple(
        p
        for p in props
        if spec is not None and p in NEEDS_FEATURE and NEEDS_FEATURE[p] not in spec.features
    )
    return " ".join(f"-O {p}" for p in props if p not in left), left


def feature_name(key: str) -> str:
    """A recorded feature by its short name; ``unsupported@<guid>`` through
    zark's table when it knows the GUID, else as it was recorded."""
    if key.startswith("unsupported@"):
        f = zfs_features.BY_GUID.get(key.removeprefix("unsupported@"))
        return f.name if f else key
    return key


def compat_installed(value: str) -> bool:
    """Whether every file a ``compatibility`` value names is on this system:
    ``zpool create`` refuses one that is not (lib/libzfs/libzfs_pool.c:826-837
    at OpenZFS 2.4.1, ``zpool_load_compat``)."""
    names = [n for n in value.split(",") if n]
    return bool(names) and all(any(Path(d, n).is_file() for d in COMPAT_DIRS) for n in names)


def create_spec(state: PoolState, known: set[str]) -> Create:
    """What recover passes to ``zpool create`` for a pool origin had as
    ``state``, on a live whose ZFS lists ``known`` features."""
    on, active, enabled = [], [], []
    for key, st in sorted(state.features.items()):
        name = feature_name(key)
        if name in known:
            on.append(name)
        elif st == "active":
            active.append(name)
        else:
            enabled.append(name)
    compat = state.compatibility if state.compatibility not in ("", "off") else ""
    # legacy names no file: zpool_load_compat takes it as every feature off
    # (lib/libzfs/libzfs_pool.c:5199-5206 at OpenZFS 2.4.1, :4876-4883 at 2.2.2).
    present = compat if compat == "legacy" or (compat and compat_installed(compat)) else ""
    return Create(
        tuple(on), present, tuple(active), tuple(enabled), compat if compat and not present else ""
    )
