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
The firmware: OVMF with Secure Boot enforced and Microsoft's keys enrolled.

Chosen from QEMU's firmware descriptors (``/usr/share/qemu/firmware``) by
what they declare, not by file name, so each runner's own ``ovmf`` package
is used whatever it calls its files.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DESCRIPTOR_DIR = Path("/usr/share/qemu/firmware")
REQUIRED_FEATURES = frozenset({"secure-boot", "enrolled-keys", "requires-smm"})


class FirmwareError(Exception):
    """No descriptor offers the firmware the bench needs."""


@dataclass(frozen=True)
class Firmware:
    """Code and NVRAM template of one firmware, and the descriptor naming them."""

    code: Path
    vars_template: Path
    descriptor: Path


def offers_secure_boot(doc: dict[str, Any]) -> bool:
    """x86_64 q35 UEFI flash with Secure Boot, enrolled keys and SMM."""
    if "uefi" not in (doc.get("interface-types") or []):
        return False
    features = set(doc.get("features") or [])
    if not REQUIRED_FEATURES <= features:
        return False
    mapping = doc.get("mapping")
    if not isinstance(mapping, dict) or mapping.get("device") != "flash":
        return False
    targets = doc.get("targets")
    if not isinstance(targets, list):
        return False
    return any(
        isinstance(t, dict)
        and t.get("architecture") == "x86_64"
        # A q35 machine of any version: the descriptor's patterns are globs.
        and any(fnmatch.fnmatch("pc-q35-99.0", m) for m in t.get("machines", []))
        for t in targets
    )


def pick(descriptors: list[Path]) -> Firmware:
    """
    The first descriptor, in QEMU's order (by file name), for x86_64 q35
    UEFI flash with Secure Boot, enrolled keys and SMM, whose files exist.
    """
    for path in sorted(descriptors, key=lambda p: p.name):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or not offers_secure_boot(doc):
            continue
        mapping = doc["mapping"]
        code = Path(mapping["executable"]["filename"])
        template = Path(mapping["nvram-template"]["filename"])
        if code.is_file() and template.is_file():
            return Firmware(code, template, path)
    raise FirmwareError("no firmware descriptor offers Secure Boot with enrolled keys and SMM")


def find(directory: Path = DESCRIPTOR_DIR) -> Firmware:
    """The runner's firmware for the bench."""
    return pick(sorted(directory.glob("*.json")))


def fresh_vars(fw: Firmware, dest: Path) -> Path:
    """A private copy of the NVRAM template: one machine's variables."""
    shutil.copyfile(fw.vars_template, dest)
    return dest
