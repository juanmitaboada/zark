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
What steps 4 to 6 read back from zark and the guests, as named verdicts:
the backup disk's name, the backup's plan, the recover's anchors, the
status file, the data that must survive the recover.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bench.console import clean
from bench.report import Check

POINT_PREFIX = "zark_"  # zark's lib/replication.py:52
# Planned states a backup sends without asking (lib/replication.py:172-174),
# and those that mean the drive lost its common ground with the system.
LOST_STATES = ("new", "rollback", "diverged")


# What steps 4 and 6 keep of the system (read-only, as root), by file name.
SYSTEM_READS = {
    "zark.log": "cat /var/log/zark.log",
    "known_drives.json": "cat /etc/zark/known_drives.json",
    "sanoid.conf": "cat /etc/sanoid/sanoid.conf",
    "zfs-all.txt": "zfs list -Hp -t all -o name,type,creation -r rpool bpool",
    "lsblk.json": "lsblk -J -o NAME,TYPE,SIZE,FSTYPE,SERIAL,TRAN,MOUNTPOINTS",
    "failed-units.txt": "systemctl --failed --no-legend --plain",
}
# The tools zark recover needs (its commands/recover.py:174-181) and apt's
# index files, read in the live before recover runs.
LIVE_TOOLS = (
    "sh -c 'which zfs zgenhostid sgdisk cryptsetup mkfs.vfat partprobe; "
    "echo apt list files: $(ls /var/lib/apt/lists | wc -l)'"
)
# What step 5 keeps of the live.
LIVE_READS = {
    "zark.log": "cat /var/log/zark.log",
    "lsblk.json": "lsblk -J -o NAME,TYPE,SIZE,FSTYPE,SERIAL,TRAN,MOUNTPOINTS",
    "cloud-init.log": "cat /var/log/cloud-init.log",
    "cloud-init-output.log": "cat /var/log/cloud-init-output.log",
    "journal-kernel.txt": "journalctl -k -b --no-pager",
}


# Lines of the live's serial console that say how far its cloud-init got
# (the seed mirrors cloud-init's output and the SSH install there).
LIVE_SERIAL = re.compile(
    r"zark-bench|cloud-init|Cloud-init|apt-get|openssh|\bGet:|\bErr:|\bE: |ssh"
)


def live_serial_lines(raw: bytes, limit: int = 80) -> list[str]:
    """The last ``limit`` lines of the live's serial log about cloud-init, apt and SSH."""
    text = clean(raw.replace(b"\r\n", b"\n")).decode("utf-8", "replace")
    lines = [ln.rstrip() for ln in text.splitlines() if LIVE_SERIAL.search(ln)]
    return lines[-limit:]


def live_timeout_lines(serial: bytes, ssh_stderr: bytes, limit: int = 80) -> list[str]:
    """The live's serial lines, then the last SSH attempt's own account of its failure."""
    tried = clean(ssh_stderr).decode("utf-8", "replace").splitlines()[-limit:]
    return [*live_serial_lines(serial, limit), "ssh -v, the last attempt:", *tried]


class ReadBackError(Exception):
    """What was read back does not have the expected shape."""


def by_id_name(names: list[str], serial: str) -> str:
    """The one whole-disk ``usb-`` name under /dev/disk/by-id holding ``serial``."""
    found = sorted(
        n
        for n in (x.strip() for x in names)
        if n.startswith("usb-") and serial in n and "-part" not in n
    )
    if len(found) != 1:
        raise ReadBackError(f"{len(found)} whole-disk usb- names hold {serial!r}: {found}")
    return found[0]


def plan_states(transcript: str) -> dict[str, int]:
    """
    The last ``Plan: <n> <state>, ...`` line a backup printed
    (commands/backup.py:287), as counts by state.
    """
    lines = [ln for ln in transcript.splitlines() if "Plan: " in ln]
    if not lines:
        raise ReadBackError("no Plan: line in the backup's output")
    body = lines[-1].split("Plan: ", 1)[1].strip()
    counts: dict[str, int] = {}
    for item in body.split(", "):
        m = re.fullmatch(r"(\d+) (.+)", item.strip())
        if not m:
            raise ReadBackError(f"not a plan item: {item!r}")
        counts[m.group(2)] = counts.get(m.group(2), 0) + int(m.group(1))
    return counts


