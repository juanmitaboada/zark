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
zark status - how full the source pools are and how old each drive's
backup is (M3 decisions 12 and 13). Read-only.

  zark status                     the report
  zark status --refresh           write the MOTD file (/run/zark/status.motd)
  zark status --refresh --notify  also a desktop notification for a pool at
                                  a warning or critical level, at most once
                                  a day per pool and level (what the hourly
                                  zark-status.timer runs)
"""

import os
from datetime import UTC, datetime

from lib import notify, status
from lib.config import Config
from lib.log import Log


def _report(st: status.Status, log: Log) -> None:
    for pool, pct in sorted(st.caps.items()):
        level = status.cap_level(pct)
        text = f"{pool}: {pct}% full" + (f" ({level})" if level else "")
        if level == "critical":
            log.error(text)  # red, as in the MOTD (M3P.23)
        elif level:
            log.warn(text)
        else:
            log.ok(text)
    stale, rpo = status.rpo_line(st)
    if rpo:
        (log.warn if stale else log.info)(rpo)
    else:
        log.info("No backup drive registered")
    for line in st.shared:
        log.warn(line)
    if st.read_error:
        log.warn(st.read_error)
    if st.residues:
        log.info(status.residue_line(st.residues))


def _notify(st: status.Status) -> None:
    path = status.NOTIFIED_FILE
    try:
        sent = set(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        sent = set()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    done = {key for key in sent if key.endswith(day)}
    for key, text in status.due_notifications(st, done):
        if notify.notify("zark: disk space", text):
            done.add(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{k}\n" for k in sorted(done)), encoding="utf-8")
    except OSError:
        pass


def run(args: list[str]) -> None:
    """Main entry point for 'zark status'."""
    cfg = Config.load()
    if "--refresh" in args:
        # Hourly from the timer: nothing goes to zark.log.
        log = Log(os.devnull)
        st = status.refresh(cfg, log)
        if "--notify" in args:
            _notify(st)
        return
    log = Log()
    cfg.check_registry(log, fatal=False)
    log.banner("STATUS", "Source pools and backup drives")
    st = status.refresh(cfg, log)
    _report(st, log)
