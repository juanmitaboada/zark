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
Configuration for zark.

known_drives.json location search order:
  1. $ZARK_CONFIG_DIR/known_drives.json  (env override)
  2. <zark_root>/etc/known_drives.json   (portable — USB drive)
  3. /etc/zark/known_drives.json         (system install)

If none found, all drives are treated as unknown and the user is
guided to create the file.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.12"


def now_utc_iso() -> str:
    """Current time as ISO-8601 UTC, second precision, ``Z`` suffix.

    Single source of truth for the timestamp written to
    ``known_drives.json`` under ``last_backup_at``. Kept here (rather
    than in ``commands/backup.py``) so tests can monkeypatch a fixed
    clock without reaching into a command module.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(s: str) -> datetime | None:
    """Inverse of :func:`now_utc_iso`. Returns ``None`` on any parse error.

    Tolerant of both the ``Z`` suffix and of an explicit ``+00:00``
    (``datetime.isoformat()`` emits the latter when ``tzinfo`` is set).
    Anything else returns ``None`` so the caller can decide what to do
    with a malformed entry without raising into the user's face.
    """
    if not s:
        return None
    candidate = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class DriveInfo:
    """A known backup drive.

    ``last_backup_at`` is an ISO-8601 UTC string (``YYYY-MM-DDTHH:MM:SSZ``)
    written by ``zark backup`` when a backup completes successfully.
    ``None`` means the drive has never been backed up since this field
    became part of the layout. New entries also start with ``None`` and
    auto-populate on the first successful backup.
    """

    name: str  # pool name (dict key)
    guid: str  # zpool GUID (decimal string)
    drive_id: str  # /dev/disk/by-id/ identifier
    last_backup_at: str | None = None  # ISO-8601 UTC, see module docstring
    autoeject: bool = False  # if True, the eject prompt times out and applies
    #                          the command's default after EJECT_TIMEOUT_SECONDS;
    #                          if False (default), the prompt waits for input.


@dataclass
class Config:
    """Runtime configuration."""

    # Where we found the config (or where to create it)
    config_dir: Path = field(default_factory=lambda: Path("/etc/zark"))
    known_drives: dict[str, DriveInfo] = field(default_factory=dict)

    # Defaults
    source_pool: str = "rpool"
    log_file: str = "/var/log/zark.log"
    version: str = VERSION

    # Populated by load() if known_drives.json existed but failed to parse.
    # The caller is expected to surface this to the user; declared here so
    # it satisfies type-checking and isn't created dynamically.
    _load_error: str | None = None

    @staticmethod
    def zark_root() -> Path:
        """Root directory of the zark project (where the 'zark' script lives)."""
        return Path(
            os.path.dirname(
                os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "zark")),
            ),
        )

    @staticmethod
    def default_config_dir() -> Path:
        """Find config dir in priority order."""
        # 1. Environment override
        env = os.environ.get("ZARK_CONFIG_DIR")
        if env and Path(env).is_dir():
            return Path(env)

        # 2. Portable: <zark_root>/etc/
        portable = Config.zark_root() / "etc"
        if (portable / "known_drives.json").exists():
            return portable

        # 3. System: /etc/zark/
        system = Path("/etc/zark")
        if (system / "known_drives.json").exists():
            return system

        # No known_drives.json exists yet (fresh install or first run).
        # Pick the appropriate default for the layout:
        #   - System install (.deb): zark_root is /usr/share/zark, which
        #     is read-only as far as dpkg is concerned. The user's config
        #     belongs in /etc/zark, where postinst created the directory.
        #   - Portable: zark_root is wherever the suite was extracted to
        #     (USB pendrive, /opt, ~/bin, ...). Keep config alongside.
        if Config.zark_root() == Path("/usr/share/zark"):
            return system
        return portable

    @classmethod
    def load(cls) -> "Config":
        """Load configuration. Never fails — returns empty config if no file."""
        cfg = cls()
        cfg.config_dir = cls.default_config_dir()

        drives_file = cfg.config_dir / "known_drives.json"
        if drives_file.exists():
            try:
                data = json.loads(drives_file.read_text())
                for name, info in data.items():
                    cfg.known_drives[name] = DriveInfo(
                        name=name,
                        guid=str(info["guid"]),
                        drive_id=str(info["drive_id"]),
                        # Optional field; may be absent on older files
                        # or freshly-prepared drives. Anything that is
                        # not a string is normalized to None so
                        # downstream code can treat it uniformly.
                        last_backup_at=(
                            str(info["last_backup_at"])
                            if isinstance(info.get("last_backup_at"), str)
                            else None
                        ),
                        # Optional opt-in: when true, the eject prompt for
                        # this drive times out and applies the command's
                        # default. Absent/!bool -> False (prompt waits).
                        autoeject=bool(info.get("autoeject", False)),
                    )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                # Will be reported by the calling command
                cfg._load_error = str(e)

        return cfg

    def save_drives(self):
        """Write known_drives.json back to disk.

        ``last_backup_at`` is emitted only when populated; absent fields
        keep on-disk JSON minimal and avoid implying a never-populated
        field is set.
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)
        drives_file = self.config_dir / "known_drives.json"
        data: dict[str, dict[str, str | bool]] = {}
        for name, info in self.known_drives.items():
            entry: dict[str, str | bool] = {
                "guid": info.guid,
                "drive_id": info.drive_id,
            }
            if info.last_backup_at:
                entry["last_backup_at"] = info.last_backup_at
            if info.autoeject:
                entry["autoeject"] = True
            data[name] = entry
        drives_file.write_text(json.dumps(data, indent=2) + "\n")

    @property
    def drives_file_path(self) -> Path:
        """Path to known_drives.json (even if it doesn't exist yet)."""
        return self.config_dir / "known_drives.json"

    def drive_autoeject(self, pool: str) -> bool:
        """Whether ``pool`` opts into the timed eject prompt.

        False for unregistered pools (recover/mount on a drive not in the
        registry) and for registered drives that did not enable it — the
        eject prompt then waits for the operator, as it always has.
        """
        info = self.known_drives.get(pool)
        return bool(info and info.autoeject)

    def drive_registration_line(self, name: str, guid: str, drive_id: str) -> str:
        """Human-readable JSON snippet for manual addition."""
        return f'  "{name}": {{"guid": "{guid}", "drive_id": "{drive_id}"}}'
