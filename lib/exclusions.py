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
Datasets left out of backups or of local snapshots (D7 to D12).

An exclusion is declared on an origin dataset as a ZFS user property set
there (M5b-3 3a): ``org.zark:replicate=no`` leaves the dataset out of every
backup, ``org.zark:snapshot=no`` out of sanoid's snapshots (D8). Only values
set on the dataset itself count (``zfs get -s local``), and an exclusion
covers the dataset and everything below it (M5b-4 4a): ZFS inheritance does
not decide, so a child cannot be included alone.

Never excluded (D10, M5b-5): the boot environments (``rpool/ROOT`` and
below), the boot pool, the keystore, the structural containers, and a
dataset whose subtree holds any of them; zvols and ``.archived-`` names are
left out already. A value found on such a dataset (set by hand) is ignored
and reported at every run.

A user property changes no mount, so it can be set with the keystore zvol
present (the chase.c:648 rule is about mount properties).
"""

import json
import shlex
from dataclasses import dataclass, field

from lib import sh
from lib.replication import (
    EXCLUDED_KEPT_PROP,
    KEYSTORE,
    STRUCTURAL,
    Origin,
    is_archived,
    is_bpool,
)
from lib.survey import checked

REPLICATE = "org.zark:replicate"
SNAPSHOT = "org.zark:snapshot"
# On every snapshot of a backup point on a drive: what that point left out
# (M5b-7 7a), so recover recreates it and never restores an older copy.
POINT_MARK = "org.zark:excluded"
# On a drive's copy of an excluded dataset the user chose to keep (M5b-10).
KEPT = EXCLUDED_KEPT_PROP
# A user property value holds at most 8191 bytes (OpenZFS 2.4.1
# include/sys/fs/zfs.h:86, module/zfs/zfs_ioctl.c:2937-2938).
MARK_LIMIT = 8191

# What `zark exclude` calls each flag.
BACKUPS, SNAPSHOTS, BOTH = "backups", "snapshots", "both"


def _q(word: str) -> str:
    return shlex.quote(word)


def under(rel: str, top: str) -> bool:
    """True for ``top`` and everything below it."""
    return rel == top or rel.startswith(f"{top}/")


def covering(rel: str, tops: frozenset[str] | set[str]) -> str:
    """The exclusion that covers ``rel`` (its nearest top), or ""."""
    best = ""
    for top in tops:
        if under(rel, top) and len(top) > len(best):
            best = top
    return best


def never_excluded(rel: str, origin: dict[str, Origin] | None = None) -> str:
    """Why ``rel`` can never be excluded, "" when it can (D10, M5b-5 5a)."""
    o = (origin or {}).get(rel)
    rules = (
        (
            rel == "rpool/ROOT" or rel.startswith("rpool/ROOT/"),
            "a boot environment is never excluded",
        ),
        (is_bpool(rel), "the boot pool is never excluded"),
        (under(rel, KEYSTORE), "the keystore is never excluded"),
        (
            rel in STRUCTURAL,
            "a structural container is not replicated itself: exclude its children instead",
        ),
        (o is not None and o.kind == "volume", "a zvol is never backed up already"),
        (is_archived(rel), "zark leaves .archived- names out already"),
    )
    why = next((text for hit, text in rules if hit), "")
    if why:
        return why
    held = next(
        (x for x in sorted(origin or {}) if x != rel and under(x, rel) and never_excluded(x)), ""
    )
    return f"its subtree holds {held}" if held else ""


@dataclass(frozen=True)
class Exclusion:
    """One declared exclusion: a dataset and its subtree."""

    top: str
    replicate: bool  # left out of backups
    snapshot: bool  # left out of sanoid's snapshots

    @property
    def flags(self) -> str:
        """``backups``, ``snapshots`` or ``both``."""
        if self.replicate and self.snapshot:
            return BOTH
        return BACKUPS if self.replicate else SNAPSHOTS


def _minimal(rels: set[str]) -> frozenset[str]:
    """The datasets of ``rels`` that no other one of them covers."""
    return frozenset(r for r in rels if not covering(r, rels - {r}))


@dataclass
class Declared:
    """The exclusions read from origin, and the values zark ignores.

    ``tops`` holds every dataset an exclusion was set on (the user's own
    words, for ``zark exclude list``); each flag covers its subtree, so a
    dataset set inside one already covered for the same flag changes nothing.
    """

    tops: dict[str, Exclusion] = field(default_factory=dict)
    ignored: list[tuple[str, str]] = field(default_factory=list)  # (dataset, why)

    @property
    def no_backup(self) -> frozenset[str]:
        """The subtree tops left out of backups."""
        return _minimal({t for t, e in self.tops.items() if e.replicate})

    @property
    def no_snapshot(self) -> frozenset[str]:
        """The subtree tops left out of sanoid's snapshots."""
        return _minimal({t for t, e in self.tops.items() if e.snapshot})

    def flags_of(self, top: str) -> str:
        """``backups``, ``snapshots`` or ``both`` for a top of :attr:`no_backup`."""
        return BOTH if covering(top, set(self.no_snapshot)) else BACKUPS


