"""
Drive detection and identification.

Finds connected drives, matches them against known_drives.json,
and provides helpful output for unknown drives.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from lib.config import Config, DriveInfo
from lib.log import Log
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
    """Get /dev/disk/by-id/ name for a device (excluding wwn/scsi)."""
    dev_name = Path(dev_path).name
    # Remove partition suffix to get base disk
    base = re.sub(r"p?\d+$", "", dev_name)

    r = run("ls -la /dev/disk/by-id/")
    if not r.ok:
        return ""

    for line in r.lines:
        if f"../../{base}" not in line:
            continue
        if "wwn-" in line or "scsi-" in line:
            continue
        parts = line.split()
        if len(parts) >= 9:
            candidate = parts[8]
            # Verify it points to the base disk, not a partition
            if f"../../{base}" == parts[-1]:
                return candidate

    # Fallback: accept wwn/scsi
    for line in r.lines:
        if f"../../{base}" == line.split()[-1]:
            parts = line.split()
            if len(parts) >= 9:
                return parts[8]
    return ""


def get_drive_info(dev_path: str) -> tuple[str, str, str]:
    """Get model, size, transport for a device."""
    base = Path(dev_path).name
    base = re.sub(r"p?\d+$", "", base)

    model = run(f"lsblk -dn -o MODEL /dev/{base}").output.strip()
    size = run(f"lsblk -dn -o SIZE /dev/{base}").output.strip()
    tran = run(f"lsblk -dn -o TRAN /dev/{base}").output.strip()
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
    entry: dict,
    cfg: Config,
    known_by_name: dict,
    known_by_guid: dict,
    seen: set,
    results: list,
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
    known_by_name: dict,
    known_by_guid: dict,
    seen: set,
    results: list,
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


def validate_external_block_device(dev: str, log: Log, *, command: str) -> None:
    """
    Refuse to operate on something that is not a safe external block device.

    Common safety check shared by `zark prepare` and `zark purge` (and any
    future destructive command). Aborts via ``log.fatal`` if any of the
    following holds:

    - ``dev`` is empty.
    - ``dev`` does not exist or is not a block device.
    - ``dev`` looks like an internal NVMe drive (refuses ``nvme`` paths).
    - ``dev`` appears to be the parent of the running root filesystem.

    Args:
        dev: device path the user supplied (e.g. ``/dev/sdb``).
        log: logger; ``log.fatal`` aborts execution.
        command: name of the calling command (``"prepare"``, ``"purge"``…)
            used only to render the usage hint on the empty-device error.
    """
    if not dev:
        log.fatal(
            "No device specified",
            solutions=[f"Usage: sudo ./zark {command} /dev/sdX"],
        )
    if not Path(dev).exists():
        log.fatal(f"{dev} does not exist")
    if not Path(dev).is_block_device():
        log.fatal(f"{dev} is not a block device")
    if "nvme" in dev:
        log.fatal(f"{dev} looks like an internal NVMe drive. Refusing.")

    root_dev = run("lsblk -no PKNAME $(findmnt -n -o SOURCE /) 2>/dev/null | head -1").output
    if root_dev and root_dev in dev:
        log.fatal(f"{dev} appears to be the system drive. Refusing.")
