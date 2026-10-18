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
                                and whether the pool GUID on that disk matches;
                                on the installed system also the drives that
                                hold anchors in origin without an entry, and
                                the backup points left in origin
  zark registry forget <name>   remove an entry (e.g. a lost or retired disk)
                                and that disk's anchors in origin (rpool
                                bookmarks, bpool snapshots)
  zark registry forget <GUID>   the same by pool GUID; for a GUID with no
                                entry, only its anchors
  zark registry fix [<name>]    rewrite drive_id from the connected disk that
                                carries the registered pool GUID, and write
                                every missing key
  zark registry retention <name> [window=N] [max_age=N|off] [margin=N]
                                show or change the drive's retention: days
                                in which everything is kept, age beyond which
                                nothing is (off by default), and the free
                                space a backup tries to leave (% of the drive)

All writes go through lib.registry (validated, atomic).
"""

import shlex
from pathlib import Path

from lib import engine, registry, sh, survey
from lib.config import Config
from lib.drives import drive_staleness_days, get_drive_id, scan_connected_drives
from lib.identity import BY_ID_DIR, read_pool_label
from lib.log import Log

USAGE = (
    "Usage: sudo zark registry list | forget <name|GUID> | fix [<name>]"
    + " | retention <name> [window=N] [max_age=N|off] [margin=N]"
)
# Command-line names of the retention settings (lib.registry.RETENTION_LIMITS).
_RETENTION_ARGS = {"window": "window_days", "max_age": "max_age_days", "margin": "margin_pct"}


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
        log.raw(f"      retention:   {_policy_text(cfg, name)}")
        log.raw(f"      state:       {state}")
    _list_origin(cfg, log)


def _policy_text(cfg: Config, name: str) -> str:
    p = cfg.drive_policy(name)
    max_age = f"{p.max_age_days} days" if p.max_age_days else "off"
    return f"window {p.window_days} days, max_age {max_age}, margin {p.margin_pct}%"


def _retention(cfg: Config, log: Log, name: str, settings: list[str]) -> None:
    """Show or change one drive's retention settings (M3 decision 2b)."""
    if name not in cfg.known_drives:
        log.fatal(f"'{name}' is not registered", solutions=["sudo zark registry list"])
    info = cfg.known_drives[name]
    if settings:
        current = info.extra.get(registry.RETENTION_KEY, {})
        values: dict[str, int] = dict(current) if isinstance(current, dict) else {}
        for item in settings:
            key, _, raw = item.partition("=")
            if key not in _RETENTION_ARGS or not raw:
                log.fatal(f"Unknown retention setting: {item}", solutions=[USAGE])
            n = 0 if key == "max_age" and raw == "off" else int(raw) if raw.isdigit() else -1
            values[_RETENTION_ARGS[key]] = n
        try:
            registry.check_retention(values, f"entry '{name}'")
        except registry.RegistryError as e:
            log.fatal(str(e), solutions=[USAGE])
        info.extra[registry.RETENTION_KEY] = values
        cfg.save_drives()
        log.ok(f"{name}: retention changed")
    log.info(f"{name}: {_policy_text(cfg, name)}")


def _list_origin(cfg: Config, log: Log) -> None:
    """Anchors of drives without an entry, and backup points left in origin."""
    pools = survey.source_pools()
    if "rpool" not in pools:
        log.info("rpool is not imported here: anchors in origin not checked")
        return
    origin = survey.read_origin(pools)
    registered = {info.guid for info in cfg.known_drives.values()}
    for guid in sorted(engine.anchored_disks(origin) - registered):
        n = len(engine.disk_anchors(origin, guid))
        log.warn(f"  drive GUID {guid}: {n} anchor(s) in origin, no registry entry")
        log.raw(f"      drop them: sudo zark registry forget {guid}")
    left = engine.leftover_points(origin)
    if left:
        log.info(
            f"  {len(left)} backup point(s) left in origin (interrupted runs, or a run whose "
            + "anchor failed)"
        )


