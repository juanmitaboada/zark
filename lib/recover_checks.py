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
record of origin's pools (M12-8, M13R-1, M13R-2, M13F-3): a point that
needs a newer zark, one whose record cannot be read, one whose ``/boot``
is on ext4 without a record to lay the disk out from.
"""

from typing import NoReturn

from lib import pool_record
from lib.boot_layout import Layout
from lib.config import VERSION
from lib.log import Log


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
    (M12-8), one whose ``/boot`` is on ext4 but which has no record to lay
    the disk out from (M13R-1), and any ext4 point, which this build does
    not lay out (M13F-3). ``layout`` is :func:`lib.pool_record.point_layout`'s."""
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
    if layout == "ext4":
        log.fatal(
            "This point's /boot is on ext4, which this zark does not recover",
            causes=[
                f"Written by zark {rec.written_by or '(not recorded)'} for a system with /boot "
                + "on an ext4 partition and no bpool",
                *(
                    [f"Its /boot record is unreadable: {rec.boot_problem}"]
                    if rec.boot_problem
                    else []
                ),
            ],
            solutions=[
                "Run recover with a zark that recovers the ext4 layout",
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
