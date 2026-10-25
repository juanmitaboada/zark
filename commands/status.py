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
backup is (M3 decisions 12 and 13). Read-only for zark's state: it writes
only the MOTD file and status.json (docs/STATUS-FILES.md), from one read.

  zark status                     the report, with what is excluded from
                                  backups (zark exclude)
  zark status --refresh           write the MOTD file (/run/zark/status.motd)
                                  and /var/lib/zark/status.json
  zark status --refresh --notify  also a desktop notification for a pool at
                                  a warning or critical level, at most once
                                  a day per pool and level (what the hourly
                                  zark-status.timer runs)
"""

import os
from datetime import UTC, datetime

from commands.setup import sanoid_conf_gap
from lib import exclusions, notify, status, status_file, statusdirs, survey
from lib.config import Config, zark_command
from lib.log import Log
from lib.zfs import ZFS


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


def _exclusions(log: Log) -> None:
    """One line when anything is excluded from backups, and the sanoid.conf
    line when setup's split of the drained subtrees is missing (M5b-13b).
    No line per drive: the registry does not know what each one holds."""
    pools = survey.source_pools()
    if "rpool" not in pools:
        return
    try:
        origin = survey.read_origin(pools, strict=True)
        declared = exclusions.read(pools, origin, strict=True)
    except survey.ReadError as e:
        log.warn(f"Cannot read what is excluded from backups: {e}")
        return
    left_out = [rel for top in declared.no_backup for rel in exclusions.subtree(top, origin)]
    if left_out:
        log.info(
            f"Excluded from backups: {len(left_out)} dataset(s) ({zark_command()} exclude list)"
        )
    if declared.no_snapshot and (gap := sanoid_conf_gap(ZFS(log))):
        log.warn(f"sanoid.conf does not keep the exclusions ({gap}): run {zark_command()} setup")
    for rel, why in declared.ignored:
        log.warn(f"Exclusion ignored on {rel}: {why}")


def _notify(st: status.Status) -> None:
    path = status.NOTIFIED_FILE
    try:
        sent = set(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        sent = set()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    done = {key for key in sent if key.endswith(day)}
    for key, text in status.due_notifications(st, done):
        # The key is "<pool> <level> <date>": only the critical level stays on screen.
        if notify.notify("zark: disk space", text, critical=key.split()[1] == "critical"):
            done.add(key)
    try:
        _ = statusdirs.ensure(path.parent)
        path.write_text("".join(f"{k}\n" for k in sorted(done)), encoding="utf-8")
    except OSError:
        pass


def run(args: list[str]) -> None:
    """Main entry point for 'zark status'."""
    cfg = Config.load()
    if "--refresh" in args:
        # Hourly from the timer: nothing goes to zark.log.
        log = Log(os.devnull)
        st = status_file.refresh(cfg, log, "timer")
        if "--notify" in args:
            _notify(st)
        return
    log = Log()
    cfg.check_registry(log, fatal=False)
    log.banner("STATUS", "Source pools and backup drives")
    st = status_file.refresh(cfg, log, "status")
    _report(st, log)
    _exclusions(log)
