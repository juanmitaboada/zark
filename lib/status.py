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
prints that file, so nothing runs at login.
"""

import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lib import anchors, certificates, sh, survey
from lib.config import Config
from lib.drives import drive_staleness_days, no_anchor_lines
from lib.log import Log
from lib.origin_prune import inventory

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


@dataclass
class Status:
    """Everything the report, the MOTD and the notifications say."""

    caps: dict[str, int] = field(default_factory=dict)  # pool → capacity %
    ages: dict[str, int | None] = field(default_factory=dict)  # drive → days since its backup
    shared: list[str] = field(default_factory=list)  # hallazgo 21 lines
    residues: int = 0
    read_error: str = ""  # origin could not be read: nothing is said about anchors (M3R-2)
    selftest: list[str] = field(default_factory=list)  # the subscribed self-test lines (M4s-15)


def gather(cfg: Config, log: Log) -> Status:
    """Read the pools, the registry and origin's anchors."""
    st = Status()
    st.selftest = certificates.warnings(log)
    pools = survey.source_pools()
    for pool in pools:
        r = sh.run(f"zpool list -Hp -o capacity {pool}")
        if r.ok and r.output.strip().isdigit():
            st.caps[pool] = int(r.output.strip())
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
            out.append((level, f"{pool} is {pct}% full ({level} from {limit}%): sudo zark prune"))
    return out


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


def motd_text(st: Status) -> str:
    """The MOTD lines, with the colours of the apt guard's MOTD."""
    yellow, red, plain = "\033[1;33m", "\033[1;31m", "\033[0m"
    lines: list[str] = []
    for level, text in cap_lines(st):
        lines.append(f"{red if level == 'critical' else yellow}zark:{plain} {text}")
    stale, rpo = rpo_line(st)
    if rpo:
        lines.append(f"{yellow}zark:{plain} {rpo}" if stale else f"zark: {rpo}")
    lines += [f"{yellow}zark:{plain} {line}" for line in st.shared]
    if st.read_error:
        lines.append(f"{yellow}zark:{plain} {st.read_error}")
    if st.residues:
        lines.append(
            f"zark: {st.residues} legacy snapshot(s) in origin no drive needs: sudo zark prune"
        )
    lines += [f"{yellow}zark:{plain} {line}" for line in st.selftest]
    return "\n".join(lines) + "\n" if lines else ""


def write_motd(text: str, path: Path | None = None) -> None:
    """Replace the MOTD file atomically (0644: the MOTD is shown to anyone)."""
    path = path or MOTD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
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
            out.append((key, f"{pool} is {pct}% full - free space with: sudo zark prune"))
    return out


def refresh_quietly(cfg: Config) -> None:
    """:func:`refresh` at the end of a command: nothing to zark.log, and a
    failure never turns a finished command into a failed one."""
    try:
        _ = refresh(cfg, Log(os.devnull))
    except (OSError, ValueError, survey.ReadError) as e:
        Log().warn(f"Could not refresh the status file: {e}")


def refresh(cfg: Config, log: Log) -> Status:
    """Gather and write the MOTD file; failures are logged, never fatal."""
    st = gather(cfg, log)
    try:
        write_motd(motd_text(st))
    except OSError as e:
        log.warn(f"Could not write {MOTD_FILE}: {e}")
    return st