def anchors_kept(states: dict[str, int]) -> Check:
    """The backup after a recover resends nothing in full and rolls nothing back."""
    lost = {s: n for s, n in states.items() if s in LOST_STATES}
    shown = ", ".join(f"{n} {s}" for s, n in sorted(states.items()))
    if lost:
        return Check("anchors-kept", False, f"plan: {shown}")
    return Check("anchors-kept", True, f"plan: {shown}")


def anchored_by_recover(transcript: str) -> tuple[int, list[str]]:
    """
    How many rpool datasets recover anchored for the drive
    (commands/recover.py:844) and the datasets it could not anchor (:822-823).
    """
    counts = re.findall(r"(\d+) rpool dataset\(s\) anchored for \S+", transcript)
    not_anchored = re.findall(r"(\S+): not anchored for \S+", transcript)
    return (int(counts[-1]) if counts else 0), not_anchored


def anchor_pattern(guid: str) -> re.Pattern[str]:
    """
    A drive's anchor name, whatever its stamp (lib/replication.py:55, :78-80):
    recover names each anchor from its snapshot's creation time
    (commands/recover.py:812-818), which need not be the point's own stamp.
    """
    return re.compile(rf"^{POINT_PREFIX}{re.escape(guid)}_\d{{4}}-\d\d-\d\d_\d\d:\d\d:\d\dZ$")


def anchors_check(bookmarks: list[str], bpool_snaps: list[str], guid: str, anchored: int) -> Check:
    """
    As many rpool datasets carry the drive's anchor bookmark as recover said it
    anchored, one each, and bpool keeps a snapshot renamed to the drive's anchor
    (commands/recover.py:826-858).
    """
    pattern = anchor_pattern(guid)
    marked = sorted(
        b.split("#", 1)[0]
        for b in (x.strip() for x in bookmarks)
        if "#" in b and pattern.match(b.split("#", 1)[1])
    )
    boot = sorted(
        s
        for s in (x.strip() for x in bpool_snaps)
        if "@" in s and pattern.match(s.split("@", 1)[1])
    )
    distinct = len(set(marked)) == len(marked)
    ok = anchored > 0 and len(marked) == anchored and distinct and len(boot) >= 1
    detail = (
        f"{len(marked)} rpool dataset(s) with the drive's anchor bookmark, "
        f"{len(boot)} bpool snapshot(s) renamed to it; recover said {anchored}"
    )
    return Check("anchors-after-recover", ok, detail)


def status_drive(status_text: str, pool: str) -> dict[str, Any]:
    """The drive entry for ``pool`` in /var/lib/zark/status.json."""
    try:
        doc = json.loads(status_text)
    except ValueError as err:
        raise ReadBackError(f"status.json: {err}") from err
    drives = doc.get("drives") if isinstance(doc, dict) else None
    for drive in drives if isinstance(drives, list) else []:
        if isinstance(drive, dict) and drive.get("name") == pool:
            return drive
    raise ReadBackError(f"status.json has no drive {pool!r}")


def last_run_check(drive: dict[str, Any], name: str, after: str = "") -> Check:
    """The drive's last backup completed, at a point newer than ``after``."""
    last = drive.get("last_run") or {}
    result = last.get("result") if isinstance(last, dict) else None
    point = str(drive.get("last_backup_point") or "")
    ok = result == "completed" and point.startswith(POINT_PREFIX) and point > after
    return Check(name, ok, f"last_run {result}, last_backup_point {point or '-'}")


def same_digest(name: str, expected: str, actual: str) -> Check:
    """A file read back is the one written before the recover."""
    return Check(name, bool(expected) and expected == actual, f"sha256 {actual or '-'}")
