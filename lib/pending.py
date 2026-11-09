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
Drives prepared but not read back yet (M20-4 4a): pending_drives.json.

When ``prepare``'s read-back is skipped or times out because apt or dpkg
holds their locks (M18-11), the drive holds a complete first backup, but
zark registers only a drive it has read back (M18-13 13a). The entry
``prepare`` would have written goes here instead, beside known_drives.json,
and the drive's anchors in origin are kept, until ``zark registry verify
<name>`` reads it back and moves the entry to the registry, or the entry is
dropped (``registry forget``, ``purge``, another ``prepare`` of the disk,
or a read-back that fails).

The file::

    {"schema_version": 1,
     "pending": {"<pool>": {"entry": {<the registry entry>},
                            "prepared_at": "<UTC>",
                            "device": "/dev/disk/by-id/<disk>-part1"}}}

``entry`` is validated as a registry entry (:func:`lib.registry.parse`) and
is what ``verify`` writes to known_drives.json. known_drives.json keeps its
format: its top level is the entries, so nothing else can go there.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib import registry
from lib.config import Config, DriveInfo, drive_info
from lib.log import Log

FILE_NAME = "pending_drives.json"
SCHEMA_VERSION = 1


class PendingError(Exception):
    """pending_drives.json is unreadable, malformed, or fails validation."""


@dataclass(frozen=True)
class Pending:
    """One drive waiting for its read-back."""

    name: str  # the pool's name
    entry: dict[str, Any]  # the registry entry prepare would have written
    prepared_at: str
    device: str  # the disk's first partition by id, as prepare read it back

    @property
    def guid(self) -> str:
        """The pool's GUID."""
        return str(self.entry["guid"])

    @property
    def info(self) -> DriveInfo:
        """The entry as the registry would hold it."""
        return drive_info(self.name, self.entry)


def path(cfg: Config) -> Path:
    """Where the file is, beside known_drives.json."""
    return cfg.config_dir / FILE_NAME


def _parse(text: str, where: Path) -> dict[str, Pending]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise PendingError(
            f"{where}: invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}"
        ) from e
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        raise PendingError(f"{where}: not a schema {SCHEMA_VERSION} pending-drives file")
    items = doc.get("pending")
    if not isinstance(items, dict):
        raise PendingError(f"{where}: 'pending' must be an object of pool names")
    out: dict[str, Pending] = {}
    for name, item in items.items():
        if not isinstance(item, dict):
            raise PendingError(f"{where}: '{name}' must be an object")
        try:
            entry = registry.parse(json.dumps({name: item.get("entry")}), where)[name]
        except registry.RegistryError as e:
            raise PendingError(str(e)) from e
        prepared_at, device = item.get("prepared_at"), item.get("device")
        if not isinstance(prepared_at, str) or not isinstance(device, str) or not device:
            raise PendingError(f"{where}: '{name}' needs 'prepared_at' and 'device' strings")
        out[name] = Pending(name, entry, prepared_at, device)
    return out


def read(cfg: Config) -> dict[str, Pending]:
    """The pending drives; an absent file is none. Raises :class:`PendingError`."""
    where = path(cfg)
    if not where.exists():
        return {}
    try:
        text = where.read_text(encoding="utf-8")
    except OSError as e:
        raise PendingError(f"{where}: cannot read: {e}") from e
    return _parse(text, where)


def write(cfg: Config, items: dict[str, Pending]) -> None:
    """Validate and atomically replace the file (removed when ``items`` is empty)."""
    where = path(cfg)
    if not items:
        where.unlink(missing_ok=True)
        return
    doc = {
        "schema_version": SCHEMA_VERSION,
        "pending": {
            name: {"entry": p.entry, "prepared_at": p.prepared_at, "device": p.device}
            for name, p in sorted(items.items())
        },
    }
    text = json.dumps(doc, indent=2) + "\n"
    _ = _parse(text, where)  # round-trip validation before anything touches disk
    registry.write_text_atomic(where, text)


def add(cfg: Config, item: Pending) -> None:
    """Record ``item``, replacing an entry of the same name."""
    items = read(cfg)
    items[item.name] = item
    write(cfg, items)


def remove(cfg: Config, name: str) -> bool:
    """Drop ``name``'s entry; False when there was none."""
    items = read(cfg)
    if name not in items:
        return False
    del items[name]
    write(cfg, items)
    return True


def by_guid(items: dict[str, Pending], guid: str) -> Pending | None:
    """The pending entry whose pool has ``guid``."""
    return next((p for p in items.values() if p.guid == guid), None)


def among(cfg: Config, guids: set[str], log: Log) -> list[str]:
    """The names of the waiting drives whose pool GUID is in ``guids`` (the
    connected ones, for backup's message); none, with a warning, when the
    file cannot be read."""
    try:
        items = read(cfg)
    except PendingError as e:
        log.warn(f"Cannot read the drives waiting for a read-back: {e}")
        return []
    return sorted(p.name for p in items.values() if p.guid in guids)
