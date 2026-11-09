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
zark registry - inspect and repair known_drives.json without hand edits.

  zark registry list            every entry, whether its disk is connected,
                                and whether the pool GUID on that disk matches;
                                on the installed system also the drives that
                                hold anchors in origin without an entry, and
                                the backup points left in origin
  zark registry forget <name>   remove an entry (e.g. a lost or retired disk)
                                and that disk's anchors in origin (rpool
                                bookmarks, bpool snapshots)
  zark registry forget <GUID>   the same by pool GUID; for a GUID with no
                                entry, only its anchors; a drive waiting for
                                its read-back (verify) is forgotten the same way
  zark registry fix [<name>]    rewrite drive_id from the connected disk that
                                carries the registered pool GUID, and write
                                every missing key
  zark registry verify <name>   read back a drive prepare could not read back
                                (apt was running) and register it
  zark registry retention <name> [window=N] [max_age=N|off] [margin=N]
                                show or change the drive's retention: days
                                in which everything is kept, age beyond which
                                nothing is (off by default), and the free
                                space a backup tries to leave (% of the drive)

All writes go through lib.registry (validated, atomic).
"""

import os
import shlex
from pathlib import Path

from lib import anchors, cli_spec, pending, registry, sh, status_file, survey
from lib.cleanup import Cleanup, prompt_eject_or_attach
from lib.config import Config, zark_command
from lib.drives import (
    Location,
    Presence,
    drive_staleness_days,
    locate_registered,
    reconcile,
    scan_connected_drives,
)
from lib.identity import DiskIdentity, names_of, whole_disk
from lib.log import Log
from lib.zfs import ZFS

USAGE = (
    f"Usage: {zark_command()} registry list | forget <name|GUID> | fix [<name>]"
    + " | verify <name> | retention <name> [window=N] [max_age=N|off] [margin=N]"
)
# Command-line names of the retention settings (lib.registry.RETENTION_LIMITS).
SPEC = cli_spec.command("registry")
_RETENTION_ARGS = {
    SPEC.word("window"): "window_days",
    SPEC.word("max_age"): "max_age_days",
    SPEC.word("margin"): "margin_pct",
}


def _connected_disk_for_guid(guid: str, cfg: Config, log: Log) -> DiskIdentity | None:
    """The connected disk that carries the pool with ``guid``, with its by-id names."""
    for d in scan_connected_drives(cfg, log):
        if d.guid != guid:
            continue
        paths = [d.dev_path] if d.dev_path else []
        if not paths:
            vdevs = sh.run(f"zpool list -vHP {d.name}")
            paths = [
                line.strip().split("\t")[0]
                for line in (vdevs.lines[1:] if vdevs.ok else [])
                if line.strip().startswith("/dev/")
            ]
        for path in paths:
            disk = whole_disk(path)
            if disk:
                return names_of(disk)
    return None


def _state_text(found: Location, log: Log) -> str:
    """How `registry list` says where a drive is: by its label, never by a name."""
    if found.presence is Presence.CONNECTED:
        return f"{log.G}connected, GUID matches{log.N}"
    if found.presence is Presence.OTHER_POOL and found.label is not None:
        return (
            f"{log.R}a different pool is at {found.name}: {found.label.name} "
            + f"(GUID {found.label.guid}), not this drive{log.N}"
        )
    if found.presence is Presence.NO_LABEL:
        return f"{log.Y}another disk is at {found.name}: no ZFS label, not this drive{log.N}"
    return "not connected"


def _waiting(cfg: Config, log: Log) -> dict[str, pending.Pending]:
    """The drives waiting for their read-back; none, with a warning, when
    the file cannot be read."""
    try:
        return pending.read(cfg)
    except pending.PendingError as e:
        log.warn(f"Cannot read the drives waiting for a read-back: {e}")
        return {}


def _list_waiting(waiting: dict[str, pending.Pending], log: Log) -> None:
    """The drives prepare wrote but could not read back (M20-6 6b)."""
    if not waiting:
        return
    log.info("Waiting for a read-back (prepared while apt was running; not registered):")
    for name, p in sorted(waiting.items()):
        here = "connected" if Path(p.device).exists() else "not connected"
        log.raw(f"  {log.W}{name}{log.N}")
        log.raw(f"      guid:        {p.guid}")
        log.raw(f"      drive_id:    {p.entry['drive_id']}")
        log.raw(f"      prepared at: {p.prepared_at}")
        log.raw(f"      state:       {here}")
        log.raw(f"      finish it:   {zark_command()} registry verify {name}")


def _list(cfg: Config, log: Log) -> None:
    waiting = _waiting(cfg, log)
    if not cfg.known_drives:
        log.info(f"No drives registered in {cfg.drives_file_path}")
        _list_waiting(waiting, log)
        return
    log.info(f"Registry: {cfg.drives_file_path}")
    for name, info in sorted(cfg.known_drives.items()):
        age = drive_staleness_days(info)
        last = f"{info.last_backup_at} ({age} day(s) ago)" if age is not None else "never"
        if info.drive_id == "<unknown>":
            state = f"{log.Y}drive_id unknown - run: zark registry fix {name}{log.N}"
        else:
            state = _state_text(locate_registered(info), log)
        log.raw(f"  {log.W}{name}{log.N}")
        log.raw(f"      guid:        {info.guid}")
        log.raw(f"      drive_id:    {info.drive_id}")
        if info.enclosure_id:
            log.raw(f"      enclosure:   {info.enclosure_id}")
        log.raw(f"      last backup: {last}")
        log.raw(f"      autoeject:   {'yes' if info.autoeject else 'no'}")
        log.raw(f"      retention:   {_policy_text(cfg, name)}")
        log.raw(f"      state:       {state}")
    _list_waiting(waiting, log)
    _list_origin(cfg, log, waiting)


def _policy_text(cfg: Config, name: str) -> str:
    p = cfg.drive_policy(name)
    max_age = f"{p.max_age_days} days" if p.max_age_days else "off"
    return f"window {p.window_days} days, max_age {max_age}, margin {p.margin_pct}%"


def _retention(cfg: Config, log: Log, name: str, settings: list[str]) -> None:
    """Show or change one drive's retention settings (M3 decision 2b)."""
    if name not in cfg.known_drives:
        log.fatal(f"'{name}' is not registered", solutions=[f"{zark_command()} registry list"])
    info = cfg.known_drives[name]
    if settings:
        current = info.extra.get(registry.RETENTION_KEY, {})
        values: dict[str, int] = dict(current) if isinstance(current, dict) else {}
        for item in settings:
            key, _, raw = item.partition("=")
            if key not in _RETENTION_ARGS or not raw:
                log.fatal(f"Unknown retention setting: {item}", solutions=[USAGE])
            n = (
                0
                if key == SPEC.word("max_age") and raw == "off"
                else int(raw)
                if raw.isdigit()
                else -1
            )
            values[_RETENTION_ARGS[key]] = n
        try:
            registry.check_retention(values, f"entry '{name}'")
        except registry.RegistryError as e:
            log.fatal(str(e), solutions=[USAGE])
        info.extra[registry.RETENTION_KEY] = values
        cfg.save_drives()
        log.ok(f"{name}: retention changed")
    log.info(f"{name}: {_policy_text(cfg, name)}")


