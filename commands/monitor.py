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
zark monitor — Live progress monitor for backup operations.

Run in a separate terminal while backup is running.
Shows pool health, sync progress, snapshot counts.
"""

import sys
import time

from lib import sh
from lib.config import Config
from lib.log import Log
from lib.zfs import ZFS


def _draw_bar(pct: int, width: int = 38) -> str:
    filled = pct * width // 100
    empty = width - filled
    return "█" * filled + "░" * empty


def run(args: list[str]):  # pylint: disable=too-many-statements, too-many-locals
    """Run the backup monitor."""
    del args  # unused
    log = Log()
    cfg = Config.load()
    zfs = ZFS(log)

    # Find imported known pool
    monitor_pool = None
    for name in cfg.known_drives:
        if zfs.pool_exists(name):
            monitor_pool = name
            break

    if not monitor_pool:
        log.warn("No known backup pool is currently imported")
        log.info("Connect a backup drive and start a backup first")
        return

    log.info(f"Monitoring: {monitor_pool}  (Ctrl+C to exit)")
    time.sleep(1)

    lines = 17
    first = True

    try:
        while True:
            if not first:
                _ = sys.stdout.write(f"\033[{lines}A")  # move cursor up
            first = False

            health = zfs.pool_health(monitor_pool)
            hc = log.G if health == "ONLINE" else log.R

            src_info = zfs.pool_info("rpool")
            dst_info = zfs.pool_info(monitor_pool)

            pct = 0
            if src_info and dst_info and src_info.used_bytes > 0:
                pct = min(100, dst_info.used_bytes * 100 // src_info.used_bytes)

            abar = _draw_bar(pct)

            snaps = len(zfs.list_snapshots(f"{monitor_pool}/rpool", "autosnap"))
            snap_names = zfs.unique_snap_names(f"{monitor_pool}/rpool")
            latest = snap_names[-1] if snap_names else ""

            is_running = sh.run("pgrep -x syncoid").ok
            if is_running:
                pid = sh.run("pgrep -x syncoid | head -1").output
                pstart = sh.run(f"stat -c %Y /proc/{pid}").output
                elapsed = int(time.time()) - int(pstart) if pstart.isdigit() else 0
                em, es = divmod(elapsed, 60)
                eta = ""
                if elapsed > 5 and 0 < pct < 100:
                    remain = elapsed * (100 - pct) // pct
                    eta = f"  ~{remain // 60}m{remain % 60}s remaining"
                status = f"{log.G}● RUNNING{log.N}  {em}m{es}s elapsed{eta}"
            else:
                status = f"{log.Y}● IDLE{log.N}  (no backup running)"

            now = time.strftime("%H:%M:%S")
            clear_eol = "\033[K"  # clear to EOL

            print(f"{log.BOLD}{log.C}╔{'═' * 58}╗{clear_eol}{log.N}")
            print(
                f"{log.BOLD}{log.C}║{clear_eol}{log.N}  "
                + f"BACKUP MONITOR                              {now}",
            )
            print(f"{log.BOLD}{log.C}╠{'═' * 58}╣{clear_eol}{log.N}")
            print(
                f"{log.BOLD}{log.C}║{clear_eol}{log.N}  "
                + f"Pool:    {log.W}{monitor_pool}{log.N}  [{hc}{health}{log.N}]",
            )
            print(f"{log.BOLD}{log.C}║{clear_eol}{log.N}  Status:  {status}")
            print(f"{log.BOLD}{log.C}║{clear_eol}{log.N}")
            print(
                f"{log.BOLD}{log.C}║{clear_eol}{log.N}  {log.Y}SOURCE{log.N}  rpool    "
                + f"used={log.W}{src_info.used if src_info else '?'}{log.N}  "
                + f"avail={log.W}{src_info.avail if src_info else '?'}{log.N}",
            )
            print(
                f"{log.BOLD}{log.C}║{clear_eol}{log.N}  {log.Y}TARGET{log.N}  {monitor_pool}  "
                + f"used={log.W}{dst_info.used if dst_info else '?'}{log.N}  "
                + f"({dst_info.pct_used if dst_info else 0}%)  "
                + f"avail={log.W}{dst_info.avail if dst_info else '?'}{log.N}",
            )
            print(f"{log.BOLD}{log.C}║{clear_eol}{log.N}")
            print(f"{log.BOLD}{log.C}║{clear_eol}{log.N}  {log.Y}PROGRESS{log.N}  target vs source")
            print(
                f"{log.BOLD}{log.C}║{clear_eol}{log.N}  {log.C}{abar}{log.N}  {log.W}{pct}%{log.N}",
            )
            print(f"{log.BOLD}{log.C}║{clear_eol}{log.N}")
            print(
                f"{log.BOLD}{log.C}║{clear_eol}{log.N}  "
                + f"{log.Y}SNAPSHOTS{log.N}  on target: {log.W}{snaps}{log.N}",
            )
            print(
                (
                    f"{log.BOLD}{log.C}║{clear_eol}{log.N}    Latest: {log.W}{latest}{log.N}"
                    if latest
                    else f"{log.BOLD}{log.C}║{clear_eol}{log.N}"
                ),
            )
            print(f"{log.BOLD}{log.C}║{clear_eol}{log.N}")
            print(f"{log.BOLD}{log.C}║{clear_eol}{log.N}  Refreshing every 5s — Ctrl+C to exit")
            print(f"{log.BOLD}{log.C}╚{'═' * 58}╝{clear_eol}{log.N}")

            time.sleep(5)
    except KeyboardInterrupt:
        print("\n  Monitor stopped.")
