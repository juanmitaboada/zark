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
What `zark status` reports, the MOTD shows and the timer notifies
(M3 decisions 12 and 13):

  * CAP of rpool and bpool: a warning from 80 %, critical from 90 %; MOTD
    and desktop notification (the latter at most once a day per pool and
    level);
  * RPO, in the MOTD only: days since every registered drive's last
    backup, highlighted when the newest backup of all is older than 7 days;
  * for a drive with no anchor in origin, what it still shares with the
    source and what its next backup does (M3 decision 5e, hallazgo 21), or
    one line saying the source pools could not be read;
  * legacy residues in origin that `zark prune` can remove.

The timer writes the MOTD text to :data:`MOTD_FILE`; the MOTD script only
prints that file, so nothing runs at login. The same read gives
``/var/lib/zark/status.json`` (:mod:`lib.status_file`): its notices are the
MOTD's lines, structured (M7-13 13a).
"""

import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lib import anchors, certificates, sh, statusdirs, survey, upgrade_risk
from lib.boot_layout import boot_mount
from lib.config import Config, now_utc_iso, zark_command
from lib.drives import drive_staleness_days, no_anchor_lines
from lib.log import Log
from lib.origin_prune import inventory
from lib.zfs import DatasetUsage, Space, read_datasets, read_space

CAP_WARNING = 80
CAP_CRITICAL = 90
RPO_WARNING_DAYS = 7
RUN_DIR = Path("/run/zark")
MOTD_FILE = RUN_DIR / "status.motd"
NOTIFIED_FILE = RUN_DIR / "notified"


def cap_level(pct: int) -> str:
    """Level of a pool's capacity in percent: "critical", "warning" or ""."""
    if pct >= CAP_CRITICAL:
        return "critical"
    if pct >= CAP_WARNING:
        return "warning"
    return ""


@dataclass(frozen=True)
class BootSpace:  # pylint: disable=too-many-instance-attributes
    """An ext4 ``/boot``'s space (M12-6 6a), from ``statvfs``."""

    mountpoint: str
    fstype: str
    source: str
    used_bytes: int
    available_bytes: int
    size_bytes: int
    capacity_pct: int
    measured_at: str

    def to_json(self) -> dict[str, object]:
        """status.json's ``boot`` object, its ``level`` aside."""
        return {
            "mountpoint": self.mountpoint,
            "fstype": self.fstype,
            "source": self.source,
            "space": {
                "used_bytes": self.used_bytes,
                "available_bytes": self.available_bytes,
                "size_bytes": self.size_bytes,
                "capacity_pct": self.capacity_pct,
                "measured_at": self.measured_at,
            },
        }


