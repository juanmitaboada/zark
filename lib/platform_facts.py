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
What platform zark runs on, as the self-test records it (M4s-8, M4s-14).

``exact`` is what the commands print; ``series`` is derived from it and is
what certificates match on: Ubuntu's ``VERSION_ID``, the kernel's
``<major>.<minor>``, OpenZFS tools and module without Ubuntu's revision,
util-linux ``<x>.<y>``, systemd's major. Nothing that names the machine
(hostname, machine-id, serials) is read here.
"""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from lib import sh

OS_RELEASE = "/etc/os-release"
CDROM_INFO = "/cdrom/.disk/info"


@dataclass(frozen=True)
class Facts:
    """The platform: exact versions, the series they belong to, and where."""

    exact: dict[str, str]
    series: dict[str, str]
    where: str  # "installed" or "live"
    iso: str | None

    @property
    def zfs_mismatch(self) -> bool:
        """Whether the loaded ZFS module and its tools are different releases
        (Ubuntu's revision ignored, M4s-8)."""
        tools, kmod = self.series.get("zfs", ""), self.series.get("zfs_kmod", "")
        return bool(tools and kmod and tools != kmod)


def _os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = Path(OS_RELEASE).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip().strip('"')
    return out


def _first(cmd: str) -> str:
    r = sh.run(cmd)
    return r.lines[0].strip() if r.ok and r.lines else ""


def _match(pattern: str, text: str) -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else ""


def upstream(version: str) -> str:
    """A package version without the distribution's revision:
    ``zfs-kmod-2.4.1-1ubuntu4`` gives ``2.4.1``."""
    return _match(r"(\d+\.\d+(?:\.\d+)?)", version)


def read() -> Facts:
    """Read the platform once."""
    rel = _os_release()
    zv = sh.run("zfs version")
    zlines = zv.lines if zv.ok else []
    tools = next((ln.strip() for ln in zlines if not ln.startswith("zfs-kmod-")), "")
    kmod = next((ln.strip() for ln in zlines if ln.startswith("zfs-kmod-")), "")
    exact = {
        "ubuntu": f"{rel.get('VERSION', '')}".strip(),
        "kernel": os.uname().release,
        "zfs": tools,
        "zfs_kmod": kmod,
        "util_linux": _match(r"util-linux (\S+)", _first("findmnt --version")),
        "systemd": _match(r"^systemd (.+)$", _first("systemctl --version")),
        "python": ".".join(str(n) for n in sys.version_info[:3]),
        "sgdisk": _match(r"version (\S+)", _first("sgdisk --version")),
    }
    series = {
        "ubuntu": rel.get("VERSION_ID", ""),
        "kernel": _match(r"^(\d+\.\d+)", exact["kernel"]),
        "zfs": upstream(tools),
        "zfs_kmod": upstream(kmod),
        "util_linux": _match(r"^(\d+\.\d+)", exact["util_linux"]),
        "systemd": _match(r"^(\d+)", exact["systemd"]),
    }
    live = sh.is_live_usb()
    iso = None
    if live:
        try:
            lines = Path(CDROM_INFO).read_text(encoding="utf-8", errors="replace").splitlines()
            iso = lines[0].strip() if lines else "unknown"
        except OSError:
            iso = "unknown"
    return Facts(exact, series, "live" if live else "installed", iso)
