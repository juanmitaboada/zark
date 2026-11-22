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
The proof that Secure Boot was on: three readings in the guest and on the
host, and a negative control in the same job. Any one missing fails.
"""

from __future__ import annotations

import re

from bench.firmware import Firmware
from bench.report import Check

EFIVAR = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d0-aa0d-00e098032b8c"

# The line the kernel logs when the firmware reports Secure Boot on. Its
# exact wording on 26.04 is not verified yet: the first install's serial
# log settles it.
KERNEL_LINE = re.compile(r"secureboot: Secure boot enabled")

# OVMF's BDS when the image fails verification, as noble's OVMF prints it
# under QEMU: "BdsDxe: failed to load Boot0001 ... Access Denied".
DENIED = re.compile(r"failed to load Boot\w+.*Access Denied")


def guest_checks(mokutil: str, efivar: bytes) -> list[Check]:
    """``mokutil --sb-state`` and the SecureBoot variable (4 attribute bytes, then 1)."""
    return [
        Check("mokutil", "SecureBoot enabled" in mokutil, mokutil.strip() or "no output"),
        Check(
            "efivar",
            len(efivar) == 5 and efivar[4] == 1,
            f"{len(efivar)} bytes, value {efivar[4] if len(efivar) == 5 else 'unreadable'}",
        ),
    ]


def kernel_check(serial: str) -> Check:
    """The kernel's Secure Boot line in the installed boot's serial log."""
    m = KERNEL_LINE.search(serial)
    return Check("kernel-line", m is not None, m.group(0) if m else "not in the serial log")


def host_check(argv: list[str], fw: Firmware) -> Check:
    """The installed boot's command line: the secure-boot code, SMM, a secure flash."""
    joined = " ".join(argv)
    parts = {
        "code": f"file={fw.code}" in joined,
        "smm": "smm=on" in joined,
        "secure flash": "driver=cfi.pflash01,property=secure,value=on" in argv,
    }
    missing = [k for k, v in parts.items() if not v]
    return Check("host-argv", not missing, "missing: " + ", ".join(missing) if missing else "")


def negative_check(serial: str) -> Check:
    """The unsigned EFI binary was refused by the firmware."""
    m = DENIED.search(serial)
    return Check("negative-control", m is not None, m.group(0) if m else "no Access Denied")
