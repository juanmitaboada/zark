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
Who ran zark, for zark.log (Z2).

zark runs as root, through pkexec (the future app, M24) or sudo, or from a
root shell; zark.log names the user behind each run so a log read later
says who started what. The uid comes from ``PKEXEC_UID`` when it holds an
integer, else ``SUDO_UID`` when it does, else zark's own uid; pkexec is
asked first because sudo inside a pkexec'd shell would name root.
"""

import os
import pwd
from collections.abc import Mapping

# The variables each launcher sets to the uid that ran it, in the order asked.
SOURCES = (("PKEXEC_UID", "pkexec"), ("SUDO_UID", "sudo"))


def invoker(environ: Mapping[str, str] | None = None) -> tuple[int, str, str]:
    """``(uid, name, via)``: the uid that ran zark, its passwd name (the
    number when it has none) and the launcher that gave it ("root" when
    none did)."""
    env = os.environ if environ is None else environ
    uid, via = os.getuid(), "root"
    for var, launcher in SOURCES:
        value = env.get(var, "").strip()
        # isdigit alone takes digits int() refuses ("²").
        if value.isascii() and value.isdigit():
            uid, via = int(value), launcher
            break
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = str(uid)
    return uid, name, via


def run_line(command: str, words: list[str], environ: Mapping[str, str] | None = None) -> str:
    """The zark.log line naming the command, its words and who ran it."""
    uid, name, via = invoker(environ)
    line = " ".join(["zark", command, *words])
    return f"{line} run by uid {uid} ({name}) via {via}"
