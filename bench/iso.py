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
The Ubuntu ISO: which one, its signed checksums, and its own checksum.

The newest point release of a series is read from the release directory's
listing; ``SHA256SUMS`` must carry a valid signature by Ubuntu's CD image
key, checked with ``gpgv`` against the keyring of the runner's own
``ubuntu-keyring`` package and pinned by fingerprint; then the ISO's
sha256 must match its line.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

RELEASES = "https://releases.ubuntu.com/{series}/"

# "Ubuntu CD Image Automatic Signing Key (2012) <cdimage@ubuntu.com>", the
# key that signs the release directories' SHA256SUMS; it lives in
# ubuntu-keyring's archive keyring (fingerprint read with gpg --list-keys).
CDIMAGE_FINGERPRINT = "843938DF228D22F7B3742BC0D94AA3F0EFE21092"
KEYRING = Path("/usr/share/keyrings/ubuntu-archive-keyring.gpg")

_CHUNK = 1 << 20


class IsoError(Exception):
    """The ISO cannot be chosen, fetched or trusted."""


@dataclass(frozen=True)
class IsoChoice:
    """The release picked and where it is fetched from."""

    name: str
    url: str


def newest_desktop_iso(listing: str, series: str) -> str:
    """
    The highest point release of the series' desktop ISO in a directory
    listing: ``ubuntu-26.04.1-desktop-amd64.iso`` beats the ``26.04`` one.
    """
    pattern = re.compile(
        r"ubuntu-" + re.escape(series) + r"(?:\.(\d+))?-desktop-amd64\.iso(?![.\w])",
    )
    found: dict[int, str] = {}
    for m in pattern.finditer(listing):
        found[int(m.group(1) or 0)] = m.group(0)
    if not found:
        raise IsoError(f"no {series} desktop ISO in the listing")
    return found[max(found)]


def choose(listing: str, series: str, wanted: str = "") -> IsoChoice:
    """The ISO named by the dispatch, or the newest one in the listing."""
    base = RELEASES.format(series=series)
    if wanted:
        # A dispatch input is free text: only a bare ISO name is accepted.
        if not re.fullmatch(r"ubuntu-[0-9.]+-desktop-amd64\.iso", wanted):
            raise IsoError(f"not a desktop ISO name: {wanted!r}")
        if wanted not in listing:
            raise IsoError(f"{wanted} is not in {base}")
        return IsoChoice(wanted, base + wanted)
    name = newest_desktop_iso(listing, series)
    return IsoChoice(name, base + name)


def parse_sha256sums(text: str) -> dict[str, str]:
    """``SHA256SUMS`` as {file name: lower-case hex digest}."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](\S+)", line.strip())
        if m:
            sums[m.group(2)] = m.group(1).lower()
    return sums


def signature_error(status: str, fingerprint: str = CDIMAGE_FINGERPRINT) -> str:
    """
    Why gpgv's ``--status-fd`` output does not prove a good signature by the
    pinned key, or "" when it does. The primary key's fingerprint is the
    last field of VALIDSIG, so a signing subkey is accepted too.
    """
    lines = [ln.split() for ln in status.splitlines() if ln.startswith("[GNUPG:] ")]
    for fields in lines:
        if fields[1] in {"BADSIG", "ERRSIG", "EXPKEYSIG", "REVKEYSIG"}:
            return f"gpgv reported {fields[1]}"
    valid = [f for f in lines if f[1] == "VALIDSIG"]
    if not valid:
        return "gpgv reported no valid signature"
    primaries = {f[-1].upper() for f in valid}
    if primaries != {fingerprint}:
        return "signed by " + ", ".join(sorted(primaries)) + ", not " + fingerprint
    return ""


def gpgv_status(sums: Path, signature: Path, keyring: Path = KEYRING, gpgv: str = "gpgv") -> str:
    """Run gpgv and return its status lines (the verdict is signature_error's)."""
    proc = subprocess.run(
        [gpgv, "--status-fd", "1", "--keyring", str(keyring), str(signature), str(sums)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def sha256_file(path: Path) -> str:
    """The file's sha256, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(_CHUNK):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path, attempts: int = 3, pause_s: float = 10.0) -> None:
    """
    Fetch a URL into ``dest`` through a temporary name, retrying a failed
    transfer; a partial file never stands under the final name.
    """
    partial = dest.with_name(dest.name + ".part")
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp, partial.open("wb") as out:
                while block := resp.read(_CHUNK):
                    out.write(block)
            partial.replace(dest)
            return
        except OSError as err:
            partial.unlink(missing_ok=True)
            if attempt == attempts:
                raise IsoError(f"{url}: {err}") from err
            time.sleep(pause_s)
