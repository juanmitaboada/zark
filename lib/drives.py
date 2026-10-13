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
Drive detection and identification.

Finds connected drives, matches them against known_drives.json,
and provides helpful output for unknown drives.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lib.config import Config, DriveInfo, parse_utc_iso
from lib.identity import (
    DiskIdentity,
    IdentityError,
    by_id_names,
    preferred_by_id,
    protected_disks,
    resolve_disk,
    whole_disk,
)
from lib.log import Log
from lib.registry import SHARED_KEY
from lib.replication import Origin, Ref, shared_kind
from lib.sanoid_retention import guaranteed_lives, guaranteed_overlap_days
from lib.sh import run


@dataclass
class ConnectedDrive:  # pylint: disable=too-many-instance-attributes
    """A physically connected drive that may or may not be known."""

    # fmt: off
    name: str           # pool name (from blkid LABEL or zpool)
    guid: str           # pool GUID
    drive_id: str       # /dev/disk/by-id/ base name
    dev_path: str       # actual device path (e.g. /dev/sda)
    known: bool         # True if in known_drives.json
    guid_changed: bool  # name matches but GUID differs
    renamed: bool       # GUID matches but name differs
    state: str          # "imported", "exported", "unknown"
    model: str = ""
    size: str = ""
    transport: str = ""
    # fmt: on

    @property
    def status_label(self) -> str:
        """Return a human-friendly status label for this drive."""
        if self.known and not self.guid_changed and not self.renamed:
            return "KNOWN"
        if self.guid_changed:
            return "GUID CHANGED"
        if self.renamed:
            return "RENAMED"
        return "UNKNOWN"

    @property
    def registration_json(self) -> str:
        """Return a JSON snippet for registering this drive in known_drives.json."""
        return f'  "{self.name}": {{"guid": "{self.guid}", "drive_id": "{self.drive_id}"}}'


SYSTEM_POOLS = {"rpool", "bpool"}


def get_drive_id(dev_path: str) -> str:
    """Stable /dev/disk/by-id name of the disk holding ``dev_path`` ("" if none).

    Accepts any spelling of the device (kernel name, partition, by-id,
    by-path): it is resolved to the whole disk first, so the answer does not
    depend on how the operator typed it.
    """
    disk = whole_disk(dev_path)
    return preferred_by_id(by_id_names(disk)) if disk else ""


def get_drive_info(dev_path: str) -> tuple[str, str, str]:
    """Model, size and transport of the disk holding ``dev_path``."""
    disk = whole_disk(dev_path)
    if not disk:
        return "", "", ""
    model = run(f"lsblk -dn -o MODEL {disk}").output.strip()
    size = run(f"lsblk -dn -o SIZE {disk}").output.strip()
    tran = run(f"lsblk -dn -o TRAN {disk}").output.strip()
    return model, size, tran


def scan_connected_drives(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    cfg: Config,
    log: Log,
) -> list[ConnectedDrive]:
    """
    Scan for all connected ZFS pools (excluding system pools).
    Match against known_drives.json and classify each.
    """
    # Build lookup tables
    known_by_name: dict[str, DriveInfo] = cfg.known_drives
    known_by_guid: dict[str, DriveInfo] = {}
    for info in cfg.known_drives.values():
        known_by_guid[info.guid] = info

    results: list[ConnectedDrive] = []
    seen_pools: set[str] = set()

    # 1. Scan via blkid (finds exported pools)
    r = run("blkid -t TYPE=zfs_member -o export")
    if r.ok:
        current: dict[str, str] = {}
        for line in r.stdout.splitlines() + [""]:
            line = line.strip()
            if not line:
                if current:
                    _process_blkid_entry(
                        current,
                        cfg,
                        known_by_name,
                        known_by_guid,
                        seen_pools,
                        results,
                        log,
                    )
                    current = {}
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                current[k.lower()] = v

    # 2. Check imported pools (may not show in blkid)
    r = run("zpool list -H -o name")
    if r.ok:
        for pool_name in r.lines:
            pool_name = pool_name.strip()
            if pool_name in SYSTEM_POOLS or pool_name in seen_pools:
                continue
            guid = run(f"zpool get -H -o value guid {pool_name}").output
            _add_pool(
                pool_name,
                guid,
                "",
                cfg,
                known_by_name,
                known_by_guid,
                seen_pools,
                results,
                log,
                state="imported",
            )

    # 3. Also check known drives by drive_id presence
    for name, info in cfg.known_drives.items():
        if name in seen_pools:
            continue
        by_id_path = Path(f"/dev/disk/by-id/{info.drive_id}")
        if by_id_path.exists() or Path(f"{by_id_path}-part1").exists():
            _add_pool(
                name,
                info.guid,
                info.drive_id,
                cfg,
                known_by_name,
                known_by_guid,
                seen_pools,
                results,
                log,
                state="exported",
            )

    return results