def _values(lines: list[str]) -> dict[str, dict[str, str]]:
    """``name property value`` lines → {dataset: {property: value}}."""
    values: dict[str, dict[str, str]] = {}
    for line in lines:
        f = line.split("\t")
        if len(f) == 3 and f[1] in (REPLICATE, SNAPSHOT) and "@" not in f[0] and "#" not in f[0]:
            values.setdefault(f[0], {})[f[1]] = f[2]
    return values


def parse(lines: list[str], origin: dict[str, Origin]) -> Declared:
    """``name property value`` lines of ``zfs get -H -s local`` into the
    declared exclusions, origin's datasets naming what is checked."""
    values = _values(lines)
    out = Declared()
    for rel, props in sorted(values.items()):
        bad = [f"{p}={v}" for p, v in sorted(props.items()) if v not in ("no", "yes")]
        if bad:
            out.ignored.append((rel, f"{', '.join(bad)}: only 'no' excludes"))
        rep, snap = props.get(REPLICATE) == "no", props.get(SNAPSHOT) == "no"
        if not (rep or snap):
            continue
        why = never_excluded(rel, origin)
        if why:
            out.ignored.append((rel, why))
            continue
        out.tops[rel] = Exclusion(rel, rep, snap)
    flag_sets = {REPLICATE: set(out.no_backup), SNAPSHOT: set(out.no_snapshot)}
    for rel, props in sorted(values.items()):
        for prop, value in sorted(props.items()):
            top = covering(rel, flag_sets[prop] - {rel})
            if value == "yes" and top:
                out.ignored.append(
                    (rel, f"{prop}=yes inside {top}, which excludes its whole subtree")
                )
    return out


def read(pools: list[str], origin: dict[str, Origin], strict: bool = False) -> Declared:
    """The exclusions declared in ``pools`` (one ``zfs get`` per pool)."""
    lines: list[str] = []
    for pool in pools:
        r = checked(
            sh.run(
                f"zfs get -H -s local -o name,property,value {REPLICATE},{SNAPSHOT} -r {_q(pool)}"
            ),
            strict,
        )
        lines += r.lines if r.ok else []
    return parse(lines, origin)


def subtree(top: str, origin: dict[str, Origin]) -> list[str]:
    """``top`` and every origin dataset below it, parents first."""
    return sorted(rel for rel in origin if under(rel, top))


def subtree_referenced(top: str, origin: dict[str, Origin]) -> int:
    """Bytes a first full send of the subtree would carry (D12, M5b-12 12a)."""
    return sum(origin[rel].referenced for rel in subtree(top, origin))


def point_mark(declared: Declared, origin: dict[str, Origin]) -> str:
    """The value recorded on a point's snapshots (M5b-7 7a, M5b-8): every
    top with its flags, and every dataset of its subtree with origin's
    canmount and mountpoint at the point. ``{}``-shaped when nothing is
    excluded, so a drive written before rc10 reads differently."""
    tops = {t: declared.flags_of(t) for t in sorted(declared.no_backup)}
    datasets = {
        rel: [origin[rel].canmount, origin[rel].mountpoint]
        for top in sorted(tops)
        for rel in subtree(top, origin)
    }
    return json.dumps({"tops": tops, "datasets": datasets}, sort_keys=True, separators=(",", ":"))


@dataclass
class PointMark:
    """What a backup point left out, read back from the drive."""

    tops: dict[str, str] = field(default_factory=dict)  # top -> flags
    datasets: dict[str, tuple[str, str]] = field(default_factory=dict)  # rel -> (canmount, mp)

    def covers(self, rel: str) -> bool:
        """True when the point left ``rel`` out."""
        return rel in self.datasets or bool(covering(rel, set(self.tops)))


def parse_point_mark(value: str) -> PointMark | None:
    """A point's mark, or None when absent or unreadable."""
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tops = data.get("tops", {})
    datasets = data.get("datasets", {})
    if not isinstance(tops, dict) or not isinstance(datasets, dict):
        return None
    out = PointMark()
    for top, flags in tops.items():
        if isinstance(top, str) and flags in (BACKUPS, BOTH, SNAPSHOTS):
            out.tops[top] = flags
    for rel, props in datasets.items():
        if isinstance(rel, str) and isinstance(props, list) and len(props) == 2:
            out.datasets[rel] = (str(props[0]), str(props[1]))
    return out