def _forget(cfg: Config, log: Log, name: str) -> None:
    if name.isdigit() and name not in cfg.known_drives:
        # Pool names cannot start with a digit: a number is a pool GUID.
        named = [n for n, info in cfg.known_drives.items() if info.guid == name]
        if not named:
            _forget_guid(cfg, log, name)
            return
        name = named[0]
    if name not in cfg.known_drives:
        log.fatal(f"'{name}' is not registered", solutions=["sudo zark registry list"])
    info = cfg.known_drives[name]
    log.warn(f"Forgetting '{name}' (GUID {info.guid}, {info.drive_id})")
    log.info("The disk and its data are not touched: the registry entry and this")
    log.info("disk's anchors in origin go, so its next backup may need a decision.")
    if not log.ask(f"Remove '{name}' from the registry?", default=False):
        log.info("Aborted")
        return
    # Anchors first, entry last: an interruption in between leaves the
    # entry, so the same command can be run again.
    pools = survey.source_pools()
    if "rpool" in pools:
        dropped, failed = engine.drop_disk_anchors(info.guid, pools, log)
        log.ok(f"{dropped} anchor(s) of '{name}' dropped from origin")
        if failed:
            log.warn(
                f"{len(failed)} anchor(s) could not be dropped: "
                + f"sudo zark registry forget {info.guid}"
            )
    else:
        log.info(
            "rpool is not imported here: on the installed system run "
            + f"sudo zark registry forget {info.guid}"
        )
    del cfg.known_drives[name]
    cfg.save_drives()
    log.ok(f"'{name}' removed from {cfg.drives_file_path}")
    if "rpool" in pools:
        _leftover_points(cfg, pools, log)


def _forget_guid(cfg: Config, log: Log, guid: str) -> None:
    """Drop the anchors of a drive that has no registry entry (purged,
    replaced, or never registered)."""
    pools = survey.source_pools()
    if "rpool" not in pools:
        log.fatal(
            "rpool is not imported here",
            solutions=[f"Run on the installed system: sudo zark registry forget {guid}"],
        )
    names = engine.disk_anchors(survey.read_origin(pools), guid)
    if not names:
        log.info(f"No anchor of drive GUID {guid} in origin and no registry entry: nothing to do")
        return
    log.warn(f"Drive GUID {guid} has no registry entry and {len(names)} anchor(s) in origin")
    if not log.ask(f"Drop the {len(names)} anchor(s) of drive GUID {guid}?", default=False):
        log.info("Aborted")
        return
    dropped, failed = engine.drop_disk_anchors(guid, pools, log)
    log.ok(f"{dropped} anchor(s) of drive GUID {guid} dropped from origin")
    if failed:
        log.warn(f"{len(failed)} anchor(s) could not be dropped (see above)")
    _leftover_points(cfg, pools, log)


def _leftover_points(cfg: Config, pools: list[str], log: Log) -> None:
    """Backup points left in origin (decision M2F-5; M3R-5): offered for
    removal only when no registered drive remains, since a registered one may
    still need one as the base of its next run."""
    left = engine.leftover_points(survey.read_origin(pools))
    if not left:
        return
    log.info(
        f"{len(left)} backup point(s) left in origin (interrupted runs, or a run whose anchor "
        + "failed):"
    )
    for name in left:
        log.raw(f"      {name}")
    if cfg.known_drives:
        log.info("The next complete backup or prepare of a registered drive removes them")
        return
    if not log.ask("No drive is registered any more. Drop them?", default=False):
        return
    dropped = 0
    for name in left:
        r = sh.run(f"zfs destroy {shlex.quote(name)}", log=log)
        if r.ok:
            dropped += 1
        else:
            log.warn(f"  could not remove {name}: {r.stderr.strip()}")
    log.ok(f"{dropped} backup point(s) dropped from origin")


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
    elif sub == "retention" and len(args) >= 2:
        _retention(cfg, log, args[1], args[2:])
    else:
        log.fatal(f"Unknown registry command: {' '.join(args)}", solutions=[USAGE])