def _process_blkid_entry(
    entry: dict[str, str],
    cfg: Config,
    known_by_name: dict[str, DriveInfo],
    known_by_guid: dict[str, DriveInfo],
    seen: set[str],
    results: list[ConnectedDrive],
    log: Log,
):
    devname = entry.get("devname", "")
    guid = entry.get("uuid", "")
    label = entry.get("label", "")

    if not label or not guid or not devname:
        return
    if label in SYSTEM_POOLS:
        return

    drive_id = get_drive_id(devname)
    _add_pool(
        label,
        guid,
        drive_id,
        cfg,
        known_by_name,
        known_by_guid,
        seen,
        results,
        log,
        state="exported",
        dev_path=devname,
    )


def _add_pool(  # pylint: disable=too-many-arguments,too-many-locals,too-many-positional-arguments
    name: str,
    guid: str,
    drive_id: str,
    cfg: Config,  # pylint: disable=unused-argument
    known_by_name: dict[str, DriveInfo],
    known_by_guid: dict[str, DriveInfo],
    seen: set[str],
    results: list[ConnectedDrive],
    log: Log,  # pylint: disable=unused-argument
    state: str = "exported",
    dev_path: str = "",
):
    del cfg, log  # Unused but may be needed for future enhancements
    if name in seen:
        return
    seen.add(name)

    known = False
    guid_changed = False
    renamed = False

    if name in known_by_name:
        known_info = known_by_name[name]
        if known_info.guid == guid:
            known = True
        else:
            guid_changed = True
        if not drive_id:
            drive_id = known_info.drive_id
    elif guid in known_by_guid:
        renamed = True
        known_info = known_by_guid[guid]
        if not drive_id:
            drive_id = known_info.drive_id

    model, size, transport = "", "", ""
    if dev_path:
        model, size, transport = get_drive_info(dev_path)
    elif drive_id:
        by_id = Path(f"/dev/disk/by-id/{drive_id}")
        if by_id.exists():
            real = by_id.resolve()
            model, size, transport = get_drive_info(str(real))

    results.append(
        ConnectedDrive(
            name=name,
            guid=guid,
            drive_id=drive_id or "<unknown>",
            dev_path=dev_path,
            known=known,
            guid_changed=guid_changed,
            renamed=renamed,
            state=state,
            model=model,
            size=size,
            transport=transport,
        ),
    )


def backup_device(drive: ConnectedDrive) -> str | None:
    """Exact vdev partition of a connected backup drive, for device-exact imports.

    Prefers ``/dev/disk/by-id/<drive_id>-part1``; falls back to the by-id of
    the disk holding the partition blkid reported, then to that partition
    itself. None when the drive cannot be located on a device (a registered
    drive that is not connected).
    """
    if drive.drive_id and drive.drive_id != "<unknown>":
        part1 = Path(f"/dev/disk/by-id/{drive.drive_id}-part1")
        if part1.exists():
            return str(part1)
    if drive.dev_path:
        disk = whole_disk(drive.dev_path)
        by_id = preferred_by_id(by_id_names(disk)) if disk else ""
        if by_id and Path(f"/dev/disk/by-id/{by_id}-part1").exists():
            return f"/dev/disk/by-id/{by_id}-part1"
        return drive.dev_path
    return None


def select_drive(
    drives: list[ConnectedDrive],
    log: Log,
    known_only: bool = True,
) -> ConnectedDrive | None:
    """
    Interactive drive selection.
    If known_only=True, filters to known drives.
    Auto-selects if only one option.
    """
    candidates = [d for d in drives if d.known] if known_only else drives

    if not candidates:
        return None

    if len(candidates) == 1:
        d = candidates[0]
        log.ok(f"Using drive: {d.name} ({d.model or d.drive_id}, {d.size})")
        return d

    options = []
    for d in candidates:
        label = f"{d.name}  ({d.model or d.drive_id}, {d.size}) [{d.state}]"
        options.append(label)

    idx = log.ask_choice("Multiple backup drives detected:", options)
    selected = candidates[idx]
    log.ok(f"Selected: {selected.name}")
    return selected


