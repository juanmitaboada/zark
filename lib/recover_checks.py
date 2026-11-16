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
What recover refuses before anything is erased because of the point's
record of origin's pools (M12-8, M13R-1, M13R-2): a point that needs a
newer zark, one whose record cannot be read, one whose ``/boot`` is on
ext4 without a readable record to lay the disk out from. And a live
session not started in UEFI mode (M27R-2), checked at recover's start.
"""

import os
import re
from typing import NoReturn

from lib import pool_record
from lib.boot_layout import Layout
from lib.config import VERSION, zark_command
from lib.log import Log

# Where the kernel shows the firmware's variables in a session booted in
# UEFI mode with its runtime services on (the suite points it elsewhere).
EFIVARS = "/sys/firmware/efi/efivars"
# This process's mount table, where efivarfs is looked for at EFIVARS.
MOUNTS = "/proc/self/mounts"


def _efivarfs_mounted() -> bool:
    """Whether MOUNTS has an efivarfs line at EFIVARS; an unreadable table
    is not mounted (recover cannot tell, and step 15 would fail)."""
    try:
        with open(MOUNTS, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    for line in lines:
        fields = line.split()
        # The kernel writes a space in a path as \040 (fs/proc_namespace.c).
        if len(fields) >= 3 and fields[2] == "efivarfs":
            where = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[1])
            if where == EFIVARS:
                return True
    return False


def refuse_no_uefi(log: Log) -> None:
    """FATAL when this session was not started in UEFI mode (M27R-2): the
    boot chain recover installs (grub-install, shim's reconfigure) writes
    the firmware's boot entry through efivarfs, and its mount is fatal at
    step 15, after the restore. Checked first, when nothing has changed:
    efivarfs mounted at EFIVARS, as a UEFI session mounts it at boot
    (M27R-7); the directory alone is there with runtime services off."""
    if _efivarfs_mounted():
        return
    present = os.path.isdir(EFIVARS)
    causes = [
        "The live USB was started in legacy BIOS or CSM mode",
        "The firmware's runtime services are off (noefi or efi=noruntime)",
    ]
    solutions = [
        "Nothing was changed: no disk was touched",
        "Start the live session again in UEFI mode (the USB's UEFI entry in the "
        + f"firmware's boot menu), then run {zark_command()} recover",
    ]
    if present:
        causes.append("The live session did not mount efivarfs there")
        solutions.append(
            f"If it was started in UEFI mode: sudo mount -t efivarfs efivarfs {EFIVARS}, "
            + f"then run {zark_command()} recover"
        )
    log.fatal(
        "This live session was not started in UEFI mode ("
        + (f"efivarfs is not mounted at {EFIVARS}" if present else f"{EFIVARS} is missing")
        + ")",
        causes=causes,
        solutions=solutions,
    )


def refuse_min_recover(min_recover: str, written_by: str, log: Log) -> None:
    """FATAL when this zark does not meet ``min_recover`` or cannot read it
    (M12-8); nothing when it does."""
    if pool_record.recovers(min_recover):
        return
    title = (
        f"This point needs zark {min_recover} or later to recover"
        if pool_record.zark_version_tuple(min_recover) is not None
        else "This point's record names an oldest zark this one cannot read: " + min_recover
    )
    log.fatal(
        title,
        causes=[f"It was written by zark {written_by or '(not recorded)'}; this is zark {VERSION}"],
        solutions=[
            "Run recover from a live session with a newer zark",
            "Nothing was erased",
        ],
    )


def refuse_unrecoverable(rec: pool_record.PoolRecord | None, layout: Layout, log: Log) -> None:
    """Refuse, before anything is erased, a point this zark must not
    recover: one whose ``min_recover`` it does not meet or cannot read
    (M12-8), and one whose ``/boot`` is on ext4 but which has no record to
    lay the disk out from (M13R-1) or whose ``boot`` entry cannot be read.
    ``layout`` is :func:`lib.pool_record.point_layout`'s."""
    if rec is None:
        if layout == "ext4":
            log.fatal(
                "This point's /boot is on ext4, and it has no record of origin's disk",
                causes=[
                    "The drive holds a copy of /boot or says its system has /boot on ext4, "
                    + "but this point does not record origin's pools and partitions",
                    "An interrupted run and a carried sanoid snapshot have no record; "
                    + "without one recover cannot lay the disk out",
                ],
                solutions=[
                    "Choose a point of a complete backup run",
                    "Nothing was erased",
                ],
            )
        return
    refuse_min_recover(rec.min_recover, rec.written_by, log)
    if layout == "ext4" and rec.boot is None:
        log.fatal(
            "This point's record of its /boot on ext4 cannot be read",
            causes=[
                f"Written by zark {rec.written_by or '(not recorded)'}; this is zark {VERSION}",
                f"Its /boot record is unreadable: {rec.boot_problem or 'absent'}",
                "recover lays the disk out from that record",
            ],
            solutions=[
                "Choose another point",
                "Run recover from a live session with a newer zark",
                "Nothing was erased",
            ],
        )


def refuse_unreadable(point: str, e: pool_record.RecordError, log: Log) -> NoReturn:
    """FATAL for a record that is there but cannot be read (M13R-2): by the
    zark it names when it names one this zark does not meet, else as
    unreadable. Never a pass: the record decides how the disk is laid out."""
    refuse_min_recover(e.min_recover, e.written_by, log)
    log.fatal(
        f"The record of origin's pools at {point} cannot be read: {e}",
        causes=[
            f"It was written by zark {e.written_by or '(not recorded)'}; this is zark {VERSION}",
            "recover lays the disk out from this record; without it the point's layout "
            + "is not known",
        ],
        solutions=[
            "Choose another point",
            "Run recover from a live session with a newer zark",
            "Nothing was erased",
        ],
    )
