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
Mock framework for sh.run — the backbone of zark unit testing.

Almost every operation in zark goes through sh.run(). By mocking it,
we can test all command logic without ZFS, root, or real disks.

Usage:
    from tests.mock_sh import MockShell, patch_sh

    mock = MockShell()
    mock.on("zpool list rpool").returns(0, "rpool\t12345\tONLINE\t1T\t500G\t500G")
    mock.on("zfs list -H").returns(0, "rpool/ROOT\t/\t100G\t50G\tnoauto")
    mock.on("false").returns(1)

    with patch_sh(mock):
        # All sh.run() calls now go through mock
        result = sh.run("zpool list rpool")
        assert result.ok

    # Check what was called:
    assert mock.was_called("zpool list rpool")
    assert mock.call_count("syncoid") == 1
"""

import re
from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

from lib.sh import RunResult


@dataclass
class MockResponse:
    """A scripted response for a command pattern."""

    pattern: str
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    call_count: int = 0
    _is_regex: bool = False

    @property
    def is_regex(self) -> bool:
        """Whether this response uses regex matching."""
        return self._is_regex

    def returns(self, rc: int, stdout: str = "", stderr: str = "") -> "MockResponse":
        """
        Set the return code, stdout, and stderr for this response.
        """
        self.returncode = rc
        self.stdout = stdout if stdout.endswith("\n") or not stdout else stdout + "\n"
        self.stderr = stderr
        return self

    def succeeds(self, stdout: str = "") -> "MockResponse":
        """
        Set a zero return code and optional stdout message.
        """
        return self.returns(0, stdout)

    def fails(self, stderr: str = "", rc: int = 1) -> "MockResponse":
        """
        Set a non-zero return code and optional stderr message.
        """
        return self.returns(rc, stderr=stderr)


class MockShell:
    """
    Scriptable mock for sh.run().

    Register expected commands with .on(), then use with patch_sh().
    Unregistered commands return rc=127 (command not found).
    """

    def __init__(self, strict: bool = False):
        self._responses: list[MockResponse] = []
        self._calls: list[str] = []
        self._strict = strict  # if True, unregistered commands raise

    def on(self, pattern: str, regex: bool = False) -> MockResponse:
        """Register a command pattern and return the response builder."""
        resp = MockResponse(pattern=pattern, _is_regex=regex)
        self._responses.append(resp)
        return resp

    def on_prefix(self, prefix: str) -> MockResponse:
        """Match any command starting with prefix."""
        return self.on(f"^{re.escape(prefix)}", regex=True)

    def _find(self, cmd: str) -> MockResponse | None:
        """Find the first matching response for a command string."""
        for resp in self._responses:
            if resp.is_regex:
                if re.search(resp.pattern, cmd):
                    return resp
            else:
                if resp.pattern == cmd or cmd.startswith(resp.pattern + " ") or cmd == resp.pattern:
                    return resp
        # Try substring match as fallback
        for resp in self._responses:
            if not resp.is_regex and resp.pattern in cmd:
                return resp
        return None

    def mock_run(  # pylint: disable=unused-argument,redefined-builtin
        self,
        cmd,
        check=False,
        input=None,
        log=None,
        timeout=None,
    ) -> RunResult:
        """Drop-in replacement for sh.run()."""
        del input, log, timeout  # unused in mock
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
        else:
            cmd_str = cmd

        self._calls.append(cmd_str)

        resp = self._find(cmd_str)
        if resp:
            resp.call_count += 1
            r = RunResult(
                returncode=resp.returncode,
                stdout=resp.stdout,
                stderr=resp.stderr,
                command=cmd_str,
            )
        elif self._strict:
            raise AssertionError(f"MockShell: unexpected command: {cmd_str}")
        else:
            r = RunResult(
                returncode=127,
                stdout="",
                stderr=f"mock: not found: {cmd_str}",
                command=cmd_str,
            )

        if check and not r.ok:
            raise RuntimeError(f"Command failed: {cmd_str}")
        return r

    def mock_run_pipe(self, cmd1, cmd2, _log=None) -> RunResult:
        """Drop-in replacement for sh.run_pipe()."""
        combined = f"{cmd1} | {cmd2}"
        return self.mock_run(combined)

    # ── Assertions ───────────────────────────────────────────────────────

    def was_called(self, pattern: str) -> bool:
        """Check if a command matching pattern was called."""
        return any(pattern in c for c in self._calls)

    def was_not_called(self, pattern: str) -> bool:
        """
        Check if no command matching pattern was called.
        """
        return not self.was_called(pattern)

    def call_count(self, pattern: str) -> int:
        """
        Count how many times a command matching pattern was called.
        """
        return sum(1 for c in self._calls if pattern in c)

    @property
    def calls(self) -> list[str]:
        """Get the list of all command strings that were called."""
        return list(self._calls)

    def reset(self):
        """Clear call history and reset response call counts."""
        self._calls.clear()
        for r in self._responses:
            r.call_count = 0


@contextmanager
def patch_sh(mock: MockShell):
    """
    Context manager that patches sh.run and sh.run_pipe everywhere.

    Must patch at every import site because 'from lib.sh import run'
    binds a local reference that isn't affected by patching lib.sh.run.
    """
    targets = [
        "lib.sh",  # definition (covers 'from lib import sh; sh.run()')
        "lib.zfs",  # from lib.sh import run
        "lib.keystore",  # from lib.sh import run
        "lib.cleanup",  # from lib.sh import run
        "lib.drives",  # from lib.sh import run
        "lib.mount",  # from lib.sh import run
    ]

    patches = []
    for target in targets:
        patches.append(patch(f"{target}.run", side_effect=mock.mock_run))
    patches.append(patch("lib.sh.run_pipe", side_effect=mock.mock_run_pipe))

    # Stack all patches
    for p in patches:
        p.start()
    try:
        yield mock
    finally:
        for p in reversed(patches):
            p.stop()