def _list_origin(cfg: Config, log: Log, waiting: dict[str, pending.Pending]) -> None:
    """Anchors of drives without an entry, and backup points left in origin."""
    pools = survey.source_pools()
    if "rpool" not in pools:
        log.info("rpool is not imported here: anchors in origin not checked")
        return
    origin = survey.read_origin(pools)
    registered = {info.guid for info in cfg.known_drives.values()}
    for guid in sorted(anchors.anchored_disks(origin) - registered):
        n = len(anchors.disk_anchors(origin, guid))
        held = pending.by_guid(waiting, guid)
        if held is not None:
            log.info(f"  drive GUID {guid}: {n} anchor(s) in origin, waiting for a read-back")
            log.raw(f"      ({held.name}): {zark_command()} registry verify {held.name}")
            continue
        log.warn(f"  drive GUID {guid}: {n} anchor(s) in origin, no registry entry")
        log.raw(f"      drop them: {zark_command()} registry forget {guid}")
    left = anchors.leftover_points(origin)
    try:
        owned = anchors.point_owners(pools)
    except survey.ReadError:
        owned = {}  # unknown owners: none is taken for a recovery's
    restored = anchors.restored_points(origin, owned)
    names = {info.guid: name for name, info in cfg.known_drives.items()}
    for guid in sorted(set(restored.values())):
        n = sum(1 for g in restored.values() if g == guid)
        log.info(
            f"  {n} backup point(s) restored by recover (anchored for {names.get(guid, guid)}): "
            + "the next complete backup of a registered drive removes them"
        )
    left = [p for p in left if p not in restored]
    if left:
        log.info(
            f"  {len(left)} backup point(s) left in origin (interrupted runs, or a run whose "
            + "anchor failed)"
        )


