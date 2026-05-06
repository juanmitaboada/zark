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
from pathlib import Path

VERSION = "1.0.7"


@dataclass
class DriveInfo:
    """A known backup drive."""

    name: str  # pool name (dict key)
    guid: str  # zpool GUID (decimal string)
    drive_id: str  # /dev/disk/by-id/ identifier


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
                    )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                # Will be reported by the calling command
                cfg._load_error = str(e)

        return cfg

    def save_drives(self):
        """Write known_drives.json back to disk."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        drives_file = self.config_dir / "known_drives.json"
        data = {}
        for name, info in self.known_drives.items():
            data[name] = {"guid": info.guid, "drive_id": info.drive_id}
        drives_file.write_text(json.dumps(data, indent=2) + "\n")

    @property
    def drives_file_path(self) -> Path:
        """Path to known_drives.json (even if it doesn't exist yet)."""
        return self.config_dir / "known_drives.json"

    def drive_registration_line(self, name: str, guid: str, drive_id: str) -> str:
        """Human-readable JSON snippet for manual addition."""
        return f'  "{name}": {{"guid": "{guid}", "drive_id": "{drive_id}"}}'
