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
No 2>/dev/null anywhere - everything is captured and visible.
"""

import shlex
import signal
import subprocess
from dataclasses import dataclass

from lib import signals
from lib.log import Log


@dataclass
class RunResult:
    """Result of a shell command."""

    returncode: int
    stdout: str
    stderr: str
    command: str
    # run_pipe only: each side's stderr, so a caller can tell which side
    # failed (``stderr`` keeps both, send's first).
    stderr_left: str = ""
    stderr_right: str = ""

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
#      not need to inject through zark - they have a shell already.
#   2. Commands are built from string literals or interpolated with
#      values that come from controlled sources only: pool names
#      and GUIDs registered in known_drives.json, drive paths from
#      udev under /dev/disk/by-id/, snapshot names from the local
#      ZFS metadata. None of these are user-supplied at runtime.
#   3. shell=True is needed for commands with redirections, globs,
#      and shell features that the rest of zark relies on.
#
# If a future caller passes user-controlled input here, that caller
# is responsible for validating or escaping it - but no such caller
# exists today.
def run(  # pylint: disable=too-many-branches,too-many-locals
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

    # A process group of its own: zark passes a signal on to the whole group
    # (lib.signals), shell included. dash 0.5.12 does not exec the last
    # command of `sh -c` (it stays the parent), so a quoted command relies on
    # the group, not on being sh's only process (M4bP.4). Without `input`, stdin is /dev/null, so
    # a child of a background group never stops reading the terminal.
    try:
        proc = subprocess.Popen(
            cmd_exec,
            shell=use_shell,  # nosec B602 - see the rationale above run()
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            process_group=0,
        )
    except FileNotFoundError as e:
        return RunResult(
            returncode=127,
            stdout="",
            stderr=str(e),
            command=cmd_str,
        )
    signals.running(signals.child_name(cmd_str))
    with proc:
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            signals.stop_children([proc], signal.SIGKILL)
            _ = proc.communicate()
            return RunResult(
                returncode=124,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                command=cmd_str,
            )
        except BaseException:
            # A signal (or anything else) while waiting: the child stops
            # first, then the exception goes on (H12).
            signals.stop_children([proc])
            raise
        finally:
            signals.running(None)

    r = RunResult(
        returncode=proc.returncode,
        stdout=out,
        stderr=err,
        command=cmd_str,
    )

    if log and r.stderr.strip():
        for line in r.stderr.strip().splitlines():
            log.dbg(f"  stderr: {line}")

    # When a command fails, also surface stdout - many tools (syncoid, zfs,
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

    # Each side in a process group of its own, stopped first when a signal
    # (or anything else) interrupts the wait: a cut `zfs receive -s` keeps
    # its partial state for the next run (H12).
    signals.running(f"{signals.child_name(cmd1)} | {signals.child_name(cmd2)}")
    with subprocess.Popen(
        cmd1,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        process_group=0,
    ) as p1:  # nosec B602 - see the rationale above run()
        assert p1.stdout is not None  # guaranteed by stdout=PIPE above
        with subprocess.Popen(
            cmd2,
            shell=True,  # nosec B602 - see the rationale above run()
            stdin=p1.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            process_group=0,
        ) as p2:
            p1.stdout.close()  # let p2 receive EOF when p1 finishes
            try:
                stdout, stderr_p2 = p2.communicate()
                # p1 has finished by now (p2.communicate waited for downstream
                # EOF, which only happens after p1 exits). Wait explicitly to
                # reap it and capture its stderr.
                _ = p1.wait()
            except BaseException:
                signals.stop_children([p1, p2])
                raise
            finally:
                signals.running(None)
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
        stderr_left=stderr_p1_s,
        stderr_right=stderr_p2_s,
    )


# ── ENOSPC detection ─────────────────────────────────────────────────────
# Marker substrings (case-insensitive) that indicate an out-of-space
# condition. List sourced from real ZFS / kernel error messages observed
# on Ubuntu (zfs receive, zfs send -nv, syncoid wrapping the same):
#   "cannot receive incremental stream: out of space"
#   "cannot receive new filesystem stream: out of space"
#   "write: No space left on device"
#   "ENOSPC" (kernel-level, occasionally surfaces verbatim)
# "disk quota exceeded" is included defensively - it is technically
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


def is_live_usb() -> bool:
    """True when running from Ubuntu live media rather than an installed system.

    Detection combines the kernel cmdline markers Ubuntu's live media sets
    (``boot=casper`` / ``boot=live`` / ``live-media``) with the overlay mount
    points a casper session creates (``/rofs``, ``/cow``). Routed through
    :func:`run` (rather than reading ``/proc`` directly) so the unit-test mock
    shell can drive it deterministically.

    Single source of truth for the "are we on live media?" question. ``recover``
    and ``repair-boot`` both consult it to refuse operating on the *running*
    installed system, where the supported path is ``zark finish`` instead.
    Note that an installed ZFS-on-root Ubuntu is **not** live media, so this
    returns False there - which is exactly the signal those commands want.
    """
    r = run("cat /proc/cmdline")
    if r.ok and any(k in r.output for k in ("boot=casper", "boot=live", "live-media")):
        return True
    return run("test -d /rofs").ok or run("test -d /cow").ok