def _forget(cfg: Config, log: Log, name: str) -> None:
    waiting = _waiting(cfg, log)
    held = waiting.get(name) or (pending.by_guid(waiting, name) if name.isdigit() else None)
    registered = name in cfg.known_drives or any(i.guid == name for i in cfg.known_drives.values())
    if held is not None and not registered:
        _forget_pending(cfg, log, held)
        return
    if name.isdigit() and name not in cfg.known_drives:
        # Pool names cannot start with a digit: a number is a pool GUID.
        named = [n for n, info in cfg.known_drives.items() if info.guid == name]
        if not named:
            _forget_guid(cfg, log, name)
            return
        name = named[0]
    if name not in cfg.known_drives:
        log.fatal(f"'{name}' is not registered", solutions=[f"{zark_command()} registry list"])
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
        dropped, failed = anchors.drop_disk_anchors(info.guid, pools, log)
        log.ok(f"{dropped} anchor(s) of '{name}' dropped from origin")
        if failed:
            log.warn(
                f"{len(failed)} anchor(s) could not be dropped: "
                + f"{zark_command()} registry forget {info.guid}"
            )
    else:
        log.info(
            "rpool is not imported here: on the installed system run "
            + f"{zark_command()} registry forget {info.guid}"
        )
    del cfg.known_drives[name]
    cfg.save_drives()
    log.ok(f"'{name}' removed from {cfg.drives_file_path}")
    if "rpool" in pools:
        _leftover_points(cfg, pools, log)


def _forget_pending(cfg: Config, log: Log, held: pending.Pending) -> None:
    """Drop a drive waiting for its read-back: its anchors, then its entry
    in pending_drives.json. The disk is not touched."""
    log.warn(f"Forgetting '{held.name}', waiting for a read-back (GUID {held.guid})")
    log.info("The disk and its data are not touched: its anchors in origin and its")
    log.info("pending registration go; the drive can then only be wiped and prepared again.")
    if not log.ask(f"Forget '{held.name}'?", default=False):
        log.info("Aborted")
        return
    pools = survey.source_pools()
    if "rpool" not in pools:
        log.fatal(
            "rpool is not imported here",
            solutions=[
                f"Run on the installed system: {zark_command()} registry forget {held.name}"
            ],
        )
    dropped, failed = anchors.drop_disk_anchors(held.guid, pools, log)
    log.ok(f"{dropped} anchor(s) of '{held.name}' dropped from origin")
    if failed:
        log.warn(
            f"{len(failed)} anchor(s) could not be dropped: "
            + f"{zark_command()} registry forget {held.guid}"
        )
        return  # the entry stays, so the same command can be run again
    _ = pending.remove(cfg, held.name)
    status_file.refresh_or_warn(cfg, Log(os.devnull), "registry", warn=log)
    log.ok(f"'{held.name}' removed from {pending.path(cfg)}")


def _verify(cfg: Config, log: Log, name: str) -> None:  # pylint: disable=too-many-branches
    """Read back a drive prepare could not read back (apt was running) and
    register it with the entry prepare recorded (M20-5 5a)."""
    if sh.is_live_usb():
        log.fatal(
            "registry verify runs on the installed system (the anchors and the registry are there)"
        )
    pools = survey.source_pools()
    if "rpool" not in pools:
        log.fatal("rpool is not imported here: run it on the installed system")
    try:
        waiting = pending.read(cfg)
    except pending.PendingError as e:
        log.fatal(f"Cannot read the drives waiting for a read-back: {e}")
    if name not in waiting:
        log.fatal(
            f"'{name}' is not waiting for a read-back",
            causes=[f"Waiting: {', '.join(sorted(waiting)) or 'none'}"],
            solutions=[f"{zark_command()} registry list"],
        )
    held = waiting[name]
    if name in cfg.known_drives or any(i.guid == held.guid for i in cfg.known_drives.values()):
        log.fatal(
            f"'{name}' (GUID {held.guid}) is already in the registry: "
            + "a later prepare registered it",
            solutions=[f"Drop the pending entry: {zark_command()} registry forget {name}"],
        )
    if not Path(held.device).exists():
        log.fatal(
            f"{name}'s disk is not connected",
            solutions=[f"Plug {held.entry['drive_id']} in and run this again"],
        )
    zfs = ZFS(log)
    if zfs.pool_exists(name):
        log.fatal(
            f"A pool named '{name}' is imported",
            solutions=[
                f"Export it first ({zark_command()} umount for a zark mount), then run this again"
            ],
        )
    if not anchors.disk_anchors(survey.read_origin(pools), held.guid):
        log.warn(
            f"{name} has no anchor in origin any more: its next backup will ask to roll back "
            + "or to send in full"
        )
        if not log.ask(f"Register {name} anyway?", default=False, on_eof=False):
            log.info("Nothing changed")
            return
    cleanup = Cleanup(log)
    cleanup.register()
    rb = zfs.verify_exported_pool_readback(
        name,
        device=held.device,
        cleanup=cleanup,
        skip_note="It would be reported as not done; the drive stays waiting.",
        guid=held.guid,
    )
    if rb.ok:
        # The registry first, the pending entry last: an interruption between
        # them leaves the drive registered, and step one names the leftover.
        cfg.known_drives[name] = held.info
        cfg.save_drives()
        _ = pending.remove(cfg, name)
        status_file.refresh_or_warn(cfg, Log(os.devnull), "registry", warn=log)
        log.banner_ok(
            "DRIVE REGISTERED",
            [
                f"Pool:     {log.W}{name}{log.N}  (GUID: {held.guid})",
                f"Drive ID: {log.W}{held.entry['drive_id']}{log.N}",
                f"Prepared: {log.W}{held.prepared_at}{log.N}",
                "",
                f"Registered in: {log.W}{cfg.drives_file_path}{log.N}",
            ],
        )
        prompt_eject_or_attach(
            held.device.removesuffix("-part1"),
            name,
            log,
            default_eject=False,
            autoeject=held.info.autoeject,
        )
        return
    if rb.skipped:
        log.banner_error(
            "DRIVE NOT VERIFIED YET",
            [
                *rb.describe(again=False),
                "",
                f"Run {zark_command()} registry verify {name} again once apt has ended.",
            ],
        )
        raise SystemExit(1)
    left = anchors.drop_anchors_of(held.guid, log)
    _ = pending.remove(cfg, name)
    status_file.refresh_or_warn(cfg, Log(os.devnull), "registry", warn=log)
    advice = (
        [f"The drive prepared as {name} is no longer there; its pending entry is dropped."]
        if rb.foreign
        else [f"Wipe and retry: {zark_command()} purge {held.device.removesuffix('-part1')}"]
    )
    log.banner_error(
        "DRIVE NOT VERIFIED",
        [
            *rb.describe(again=False),
            "",
            "The drive has NOT been registered.",
            *advice,
            *([left] if left else []),
        ],
    )
    raise SystemExit(1)


