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
How recover lays out the disk of a point whose ``/boot`` was an ext4
partition on origin (M12-5 5a, M12 plan 5.4): the partitions as the
point's record has them, the new ``/boot`` file system, and the tools the
live needs for it.
"""

import shlex
from pathlib import Path
from typing import NamedTuple

from lib import boot_rewrite, sh
from lib.log import Log
from lib.pool_record import BootRecord

_MIB = 1024**2
_GIB = 1024**3


class Part(NamedTuple):
    """One partition recover makes: its number, size in bytes (0 for the
    rest of the disk) and sgdisk type code."""

    number: int
    size: int
    type_code: str
    role: str


# The ESP's size is the recorded one within these bounds (N3): the record
# takes /boot/efi's mount, which can be another disk's ESP; the 26.10
# installer makes about 1.05 GiB, and recover writes the ESP's content anew.
ESP_MIN = _GIB
ESP_MAX = 4 * _GIB


def esp_bytes(boot: BootRecord) -> int:
    """The new ESP's size: the recorded one, at least :data:`ESP_MIN` and at
    most :data:`ESP_MAX`; :data:`ESP_MIN` when none was recorded."""
    return min(max(boot.esp.size_bytes if boot.esp else 0, ESP_MIN), ESP_MAX)


def ext4_partitions(boot: BootRecord) -> list[Part]:
    """An ext4 point's disk, as origin had it (M12 plan 5.4): the ESP
    (:func:`esp_bytes`), ``/boot`` (at least 2 GiB, Linux filesystem as the
    installer sets it), the swap when origin had one, rpool the rest.
    rpool's partition keeps zark's own BF00: nothing reads it back (M12R-8)."""
    parts = [
        Part(1, esp_bytes(boot), "EF00", "esp"),
        Part(2, max(boot.partition.size_bytes, 2 * _GIB), "8300", "boot"),
    ]
    if boot.swap is not None:
        parts.append(Part(3, boot.swap.size_bytes, "8300", "swap"))
    parts.append(Part(len(parts) + 1, 0, "BF00", "rpool"))
    return parts


def sgdisk_commands(parts: list[Part], disk: str) -> list[str]:
    """One ``sgdisk -n -t`` per partition, the first aligned at 1 MiB."""
    return [
        f"sgdisk -n{p.number}:{'1M' if p.number == 1 else '0'}:"
        + (f"+{p.size // 1024}K" if p.size else "0")
        + f" -t{p.number}:{p.type_code} {disk}"
        for p in parts
    ]


def before_rpool_bytes(boot: BootRecord) -> int:
    """The disk space before rpool's partition, alignment slack included."""
    return 2 * _MIB + sum(p.size for p in ext4_partitions(boot))


def mkfs_boot_command(boot: BootRecord, device: str) -> str:
    """The new ``/boot``: exactly the recorded features (``none`` first
    clears mke2fs.conf's defaults, lib/e2p/feature.c:353-358 at e2fsprogs
    1.47.4), the recorded label, a new UUID (M12-3 3a)."""
    label = f"-L {shlex.quote(boot.label)} " if boot.label else ""
    return f"mkfs.ext4 -F -O none,{','.join(boot.features)} {label}{device}"


# What recover runs on the live for a point whose /boot is on ext4 only
# (M12R-8, M13F-5 5a): a bpool point needs none of them.
EXT4_RECOVER_TOOLS = {
    "rsync": "rsync",
    "mkfs.ext4": "e2fsprogs",
    "tune2fs": "e2fsprogs",
    "blkid": "util-linux",
}


def ext4_fstab(fstab: Path, old_uuid: str, new_uuid: str, log: Log) -> list[str]:
    """The restored fstab's ``/boot`` line moved to the new file system
    (M12-3 3a); the error lines."""
    try:
        text = fstab.read_text(encoding="utf-8")
    except OSError as e:
        return [f"fstab: not read ({e}): its /boot line still names {old_uuid}"]
    new, changed = boot_rewrite.rewrite_fstab(text, old_uuid, new_uuid)
    if not changed:
        return [f"fstab: no /boot line names {old_uuid}: /boot may not mount at boot"]
    _ = fstab.write_text(new, encoding="utf-8")
    log.ok(f"fstab /boot UUID updated: {new_uuid}")
    return []


def ext4_crypttab(crypttab: Path, boot: BootRecord, disk: str, log: Log) -> list[str]:
    """The restored crypttab's swap line pointed at the new swap partition,
    every other line as origin had it (M12 plan 5.4); nothing when origin
    had no swap. The error lines."""
    if boot.swap is None:
        return []
    swap_n = next(p.number for p in ext4_partitions(boot) if p.role == "swap")
    partuuid = sh.run(f"blkid -s PARTUUID -o value {sh.part(disk, swap_n)}").output.strip()
    if not partuuid:
        return [
            f"crypttab: the new swap partition's PARTUUID cannot be read ({sh.part(disk, swap_n)})"
        ]
    try:
        text = crypttab.read_text(encoding="utf-8")
    except OSError as e:
        return [f"crypttab: not read ({e}): the swap will not be opened at boot"]
    new, changed = boot_rewrite.rewrite_crypttab(text, partuuid)
    if not changed:
        return ["crypttab: no swap line with a PARTUUID: the swap will not be opened at boot"]
    _ = crypttab.write_text(new, encoding="utf-8")
    log.ok(f"crypttab swap PARTUUID updated: {partuuid}")
    return []
