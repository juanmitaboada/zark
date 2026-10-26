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
Live pool and dataset figures for ``zark monitor`` (M8-4, M8-5).

Two read-only listings, run as any user, never through a shell:

1. ``zpool list -Hp -o name,guid,size,alloc,free,capacity,health``: every
   imported pool;
2. ``zfs list -Hp -t filesystem,volume -r -o name,type,used,avail,...``
   over the source pools that listing 1 found: their roots' ``used`` and
   ``avail`` and every dataset (R2).

These are the sources the status files' Space object and ``datasets`` name,
read in one pass. :func:`run_command` refuses any other command line, so the
monitor cannot import, export or change a pool. The figures come back in the
contract's shapes (``used_bytes``, ``pool_free_bytes``, ...), so a reader
treats live and file figures alike.
"""

import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

SOURCE_POOLS = ("rpool", "bpool")
TIMEOUT_S = 30
ZPOOL_FIELDS = ("name", "guid", "size", "alloc", "free", "capacity", "health")
ZFS_FIELDS = (
    "name",
    "type",
    "used",
    "avail",
    "usedbydataset",
    "usedbysnapshots",
    "usedbychildren",
    "usedbyrefreservation",
)
_ZFS_HEAD = ("zfs", "list", "-Hp", "-t", "filesystem,volume", "-r", "-o", ",".join(ZFS_FIELDS))
_ZPOOL = ("zpool", "list", "-Hp", "-o", ",".join(ZPOOL_FIELDS))

# (return code, stdout, stderr) of one listing.
Runner = Callable[[Sequence[str], int], tuple[int, str, str]]


@dataclass
class LiveFigures:  # pylint: disable=too-few-public-methods
    """One pass of both listings.

    ``pools``: every imported pool by name, with ``guid`` and ``health``;
    ``spaces``: a Space object per source pool whose figures could be read;
    ``datasets``: the source pools' datasets, sorted by name; ``error``: the
    first line of the first failure, empty when both listings worked.
    """

    measured_at: datetime
    pools: dict[str, dict[str, Any]] = field(default_factory=dict)
    spaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    datasets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    error: str = ""


def zfs_head() -> list[str]:
    """Listing 2 without its pools (the self-test runs it on its bench)."""
    return list(_ZFS_HEAD)


def zpool_argv() -> list[str]:
    """Listing 1."""
    return list(_ZPOOL)


def zfs_argv(pools: Sequence[str]) -> list[str]:
    """Listing 2 over ``pools``, which must be source pools."""
    if not pools or len(set(pools)) != len(pools) or any(p not in SOURCE_POOLS for p in pools):
        raise ValueError(f"not source pools: {list(pools)!r}")
    return [*_ZFS_HEAD, *pools]


def allowed(argv: Sequence[str]) -> bool:
    """Whether ``argv`` is one of the two listings."""
    if list(argv) == list(_ZPOOL):
        return True
    head, tail = tuple(argv[: len(_ZFS_HEAD)]), list(argv[len(_ZFS_HEAD) :])
    try:
        return head == _ZFS_HEAD and zfs_argv(tail) == list(argv)
    except ValueError:
        return False


def run_command(argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
    """Run one of the two listings, with no stdin and C messages."""
    if not allowed(argv):
        raise ValueError(f"zark monitor runs only its two listings, not {list(argv)!r}")
    env = {**os.environ, "LC_ALL": "C"}
    try:
        r = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} list did not answer within {timeout} s"
    except OSError as e:
        return 126, "", f"{argv[0]}: {e.strerror or e}"
    return r.returncode, r.stdout, r.stderr


def _number(text: str) -> int | None:
    return int(text) if text.isdigit() else None


def parse_zpool(text: str) -> dict[str, dict[str, Any]]:
    """Listing 1's rows by pool name; a figure the pool cannot give (``-``)
    is None."""
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) != len(ZPOOL_FIELDS) or not cols[0]:
            continue
        name, guid, size, alloc, free, cap, health = cols
        out[name] = {
            "name": name,
            "guid": guid,
            "health": health,
            "pool_size_bytes": _number(size),
            "pool_alloc_bytes": _number(alloc),
            "pool_free_bytes": _number(free),
            "capacity_pct": _number(cap),
        }
    return out


def parse_zfs(text: str) -> list[dict[str, Any]]:
    """Listing 2's rows, with the contract's field names (``avail`` kept as
    ``available_bytes`` for a root's Space object)."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) != len(ZFS_FIELDS) or not cols[0]:
            continue
        numbers = [_number(c) for c in cols[2:]]
        if any(n is None for n in numbers):
            continue
        used, avail, by_ds, by_snap, by_child, by_refres = (n or 0 for n in numbers)
        out.append(
            {
                "name": cols[0],
                "type": cols[1],
                "used_bytes": used,
                "available_bytes": avail,
                "usedbydataset_bytes": by_ds,
                "usedbysnapshots_bytes": by_snap,
                "usedbychildren_bytes": by_child,
                "usedbyrefreservation_bytes": by_refres,
            }
        )
    return sorted(out, key=lambda d: str(d["name"]))


def _first_line(rc: int, err: str, what: str) -> str:
    line = next((li.strip() for li in err.splitlines() if li.strip()), "")
    return line or f"{what} ended with status {rc}"


def read_live(runner: Runner = run_command, now: datetime | None = None) -> LiveFigures:
    """Both listings, one pass."""
    live = LiveFigures(measured_at=now or datetime.now(UTC))
    stamp = live.measured_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    rc, out, err = runner(zpool_argv(), TIMEOUT_S)
    if rc != 0:
        live.error = _first_line(rc, err, "zpool list")
        return live
    live.pools = parse_zpool(out)
    sources = [p for p in SOURCE_POOLS if p in live.pools]
    if not sources:
        return live
    rc, out, err = runner(zfs_argv(sources), TIMEOUT_S)
    if rc != 0:
        live.error = _first_line(rc, err, "zfs list")
        return live
    rows = parse_zfs(out)
    for pool in sources:
        mine = [d for d in rows if d["name"] == pool or str(d["name"]).startswith(f"{pool}/")]
        root = next((d for d in mine if d["name"] == pool), None)
        zp = live.pools[pool]
        figures = ("pool_size_bytes", "pool_alloc_bytes", "pool_free_bytes", "capacity_pct")
        if root is None or any(zp[k] is None for k in figures):
            continue
        live.spaces[pool] = {
            "used_bytes": root["used_bytes"],
            "available_bytes": root["available_bytes"],
            **{k: zp[k] for k in figures},
            "measured_at": stamp,
        }
        live.datasets[pool] = [{k: v for k, v in d.items() if k != "available_bytes"} for d in mine]
    return live
