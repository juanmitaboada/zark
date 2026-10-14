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
Forensic capture of a pool before anything changes it (I7, M4a-9).

black's checksum errors of 2026-09-25 could not be explained afterwards:
purge destroyed the pool, and with it the only ``zpool status -v``. This
module writes, read-only and without asking, what a later diagnosis needs:
``zpool status -vP``, ``zpool events -v`` for the pool, the end of
``zpool history -il``, the kernel log of the last two hours, ``lsblk`` and
the USB bridge ids of the pool's disks, and zark's version.

The file goes next to zark.log (``/var/log`` on an installed system, the
stick on a live session), mode 0600, named
``zark-forensics-<pool>-<UTC>.txt``. Each section shows the command and its
exit status; a section that fails is kept with its error, never skipped.
"""

import os
import time
from pathlib import Path

from lib import sh
from lib.config import VERSION
from lib.identity import disks_under
from lib.log import Log

HISTORY_LINES = 200


def _section(title: str, cmd: str) -> str:
    r = sh.run(cmd)
    body = r.stdout.rstrip("\n")
    if r.stderr.strip():
        body += ("\n" if body else "") + r.stderr.rstrip("\n")
    return f"== {title}\n$ {cmd}  (exit {r.returncode})\n{body}\n"


def _usb_ids(pool: str) -> str:
    """VID:PID of the USB bridge behind each disk of ``pool``."""
    # Late import: lib.health is not needed by anything else here.
    from lib.health import _usb_ids_for  # pylint: disable=import-outside-toplevel

    r = sh.run(f"zpool list -vHP {pool}")
    lines: list[str] = []
    for line in r.lines[1:] if r.ok else []:
        path = line.strip().split("\t")[0]
        if not path.startswith("/dev/"):
            continue
        for disk in sorted(disks_under(path)):
            vid, pid = _usb_ids_for(Path(disk).name)
            lines.append(f"{path} -> {disk}: {vid or '?'}:{pid or '?'}")
    return "== USB bridge ids\n" + ("\n".join(lines) or "(no USB disk found)") + "\n"


def capture(pool: str, reason: str, log: Log) -> str:
    """Write the capture for ``pool``; return its path ("" if it could not be written)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    directory = os.path.dirname(Log.default_file()) or "."
    path = os.path.join(directory, f"zark-forensics-{pool}-{stamp}.txt")
    kernel = (
        "journalctl -k --since '-2h' --no-pager"
        if sh.run("command -v journalctl").ok
        else "dmesg -T"
    )
    parts = [
        f"zark {VERSION} forensic capture of {pool}\nreason: {reason}\n",
        _section("pool status", f"zpool status -vP {pool}"),
        _section("pool events", f"zpool events -v {pool}"),
        _section(
            f"pool history (last {HISTORY_LINES} lines)",
            f"zpool history -il {pool} | tail -n {HISTORY_LINES}",
        ),
        _section("kernel log, last two hours", kernel),
        _section("block devices", "lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN,TYPE,MOUNTPOINTS"),
        _usb_ids(pool),
    ]
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _ = f.write("\n".join(parts))
    except OSError as e:
        log.warn(f"Could not write the forensic capture of {pool}: {e}")
        return ""
    log.info(f"Forensic capture of {pool} ({reason}): {path}")
    return path
