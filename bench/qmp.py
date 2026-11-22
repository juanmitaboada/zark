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
"""A small QMP client: one Unix socket, one command at a time."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any


class QmpError(Exception):
    """QEMU refused a command, or the socket closed."""


class Qmp:
    """A QMP session; events seen while waiting for answers are kept."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._reader = sock.makefile("r", encoding="utf-8")
        self.events: list[dict[str, Any]] = []
        greeting = self._read()
        if "QMP" not in greeting:
            raise QmpError(f"not a QMP greeting: {greeting}")
        self.execute("qmp_capabilities")

    @classmethod
    def connect(cls, path: Path, timeout_s: float = 30.0) -> Qmp:
        """Connect, waiting for QEMU to create the socket."""
        deadline = time.monotonic() + timeout_s
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(path))
                return cls(sock)
            except OSError:
                sock.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    def _read(self) -> dict[str, Any]:
        try:
            line = self._reader.readline()
        except OSError as err:
            raise QmpError(f"QMP socket closed: {err}") from err
        if not line:
            raise QmpError("QMP socket closed")
        doc = json.loads(line)
        if not isinstance(doc, dict):
            raise QmpError(f"not a QMP message: {line.strip()}")
        return doc

    def execute(self, command: str, **arguments: Any) -> Any:
        """Send one command and return its answer's ``return`` value."""
        msg: dict[str, Any] = {"execute": command}
        if arguments:
            msg["arguments"] = arguments
        try:
            self._sock.sendall((json.dumps(msg) + "\n").encode())
        except OSError as err:
            # The same words as a read that finds the socket closed: which of
            # the two sees QEMU gone first is a matter of timing.
            raise QmpError(f"QMP socket closed sending {command}: {err}") from err
        while True:
            doc = self._read()
            if "event" in doc:
                self.events.append(doc)
            elif "error" in doc:
                raise QmpError(f"{command}: {doc['error'].get('desc', doc['error'])}")
            elif "return" in doc:
                return doc["return"]

    def screendump(self, path: Path) -> None:
        """The VGA screen as a PNG."""
        self.execute("screendump", filename=str(path), format="png")

    def close(self) -> None:
        """Close the session; QEMU keeps running."""
        self._reader.close()
        self._sock.close()