def _forget_guid(cfg: Config, log: Log, guid: str) -> None:
    """Drop the anchors of a drive that has no registry entry (purged,
    replaced, or never registered)."""
    pools = survey.source_pools()
    if "rpool" not in pools:
        log.fatal(
            "rpool is not imported here",
            solutions=[f"Run on the installed system: {zark_command()} registry forget {guid}"],
        )
    names = anchors.disk_anchors(survey.read_origin(pools), guid)
    if not names:
        log.info(f"No anchor of drive GUID {guid} in origin and no registry entry: nothing to do")
        return
    log.warn(f"Drive GUID {guid} has no registry entry and {len(names)} anchor(s) in origin")
    if not log.ask(f"Drop the {len(names)} anchor(s) of drive GUID {guid}?", default=False):
        log.info("Aborted")
        return
    dropped, failed = anchors.drop_disk_anchors(guid, pools, log)
    log.ok(f"{dropped} anchor(s) of drive GUID {guid} dropped from origin")
    if failed:
        log.warn(f"{len(failed)} anchor(s) could not be dropped (see above)")
    _leftover_points(cfg, pools, log)


def _leftover_points(cfg: Config, pools: list[str], log: Log) -> None:
    """Backup points left in origin (decision M2F-5; M3R-5): offered for
    removal only when no registered drive remains, since a registered one may
    still need one as the base of its next run."""
    left = anchors.leftover_points(survey.read_origin(pools))
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
            log.fatal(f"'{name}' is not registered", solutions=[f"{zark_command()} registry list"])
    changed = 0
    for name in names:
        info = cfg.known_drives[name]
        ident = _connected_disk_for_guid(info.guid, cfg, log)
        if ident is None:
            log.info(f"{name}: pool GUID {info.guid} not found on any connected disk - skipped")
        elif reconcile(info, ident, log):
            changed += 1
        else:
            log.ok(f"{name}: drive_id already correct ({info.drive_id})")
    cfg.save_drives()  # also writes every key of every entry
    log.ok(f"Registry written ({changed} drive_id change(s)): {cfg.drives_file_path}")


def run(args: list[str]) -> None:
    """Main entry point for 'zark registry'."""
    log = Log()
    cfg = Config.load()
    sub = args[0] if args else SPEC.word("list")
    cfg.check_registry(log, fatal=sub != SPEC.word("list"))

    log.banner("DRIVE REGISTRY", "known_drives.json")
    if sub == SPEC.word("list"):
        _list(cfg, log)
    elif sub == SPEC.word("forget") and len(args) == 2:
        _forget(cfg, log, args[1])
    elif sub == SPEC.word("fix") and len(args) <= 2:
        _fix(cfg, log, args[1] if len(args) == 2 else None)
    elif sub == SPEC.word("verify") and len(args) == 2:
        _verify(cfg, log, args[1])
    elif sub == SPEC.word("retention") and len(args) >= 2:
        _retention(cfg, log, args[1], args[2:])
    else:
        log.fatal(f"Unknown registry command: {' '.join(args)}", solutions=[USAGE])
