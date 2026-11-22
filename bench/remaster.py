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
The ISO's remaster: its ``grub.cfg`` edited, nothing else.

The live's GRUB and kernel are signed and stay as they are; ``grub.cfg``
is not signed, so editing it keeps Secure Boot. The first menu entry gets
the arguments the bench needs before the ``---`` separator (what follows
``---`` is carried over to the installed system's command line), and
``splash`` is dropped so plymouth does not take the serial console.
"""

from __future__ import annotations

import re
from pathlib import Path

GRUB_CFG = "/boot/grub/grub.cfg"

# autoinstall: subiquity confirms the install only with this word on the
# kernel command line (subiquity 26.04, controllers/install.py:616-618).
# ds=nocloud: cloud-init reads the CIDATA seed without probing for it.
INSTALL_ARGS = ("autoinstall", "ds=nocloud", "console=tty0", "console=ttyS0,115200n8")
DROPPED_ARGS = frozenset({"splash"})


class RemasterError(Exception):
    """grub.cfg does not have the shape the edit expects."""


def _edit_linux_line(line: str, args: tuple[str, ...]) -> str:
    indent = line[: len(line) - len(line.lstrip())]
    tokens = [t for t in line.split() if t not in DROPPED_ARGS]
    added = [a for a in args if a not in tokens]
    cut = tokens.index("---") if "---" in tokens else len(tokens)
    return indent + " ".join(tokens[:cut] + added + tokens[cut:])


def edit_grub_cfg(text: str, args: tuple[str, ...] = INSTALL_ARGS, timeout_s: int = 1) -> str:
    """
    The first menu entry's ``linux`` line with ``args`` added and ``splash``
    dropped, and every ``set timeout=`` set to ``timeout_s``. Refuses a
    file with no menu entry holding a ``linux`` line, rather than guess.
    """
    lines = text.splitlines(keepends=False)
    in_entry = False
    edited = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not edited and stripped.startswith("menuentry "):
            in_entry = True
        elif in_entry and stripped == "}":
            in_entry = False
        elif in_entry and not edited and re.match(r"linux(efi)?\s", stripped):
            lines[i] = _edit_linux_line(line, args)
            edited = True
    if not edited:
        raise RemasterError("no menuentry with a linux line in grub.cfg")
    out = [re.sub(r"^(\s*set\s+timeout=)\S+", rf"\g<1>{timeout_s}", ln) for ln in lines]
    if not any(re.match(r"\s*set\s+timeout=", ln) for ln in out):
        out.insert(0, f"set timeout={timeout_s}")
    return "\n".join(out) + "\n"


def first_linux_args(text: str) -> list[str]:
    """The first menu entry's kernel arguments (after the kernel's path)."""
    in_entry = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("menuentry "):
            in_entry = True
        elif in_entry and re.match(r"linux(efi)?\s", stripped):
            return stripped.split()[2:]
    return []


def extract_cmd(iso: Path, inner: str, dest: Path) -> list[str]:
    """xorriso reading one file out of the ISO, read-only."""
    return ["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", inner, str(dest)]


def remaster_cmd(iso: Path, out: Path, grub_cfg: Path) -> list[str]:
    """
    xorriso writing a copy of the ISO with grub.cfg replaced; ``-boot_image
    any replay`` keeps the El Torito records, the EFI image and the hybrid
    partition table of the original.
    """
    return [
        "xorriso",
        "-indev",
        str(iso),
        "-outdev",
        str(out),
        "-map",
        str(grub_cfg),
        GRUB_CFG,
        "-boot_image",
        "any",
        "replay",
    ]


def report_cmd(iso: Path) -> list[str]:
    """xorriso describing the ISO's boot records, read-only."""
    return [
        "xorriso",
        "-indev",
        str(iso),
        "-report_el_torito",
        "plain",
        "-report_system_area",
        "plain",
    ]


def boot_shape(report: str) -> list[str]:
    """
    The boot records of a ``report_cmd`` output, reduced to what a remaster
    must keep: the El Torito images (platform, emulation, path), the
    system area's kind, the MBR partitions' status and type, and each GPT
    partition's name and type. Addresses, sizes and GUIDs are left out (a
    remaster moves files and draws new GUIDs), and so are xorriso's gap
    fillers ("Gap0", "Gap1"), which only pad the table.
    """
    shape: list[str] = []
    gpt: dict[str, dict[str, str]] = {}
    for line in report.splitlines():
        head, _, rest = line.partition(":")
        head, fields = head.strip(), rest.split()
        if head == "El Torito boot img" and len(fields) >= 4:
            shape.append("eltorito " + " ".join(fields[1:4]))
        elif head == "El Torito img path" and len(fields) >= 2:
            shape.append("eltorito path " + fields[1])
        elif head == "System area summary":
            shape.append("system area " + " ".join(fields))
        elif head == "MBR partition" and len(fields) >= 3:
            shape.append("mbr " + " ".join(fields[1:3]))
        elif head in {"GPT partname local", "GPT type GUID"} and len(fields) >= 2:
            gpt.setdefault(fields[0], {})[head] = " ".join(fields[1:])
    for idx in sorted(gpt, key=int):
        name = gpt[idx].get("GPT partname local", "")
        if not re.fullmatch(r"Gap\d+", name):
            shape.append(f"gpt {name} {gpt[idx].get('GPT type GUID', '')}")
    return shape