def validate_external_block_device(
    dev: str,
    log: Log,
    *,
    command: str,
    own_pool: str | None = None,
) -> DiskIdentity:
    """
    Refuse to operate on something that is not a safe external whole disk.

    Common safety check shared by the destructive commands (``prepare``,
    ``purge``, destructive ``health``). Aborts via ``log.fatal`` when:

    - ``dev`` is empty, does not exist, or is not a block device;
    - ``dev`` is a partition (the whole disk must be named explicitly);
    - the disk holds a vdev of an imported pool (rpool/bpool on the running
      system, or any other pool except ``own_pool``), a mounted filesystem
      (the live USB, the ESP, a LUKS container) or active swap.

    Returns the resolved :class:`DiskIdentity`, so the caller uses the same
    canonical disk the check was made against.
    """
    if not dev:
        log.fatal(
            "No device specified",
            solutions=[f"Usage: sudo zark {command} /dev/disk/by-id/<disk>"],
        )
    if not Path(dev).exists():
        log.fatal(f"{dev} does not exist")
    if not Path(dev).is_block_device():
        log.fatal(f"{dev} is not a block device")
    try:
        ident = resolve_disk(dev)
    except IdentityError as e:
        log.fatal(f"Cannot use {dev}", causes=[str(e)])

    # The pool labelled on this very disk may be imported (purge of a mounted
    # backup); its vdevs are on this disk by definition and do not protect it.
    if own_pool is None and ident.label and ident.label.name not in SYSTEM_POOLS:
        own_pool = ident.label.name
    reason = protected_disks(own_pool=own_pool).get(ident.disk)
    if reason:
        log.fatal(
            f"{dev} ({ident.disk}) is not an external backup disk. Refusing.",
            causes=[f"{ident.disk} {reason}"],
        )
    return ident


# ── last_backup_at staleness helpers ─────────────────────────────────────────
#
# backup reports, for every other registered drive, the days since its last
# backup (its RPO). A drive with no anchor in origin depends on the snapshots
# it shares with the source: shared_snapshot_line checks the ones recorded
# at its last complete backup (M3 decision 5e), or falls back to the horizon
# of lib.sanoid_retention.guaranteed_overlap_days (hallazgo 21). What the
# next backup does with a shared snapshot depends on the drive's newest one
# (lib.replication): the base of an incremental is the drive's newest
# snapshot, and on a zark 2 drive that is the backup point, which origin
# keeps only as an anchor. So a zark 2 drive without anchors never stays
# incremental: its next backup asks to roll each dataset back to the newest
# shared snapshot (the drive's backup points after it go) or to resend it in
# full (M3R-1). Only a drive written by zark 1.x, whose newest snapshot is a
# syncoid_* residue origin still holds, continues incrementally.


def drive_staleness_days(info: DriveInfo, *, now: datetime | None = None) -> int | None:
    """Return how many days ago this drive was last backed up.

    Returns ``None`` when the drive has no recorded backup, so callers
    can distinguish "we don't know" from "we know it's recent". A
    malformed ``last_backup_at`` value is also treated as ``None`` —
    we'd rather skip the check than fatal on a typo. ``now`` is exposed
    only so tests can pass a fixed clock.
    """
    if not info.last_backup_at:
        return None
    last = parse_utc_iso(info.last_backup_at)
    if last is None:
        return None
    if now is None:
        now = datetime.now(UTC)
    delta = now - last
    return max(0, delta.days)


def is_drive_stale(
    info: DriveInfo,
    threshold_days: int,
    *,
    now: datetime | None = None,
) -> bool:
    """True when the drive's last backup is older than ``threshold_days``.

    Drives without a ``last_backup_at`` (older entries, freshly
    prepared drives) return False — see module docstring for the
    rationale.
    """
    age = drive_staleness_days(info, now=now)
    if age is None:
        return False
    return age > threshold_days


def shared_snapshot_line(
    info: DriveInfo,
    horizon_days: int | None,
    *,
    origin: dict[str, Origin] | None = None,
    lives: dict[str, float] | None = None,
    now: datetime | None = None,
) -> str:
    """What a drive with no anchor in origin can expect from its next backup.

    With a record of what it shared at its last complete backup (M3
    decision 5e) the line comes from checking those snapshots against
    ``origin`` now; without one (an entry no complete backup has written
    since), from the guaranteed horizon of hallazgo 21, worded as a
    condition: after a recover origin may share nothing at all.
    """
    head = f"{info.name}: no anchor in origin"
    record = info.extra.get(SHARED_KEY)
    if isinstance(record, dict) and origin is not None:
        return _from_record(head, record, origin, lives)
    age = drive_staleness_days(info, now=now)
    if age is None:
        return f"{head}; no backup recorded"
    # Without a record the drive may be a 1.x one (incremental) or a 2.x one
    # (rollback), so the line covers both (M3F-4 4a).
    continues = (
        "its next backup continues from the newest of them, asking first to roll the drive "
        + "back where its own newest snapshot is not shared (its backup points after it go)"
    )
    if horizon_days is None:
        return (
            f"{head}: if it still shares snapshots with the source, {continues}; "
            + "otherwise it resends in full"
        )
    left = horizon_days - age
    if left > 0:
        return (
            f"{head}: if it still shares snapshots with the source, {continues}, for at "
            + f"least {left} more day(s) ({horizon_days} days after its last backup); "
            + "otherwise it resends in full"
        )
    return (
        f"{head}, {age} days since its last backup: past the {horizon_days}-day horizon, "
        + "its next backup may need to resend some datasets in full"
    )


