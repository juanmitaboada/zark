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

from lib.retention import Policy

KNOWN_KEYS = ("guid", "drive_id", "last_backup_at", "autoeject")
UNKNOWN_DRIVE_ID = "<unknown>"
# Optional per-drive retention settings (M3 decision 2b): key → (min, max).
RETENTION_KEY = "retention"
RETENTION_LIMITS = {"window_days": (1, 3650), "max_age_days": (0, 36500), "margin_pct": (0, 50)}
# What the drive shared with origin at its last complete backup (M3 decision 5e):
# {"<dataset>": [["<snapshot>", "<guid>", <creation>], ...]}.
SHARED_KEY = "shared"
# Information only (M3 decision 5e): strings, absent when unknown.
TEXT_KEYS = ("prepared_at", "prepared_version", "last_backup_version")


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
    if RETENTION_KEY in entry:
        check_retention(entry[RETENTION_KEY], where)
    for key in TEXT_KEYS:
        if key in entry and not isinstance(entry[key], str):
            raise RegistryError(f"{where}: {key!r} must be a string")
    if SHARED_KEY in entry:
        check_shared(entry[SHARED_KEY], where)


def check_shared(value: Any, where: str) -> None:
    """Validate a ``shared`` object; raises RegistryError."""
    if not isinstance(value, dict):
        raise RegistryError(f"{where}: 'shared' must be an object of datasets")
    for rel, snaps in value.items():
        if not isinstance(snaps, list):
            raise RegistryError(f"{where}: 'shared' of {rel!r} must be a list")
        for snap in snaps:
            ok = (
                isinstance(snap, list)
                and len(snap) == 3
                and isinstance(snap[0], str)
                and isinstance(snap[1], str)
                and snap[1].isdigit()
                and isinstance(snap[2], int)
                and not isinstance(snap[2], bool)
            )
            if not ok:
                raise RegistryError(
                    f"{where}: 'shared' of {rel!r}: each entry must be [name, guid, creation]"
                )


def check_retention(value: Any, where: str) -> None:
    """Validate a ``retention`` object; raises RegistryError."""
    if not isinstance(value, dict):
        raise RegistryError(f"{where}: 'retention' must be an object")
    for key, n in value.items():
        if key not in RETENTION_LIMITS:
            raise RegistryError(f"{where}: unknown retention setting {key!r}")
        lo, hi = RETENTION_LIMITS[key]
        if not isinstance(n, int) or isinstance(n, bool) or not lo <= n <= hi:
            raise RegistryError(f"{where}: retention {key!r} must be a whole number {lo}-{hi}")
    window = value.get("window_days", Policy().window_days)
    max_age = value.get("max_age_days", 0)
    if 0 < max_age <= window:
        raise RegistryError(f"{where}: retention 'max_age_days' must exceed 'window_days'")


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
