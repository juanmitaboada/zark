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
The rewrites recover makes in a restored system whose ``/boot`` is a new
ext4 file system (M12-3 3a): origin's ``/boot`` UUID to the new one, and
the swap's PARTUUID to the new partition's. Every other line is kept byte
for byte. Pure functions on text, shared with the self-test (BOOT-07,
BOOT-08).
"""

import re

# An ext4 or swap UUID, 8-4-4-4-12 hex (not a pool GUID's 16 hex digits).
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_RE = re.compile(UUID)


def _fields(line: str) -> list[str]:
    """A table line's fields, none for a comment or a blank line."""
    stripped = line.strip()
    return [] if not stripped or stripped.startswith("#") else stripped.split()


def rewrite_fstab(text: str, old_uuid: str, new_uuid: str) -> tuple[str, int]:
    """``/boot``'s line with ``new_uuid`` for ``old_uuid`` (``UUID=`` or
    ``/dev/disk/by-uuid/``), and how many lines changed."""
    out: list[str] = []
    changed = 0
    for line in text.splitlines(keepends=True):
        f = _fields(line)
        if len(f) >= 2 and f[1] == "/boot" and old_uuid.lower() in f[0].lower():
            new = re.sub(re.escape(old_uuid), new_uuid, line, flags=re.IGNORECASE)
            changed += new != line
            line = new
        out.append(line)
    return "".join(out), changed


def rewrite_crypttab(text: str, new_partuuid: str) -> tuple[str, int]:
    """The swap's ``PARTUUID=`` (a line whose options name ``swap``) set to
    ``new_partuuid``, and how many lines changed."""
    out: list[str] = []
    changed = 0
    for line in text.splitlines(keepends=True):
        f = _fields(line)
        if len(f) >= 4 and "swap" in f[3].split(",") and f[1].startswith("PARTUUID="):
            new = line.replace(f[1], f"PARTUUID={new_partuuid}", 1)
            changed += new != line
            line = new
        out.append(line)
    return "".join(out), changed


def uuids_in(text: str) -> set[str]:
    """Every UUID a text names, lower-case."""
    return {u.lower() for u in _UUID_RE.findall(text)}


def _is_search(line: str) -> bool:
    """A GRUB ``search`` command: ``search ...`` or ``search.fs_uuid ...``."""
    words = line.split(maxsplit=1)
    return bool(words) and (words[0] == "search" or words[0].startswith("search."))


def rewrite_grub_uuid(text: str, old_uuid: str, new_uuid: str) -> tuple[str, int]:
    """Every ``search`` line of a ``grub.cfg`` that names ``old_uuid`` (with
    ``--fs-uuid`` and ``--hint-*``, bare, or ``search.fs_uuid``) with
    ``new_uuid`` instead, and how many lines changed. A ``search`` for
    another UUID and every other line are kept byte for byte (M12-3 3a)."""
    out: list[str] = []
    changed = 0
    for line in text.splitlines(keepends=True):
        if _is_search(line) and old_uuid.lower() in line.lower():
            new = re.sub(re.escape(old_uuid), new_uuid, line, flags=re.IGNORECASE)
            changed += new != line
            line = new
        out.append(line)
    return "".join(out), changed


def grub_uuid_problems(
    grub_cfg: str, esp_cfg: str, fstab: str, old_uuid: str, new_uuid: str
) -> list[str]:
    """Check (a) of a recovered ext4 ``/boot`` (M12R-13): ``old_uuid`` in
    none of ``/boot/grub/grub.cfg``, the ESP's ``EFI/ubuntu/grub.cfg`` and
    ``/etc/fstab``; ``new_uuid`` in the ESP's file and in a ``search`` line
    of ``grub.cfg``. One line per failure."""
    old, new = old_uuid.lower(), new_uuid.lower()
    problems = [
        f"{name} still names /boot's old UUID {old_uuid}"
        for name, text in (
            ("/boot/grub/grub.cfg", grub_cfg),
            ("the ESP's EFI/ubuntu/grub.cfg", esp_cfg),
            ("/etc/fstab", fstab),
        )
        if old in uuids_in(text)
    ]
    if new not in uuids_in(esp_cfg):
        problems.append(f"the ESP's EFI/ubuntu/grub.cfg does not name /boot's UUID {new_uuid}")
    if not any(_is_search(ln) and new in uuids_in(ln) for ln in grub_cfg.splitlines()):
        problems.append(f"no search line of /boot/grub/grub.cfg names /boot's UUID {new_uuid}")
    return problems


_LINUX_RE = re.compile(r"^\s*linux\s+\"?/vmlinuz-(\S+?)\"?\s", re.MULTILINE)


def kernels_without_entry(grub_cfg: str, kernels: list[str]) -> list[str]:
    """Check (b) (M12R-13): the versions among ``kernels`` (each ``vmlinuz-<v>``
    of the restored ``/boot``) that no ``linux /vmlinuz-<v>`` line of
    ``grub.cfg`` boots."""
    booted = set(_LINUX_RE.findall(grub_cfg))
    return [v for v in kernels if v not in booted]


_BY_UUID = "/dev/disk/by-uuid/"


def fstab_boot(text: str) -> tuple[str, str]:
    """``/boot``'s file system UUID and type in an fstab (``UUID=`` or
    ``/dev/disk/by-uuid/``); ("", "") without such a line."""
    for line in text.splitlines():
        f = _fields(line)
        if len(f) >= 3 and f[1] == "/boot":
            spec = f[0]
            uuid = spec[5:] if spec.startswith("UUID=") else spec.removeprefix(_BY_UUID)
            if uuid != spec and _UUID_RE.fullmatch(uuid):
                return uuid.lower(), f[2]
    return "", ""
