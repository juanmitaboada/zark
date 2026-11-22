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
"""Results shared by the bench's checks: one named verdict each, as JSON."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from bench import RECORD_VERSION


@dataclass(frozen=True)
class Check:
    """One verdict: its name, whether it holds, and what was seen."""

    name: str
    ok: bool
    detail: str = ""


def all_ok(checks: list[Check]) -> bool:
    """True when there is at least one check and every one holds."""
    return bool(checks) and all(c.ok for c in checks)


def write_checks(path: Path, checks: list[Check]) -> None:
    """Write the verdicts as JSON, failures first so a reader sees them."""
    ordered = sorted(checks, key=lambda c: (c.ok, c.name))
    doc = {"record_version": RECORD_VERSION, "checks": [asdict(c) for c in ordered]}
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def summary(checks: list[Check]) -> str:
    """One line per check, for the job's log."""
    return "\n".join(f"{'OK  ' if c.ok else 'FAIL'} {c.name}: {c.detail}" for c in checks)
