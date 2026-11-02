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
Which boot layout a system has (M12-1 1a).

Two layouts are backed up: ``bpool``, the 24.04 and 26.04 installers'
(``/boot`` on a pool of its own), and ``ext4``, the 26.10 installer's on
UEFI (``/boot`` a plain ext4 partition on rpool's disk, no bpool;
subiquity ``requirements.py:133-138`` at 26.10's installer). Anything
else is refused with its reason.

The ext4 layout is read at every ``backup`` and ``prepare``, not once at
``setup``: an fstab mount that failed at boot leaves ``/boot`` an empty
directory of the root, and a ``/boot`` without a kernel of this root
would make a point that cannot boot (M12R-7).
"""

from dataclasses import dataclass
from typing import Literal

from lib import sh
from lib.identity import disks_under
from lib.initrd import installed_kernels

Layout = Literal["bpool", "ext4"]

# The dataset of origin's rpool that holds a copy of an ext4 /boot, one
# per boot environment (M12-2 2e).
BOOT_COPY = "zark-boot"


def boot_copy(be: str) -> str:
    """The copy of ``/boot`` for boot environment ``be``."""
    return f"rpool/ROOT/{be}/{BOOT_COPY}"


@dataclass(frozen=True)
class BootMount:
    """``/boot`` as ``findmnt`` shows it when it is a mount of its own."""

    fstype: str
    source: str
    uuid: str


def boot_mount() -> BootMount | None:
    """``/boot``'s mount, or None when it is a directory of the root."""
    r = sh.run("findmnt -n -o FSTYPE,SOURCE,UUID /boot")
    fields = r.output.split() if r.ok else []
    if len(fields) < 2:
        return None
    return BootMount(fields[0], fields[1], fields[2] if len(fields) > 2 else "")


def ext4_problems(mount: BootMount, rpool_disks: set[str], root: str = "") -> list[str]:
    """Why an ext4 ``/boot`` is not the installer's layout; [] when it is.

    ``rpool_disks`` are the whole disks under rpool's vdevs: the installer
    puts ``/boot`` on a partition of that disk. ``root`` prefixes ``/boot``
    and ``/lib/modules`` (tests).
    """
    problems: list[str] = []
    if not mount.source.startswith("/dev/"):
        problems.append(f"/boot's source {mount.source} is not a block device")
    elif not rpool_disks:
        problems.append("rpool's disk cannot be read, so /boot's place cannot be checked")
    elif not disks_under(mount.source) & rpool_disks:
        problems.append(f"/boot ({mount.source}) is not on rpool's disk")
    if not installed_kernels(root):
        problems.append(
            "/boot holds no kernel of this root (a vmlinuz-<version> with its "
            + "/lib/modules/<version>): it may not have been mounted at boot"
        )
    return problems
