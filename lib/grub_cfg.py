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
grub.cfg regeneration with an honest verdict (repair-boot, finish).

update-grub can fail (the backup guard refuses while an external pool is
visible) or succeed with no kernel entries (10_linux_zfs lists only
kernels that have an initrd, and exits 0 even when it aborts half way).
Either way the caller must report it: a banner that says "regenerated" over
a stale or empty grub.cfg sends the operator to a reboot that may not work.
So the verdict reads the content: every installed kernel needs a ``linux``
line naming its ``vmlinuz-<v>`` (M4c-6).
"""

import re
from pathlib import Path

from lib import sh
from lib.log import Log


def _failure_detail(stderr: str) -> str:
    """The line that explains an update-grub failure, not its whole chatter."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("ERROR"):
            return ln
    return lines[-1] if lines else "no error output"


# A menu entry's kernel line, as 10_linux_zfs writes it:
# ``linux "/BOOT/<be>@/vmlinuz-<v>" root=ZFS=...``. The path is absolute, so
# the ``linux "${kernel}"`` line of its zsyshistorymenu function never matches.
_LINUX_LINE = r'^\s*linux\s+"?/[^"$\s]*vmlinuz-'


def missing_entries(content: str, kernels: list[str]) -> list[str]:
    """The kernels of ``kernels`` without a ``linux`` line in ``content``;
    with no kernel given, ``[""]`` when no line names any vmlinuz."""
    if not kernels:
        return [] if re.search(_LINUX_LINE + r"\S", content, re.MULTILINE) else [""]
    return [
        v
        for v in kernels
        if not re.search(_LINUX_LINE + re.escape(v) + r'(?:["\s]|$)', content, re.MULTILINE)
    ]


def regenerate_grub_cfg(
    grub_cfg: Path,
    command: str,
    backup: Path,
    kernels: list[str],
    log: Log,
) -> str:
    """Run ``command`` (update-grub); "" on success, else why grub.cfg is not new.

    Success needs update-grub's exit status 0 and an entry for every kernel
    of ``kernels`` (the installed ones, ``lib.initrd.installed_kernels``).
    The copy taken first is restored only when this run made it: a backup
    left by an earlier run describes an older system.
    """
    backed_up = grub_cfg.exists() and sh.run(f"cp {grub_cfg} {backup}").ok
    if backed_up:
        log.dbg(f"Backed up grub.cfg → {backup.name}")

    r = sh.run(command, log=log)
    # Titles come raw from each dataset's os-release: never assume UTF-8.
    content = grub_cfg.read_bytes().decode("utf-8", "replace") if grub_cfg.exists() else ""
    missing = missing_entries(content, kernels) if r.ok else []
    if r.ok and not missing:
        log.ok("grub.cfg regenerated with kernel entries ✓")
        return ""

    if not r.ok:
        reason = f"update-grub failed: {_failure_detail(r.stderr)}"
    elif missing == [""]:
        reason = "update-grub produced no kernel entries"
    else:
        reason = f"update-grub wrote no entry for {', '.join(missing)}"
    log.warn(reason)
    if backed_up and sh.run(f"cp {backup} {grub_cfg}").ok:
        log.warn("Restored the grub.cfg found at the start of this run")
    else:
        log.warn("No grub.cfg from this run to restore - the generated one is left in place")
    return reason
