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
