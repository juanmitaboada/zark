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
line naming its ``vmlinuz-<v>`` (M4c-6), inside the sections of the two
scripts that list this system's kernels (M18R-23).
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
# The image is named vmlinuz-, vmlinux- or kernel-: the names 10_linux_zfs's
# find -regex and 10_linux's globs take (10_linux_zfs.in:420 on resolute and
# stonking, :421 on noble; 10_linux.in:307 and :312, :321 and :326 on noble),
# the name right after a "/" (M18R-24).
_LINUX_LINE = r'^\s*linux\s+"?/(?:[^"$\s]*/)?(?:vmlinuz|vmlinux|kernel)-'

# The same line in POSIX ERE, for grep in 99_zark_cfg_check (lib/grub_guard.py):
# \s as [:space:], outside and inside the bracket (M18E-4).
LINUX_LINE_ERE = r'^[[:space:]]*linux[[:space:]]+"?/([^"$[:space:]]*/)?(vmlinuz|vmlinux|kernel)-'

# The scripts whose sections list this system's kernels. grub-mkconfig writes
# "### BEGIN $i ###" and "### END $i ###" around each script's output
# (grub-mkconfig.in:304 and :306 on noble, :305 and :307 on resolute and
# stonking), $i being "${grub_mkconfig_dir}"/<name> with grub_mkconfig_dir
# "${sysconfdir}"/grub.d (:40) and sysconfdir /etc in Ubuntu's build. The
# entries of 30_os-prober and 40_custom boot other systems (M18R-23).
OWN_SCRIPTS = ("/etc/grub.d/10_linux_zfs", "/etc/grub.d/10_linux")


def own_sections(content: str) -> str:
    """The lines of ``content`` inside the sections of ``OWN_SCRIPTS``, as
    ``sed -n '/BEGIN/,/END/p'`` prints them (markers included; a section
    without its END runs to the end, as sed's range does)."""
    begins = {f"### BEGIN {s} ###": f"### END {s} ###" for s in OWN_SCRIPTS}
    out: list[str] = []
    end = ""
    for line in content.splitlines(keepends=True):
        bare = line.rstrip("\n")
        if not end:
            end = begins.get(bare, "")
            if end:
                out.append(line)
            continue
        out.append(line)
        if bare == end:
            end = ""
    return "".join(out)


def missing_entries(content: str, kernels: list[str]) -> list[str]:
    """The kernels of ``kernels`` without a ``linux`` line in the sections of
    ``OWN_SCRIPTS``; with no kernel given, ``[""]`` when none holds a kernel."""
    own = own_sections(content)
    if not kernels:
        return [] if re.search(_LINUX_LINE + r"\S", own, re.MULTILINE) else [""]
    return [
        v
        for v in kernels
        if not re.search(_LINUX_LINE + re.escape(v) + r'(?:["\s]|$)', own, re.MULTILINE)
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
