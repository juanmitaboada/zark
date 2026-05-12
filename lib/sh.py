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
Shell command execution with full logging.

Every command is logged before and after execution.
No 2>/dev/null anywhere — everything is captured and visible.
"""

import shlex
import subprocess
from dataclasses import dataclass

from lib.log import Log


@dataclass
class RunResult:
    """Result of a shell command."""

    returncode: int
    stdout: str
    stderr: str
    command: str

    @property
    def ok(self) -> bool:
        """True if returncode is 0 (success)."""
        return self.returncode == 0

    @property
    def output(self) -> str:
        """Combined stdout, stripped."""
        return self.stdout.strip()

    @property
    def lines(self) -> list[str]:
        """stdout split into non-empty lines."""
        return [li for li in self.stdout.strip().splitlines() if li.strip()]


# ── nosec B602 rationale ─────────────────────────────────────────────
# We pass shell=use_shell (True by default) deliberately. Bandit
# flags this as CWE-78 (command injection), which does not apply to
# zark's threat model:
#
#   1. zark always runs as root. An attacker with root access does
#      not need to inject through zark — they have a shell already.
#   2. Commands are built from string literals or interpolated with
#      values that come from controlled sources only: pool names
#      and GUIDs registered in known_drives.json, drive paths from
#      udev under /dev/disk/by-id/, snapshot names from the local
#      ZFS metadata. None of these are user-supplied at runtime.
#   3. shell=True is needed for commands with redirections, globs,
#      and shell features that the rest of zark relies on.
#
# If a future caller passes user-controlled input here, that caller
# is responsible for validating or escaping it — but no such caller
# exists today.
def run(  # pylint: disable=too-many-branches
    cmd: str | list[str],
    check: bool = False,
    input: str | None = None,  # pylint: disable=redefined-builtin
    log: Log | None = None,
    timeout: int | None = None,
) -> RunResult:
    """
    Run a shell command. Always captures output, always logs.

    Uses shell=False (direct exec) for simple commands to avoid /bin/sh
    dependency. Falls back to shell=True only for commands with shell
    metacharacters (pipes, redirects, &&, ||, etc.).
    """
    if isinstance(cmd, list):
        cmd_str = " ".join(cmd)
        use_shell = False
        cmd_exec: str | list[str] = cmd
    else:
        cmd_str = cmd
        # Detect if shell features are needed
        shell_chars = set("|&;<>()$`\"'\\!{}*?[]~")
        if any(c in cmd for c in shell_chars):
            use_shell = True
            cmd_exec = cmd
        else:
            use_shell = False
            cmd_exec = shlex.split(cmd)

    if log:
        log.cmd(cmd_str)

    try:
        result = subprocess.run(
            cmd_exec,
            shell=use_shell,  # nosec B602 — see rationale at top of run()# # nosec B602 - rationale
            capture_output=True,
            text=True,
            input=input,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return RunResult(
            returncode=124,
            stdout="",
            stderr=f"Command timed out after {timeout}s",
            command=cmd_str,
        )
    except FileNotFoundError as e:
        return RunResult(
            returncode=127,
            stdout="",
            stderr=str(e),
            command=cmd_str,
        )

    r = RunResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        command=cmd_str,
    )

    if log and r.stderr.strip():
        for line in r.stderr.strip().splitlines():
            log.dbg(f"  stderr: {line}")

    # When a command fails, also surface stdout — many tools (syncoid, zfs,
    # zpool) emit error messages to stdout rather than stderr. Without this,
    # fatal errors disappear into the void and the user sees a generic
    # "check log" message with nothing useful in the log.
    if log and not r.ok and r.stdout.strip():
        for line in r.stdout.strip().splitlines():
            log.dbg(f"  stdout: {line}")

    if check and not r.ok:
        msg = f"Command failed (rc={r.returncode}): {cmd_str}"
        if r.stderr.strip():
            msg += f"\n  {r.stderr.strip()}"
        raise RuntimeError(msg)

    return r


def run_pipe(cmd1: str, cmd2: str, log: Log | None = None) -> RunResult:
    """Run cmd1 | cmd2 pipeline.

    Returns a RunResult whose returncode is non-zero if EITHER side of
    the pipeline failed. stderr from both sides is concatenated so that
    callers (and helpers like is_enospc()) can inspect the combined error
    output without caring which side produced it.

    Rationale: with only `p2.returncode` checked, a failure on the left
    side (e.g. `zfs send` of a deleted snapshot) followed by a clean EOF
    on the right side would be reported as success. That is wrong: the
    stream is truncated and the receiving dataset is incomplete. For our
    use case (zfs send | zfs receive), an out-of-space condition on the
    right side already produces a non-zero p2.returncode, so ENOSPC was
    detected correctly even before this change. The fix is for the other
    failure modes.
    """
    if log:
        log.cmd(f"{cmd1} | {cmd2}")

    with subprocess.Popen(
        cmd1,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as p1:  # nosec B602 — see run() rationale; pipelines need shell=True
        assert p1.stdout is not None  # guaranteed by stdout=PIPE above
        with subprocess.Popen(
            cmd2,
            shell=True,  # nosec B602 — see run() rationale; pipelines need shell=True
            stdin=p1.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as p2:
            p1.stdout.close()  # let p2 receive EOF when p1 finishes
            stdout, stderr_p2 = p2.communicate()

        # p1 has finished by now (p2.communicate waited for downstream EOF,
        # which only happens after p1 exits). Wait explicitly to reap it
        # and capture its stderr.
        _ = p1.wait()
        stderr_p1 = p1.stderr.read() if p1.stderr is not None else b""

    # Combine stderr from both sides for transparent error inspection.
    stderr_p1_s = stderr_p1.decode(errors="replace")
    stderr_p2_s = stderr_p2.decode(errors="replace")
    if stderr_p1_s and stderr_p2_s:
        combined_stderr = stderr_p1_s.rstrip() + "\n" + stderr_p2_s
    else:
        combined_stderr = stderr_p1_s or stderr_p2_s

    # Pick the most informative non-zero returncode. If p2 failed, that is
    # what the caller usually cares about (downstream error). Otherwise,
    # bubble up p1's failure if any. If both succeeded, return 0.
    if p2.returncode != 0:
        rc = p2.returncode
    elif p1.returncode != 0:
        rc = p1.returncode
    else:
        rc = 0

    return RunResult(
        returncode=rc,
        stdout=stdout.decode(errors="replace"),
        stderr=combined_stderr,
        command=f"{cmd1} | {cmd2}",
    )


# ── ENOSPC detection ─────────────────────────────────────────────────────
# Marker substrings (case-insensitive) that indicate an out-of-space
# condition. List sourced from real ZFS / kernel error messages observed
# on Ubuntu (zfs receive, zfs send -nv, syncoid wrapping the same):
#   "cannot receive incremental stream: out of space"
#   "cannot receive new filesystem stream: out of space"
#   "write: No space left on device"
#   "ENOSPC" (kernel-level, occasionally surfaces verbatim)
# "disk quota exceeded" is included defensively — it is technically
# EDQUOT, not ENOSPC, but for backup/recover purposes the user-visible
# remediation is the same (free space) and treating it as ENOSPC avoids
# falling through to a generic "syncoid failed" message.
_ENOSPC_MARKERS = (
    "no space left on device",
    "out of space",
    "enospc",
    "disk quota exceeded",
)


def is_enospc(text: str) -> bool:
    """True if the given text contains an out-of-space marker.

    Searches case-insensitively in the combined stdout+stderr or any
    other captured output. Safe against empty strings.
    """
    if not text:
        return False
    lower = text.lower()
    return any(m in lower for m in _ENOSPC_MARKERS)


# ── Size formatting ──────────────────────────────────────────────────────
# zpool/zfs already give us human-readable size strings (e.g. "1.86T"),
# but callers that compute thresholds in bytes need a way to format their
# own values consistently with that style for log messages. Using IEC
# units (1024-based) so a "1G" threshold matches what GiB-scale users
# expect; ZFS also reports IEC, so messages stay coherent.
def humanize_bytes(n: int) -> str:
    """Format a byte count as a short human-readable string (IEC units).

    Uses 1024-based units to match ZFS's own output style. Returns
    values like '512B', '1.5K', '4.0M', '12G', '1.9T'. Negative values
    are formatted with a leading minus.
    """
    if n < 0:
        return "-" + humanize_bytes(-n)
    units = ("B", "K", "M", "G", "T", "P", "E")
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            # No decimals for raw bytes, one decimal otherwise
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024
    # Unreachable: the loop's last iteration always returns
    return f"{size:.1f}E"  # pragma: no cover


def part(disk: str, num: int) -> str:
    """Return partition device path: /dev/sda1 or /dev/nvme0n1p1.

    NVMe/loop/mmcblk devices end in a digit → need 'p' separator.
    SATA/USB/virtio devices (sda, vda) don't.
    """
    if disk[-1].isdigit():
        return f"{disk}p{num}"
    return f"{disk}{num}"
