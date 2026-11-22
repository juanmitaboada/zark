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
The serial console driver: waits for a prompt, answers it once.

It reads the guest's serial socket, matches prompts across read
boundaries (with terminal escapes and carriage returns removed), and
writes the passphrase only into the socket: the transcript it keeps is
what the guest printed, never what the bench typed.
"""

from __future__ import annotations

import re
import select
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# Waits up to the given seconds for the guest's next bytes: None when none
# came, b"" when the line closed.
Reader = Callable[[float], bytes | None]


class ConsoleError(Exception):
    """A prompt did not come, came twice, or the console closed."""


@dataclass(frozen=True)
class Prompt:
    """A text the guest prints, and where that text comes from."""

    name: str
    pattern: re.Pattern[bytes]
    source: str


UNLOCK_PROMPTS = (
    Prompt(
        "systemd",
        re.compile(rb"Please enter passphrase for disk [^\n]*?keystore-rpool[^\n]*?: ?"),
        "26.04's dracut initramfs, seen on the serial console as "
        '"Please enter passphrase for disk keystore-rpool: (press TAB for no echo)" '
        "(systemd 259.5-0ubuntu3.4, dracut 110-11ubuntu0.1); the format of systemd v259 "
        "src/cryptsetup/cryptsetup.c:928 and src/shared/ask-password-api.c:467 (which "
        "component asks was not read)",
    ),
    Prompt(
        "askpass",
        re.compile(rb"Please unlock disk keystore-rpool: ?"),
        "cryptsetup 2:2.7.0-1ubuntu4.2, /lib/cryptsetup/functions:295 (CRYPTTAB_NAME is the "
        "keystore-<pool> line zfs-initramfs adds, scripts/zfs:996)",
    ),
    Prompt(
        "cryptsetup",
        re.compile(rb"Enter passphrase for \S*keystore\S*: ?"),
        "cryptsetup's own prompt when it is asked without askpass",
    ),
)
LOGIN = Prompt("login", re.compile(rb"\S+ login: ?"), "agetty on the serial console")

# CSI sequences in full (ECMA-48: parameters, intermediates, final byte, so
# "\x1b[!p" and "\x1b[6n" too), OSC, DCS, APC and PM strings (systemd 259's
# "\x1b]3008;" context markers, agetty's "\x1bP+q" queries), charset
# selections and carriage returns: all seen on 26.04's serial console.
_ESCAPES = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]"
    rb"|\x1b[\]P_^][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b[()][A-Za-z0-9]"
    rb"|\r"
)


def clean(raw: bytes) -> bytes:
    """Terminal escapes and carriage returns removed, for matching."""
    return _ESCAPES.sub(b"", raw)


class Console:
    """One serial console, read until a prompt shows."""

    def __init__(
        self,
        sock: socket.socket,
        transcript: BinaryIO,
        reader: Reader | None = None,
    ) -> None:
        self._sock = sock
        self._transcript = transcript
        # A test gives its own reader, so the chunks arrive in a fixed order
        # instead of however a thread and the scheduler interleave them.
        self._read: Reader = reader or self._socket_read
        self._raw = b""
        self._pos = 0

    def _socket_read(self, wait_s: float) -> bytes | None:
        ready, _, _ = select.select([self._sock], [], [], wait_s)
        return self._sock.recv(65536) if ready else None

    @classmethod
    def connect(cls, path: Path, transcript: BinaryIO, timeout_s: float = 30.0) -> Console:
        """Connect to QEMU's serial socket, waiting for it to exist."""
        deadline = time.monotonic() + timeout_s
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(path))
                return cls(sock, transcript)
            except OSError:
                sock.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    def expect(self, prompts: tuple[Prompt, ...], timeout_s: float) -> Prompt:
        """The first of ``prompts`` printed after the last match."""
        deadline = time.monotonic() + timeout_s
        while True:
            text = clean(self._raw)
            first: Prompt | None = None
            first_start, first_end = len(text) + 1, 0
            for prompt in prompts:
                m = prompt.pattern.search(text, self._pos)
                if m and m.start() < first_start:
                    first, first_start, first_end = prompt, m.start(), m.end()
            if first is not None:
                self._pos = first_end
                return first
            left = deadline - time.monotonic()
            if left <= 0:
                names = ", ".join(p.name for p in prompts)
                raise ConsoleError(f"no prompt ({names}) within {timeout_s:.0f} s")
            chunk = self._read(min(left, 1.0))
            if chunk is None:
                continue
            if not chunk:
                raise ConsoleError("the serial console closed")
            self._raw += chunk
            self._transcript.write(chunk)
            self._transcript.flush()

    def send(self, data: bytes) -> None:
        """Type into the console; not written to the transcript."""
        self._sock.sendall(data)

    def close(self) -> None:
        """Close the socket; QEMU keeps the machine running."""
        self._sock.close()


def unlock(console: Console, passphrase: str, prompt_s: float, login_s: float) -> str:
    """
    Answer the boot's unlock prompt once and wait for the login prompt.
    A second unlock prompt fails: a refused passphrase is never retried.
    Returns the name of the prompt that was answered.
    """
    first = console.expect(UNLOCK_PROMPTS, prompt_s)
    console.send(passphrase.encode() + b"\r")
    after = console.expect((*UNLOCK_PROMPTS, LOGIN), login_s)
    if after is not LOGIN:
        raise ConsoleError(f"the guest asked again ({after.name}): the passphrase was refused")
    return first.name
