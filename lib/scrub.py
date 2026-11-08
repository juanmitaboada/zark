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
A backup pool's scrub: its state, and the only ``zpool scrub`` calls zark
makes (M19, ``zark fullcheck``).

Rule R1 (M19R-1): before any ``zpool scrub``, the pool must read as
imported read-write (``readonly`` = ``off``) and not suspended (``health``
not ``SUSPENDED``); either value unreadable is a refusal. A module without
OpenZFS PR 17717 (noble's 2.2.2) takes a scrub, a pause or a stop of a
read-only import into a sync that never comes, and of a suspended pool into
a sync that waits for the drive: the ``zpool`` process stays in state D and
cannot be killed. Both values come from the module's memory, with no I/O to
the drive, so reading them is safe on any pool.

The state is read from ``zpool status -j --json-int`` (OpenZFS 2.3 and
later: exact integers) and, when that is refused, from the text of
``zpool status`` in the C locale (2.2: the percentage is exact, the byte
figures rounded).
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from lib import sh
from lib.config import zark_command
from lib.log import Log

FUNCTIONS = ("NONE", "SCRUB", "RESILVER", "ERRORSCRUB")
STATES = ("NONE", "SCANNING", "FINISHED", "CANCELED")


@dataclass(frozen=True)
class Scan:  # pylint: disable=too-many-instance-attributes
    """A pool's last or current scan, as ``zpool status`` reports it."""

    function: str  # one of FUNCTIONS
    state: str  # one of STATES
    paused: bool = False
    to_examine: int | None = None  # bytes to scan (allocated at its start)
    examined: int | None = None
    skipped: int | None = None
    issued: int | None = None
    processed: int | None = None  # bytes repaired
    errors: int | None = None
    percent: int | None = None  # 0-100, of what is issued
    start_time: int | None = None  # epoch seconds

    @property
    def scrubbing(self) -> bool:
        """A scrub is under way, paused or not."""
        return self.function == "SCRUB" and self.state == "SCANNING"

    @property
    def running(self) -> bool:
        """A scrub is under way and not paused: one a pause applies to."""
        return self.scrubbing and not self.paused

    @property
    def resilvering(self) -> bool:
        """A resilver is under way (zark never touches one)."""
        return self.function == "RESILVER" and self.state == "SCANNING"

    @property
    def bytes_total(self) -> int | None:
        """What the scrub issues in all: to examine, less what it skips."""
        if self.to_examine is None:
            return None
        return max(0, self.to_examine - (self.skipped or 0))


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_json(text: str, pool: str) -> Scan | None:
    """The scan of ``pool`` in ``zpool status -j --json-int``; None when the
    text is not that document. A pool never scanned has no ``scan_stats``."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    pools = doc.get("pools") if isinstance(doc, dict) else None
    entry = pools.get(pool) if isinstance(pools, dict) else None
    if not isinstance(entry, dict):
        return None
    stats = entry.get("scan_stats")
    if not isinstance(stats, dict):
        return Scan("NONE", "NONE")
    function, state = stats.get("function"), stats.get("state")
    if function not in FUNCTIONS or state not in STATES:
        return None
    scan = Scan(
        function,
        state,
        paused=bool(_int(stats.get("scrub_pause"))),
        to_examine=_int(stats.get("to_examine")),
        examined=_int(stats.get("examined")),
        skipped=_int(stats.get("skipped")),
        issued=_int(stats.get("issued")),
        processed=_int(stats.get("processed")),
        errors=_int(stats.get("errors")),
        start_time=_int(stats.get("start_time")),
    )
    total, issued = scan.bytes_total, scan.issued
    percent = None
    if state == "FINISHED":
        percent = 100
    elif total and issued is not None:
        percent = min(100, issued * 100 // total)
    return Scan(**{**scan.__dict__, "percent": percent})


_NICE = re.compile(r"^([0-9.]+)([BKMGTPE]?)$")
_UNITS = "BKMGTPE"


def nice_bytes(text: str) -> int | None:
    """Bytes of a ``zfs_nicebytes`` figure (``0B``, ``1.21G``), rounded as
    printed; None when it is not one."""
    m = _NICE.match(text.strip())
    if not m:
        return None
    unit = m.group(2) or "B"
    return int(float(m.group(1)) * 1024 ** _UNITS.index(unit))


_FINISHED = re.compile(r"scrub repaired (\S+) in .* with (\d+) errors on ")
_DONE = re.compile(r"(\S+) repaired, ([0-9.]+)% done")


def parse_text(text: str) -> Scan | None:  # pylint: disable=too-many-return-statements
    """The scan in the C-locale text of ``zpool status <pool>``
    (``print_scan_scrub_resilver_status``); None without a ``scan:`` line."""
    lines = text.splitlines()
    head = next((i for i, ln in enumerate(lines) if ln.strip().startswith("scan:")), None)
    if head is None:
        return None
    first = lines[head].strip()[len("scan:") :].strip()
    rest = " ".join(ln.strip() for ln in lines[head + 1 : head + 4])
    if first.startswith("none requested"):
        return Scan("NONE", "NONE")
    if m := _FINISHED.search(first):
        return Scan(
            "SCRUB",
            "FINISHED",
            processed=nice_bytes(m.group(1)),
            errors=int(m.group(2)),
            percent=100,
        )
    if first.startswith("scrub canceled"):
        return Scan("SCRUB", "CANCELED")
    if first.startswith("resilver"):
        return Scan("RESILVER", "FINISHED" if first.startswith("resilvered") else "SCANNING")
    if first.startswith(("scrub in progress", "scrub paused")):
        m = _DONE.search(rest)
        return Scan(
            "SCRUB",
            "SCANNING",
            paused=first.startswith("scrub paused"),
            processed=nice_bytes(m.group(1)) if m else None,
            percent=min(100, int(float(m.group(2)))) if m else None,
        )
    return None


def read(pool: str) -> Scan | None:
    """``pool``'s scan, None when it cannot be read. A read: R1 does not apply."""
    r = sh.run(f"zpool status -j --json-int {pool}")
    if r.ok and (scan := parse_json(r.stdout, pool)) is not None:
        return scan
    r = sh.run(f"env LC_ALL=C zpool status {pool}")
    return parse_text(r.stdout) if r.ok else None


def refusal(pool: str) -> str:
    """Why rule R1 refuses a ``zpool scrub`` of ``pool``; "" when it allows it.

    Both reads are answered from the module's memory: ``readonly`` is fixed
    at import, ``health`` says ``SUSPENDED`` while the drive is gone."""
    ro = sh.run(f"zpool get -H -o value readonly {pool}")
    if not ro.ok or ro.output != "off":
        return (
            f"{pool} is not imported read-write (readonly={ro.output if ro.ok else 'unreadable'})"
        )
    health = sh.run(f"zpool get -H -o value health {pool}")
    if not health.ok or not health.output or health.output == "SUSPENDED":
        state = health.output if health.ok and health.output else "unreadable"
        return f"{pool} is not usable (health={state})"
    return ""


def is_suspended(pool: str) -> bool:
    """True when ``pool``'s health is ``SUSPENDED`` or cannot be read: a
    pause or an export would wait for a drive that is gone (M19R-1)."""
    health = sh.run(f"zpool get -H -o value health {pool}")
    return not health.ok or not health.output or health.output == "SUSPENDED"


def _scrub(pool: str, flag: str, log: Log) -> bool:
    """The one place zark runs ``zpool scrub``, behind R1."""
    why = refusal(pool)
    if why:
        log.warn(f"No zpool scrub of {pool}: {why}")
        return False
    cmd = f"zpool scrub {flag} {pool}" if flag else f"zpool scrub {pool}"
    return sh.run(cmd, log=log).ok


def start(pool: str, log: Log) -> bool:
    """Start a scrub, or resume a paused one (``zpool scrub``)."""
    return _scrub(pool, "", log)


def pause(pool: str, log: Log) -> bool:
    """Pause the scrub under way (``zpool scrub -p``)."""
    return _scrub(pool, "-p", log)


def pause_interrupted(pool: str, log: Log) -> None:
    """After a read-write import by a command other than ``fullcheck``: a
    scrub that a dead or unplugged ``fullcheck`` left running resumed with
    the import; pause it, so the command runs at full speed and the scrub's
    progress waits for the next ``fullcheck`` (M19-5 5a, M19R-10). Only a
    running scrub is touched: not a paused one, a resilver or an error scrub."""
    scan = read(pool)
    if scan is None or not scan.running:
        return
    if pause(pool, log):
        log.warn(
            f"{pool}: a full check's scrub was interrupted; paused, "
            + f"`{zark_command()} fullcheck` resumes it"
        )
    else:
        log.warn(f"{pool}: a full check's scrub runs on; it was not paused")
