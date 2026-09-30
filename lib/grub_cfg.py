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
kernels that have an initrd). Either way the caller must report it: a
banner that says "regenerated" over a stale or empty grub.cfg sends the
operator to a reboot that may not work.
"""

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


def regenerate_grub_cfg(grub_cfg: Path, command: str, backup: Path, log: Log) -> str:
    """Run ``command`` (update-grub); "" on success, else why grub.cfg is not new.

    The copy taken first is restored only when this run made it: a backup
    left by an earlier run describes an older system.
    """
    backed_up = grub_cfg.exists() and sh.run(f"cp {grub_cfg} {backup}").ok
    if backed_up:
        log.dbg(f"Backed up grub.cfg → {backup.name}")

    r = sh.run(command, log=log)
    content = grub_cfg.read_text(encoding="utf-8") if grub_cfg.exists() else ""
    if r.ok and "vmlinuz" in content:
        log.ok("grub.cfg regenerated with kernel entries ✓")
        return ""

    reason = (
        "update-grub produced no kernel entries"
        if r.ok
        else f"update-grub failed: {_failure_detail(r.stderr)}"
    )
    log.warn(reason)
    if backed_up and sh.run(f"cp {backup} {grub_cfg}").ok:
        log.warn("Restored the grub.cfg found at the start of this run")
    else:
        log.warn("No grub.cfg from this run to restore — the generated one is left in place")
    return reason
