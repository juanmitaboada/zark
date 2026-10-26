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
Reads of origin and of a backup drive, and the checks before a run.

``read_origin`` and ``read_dest`` list the datasets, snapshots and
bookmarks a run plans from (strictly when asked: :class:`ReadError`);
``require_bookmark_v2`` and ``require_boot_pool`` refuse a run that could
not anchor or would leave ``/boot`` out.
"""

import re
import shlex

from lib import sh
from lib.config import zark_command
from lib.identity import disks_under
from lib.log import Log
from lib.replication import EXCLUDED_KEPT_PROP, Dest, Origin, Ref


def _q(word: str) -> str:
    return shlex.quote(word)


def _parse_refs(lines: list[str], strip: str) -> dict[str, tuple[list[Ref], list[Ref]]]:
    """``name guid createtxg creation`` lines → {rel: (snapshots, bookmarks)}."""
    out: dict[str, tuple[list[Ref], list[Ref]]] = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) < 4:
            continue
        full = fields[0]
        sep = "@" if "@" in full else "#" if "#" in full else ""
        if not sep or not full.startswith(strip):
            continue
        ds, name = full[len(strip) :].split(sep, 1)
        try:
            ref = Ref(name, fields[1], int(fields[2]), int(fields[3]))
        except ValueError:
            continue
        snaps, bms = out.setdefault(ds, ([], []))
        (snaps if sep == "@" else bms).append(ref)
    for snaps, bms in out.values():
        snaps.sort(key=lambda r: r.createtxg)
        bms.sort(key=lambda r: r.createtxg)
    return out


def source_pools() -> list[str]:
    """Origin pools zark replicates that exist here: rpool, and bpool if present."""
    return [p for p in ("rpool", "bpool") if sh.run(f"zpool list -H -o name {p}").ok]


class ReadError(Exception):
    """A ``zfs list`` needed for a safe decision failed."""


def checked(r: sh.RunResult, strict: bool) -> sh.RunResult:
    """``r``, or ReadError when ``strict`` and the read failed."""
    if strict and not r.ok:
        raise ReadError(f"could not read: {r.command or 'zfs list'}")
    return r


def read_origin(pools: list[str], strict: bool = False) -> dict[str, Origin]:
    """Origin datasets with kind, mount properties, snapshots and bookmarks.

    ``strict`` raises :class:`ReadError` when a listing fails instead of
    returning what could be read.
    """
    origin: dict[str, Origin] = {}
    for pool in pools:
        r = sh.run(
            "zfs list -Hp -t filesystem,volume "
            + f"-o name,type,canmount,mountpoint,referenced,origin -r {pool}",
        )
        r = checked(r, strict)
        for line in r.lines if r.ok else []:
            f = line.split("\t")
            if len(f) >= 6:
                ref = int(f[4]) if f[4].isdigit() else 0
                clone_of = "" if f[5] in ("-", "") else f[5]
                origin[f[0]] = Origin(f[0], f[1], f[2], f[3], ref, clone_of=clone_of)
        r = sh.run(
            f"zfs list -Hp -t snapshot,bookmark -o name,guid,createtxg,creation -r {pool}",
        )
        r = checked(r, strict)
        for rel, (snaps, bms) in _parse_refs(r.lines if r.ok else [], "").items():
            if rel in origin:
                origin[rel].snaps, origin[rel].bookmarks = snaps, bms
    return origin


def read_dest(pool: str, strict: bool = False) -> dict[str, Dest]:
    """Destination datasets under ``<pool>/rpool`` and ``<pool>/bpool``, by origin name.

    ``strict`` raises :class:`ReadError` when the pool does not list (an
    exported pool is not an empty one) or a listing of an existing root fails;
    a missing ``<pool>/rpool`` or ``<pool>/bpool`` is accepted only then.
    """
    dest: dict[str, Dest] = {}
    strip = f"{pool}/"
    children: list[str] | None = None
    if strict:
        # One listing: a root is missing only when it is not in it, never
        # because its own probe failed for another reason (M2V-7 d).
        children = checked(sh.run(f"zfs list -H -o name -d 1 {_q(pool)}"), strict).lines
    for root in (f"{pool}/rpool", f"{pool}/bpool"):
        if children is not None and root not in children:
            continue
        if children is None and not sh.run(f"zfs list -H -o name {root}").ok:
            continue
        r = sh.run(
            "zfs list -Hp -t filesystem,volume "
            + f"-o name,type,receive_resume_token,{ORPHAN},used,written,{EXCLUDED_KEPT_PROP} "
            + f"-r {root}",
        )
        r = checked(r, strict)
        for line in r.lines if r.ok else []:
            f = line.split("\t")
            if len(f) >= 6 and f[0].startswith(strip):
                rel = f[0][len(strip) :]
                dest[rel] = _dest_row(rel, f)
        r = checked(
            sh.run(f"zfs list -Hp -t snapshot -o name,guid,createtxg,creation -r {root}"),
            strict,
        )
        for rel, (snaps, _) in _parse_refs(r.lines if r.ok else [], strip).items():
            if rel in dest:
                dest[rel].snaps = snaps
    return dest


def read_dest_used(pool: str) -> dict[str, int]:
    """``rel@name`` → ``used`` of every snapshot under ``<pool>/rpool`` and
    ``<pool>/bpool`` (what each holds alone), read strictly as
    :func:`read_dest` reads them: :class:`ReadError` when a listing of a
    root that exists fails (M9a: a point's ``used_bytes`` is never a sum
    over part of its snapshots)."""
    out: dict[str, int] = {}
    strip = f"{pool}/"
    children = checked(sh.run(f"zfs list -H -o name -d 1 {_q(pool)}"), True).lines
    for root in (f"{pool}/rpool", f"{pool}/bpool"):
        if root not in children:
            continue
        r = checked(sh.run(f"zfs list -Hp -t snapshot -o name,used -r {_q(root)}"), True)
        for line in r.lines:
            f = line.split("\t")
            if len(f) == 2 and f[0].startswith(strip) and "@" in f[0] and f[1].isdigit():
                out[f[0][len(strip) :]] = int(f[1])
    return out


ORPHAN = "org.zark:orphan"


def _dest_row(rel: str, f: list[str]) -> Dest:
    """One ``name type token orphan used written excluded-kept`` row."""

    def text(i: int) -> str:
        return "" if len(f) <= i or f[i] in ("-", "") else f[i]

    def number(i: int) -> int:
        return int(f[i]) if len(f) > i and f[i].isdigit() else 0

    return Dest(
        rel,
        kind=f[1],
        token=text(2),
        orphan=text(3),
        used=number(4),
        written=number(5),
        excluded_kept=text(6),
    )


def bookmark_feature(pool: str = "rpool") -> str:
    """``feature@bookmark_v2`` state on ``pool`` (enabled/active/disabled/"")."""
    r = sh.run(f"zpool get -H -o value feature@bookmark_v2 {pool}")
    return r.output.strip() if r.ok else ""


def require_bookmark_v2(log: Log) -> None:
    """Refuse without bookmark_v2 on rpool (M2 decision 4a; `zark setup` enables it)."""
    state = bookmark_feature("rpool")
    if state not in ("enabled", "active"):
        log.fatal(
            f"rpool feature@bookmark_v2 is {state or 'unknown'}",
            causes=["zark's backup anchors are rpool bookmarks, which need this feature"],
            solutions=[f"Run: {zark_command()} setup  (it explains the change and asks first)"],
        )


def _importable_bpools() -> list[tuple[str, list[str]]]:
    """(id, vdev paths) of every pool named bpool that ``zpool import`` lists.

    ``zpool import`` without a pool name only lists what could be imported
    (cmd/zpool/zpool_main.c:3986-4017); ZPOOL_VDEV_NAME_PATH=1 makes it print
    each vdev by its full path (lib/libzfs/libzfs_pool.c:4521).
    """
    r = sh.run("env LC_ALL=C ZPOOL_VDEV_NAME_PATH=1 zpool import")
    found: list[tuple[str, list[str]]] = []
    for block in re.split(r"^\s*pool: ", r.stdout, flags=re.MULTILINE)[1:]:
        lines = block.splitlines()
        if not lines or lines[0].strip() != "bpool":
            continue
        m = re.search(r"^\s*id: (\d+)", block, re.MULTILINE)
        devices = [w for ln in lines[1:] for w in ln.split()[:1] if w.startswith("/dev/")]
        found.append((m.group(1) if m else "?", devices))
    return found


def _rpool_disks() -> set[str]:
    """Whole disks under the imported rpool's vdevs (empty when it is not imported)."""
    r = sh.run("zpool list -vHP rpool")
    disks: set[str] = set()
    for line in r.lines[1:] if r.ok else []:
        path = line.strip().split("\t")[0].split(" ")[0]
        if path.startswith("/dev/"):
            disks |= disks_under(path)
    return disks


