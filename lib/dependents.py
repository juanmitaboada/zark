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
What a destroy on a backup drive must take first, and in which order.

``zfs destroy -r <ds>`` walks every dependent of the tree's snapshots before
it destroys anything, clones of clones included, and refuses on the first
one outside ``<ds>/`` and ``<ds>@`` (OpenZFS 2.4.1 cmd/zfs/zfs_main.c:1399-1452,
:1880, lib/libzfs/libzfs_iter.c:528-585; 2.2.2 :1354-1406, :1834, :527-584).
``zfs rollback -r`` refuses the same way while a snapshot it would destroy
has a clone (2.4.1 :4325-4344, :4377-4389; 2.2.2 :4016-4034, :4073).

One reader, :func:`destroy_plan`, serves every typed destroy on a drive (the
orphan's, DIVERGED's and the excluded copy's): the dependents an archived
lineage of the tree may take with it, in an order ZFS accepts, or the
reasons nothing can be offered. :func:`destroy_dependents` runs that order.
Names are relative to the drive's pool root, as :class:`Dest` keys are.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Collection
from dataclasses import dataclass, field

from lib import sh
from lib.log import Log
from lib.replication import Dest, is_archived

_ARCHIVED = re.compile(r"\.archived-[^/]*")


def _under(rel: str, tops: Collection[str]) -> bool:
    return any(rel == t or rel.startswith(f"{t}/") for t in tops)


def _dataset(snapshot: str) -> str:
    return snapshot.split("@", 1)[0]


@dataclass(frozen=True)
class Dependent:
    """A dataset on the drive that ``zfs destroy -r`` of a tree refuses for:
    a clone, outside the tree, of one of its snapshots (or of one of
    another dependent's)."""

    rel: str
    origin: str  # the snapshot it is a clone of, ``rel@snap``
    archived: bool  # an archived lineage of a dataset in the tree
    depth: int  # 1: cloned from the tree; 2: from a dependent of depth 1...


@dataclass(frozen=True)
class Blocker:
    """A clone that keeps a destroy from being offered."""

    rel: str
    origin: str  # the snapshot it is a clone of, ``rel@snap``
    why: str  # "live", "inside", "cycle" or "newer"

    def text(self, pool: str) -> str:
        """Why, naming the clone and its snapshot."""
        clone, of = f"{pool}/{self.rel}", f"{pool}/{self.origin}"
        if self.why == "inside":
            return f"{clone}, inside it, is a clone of {of}, which would have to go first"
        if self.why == "newer":
            return f"{clone} is a clone of {of}, a snapshot the rollback would destroy"
        if self.why == "cycle":
            return f"{clone} is a clone of {of}, in a loop of clones no order can destroy"
        return f"{clone} is a clone of {of}, not an archived lineage of it"


@dataclass(frozen=True)
class DestroyPlan:
    """What ``zfs destroy -r <top>`` needs gone first, in order."""

    top: str
    order: list[Dependent] = field(default_factory=list)  # each before what it was cloned from
    blockers: list[Blocker] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the destroy can be offered."""
        return not self.blockers


def destroy_plan(dest: dict[str, Dest], top: str) -> DestroyPlan:
    """The dependents of ``top``'s tree: every dataset outside it whose
    origin is a snapshot in it or in another dependent, recursively, one
    entry per tree (a dependent below another goes with it). The order is
    topological over the clones: a tree goes before every tree one of its
    datasets is cloned from; among the ready ones, the deepest first, so
    every graph zark itself makes keeps that order (M9bR-1). Blockers: a
    dependent that is not an archived lineage of the tree (``live``); a
    dataset in the tree cloned from a dependent's snapshot (``inside``);
    trees cloned from each other (``cycle``)."""
    trees = _trees(dest, top)
    blockers = [Blocker(x.rel, x.origin, "live") for x in trees.values() if not x.archived]
    edges: dict[str, list[tuple[str, str]]] = {t: [] for t in trees}
    for rel, d in sorted(dest.items()):
        of = _tree_of(trees, _dataset(d.clone_of)) if d.clone_of else ""
        if not of:
            continue
        if _under(rel, (top,)):
            blockers.append(Blocker(rel, d.clone_of, "inside"))
        elif (mine := _tree_of(trees, rel)) and mine != of:
            edges[mine].append((rel, d.clone_of))
    order, stuck = _topological(trees, edges)
    blockers += [Blocker(rel, o, "cycle") for t in stuck for rel, o in edges[t]]
    return DestroyPlan(top, order, blockers)


def _trees(dest: dict[str, Dest], top: str) -> dict[str, Dependent]:
    """What ``zfs destroy -r`` of ``top`` refuses for, as libzfs walks it,
    one entry per tree."""
    found: dict[str, Dependent] = {}
    gone = {top}
    depth = 0
    while True:
        depth += 1
        new = {
            rel: d.clone_of
            for rel, d in dest.items()
            if d.clone_of and not _under(rel, gone) and _under(_dataset(d.clone_of), gone)
        }
        if not new:
            break
        for rel, origin in new.items():
            plain = _ARCHIVED.sub("", rel)
            found[rel] = Dependent(rel, origin, is_archived(rel) and _under(plain, (top,)), depth)
        gone |= set(new)
    return {x.rel: x for x in found.values() if not _under(x.rel, set(found) - {x.rel})}


def _tree_of(trees: Collection[str], rel: str) -> str:
    return next((t for t in trees if _under(rel, (t,))), "")


def _topological(
    trees: dict[str, Dependent], edges: dict[str, list[tuple[str, str]]]
) -> tuple[list[Dependent], list[str]]:
    """The trees in an order ``zfs destroy -r`` accepts, and those that
    cannot be ordered (cloned from each other)."""
    order: list[Dependent] = []
    left = dict(trees)
    while left:
        # A tree is ready when no tree left holds a clone of its snapshots.
        needed = {_tree_of(trees, _dataset(o)) for t in left for _, o in edges[t]}
        ready = sorted(
            (x for t, x in left.items() if t not in needed), key=lambda x: (-x.depth, x.rel)
        )
        if not ready:
            return order, sorted(left)
        order.append(ready[0])
        del left[ready[0].rel]
    return order, []


def rollback_blockers(dest: dict[str, Dest], rel: str, newer: list[str]) -> list[Blocker]:
    """Clones of the snapshots ``zfs rollback -r`` would destroy (``newer``,
    ``@name`` each): while one exists, the rollback is refused (M11-5)."""
    taken = {f"{rel}{n}" for n in newer}
    return [Blocker(r, d.clone_of, "newer") for r, d in sorted(dest.items()) if d.clone_of in taken]


def destroy_dependents(pool: str, plan: DestroyPlan, log: Log) -> tuple[list[str], bool]:
    """``zfs destroy -r`` of each dependent in ``plan.order``, by its exact
    name and logged; the first failure stops there. Returns what went and
    whether every one did. Runs nothing for a plan with a blocker."""
    if not plan.ok:
        log.error(f"Could not destroy {pool}/{plan.top}: {plan.blockers[0].text(pool)}")
        return [], False
    gone: list[str] = []
    for dep in plan.order:
        name = f"{pool}/{dep.rel}"
        r = sh.run(f"zfs destroy -r {shlex.quote(name)}", log=log)
        if not r.ok:
            log.error(f"Could not destroy {name} on the backup: {r.stderr.strip()}")
            return gone, False
        log.ok(f"Destroyed {name} on the backup (cloned from {dep.origin})")
        gone.append(dep.rel)
    return gone, True


def takes_lines(plan: DestroyPlan) -> list[str]:
    """The heading line for what a destroy takes beyond the tree, when it takes any."""
    return ["    and what was cloned from its snapshots:"] if plan.order else []


def also(plan: DestroyPlan) -> str:
    """The typed sentence's addition for the archived lineages a destroy takes."""
    n = len(plan.order)
    return f", and {n} archived lineage(s) cloned from it" if n else ""
