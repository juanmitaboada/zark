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
The leak check: no secret in anything that leaves the runner.

Secrets are searched as bytes, streamed in blocks with an overlap so a
match across two reads is found. Only where a secret was found is
reported, never the secret.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

_BLOCK = 1 << 20

# Where the guest's own search looks: the disk-image scan sees only bpool
# and the ESP, so rpool is searched from inside the guest.
GUEST_PATHS = ("/var/log", "/etc", "/var", "/root", "/home", "/boot", "/tmp")


def guest_grep_argv() -> list[str]:
    """
    The guest's search: GNU grep reads its patterns from stdin (``-f -``),
    so the passphrase is never an argument. Exit 1 means no match.
    """
    return ["sudo", "-n", "grep", "-rlFs", "--devices=skip", "-f", "-", *GUEST_PATHS]


def found_in_stream(stream: BinaryIO, needles: list[bytes]) -> set[int]:
    """Indexes of ``needles`` present in the stream."""
    needles_ok = [n for n in needles if n]
    keep = max((len(n) for n in needles_ok), default=1) - 1
    found: set[int] = set()
    tail = b""
    while block := stream.read(_BLOCK):
        window = tail + block
        for i, needle in enumerate(needles):
            if needle and i not in found and needle in window:
                found.add(i)
        tail = window[-keep:] if keep else b""
    return found


def scan_file(path: Path, needles: list[bytes]) -> set[int]:
    """Indexes of ``needles`` in one file."""
    with path.open("rb") as f:
        return found_in_stream(f, needles)


def scan_tree(root: Path, needles: list[bytes]) -> list[tuple[Path, int]]:
    """(file, needle index) for every regular file under ``root`` holding a needle."""
    hits: list[tuple[Path, int]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        hits.extend((path, i) for i in sorted(scan_file(path, needles)))
    return hits


def upload_verdict(
    host_clean: bool, guest_seen: bool, guest_status: int | None
) -> tuple[bool, str]:
    """
    Whether out/ may be uploaded, and why. The host's search (out/ and the
    disk image) must be clean. Once the guest was up over SSH, its own
    search must also have run and passed (``guest_status`` 0): a hit, a
    search that failed or one that never ran (the budget) all hold the
    upload. Only a guest that never came up skips it: nothing of its
    encrypted rpool reached out/ then (decision 3a).
    """
    if not host_clean:
        return False, "the host's search found a secret in out/ or the disk image, or could not run"
    if guest_seen and guest_status is None:
        return False, "the guest was up but its own search did not run"
    if guest_seen and guest_status != 0:
        return False, "the guest's own search found a secret or failed"
    if not guest_seen:
        return True, "no secret found in out/ or the disk image; the guest never came up"
    return True, "no secret found in out/, the disk image or the guest's own search"


def needles_of(secrets: Iterable[str]) -> list[bytes]:
    """The secrets as UTF-8 bytes, empty ones dropped."""
    return [s.encode() for s in secrets if s]
