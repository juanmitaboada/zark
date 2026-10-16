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
The live session's installer probe (M4aF-6, M4b-14).

On Ubuntu's live media, ``ubuntu-desktop-bootstrap.subiquity-server`` probes
the disks on every udev event and, after each pool zark imports, writes a
``block_probe_fail`` or ``disk_probe_fail`` crash report that pops up
"System program problem detected" (seen on eli, M4a handoff §6). zark stops
it for this session before any command that imports pools. ``snap stop``
without ``--disable`` changes nothing for the next boot (snapd 2.75.2
cmd/snap/cmd_services.go:265-282, overlord/servicestate/service_control.go:
131-143); a snap or service that is not there is reported and ignored
(cmd/snap/error.go:124).
"""

from lib import sh
from lib.log import Log

PROBE_SERVICE = "ubuntu-desktop-bootstrap.subiquity-server"
# The commands that import pools, as M4b-14 lists them.
IMPORTING = frozenset(
    {
        "recover",
        "mount",
        "umount",
        "chroot",
        "repair-boot",
        "fix-rpool-mountpoint",
        "clean",
        "purge",
        "prepare",
        "health",
        "explore",
    }
)


def stop_installer_probe(command: str, log: Log) -> bool:
    """Stop the installer's probe service for this session when ``command``
    imports pools and this is a live session; True when it was stopped."""
    if command not in IMPORTING or not sh.is_live_usb():
        return False
    r = sh.run(f"snap stop {PROBE_SERVICE}")
    if r.ok:
        log.info(f"Stopped {PROBE_SERVICE} for this session (its disk probe reports crashes)")
    else:
        log.file(f"[INFO]    {PROBE_SERVICE} not stopped: {(r.stderr or r.output).strip()}")
    return r.ok
