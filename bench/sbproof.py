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
from dataclasses import dataclass
from pathlib import Path

from bench.firmware import Firmware
from bench.qemu import opt_path
from bench.report import Check

# EFI_GLOBAL_VARIABLE, 8be4df61-93ca-11d2-aa0d-00e098032b8c (systemd v259.5
# src/fundamental/efi-fundamental.h:74-75; efivarfs lists it so on 26.04,
# run 45182632398). The bench had 11d0 until then, and found no such file.
EFIVAR = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"

# Read alongside the variable, so a missing one says why: where efivarfs is
# mounted, and which SecureBoot* names it lists.
EFIVAR_CONTEXT = {
    "efivarfs-mount": "findmnt -J -t efivarfs",
    "efivars-secureboot": "sh -c 'ls -1 /sys/firmware/efi/efivars | grep -i secureboot'",
}

# The line the kernel logs when the firmware reports Secure Boot on, seen
# with these words on 26.04's kernel 7.0.0-44 (run 45120463779).
KERNEL_LINE = re.compile(r"secureboot: Secure boot enabled")

# OVMF's BDS when the image fails verification, as noble's OVMF prints it
# under QEMU: "BdsDxe: failed to load Boot0001 ... Access Denied".
DENIED = re.compile(r"failed to load Boot\w+.*Access Denied")


@dataclass(frozen=True)
class Reading:
    """One command's result in the guest: a failed read is a reading too."""

    rc: int
    out: bytes
    err: str = ""

    def failure(self) -> str:
        """Why the read failed, or "" when it did not."""
        return f"exit {self.rc}: {self.err.strip()[-200:]}" if self.rc != 0 else ""


def guest_checks(mokutil: Reading, efivar: Reading) -> list[Check]:
    """``mokutil --sb-state`` and the SecureBoot variable (4 attribute bytes, then 1)."""
    text = mokutil.out.decode("utf-8", "replace")
    data = efivar.out
    return [
        Check(
            "mokutil",
            mokutil.rc == 0 and "SecureBoot enabled" in text,
            mokutil.failure() or text.strip() or "no output",
        ),
        Check(
            "efivar",
            efivar.rc == 0 and len(data) == 5 and data[4] == 1,
            efivar.failure()
            or f"{len(data)} bytes, value {data[4] if len(data) == 5 else 'unreadable'}",
        ),
    ]


def kernel_check(serial: str) -> Check:
    """The kernel's Secure Boot line in the installed boot's serial log."""
    m = KERNEL_LINE.search(serial)
    return Check("kernel-line", m is not None, m.group(0) if m else "not in the serial log")


def host_check(argv: list[str], fw: Firmware, vars_file: Path) -> Check:
    """
    The installed boot's command line: the secure-boot code read-only, a
    copy of its enrolled variables (never the template itself), SMM and a
    flash only SMM may write. A pass names what it found.
    """
    parts = {
        "code": f"if=pflash,format=raw,unit=0,readonly=on,file={opt_path(fw.code)}" in argv,
        "vars copy": vars_file != fw.vars_template
        and f"if=pflash,format=raw,unit=1,file={opt_path(vars_file)}" in argv,
        "smm": any(a.startswith("q35,smm=on,") for a in argv),
        "secure flash": "driver=cfi.pflash01,property=secure,value=on" in argv,
    }
    missing = [k for k, v in parts.items() if not v]
    if missing:
        return Check("host-argv", False, "missing: " + ", ".join(missing))
    return Check(
        "host-argv",
        True,
        f"code {fw.code} read-only; vars {vars_file}, a copy of {fw.vars_template}; "
        "smm=on; secure flash",
    )


def negative_check(serial: str) -> Check:
    """The unsigned EFI binary was refused by the firmware."""
    m = DENIED.search(serial)
    return Check("negative-control", m is not None, m.group(0) if m else "no Access Denied")
