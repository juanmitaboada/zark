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
known_drives.json: strict parsing, schema validation and atomic writes.

The registry is parsed strictly: a syntax error or a schema violation
raises :class:`RegistryError` with the file path and position, and the
caller decides whether that is fatal. Writes serialise every key of every
entry (``autoeject`` and ``last_backup_at`` included), validate the result
by parsing it back, and replace the file atomically (temporary file in the
same directory, fsync, ``os.replace``, fsync of the directory), so a crash
or a full disk never leaves a truncated or half-written registry.

Unknown keys inside an entry are preserved on rewrite, so a newer zark can
add fields without an older one dropping them.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

KNOWN_KEYS = ("guid", "drive_id", "last_backup_at", "autoeject")
UNKNOWN_DRIVE_ID = "<unknown>"


class RegistryError(Exception):
    """known_drives.json is unreadable, malformed, or fails validation."""


def _check_entry(path: Path, name: str, entry: Any) -> None:
    where = f"{path}: entry '{name}'"
    if not isinstance(entry, dict):
        raise RegistryError(f"{where}: must be an object, got {type(entry).__name__}")
    guid = entry.get("guid")
    if isinstance(guid, int) and not isinstance(guid, bool):
        guid = str(guid)
    if not isinstance(guid, str) or not guid.isdigit():
        raise RegistryError(f"{where}: 'guid' must be a decimal pool GUID, got {guid!r}")
    drive_id = entry.get("drive_id")
    if not isinstance(drive_id, str) or not drive_id.strip():
        raise RegistryError(f"{where}: 'drive_id' must be a non-empty string")
    if "autoeject" in entry and not isinstance(entry["autoeject"], bool):
        raise RegistryError(f"{where}: 'autoeject' must be true or false")
    last = entry.get("last_backup_at")
    if last is not None and not isinstance(last, str):
        raise RegistryError(f"{where}: 'last_backup_at' must be a string or null")


def parse(text: str, path: Path) -> dict[str, dict[str, Any]]:
    """Parse and validate registry text. Raises RegistryError."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RegistryError(
            f"{path}: invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}",
        ) from e
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: top level must be an object of pool names")
    for name, entry in data.items():
        if not name:
            raise RegistryError(f"{path}: empty pool name")
        _check_entry(path, name, entry)
    return data


def load(path: Path) -> dict[str, dict[str, Any]]:
    """Read and validate ``path``; an absent file is an empty registry."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RegistryError(f"{path}: cannot read: {e}") from e
    return parse(text, path)


def serialize(entries: dict[str, dict[str, Any]], path: Path) -> str:
    """Render entries with every known key, then validate the result."""
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(entries):
        entry = entries[name]
        full: dict[str, Any] = {
            "guid": str(entry["guid"]),
            "drive_id": entry["drive_id"],
            "last_backup_at": entry.get("last_backup_at"),
            "autoeject": bool(entry.get("autoeject", False)),
        }
        for key, value in entry.items():
            if key not in KNOWN_KEYS:
                full[key] = value
        out[name] = full
    text = json.dumps(out, indent=2) + "\n"
    parse(text, path)  # round-trip validation before anything touches disk
    return text


def write_atomic(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Validate and atomically replace ``path`` with ``entries``."""
    text = serialize(entries, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
