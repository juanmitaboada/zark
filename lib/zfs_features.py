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
OpenZFS pool features: GUID, short name and the first release that has each.

From OpenZFS 2.4.1: the GUIDs and flags from
module/zcommon/zfeature_common.c:374-806; the release is the first of
cmd/zpool/compatibility.d/openzfs-2.0-linux, openzfs-2.1-linux, openzfs-2.2,
openzfs-2.3 and openzfs-2.4 that lists the feature (2.0 stands for "2.0 or
older"). recover names with it what a drive needs that the live's ZFS lacks
(M4s-11): ``zpool import`` names an unsupported feature by its GUID
(lib/libzfs/libzfs_pool.c:2120-2140).
"""

import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lib.config import zark_command
from lib.sh import run

if TYPE_CHECKING:
    from lib.log import Log


@dataclass(frozen=True)
class Feature:
    """One pool feature."""

    name: str
    guid: str
    since: tuple[int, int]


FEATURES: tuple[Feature, ...] = (
    Feature("async_destroy", "com.delphix:async_destroy", (2, 0)),
    Feature("empty_bpobj", "com.delphix:empty_bpobj", (2, 0)),
    Feature("lz4_compress", "org.illumos:lz4_compress", (2, 0)),
    Feature("multi_vdev_crash_dump", "com.joyent:multi_vdev_crash_dump", (2, 0)),
    Feature("spacemap_histogram", "com.delphix:spacemap_histogram", (2, 0)),
    Feature("enabled_txg", "com.delphix:enabled_txg", (2, 0)),
    Feature("hole_birth", "com.delphix:hole_birth", (2, 0)),
    Feature("zpool_checkpoint", "com.delphix:zpool_checkpoint", (2, 0)),
    Feature("spacemap_v2", "com.delphix:spacemap_v2", (2, 0)),
    Feature("extensible_dataset", "com.delphix:extensible_dataset", (2, 0)),
    Feature("bookmarks", "com.delphix:bookmarks", (2, 0)),
    Feature("filesystem_limits", "com.joyent:filesystem_limits", (2, 0)),
    Feature("embedded_data", "com.delphix:embedded_data", (2, 0)),
    Feature("livelist", "com.delphix:livelist", (2, 0)),
    Feature("log_spacemap", "com.delphix:log_spacemap", (2, 0)),
    Feature("large_blocks", "org.open-zfs:large_blocks", (2, 0)),
    Feature("large_dnode", "org.zfsonlinux:large_dnode", (2, 0)),
    Feature("sha512", "org.illumos:sha512", (2, 0)),
    Feature("skein", "org.illumos:skein", (2, 0)),
    Feature("edonr", "org.illumos:edonr", (2, 0)),
    Feature("redaction_bookmarks", "com.delphix:redaction_bookmarks", (2, 0)),
    Feature("redacted_datasets", "com.delphix:redacted_datasets", (2, 0)),
    Feature("bookmark_written", "com.delphix:bookmark_written", (2, 0)),
    Feature("device_removal", "com.delphix:device_removal", (2, 0)),
    Feature("obsolete_counts", "com.delphix:obsolete_counts", (2, 0)),
    Feature("userobj_accounting", "org.zfsonlinux:userobj_accounting", (2, 0)),
    Feature("bookmark_v2", "com.datto:bookmark_v2", (2, 0)),
    Feature("encryption", "com.datto:encryption", (2, 0)),
    Feature("project_quota", "org.zfsonlinux:project_quota", (2, 0)),
    Feature("allocation_classes", "org.zfsonlinux:allocation_classes", (2, 0)),
    Feature("resilver_defer", "com.datto:resilver_defer", (2, 0)),
    Feature("device_rebuild", "org.openzfs:device_rebuild", (2, 0)),
    Feature("zstd_compress", "org.freebsd:zstd_compress", (2, 0)),
    Feature("draid", "org.openzfs:draid", (2, 1)),
    Feature("zilsaxattr", "org.openzfs:zilsaxattr", (2, 2)),
    Feature("head_errlog", "com.delphix:head_errlog", (2, 2)),
    Feature("blake3", "org.openzfs:blake3", (2, 2)),
    Feature("block_cloning", "com.fudosecurity:block_cloning", (2, 2)),
    Feature("block_cloning_endian", "com.truenas:block_cloning_endian", (2, 4)),
    Feature("vdev_zaps_v2", "com.klarasystems:vdev_zaps_v2", (2, 2)),
    Feature("redaction_list_spill", "com.delphix:redaction_list_spill", (2, 3)),
    Feature("raidz_expansion", "org.openzfs:raidz_expansion", (2, 3)),
    Feature("fast_dedup", "com.klarasystems:fast_dedup", (2, 3)),
    Feature("longname", "org.zfsonlinux:longname", (2, 3)),
    Feature("large_microzap", "com.klarasystems:large_microzap", (2, 3)),
    Feature("dynamic_gang_header", "com.klarasystems:dynamic_gang_header", (2, 4)),
    Feature("physical_rewrite", "com.truenas:physical_rewrite", (2, 4)),
)

BY_GUID = {f.guid: f for f in FEATURES}

# cmd/zpool/compatibility.d/openzfs-2.2: what a drive prepared with
# compatibility=openzfs-2.2 may have enabled (M4s-10).
COMPAT_2_2: tuple[str, ...] = tuple(sorted(f.name for f in FEATURES if f.since <= (2, 2)))

# The two zark streams may need that openzfs-2.2 lacks: a dataset in origin with
# them active sends streams a 2.2 drive refuses (module/zfs/dmu_recv.c:599, :614;
# dmu_send.c:2018-2030). lib.zfs.drive_compatibility reads it.
BEYOND_2_2_STREAM = ("longname", "large_microzap")

_UNSUP = "not supported on this system:"


def release_tuple(version: str) -> tuple[int, int]:
    """``(major, minor)`` of an OpenZFS version (``2.4.1-1ubuntu5.1`` gives
    ``(2, 4)``); ``(0, 0)`` when it cannot be read."""
    m = re.match(r"(?:zfs-(?:kmod-)?)?(\d+)\.(\d+)", version.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def release_text(since: tuple[int, int]) -> str:
    """``OpenZFS 2.3`` (``2.0`` reads "2.0 or older")."""
    return "OpenZFS 2.0" if since == (2, 0) else f"OpenZFS {since[0]}.{since[1]}"


def parse_upgrade_v(text: str) -> list[str]:
    """Short names listed by ``zpool upgrade -v``
    (cmd/zpool/zpool_main.c:11835-11880: ``%-37s`` name lines, each followed
    by an indented description, until the legacy versions)."""
    names: list[str] = []
    started = False
    for line in text.splitlines():
        if line.startswith("FEAT DESCRIPTION"):
            started = True
            continue
        if not started or not line.strip() or line.startswith("-"):
            continue
        if line.startswith("The following legacy versions"):
            break
        if not line[0].isspace():
            names.append(line.split()[0])
    return names


@dataclass(frozen=True)
class Unsupported:
    """What ``zpool import`` (listing) says this ZFS cannot use on a pool."""

    pool: str
    read: bool  # True: no import at all; False: read-only only
    guids: tuple[str, ...]


def parse_unsupported(listing: str) -> list[Unsupported]:
    """Pools in a ``zpool import -d`` listing whose status names features this
    ZFS does not support (cmd/zpool/zpool_main.c:3492-3511; the features are
    tab-indented ``<guid> (<description>)`` lines,
    lib/libzfs/libzfs_pool.c:2120-2140)."""
    out: list[Unsupported] = []
    pool, read, collecting = "", False, False
    guids: list[str] = []
    for line in listing.splitlines() + ["  pool: "]:
        stripped = line.strip()
        if stripped.startswith("pool:"):
            if pool and guids:
                out.append(Unsupported(pool, read, tuple(guids)))
            pool, read, guids, collecting = stripped[5:].strip(), False, [], False
        elif _UNSUP in line:
            collecting = True
            read = "The pool uses the following feature(s)" in line
        elif collecting and line.startswith("\t") and ":" in stripped.split(" ")[0]:
            guids.append(stripped.split(" ")[0])
        elif collecting and not line.startswith("\t") and stripped:
            collecting = False
    return out


def describe(guid: str) -> str:
    """``<name> (OpenZFS <x.y>)`` for a GUID, or the GUID as a feature newer
    than this table."""
    f = BY_GUID.get(guid)
    if f is None:
        return f"{guid} (a feature newer than OpenZFS 2.4)"
    return f"{f.name} ({release_text(f.since)})"


def needed_release(guids: tuple[str, ...]) -> str:
    """The oldest OpenZFS that has every one of ``guids``, as text."""
    known = [BY_GUID[g].since for g in guids if g in BY_GUID]
    if len(known) < len(guids):
        return "a release newer than OpenZFS 2.4"
    return release_text(max(known)) if known else "a newer OpenZFS"


def check_drive(
    pool: str,
    device: str,
    log: "Log",
    command: str = "recover",
    writing: bool = False,
) -> None:
    """Before ``command`` imports a drive: stop when this ZFS cannot use it.

    ``zpool import -d <device>`` (listing only, nothing imported) says
    which features of the pool this system lacks: for reading (no import is
    possible, a FATAL naming each feature, the release that has it and this
    system's OpenZFS) or only for writing: one line when ``command`` imports
    it read-only, a FATAL that says to import it read-only when ``writing``
    (zark mount's read-write mode, M4c-23). A listing that fails or names
    nothing leaves the command as it was.
    """
    r = run(f"zpool import -d {shlex.quote(device)}", log=log)
    found = [u for u in parse_unsupported(r.stdout) if u.pool == pool]
    if not found:
        return
    u = found[0]
    names = [describe(g) for g in u.guids]
    if not u.read and not writing:
        log.info(
            f"{pool} uses feature(s) this system can only read, not write "
            + f"({', '.join(names)}): {command} imports it read-only"
        )
        return
    here = run("zfs version").lines
    if not u.read:
        log.fatal(
            f"This system's ZFS can only read {pool}, not write it",
            causes=[
                f"{pool} uses: {', '.join(names)}",
                f"This system runs {here[0] if here else 'an unknown OpenZFS'}",
            ],
            solutions=[
                f"Mount it read-only: {zark_command()} {command.removeprefix('zark ')}, "
                + "then choose Read-only",
                "Nothing was imported or changed",
            ],
        )
    log.fatal(
        f"This system's ZFS cannot read {pool}",
        causes=[
            f"{pool} uses: {', '.join(names)}",
            f"This system runs {here[0] if here else 'an unknown OpenZFS'}",
        ],
        solutions=[
            f"Boot a live session with {needed_release(u.guids)} or later "
            + f"(Ubuntu 26.04 or newer for OpenZFS 2.4) and run {command} from it",
            "Nothing was imported or changed",
        ],
    )
