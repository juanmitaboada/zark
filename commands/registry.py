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
zark registry — inspect and repair known_drives.json without hand edits.

  zark registry list            every entry, whether its disk is connected,
                                and whether the pool GUID on that disk matches
  zark registry forget <name>   remove an entry (e.g. a lost or retired disk)
                                and that disk's anchors in origin (rpool
                                bookmarks, bpool snapshots)
  zark registry fix [<name>]    rewrite drive_id from the connected disk that
                                carries the registered pool GUID, and write
                                every missing key

All writes go through lib.registry (validated, atomic).
"""

from pathlib import Path

from lib import engine, sh
from lib.config import Config
from lib.drives import drive_staleness_days, get_drive_id, scan_connected_drives
from lib.identity import BY_ID_DIR, read_pool_label
from lib.log import Log

USAGE = "Usage: sudo zark registry list | forget <name> | fix [<name>]"


def _connected_by_id_for_guid(guid: str, cfg: Config, log: Log) -> str:
    """By-id name of the connected disk that carries the pool with ``guid``."""
    for d in scan_connected_drives(cfg, log):
        if d.guid != guid:
            continue
        if d.dev_path:
            return get_drive_id(d.dev_path)
        vdevs = sh.run(f"zpool list -vHP {d.name}")
        for line in vdevs.lines[1:] if vdevs.ok else []:
            path = line.strip().split("\t")[0]
            if path.startswith("/dev/"):
                return get_drive_id(path)
    return ""


def _list(cfg: Config, log: Log) -> None:
    if not cfg.known_drives:
        log.info(f"No drives registered in {cfg.drives_file_path}")
        return
    log.info(f"Registry: {cfg.drives_file_path}")
    for name, info in sorted(cfg.known_drives.items()):
        age = drive_staleness_days(info)
        last = f"{info.last_backup_at} ({age} day(s) ago)" if age is not None else "never"
        part1 = Path(f"{BY_ID_DIR}/{info.drive_id}-part1")
        if info.drive_id == "<unknown>":
            state = f"{log.Y}drive_id unknown — run: zark registry fix {name}{log.N}"
        elif not part1.exists():
            state = "not connected"
        else:
            label = read_pool_label(str(part1))
            if label is None:
                state = f"{log.Y}connected, no ZFS label{log.N}"
            elif label.guid == info.guid:
                state = f"{log.G}connected, GUID matches{log.N}"
            else:
                state = f"{log.R}connected, GUID {label.guid} ≠ registered{log.N}"
        log.raw(f"  {log.W}{name}{log.N}")
        log.raw(f"      guid:        {info.guid}")
        log.raw(f"      drive_id:    {info.drive_id}")
        log.raw(f"      last backup: {last}")
        log.raw(f"      autoeject:   {'yes' if info.autoeject else 'no'}")
        log.raw(f"      state:       {state}")


def _forget(cfg: Config, log: Log, name: str) -> None:
    if name not in cfg.known_drives:
        log.fatal(f"'{name}' is not registered", solutions=["sudo zark registry list"])
    info = cfg.known_drives[name]
    log.warn(f"Forgetting '{name}' (GUID {info.guid}, {info.drive_id})")
    log.info("The disk and its data are not touched: the registry entry and this")
    log.info("disk's anchors in origin go, so its next backup may need a decision.")
    if not log.ask(f"Remove '{name}' from the registry?", default=False):
        log.info("Aborted")
        return
    del cfg.known_drives[name]
    cfg.save_drives()
    log.ok(f"'{name}' removed from {cfg.drives_file_path}")
    pools = engine.source_pools()
    if "rpool" not in pools:
        log.info("rpool is not imported here: run this on the installed system to drop the anchors")
        return
    dropped = engine.drop_disk_anchors(info.guid, pools, log)
    log.ok(f"{dropped} anchor(s) of '{name}' dropped from origin")


def _fix(cfg: Config, log: Log, only: str | None) -> None:
    names = [only] if only else sorted(cfg.known_drives)
    for name in names:
        if name not in cfg.known_drives:
            log.fatal(f"'{name}' is not registered", solutions=["sudo zark registry list"])
    changed = 0
    for name in names:
        info = cfg.known_drives[name]
        by_id = _connected_by_id_for_guid(info.guid, cfg, log)
        if not by_id:
            log.info(f"{name}: pool GUID {info.guid} not found on any connected disk — skipped")
            continue
        if by_id == info.drive_id:
            log.ok(f"{name}: drive_id already correct ({by_id})")
            continue
        log.ok(f"{name}: drive_id {info.drive_id} → {by_id}")
        info.drive_id = by_id
        changed += 1
    cfg.save_drives()  # also writes every key of every entry
    log.ok(f"Registry written ({changed} drive_id change(s)): {cfg.drives_file_path}")


def run(args: list[str]) -> None:
    """Main entry point for 'zark registry'."""
    log = Log()
    cfg = Config.load()
    sub = args[0] if args else "list"
    cfg.check_registry(log, fatal=sub != "list")

    log.banner("DRIVE REGISTRY", "known_drives.json")
    if sub == "list":
        _list(cfg, log)
    elif sub == "forget" and len(args) == 2:
        _forget(cfg, log, args[1])
    elif sub == "fix" and len(args) <= 2:
        _fix(cfg, log, args[1] if len(args) == 2 else None)
    else:
        log.fatal(f"Unknown registry command: {' '.join(args)}", solutions=[USAGE])
