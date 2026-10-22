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

The states are what ``zpool get`` prints: ``enabled`` or ``active`` for a
feature the tools know, ``inactive`` or ``readonly`` for one they do not,
named ``unsupported@<guid>`` (lib/libzfs/libzfs_pool.c:963-990, :1139-1144
at OpenZFS 2.2.2). Disabled features are left out. The record is one JSON
value, well under a user property's 8191 bytes (module/zfs/dsl_prop.c:
963-970) with every feature of :data:`lib.zfs_features.FEATURES` in both
pools.
"""

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from lib import sh, zfs_features
from lib.drive_marks import snapshot_marks
from lib.log import Log
from lib.restore_points import Snap
from lib.zfs import COMPAT_DIRS

POOLS_PROP = "org.zark:pools"
FORMAT = 1
ORIGIN_POOLS = ("rpool", "bpool")
OS_RELEASE = "/etc/os-release"

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
class PoolRecord:
    """Origin's pools at a point, and what ran them."""

    ubuntu: str
    zfs: str
    kmod: str
    pools: dict[str, PoolState]


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


def _ubuntu() -> str:
    try:
        text = Path(OS_RELEASE).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "VERSION_ID":
            return value.strip().strip('"')
    return ""


def dump(rec: PoolRecord) -> str:
    """The record's JSON (ASCII, sorted, compact)."""
    return json.dumps(
        {
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
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def capture(log: Log, pools: tuple[str, ...] = ORIGIN_POOLS) -> str:
    """Origin's record for the point a run is about to take, or "" when a
    pool cannot be read (a warning; the point is then written without one,
    as a drive written before 2.0.0-rc11 is: M6-9)."""
    states: dict[str, PoolState] = {}
    for pool in pools:
        r = sh.run(f"zpool get -H -p -o property,value all {shlex.quote(pool)}")
        if not r.ok:
            log.warn(
                f"Cannot read {pool}'s features: {r.stderr.strip() or 'no output'} "
                + "(the point is recorded without origin's pools: recover then gives the "
                + "new pools every feature of its live)"
            )
            return ""
        states[pool] = parse_get(r.stdout)
    zv = sh.run("zfs version")
    lines = [ln.strip() for ln in zv.lines] if zv.ok else []
    tools = next((ln for ln in lines if not ln.startswith("zfs-kmod-")), "")
    kmod = next((ln for ln in lines if ln.startswith("zfs-kmod-")), "")
    return dump(PoolRecord(_ubuntu(), tools, kmod, states))


def parse(value: str) -> PoolRecord | None:
    """A record read back, or None when it is not one this zark reads."""
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
    return PoolRecord(fields[0], fields[1], fields[2], pools)


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
    2.0.0-rc11); :class:`ValueError` when the snapshots disagree or the
    value is not a record."""
    marks = snapshot_marks(f"{pool}/rpool", strict=True, prop=POOLS_PROP)
    values = {
        marks[n]
        for n in (f"{pool}/{s.stored}@{s.name}" for s in snaps if s.name == point)
        if n in marks
    }
    if not values:
        return None
    rec = parse(values.pop()) if len(values) == 1 else None
    if rec is None:
        raise ValueError(
            f"the snapshots of {point} disagree on origin's pools, or their record is unreadable"
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
        feats = " ".join(f"-o feature@{f}=enabled" for f in self.features)
        compat = (
            f" -o compatibility={shlex.quote(self.compatibility)}" if self.compatibility else ""
        )
        return f"-d {feats}{compat}".strip()


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
    compat = state.compatibility if state.compatibility not in ("", "off", "legacy") else ""
    present = compat if compat and compat_installed(compat) else ""
    return Create(
        tuple(on), present, tuple(active), tuple(enabled), compat if compat and not present else ""
    )
