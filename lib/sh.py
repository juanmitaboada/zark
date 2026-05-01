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
def run(
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

    if check and not r.ok:
        msg = f"Command failed (rc={r.returncode}): {cmd_str}"
        if r.stderr.strip():
            msg += f"\n  {r.stderr.strip()}"
        raise RuntimeError(msg)

    return r


def run_pipe(cmd1: str, cmd2: str, log: Log | None = None) -> RunResult:
    """Run cmd1 | cmd2 pipeline."""
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
            stdout, stderr = p2.communicate()

    return RunResult(
        returncode=p2.returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
        command=f"{cmd1} | {cmd2}",
    )


def part(disk: str, num: int) -> str:
    """Return partition device path: /dev/sda1 or /dev/nvme0n1p1.

    NVMe/loop/mmcblk devices end in a digit → need 'p' separator.
    SATA/USB/virtio devices (sda, vda) don't.
    """
    if disk[-1].isdigit():
        return f"{disk}p{num}"
    return f"{disk}{num}"