def read_boot_space(path: str = "/boot") -> BootSpace | None:
    """``/boot``'s space when it is an ext4 mount of its own; None otherwise
    (a bpool system: bpool's space is a source pool). The percentage is
    df's: used over used plus what is available to users, rounded up."""
    mount = boot_mount()
    if mount is None or mount.fstype != "ext4":
        return None
    try:
        v = os.statvfs(path)
    except OSError:
        return None
    used = (v.f_blocks - v.f_bfree) * v.f_frsize
    avail = v.f_bavail * v.f_frsize
    pct = -(-used * 100 // (used + avail)) if used + avail else 0
    return BootSpace(
        path, mount.fstype, mount.source, used, avail, v.f_blocks * v.f_frsize, pct, now_utc_iso()
    )


@dataclass
class Status:  # pylint: disable=too-many-instance-attributes
    """Everything the report, the MOTD and the notifications say."""

    caps: dict[str, int] = field(default_factory=dict)  # pool → capacity %
    ages: dict[str, int | None] = field(default_factory=dict)  # drive → days since its backup
    shared: list[str] = field(default_factory=list)  # hallazgo 21 lines
    residues: int = 0
    read_error: str = ""  # origin could not be read: nothing is said about anchors (M3R-2)
    selftest: list[str] = field(default_factory=list)  # the subscribed self-test lines (M4s-15)
    shared_drives: list[str] = field(default_factory=list)  # the drive of each `shared` line
    spaces: dict[str, Space | None] = field(default_factory=dict)  # pool → its Space (R1)
    datasets: dict[str, list[DatasetUsage] | None] = field(default_factory=dict)  # pool → R2
    boot: BootSpace | None = None  # an ext4 /boot (M12-6 6a)
    upgrade_risk: str = ""  # why the 24.04 to 26.04 upgrade would stop the mounts (M12P.3)


@dataclass(frozen=True)
class Notice:
    """One MOTD line, with the level and code status.json gives it."""

    level: str  # "info", "warning" or "critical"
    code: str
    subject: str | None
    text: str  # without colour codes and without the "zark:" prefix


def gather(cfg: Config, log: Log) -> Status:
    """Read the pools, the registry and origin's anchors."""
    st = Status()
    st.selftest = certificates.warnings(log)
    pools = survey.source_pools()
    for pool in pools:
        space = st.spaces[pool] = read_space(pool)
        st.datasets[pool] = read_datasets(pool) if space else None
        if space:
            st.caps[pool] = space.capacity_pct
            continue
        r = sh.run(f"zpool list -Hp -o capacity {pool}")
        if r.ok and r.output.strip().isdigit():
            st.caps[pool] = int(r.output.strip())
    if "bpool" not in pools:
        st.boot = read_boot_space()
    st.upgrade_risk = upgrade_risk.exposed()
    st.ages = {name: drive_staleness_days(info) for name, info in sorted(cfg.known_drives.items())}
    if "rpool" not in pools:
        return st
    try:
        origin = survey.read_origin(pools, strict=True)
    except survey.ReadError as e:
        # A failed listing is not an empty origin: every drive would read as
        # unanchored and the MOTD would promise full resends (M3R-2).
        st.read_error = f"could not read the source pools: {e}"
        return st
    anchored = anchors.anchored_disks(origin)
    unanchored = [info for info in cfg.known_drives.values() if info.guid not in anchored]
    st.shared = no_anchor_lines(unanchored, origin, log)
    names = {info.guid: name for name, info in cfg.known_drives.items()}
    st.shared_drives = [names[info.guid] for info in unanchored]
    drives = {info.guid: name for name, info in cfg.known_drives.items()}
    st.residues = len(inventory(origin, drives).residues)
    return st


def cap_lines(st: Status) -> list[tuple[str, str]]:
    """(level, text) for every pool at a warning or critical level."""
    out: list[tuple[str, str]] = []
    for pool, pct in sorted(st.caps.items()):
        level = cap_level(pct)
        if level:
            limit = CAP_CRITICAL if level == "critical" else CAP_WARNING
            out.append(
                (level, f"{pool} is {pct}% full ({level} from {limit}%): {zark_command()} prune")
            )
    return out


def boot_line(st: Status) -> tuple[str, str] | None:
    """(level, text) when an ext4 ``/boot`` is at a warning or critical
    level, with the pools' thresholds (M12-6 6a); old kernels fill it."""
    level = cap_level(st.boot.capacity_pct) if st.boot else ""
    if not st.boot or not level:
        return None
    limit = CAP_CRITICAL if level == "critical" else CAP_WARNING
    return level, (
        f"/boot is {st.boot.capacity_pct}% full ({level} from {limit}%): "
        + "remove old kernels with sudo apt autoremove --purge"
    )


def residue_line(n: int) -> str:
    """The one wording of the legacy residues in origin, for backup,
    `zark status` and the MOTD (decided 2026-10-13, M3P.8 2a)."""
    return (
        f"In origin: {n} snapshot(s) no registered drive needs "
        + "(`syncoid_*` left by zark 1.x, keystore `prepare_*`): "
        + f"{zark_command()} prune removes them"
    )


def rpo_line(st: Status) -> tuple[bool, str]:
    """(stale, text): days since every drive's last backup; stale when the
    newest backup of all is older than RPO_WARNING_DAYS."""
    if not st.ages:
        return False, ""
    parts = [
        f"{name} {'never' if age is None else f'{age} day(s) ago'}" for name, age in st.ages.items()
    ]
    known = [age for age in st.ages.values() if age is not None]
    stale = not known or min(known) > RPO_WARNING_DAYS
    return stale, "last backups: " + ", ".join(parts)


def notices(st: Status) -> list[Notice]:
    """The MOTD's lines in its order, each with its level, code and subject
    (docs/STATUS-FILES.md, "Notice entry")."""
    out = [
        Notice(level, "capacity", pool, text)
        for (level, text), pool in zip(cap_lines(st), _cap_pools(st), strict=True)
    ]
    if line := boot_line(st):
        out.append(Notice(line[0], "boot_capacity", "/boot", line[1]))
    stale, rpo = rpo_line(st)
    if rpo:
        out.append(Notice("warning" if stale else "info", "backup_age", None, rpo))
    drives: list[str | None] = [*st.shared_drives] or [None] * len(st.shared)
    out += [
        Notice("warning", "no_anchor", drive, line)
        for line, drive in zip(st.shared, drives, strict=True)
    ]
    if st.read_error:
        out.append(Notice("warning", "read_error", None, st.read_error))
    out += [
        Notice("warning", "read_error", pool, f"could not list the datasets of {pool}")
        for pool, space in st.spaces.items()
        if space is not None and st.datasets.get(pool) is None
    ]
    if st.residues:
        out.append(Notice("info", "origin_residues", None, residue_line(st.residues)))
    out += [Notice("warning", "selftest", None, line) for line in st.selftest]
    if st.upgrade_risk:
        out.append(Notice("warning", "upgrade_risk", None, upgrade_risk.WARNING))
    return out


def _cap_pools(st: Status) -> list[str]:
    """The pool of each :func:`cap_lines` line, in its order."""
    return [pool for pool, pct in sorted(st.caps.items()) if cap_level(pct)]


def motd_text(st: Status) -> str:
    """The MOTD lines, with the colours of the apt guard's MOTD."""
    yellow, red, plain = "\033[1;33m", "\033[1;31m", "\033[0m"
    color = {"critical": red, "warning": yellow}
    lines = [
        f"{color[n.level]}zark:{plain} {n.text}" if n.level in color else f"zark: {n.text}"
        for n in notices(st)
    ]
    return "\n".join(lines) + "\n" if lines else ""


def write_motd(text: str, path: Path | None = None) -> None:
    """Replace the MOTD file atomically (0644: the MOTD is shown to anyone)."""
    path = path or MOTD_FILE
    _ = statusdirs.ensure(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".status.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def due_notifications(
    st: Status,
    sent: set[str],
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """(key, text) of the CAP notifications not sent today yet; ``sent``
    holds the keys already sent (``<pool> <level> <date>``)."""
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    out: list[tuple[str, str]] = []
    for pool, pct in sorted(st.caps.items()):
        level = cap_level(pct)
        key = f"{pool} {level} {day}"
        if level and key not in sent:
            out.append((key, f"{pool} is {pct}% full - free space with: {zark_command()} prune"))
    return out