def require_boot_pool(log: Log) -> None:
    """Refuse while a pool named bpool can be imported here but is not (M2R-4).

    With bpool not imported ``/boot`` is not mounted either, so the pool's
    absence from ``zpool list`` is all there is to see; a run would then
    leave the kernels and initrds out and still call itself complete.

    Each importable bpool is named with its id and devices (M2V-2, M4a-6):
    one with a disk in common with rpool is this system's and is imported by
    id; any other belongs to another disk and must not be imported, since it
    would mount over ``/boot``; one whose devices cannot be read is named as
    such, with no advice about its disk. The advice never includes exporting
    bpool: on a running system that cannot succeed and leaves ``/boot``
    unmounted (P6).
    """
    if sh.run("zpool list -H -o name bpool").ok:
        return
    candidates = _importable_bpools()
    if not candidates:
        return
    mine = _rpool_disks()
    own: list[str] = []
    unknown = False
    causes = ["Without this system's bpool the backup would leave out /boot"]
    solutions: list[str] = []
    for pool_id, devices in candidates:
        disks = set().union(*(disks_under(d) for d in devices)) if devices else set()
        where = ", ".join(devices)
        if not disks:
            unknown = True
            causes.append(f"bpool id {pool_id}: no readable device; cannot tell whose it is")
            solutions.append("Look at it without importing: sudo zpool import (it only lists)")
        elif disks & mine:
            own.append(pool_id)
            causes.append(f"bpool id {pool_id} on {where}: on rpool's disk, not imported")
        else:
            causes.append(f"bpool id {pool_id} on {where}: another disk's")
            solutions.append(
                f"Disconnect the disk holding bpool id {pool_id}; do not import it: "
                + "it would mount over /boot"
            )
    if len(own) == 1:
        solutions.insert(
            0, f"Import this system's bpool, then run again: sudo zpool import {own[0]}"
        )
    elif own:
        solutions.insert(0, "More than one bpool on rpool's disk: import the right one by id")
    elif not unknown:
        causes.append("rpool's disk holds no bpool: this system has none of its own")
        solutions.append("zark needs Ubuntu's ZFS layout, with bpool on rpool's disk")
    log.fatal("bpool is not imported", causes=causes, solutions=solutions)