def no_anchor_lines(drives: list[DriveInfo], origin: dict[str, Origin], log: Log) -> list[str]:
    """``shared_snapshot_line`` for each drive with no anchor in origin,
    reading sanoid.conf once; H21's horizon only when a drive has no record."""
    if not drives:
        return []
    lives = guaranteed_lives()
    legacy = any(not isinstance(i.extra.get(SHARED_KEY), dict) for i in drives)
    horizon = guaranteed_overlap_days(log) if legacy else None
    return [shared_snapshot_line(i, horizon, origin=origin, lives=lives) for i in drives]


# A record exists only after a zark 2 backup, whose point is the drive's
# newest snapshot: continuing from a shared snapshot costs a rollback (M3R-1).
ROLLBACK_NOTE = (
    ": its next backup asks to roll the drive back to them (its backup points after them go) "
    + "or to resend in full"
)


def _from_record(
    head: str,
    record: dict[str, list[list[object]]],
    origin: dict[str, Origin],
    lives: dict[str, float] | None,
) -> str:
    """The 5e line: every recorded dataset still in origin must keep one of
    the snapshots it shared; the date is when the first of them may go."""
    lost: list[str] = []
    best: list[tuple[float, str]] = []  # (until, snapshot) per dataset
    for rel, snaps in sorted(record.items()):
        o = origin.get(rel)
        if o is None:
            continue  # gone from origin: nothing of it is sent any more
        end = _dataset_end(rel, snaps, o, lives)
        if end is None:
            lost.append(rel)
        else:
            best.append(end)
    if lost:
        return (
            f"{head}; {len(lost)} dataset(s) share no snapshot with it: "
            + "its next backup resends them in full"
        )
    if not best:
        return f"{head}; nothing it holds is still in origin"
    if any(math.isnan(b[0]) for b in best):
        return f"{head}; it still shares snapshots with it{ROLLBACK_NOTE}"
    until, name = min(best)
    if math.isinf(until):
        return (
            f"{head}; it still shares snapshots with it until they are removed with zark prune"
            + ROLLBACK_NOTE
        )
    day = datetime.fromtimestamp(until, UTC).strftime("%Y-%m-%d")
    return f"{head}; it still shares snapshots with it until about {day} ({name}){ROLLBACK_NOTE}"


def _dataset_end(
    rel: str,
    snaps: list[list[object]],
    o: Origin,
    lives: dict[str, float] | None,
) -> tuple[float, str] | None:
    """A dataset's longest-lived shared snapshot origin still holds, as
    (until, "rel@name"); NaN when kept but undated; None when none is kept."""
    ends: list[tuple[float, str]] = []
    for entry in snaps:
        name, guid, creation = str(entry[0]), str(entry[1]), entry[2]
        if o.snap_by_guid(guid) is None or not isinstance(creation, int):
            continue
        kind = shared_kind(name)
        if kind == "syncoid":
            end = math.inf  # zark prune keeps a residue a drive may need (M3F-3)
        elif lives is not None and kind in lives:
            end = creation + lives[kind]
        else:
            end = math.nan  # kept, but no date can be given
        ends.append((end, f"{rel}@{name}"))
    if not ends:
        return None
    dated = [e for e in ends if not math.isnan(e[0])]
    return max(dated) if dated else (math.nan, ends[-1][1])


# ── What a drive shares with origin, and its versions (M3 decision 5e) ──────


def record_backup(
    info: DriveInfo,
    version: str,
    shared: dict[str, list[Ref]] | None,
    meta: dict[str, str],
) -> None:
    """Record a complete backup's version and what the drive now shares
    with origin (``None``: unknown, the previous record stays). Entries
    written before 5e get the preparation fields from the drive's own
    ``org.zark:prepared-at`` / ``prepared-version`` when it has them."""
    info.extra["last_backup_version"] = version
    for key, prop in (("prepared_at", "prepared-at"), ("prepared_version", "prepared-version")):
        if key not in info.extra and meta.get(prop):
            info.extra[key] = meta[prop]
    if shared is not None:
        info.extra[SHARED_KEY] = {
            rel: [[r.name, r.guid, r.creation] for r in refs]
            for rel, refs in sorted(shared.items())
        }
