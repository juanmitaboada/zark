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
Whether this system is exposed to the 24.04 to 26.04 upgrade that stops
rpool's mounts and the session in the middle of dpkg's configure phase
(M12P.3, M12C-6, M12R-9): Ubuntu 24.04 with zfsutils-linux 2.2, an
encrypted rpool whose datasets the shell generator mounts from
``/etc/zfs/zfs-list.cache/rpool``, so every mount unit is bound to
``zfs-load-key-rpool.service``. 26.04's package builds the C generator,
which names the key unit ``zfs-load-key@rpool.service``: the first
daemon-reload after the unpack stops every mount bound to the old name
(reported upstream against zfs-linux).

Read-only: nothing here changes the system.
"""

from pathlib import Path

from lib import platform_facts, sh

GENERATOR = Path("/usr/lib/systemd/system-generators/zfs-mount-generator")
LIST_CACHE = Path("/etc/zfs/zfs-list.cache/rpool")
KEY_UNIT = "zfs-load-key-rpool.service"
# A dataset the 24.04 installer always creates and mounts through the cache.
PROBE_UNIT = "var-lib-dpkg.mount"

WARNING = (
    "Upgrading this Ubuntu 24.04 with an encrypted ZFS root to 26.04 stops rpool's "
    + "mounts and the session halfway through (the ZFS key unit is renamed). If it "
    + "happens: log in on a text console (Ctrl+Alt+F3), run sudo dpkg --configure -a, "
    + "then reboot. See docs/UPGRADING.md"
)


def _encrypted_rpool() -> bool:
    """rpool holds an encryption root (the installer's ZFS with encryption)."""
    r = sh.run("zfs get -H -o value -r -t filesystem encryptionroot rpool")
    return r.ok and any(v.strip() not in ("", "-") for v in r.lines)


def generator_is_shell(path: Path | None = None) -> bool:
    """The installed generator is 2.2's shell script, not 2.4's C binary."""
    try:
        with open(path or GENERATOR, "rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def binds_to(unit: str = PROBE_UNIT) -> list[str]:
    """``systemctl show -p BindsTo`` of ``unit``: the units it is bound to."""
    r = sh.run(f"systemctl show -p BindsTo --value {unit}")
    return r.output.split() if r.ok else []


def exposed() -> str:
    """Why this system is exposed, "" when it is not (any condition false,
    or unreadable)."""
    if platform_facts.ubuntu_release() != "24.04":
        return ""
    version = sh.run("dpkg-query -W -f='${Version}' zfsutils-linux").output.strip()
    if not version.startswith("2.2."):
        return ""
    try:
        cached = LIST_CACHE.stat().st_size > 0
    except OSError:
        cached = False
    if not cached or not _encrypted_rpool():
        return ""
    if not generator_is_shell() or KEY_UNIT not in binds_to():
        return ""
    return (
        f"Ubuntu 24.04, zfsutils-linux {version}, an encrypted rpool mounted through "
        + f"{LIST_CACHE} with its units bound to {KEY_UNIT}"
    )
