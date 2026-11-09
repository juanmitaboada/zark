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
Tests of 2.3's readers and small fixes (M20): the health report's file,
simulate's temporary files, the status lines, exclude's and prune's
warnings and tables, the MOTD, the chroot binds, the pending registration,
``backup --drive``, the invoking user and the question object.

``tests/test_unit.py`` imports every Test* class here by name, so its runner
finds them; run that file, not this one.
"""

# pylint: disable=too-many-lines

import os
import stat
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from lib import health
from lib.log import Log
from tests.mock_sh import MockShell, patch_sh


class TestHealthReportFile:  # pylint: disable=missing-function-docstring
    """M23T-1: the report through mkstemp, 0600, owned by the user of sudo."""

    @staticmethod
    def _dir() -> str:
        return tempfile.mkdtemp()

    def test_a_new_private_file_each_time(self):
        d = self._dir()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUDO_UID", None)
            os.environ.pop("SUDO_GID", None)
            first = health.write_report_file("one", directory=d)
            second = health.write_report_file("two", directory=d)
        assert first != second, first
        for path, text in ((first, "one"), (second, "two")):
            name = Path(path).name
            assert name.startswith("zark-health-report-") and name.endswith(".txt"), name
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, oct(os.stat(path).st_mode)
            assert Path(path).read_text(encoding="utf-8") == text

    def test_never_opened_by_name(self):
        d = self._dir()
        with (
            patch.object(Path, "write_text", side_effect=AssertionError("by name")),
            patch("builtins.open", side_effect=AssertionError("by name")),
        ):
            path = health.write_report_file("x", directory=d)
        assert Path(path).read_text(encoding="utf-8") == "x"

    def test_handed_to_the_user_of_sudo(self):
        d = self._dir()
        with (
            patch.dict(os.environ, {"SUDO_UID": "1000", "SUDO_GID": "1001"}),
            patch.object(os, "fchown") as fchown,
        ):
            _ = health.write_report_file("x", directory=d)
        assert fchown.call_count == 1 and fchown.call_args.args[1:] == (1000, 1001), (
            fchown.call_args
        )

    def test_root_s_without_a_usable_sudo_user(self):
        for env in ({}, {"SUDO_UID": "1000"}, {"SUDO_UID": "x", "SUDO_GID": "1"}):
            d = self._dir()
            clean = {k: v for k, v in os.environ.items() if k not in ("SUDO_UID", "SUDO_GID")}
            with (
                patch.dict(os.environ, {**clean, **env}, clear=True),
                patch.object(os, "fchown") as fchown,
            ):
                _ = health.write_report_file("x", directory=d)
            fchown.assert_not_called()


class TestSimulateTempFiles:  # pylint: disable=missing-function-docstring
    """M23T-2: the OVMF vars and the serial log in one mkdtemp directory."""

    @staticmethod
    def _run(write_log: bool, qemu_missing: bool = False) -> tuple[list[str], str, str]:
        import commands.simulate as sim  # pylint: disable=import-outside-toplevel

        root = tempfile.mkdtemp()
        made: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(**kw: Any) -> str:
            d = real_mkdtemp(dir=root, **kw)
            made.append(d)
            return d

        seen: list[list[str]] = []

        def qemu(cmd: list[str], **_kw: Any) -> int:
            seen.append(list(cmd))
            if qemu_missing:
                raise FileNotFoundError
            if write_log:
                log_arg = cmd[cmd.index("-serial") + 1]
                Path(log_arg.removeprefix("file:")).write_text("boot\n", encoding="utf-8")
            return 0

        firmware = Path(root, "OVMF_src.fd")
        firmware.write_bytes(b"vars")
        mock = MockShell()
        mock.on("which qemu-system-x86_64").succeeds("/usr/bin/qemu-system-x86_64")
        with (
            patch_sh(mock),
            patch.object(sim, "_detect_gl", return_value=(False, "test")),
            patch.object(sim, "_find_file", return_value=str(firmware)),
            patch.object(sim, "_disk_in_use_reasons", return_value=[]),
            patch.object(sim.tempfile, "mkdtemp", side_effect=mkdtemp),
            patch.object(sim.signals, "run_interactive", side_effect=qemu),
            patch("builtins.input", side_effect=EOFError),  # the pause after a fatal
            redirect_stdout(StringIO()) as out,
        ):
            try:
                sim.run(["/dev/null"])  # exists and reads; in-use check stubbed
            except SystemExit:
                pass
        assert len(made) == 1, made
        return seen[0] if seen else [], made[0], out.getvalue()

    def test_both_files_in_the_run_s_private_directory(self):
        cmd, run_dir, out = self._run(write_log=True)
        assert stat.S_IMODE(os.stat(run_dir).st_mode) == 0o700, oct(os.stat(run_dir).st_mode)
        assert Path(run_dir).name.startswith("zark-simulate-"), run_dir
        joined = " ".join(cmd)
        assert f"file={run_dir}/OVMF_VARS.fd" in joined, joined
        assert f"file:{run_dir}/serial.log" in joined, joined
        assert "/tmp/zark_" not in joined, joined
        assert not Path(run_dir, "OVMF_VARS.fd").exists()
        assert Path(run_dir, "serial.log").exists()
        assert f"Kernel/serial log saved at: {run_dir}/serial.log" in out, out

    def test_an_empty_directory_is_removed(self):
        _, run_dir, out = self._run(write_log=False)
        assert not Path(run_dir).exists(), run_dir
        assert "serial log saved" not in out

    def test_the_vars_go_when_qemu_is_missing(self):
        _, run_dir, out = self._run(write_log=False, qemu_missing=True)
        assert "qemu-system-x86_64 not found in PATH" in out, out
        assert not Path(run_dir, "OVMF_VARS.fd").exists()


class TestStatusLastRun:  # pylint: disable=missing-function-docstring
    """M13E.2: zark status names a drive's last run when it calls for attention."""

    @staticmethod
    def _out(*runs: dict[str, Any] | None) -> str:
        import commands.status as status_cmd  # pylint: disable=import-outside-toplevel

        drives = [
            {"name": f"d{i}", **({"last_run": r} if r is not None else {})}
            for i, r in enumerate(runs)
        ]
        with redirect_stdout(StringIO()) as out:
            status_cmd._last_runs({"drives": drives}, Log(os.devnull))  # pylint: disable=protected-access
        return " ".join(out.getvalue().split())

    @staticmethod
    def _run(result: str, missing: int = 0, message: str = "why") -> dict[str, Any]:
        return {
            "result": result,
            "ended_at": "2026-11-09T10:00:00Z",
            "datasets_missing": missing,
            "message": message,
        }

    def test_the_three_attention_results(self):
        text = self._out(
            self._run("interrupted", message="interrupted by SIGINT"),
            self._run("incomplete", 2, "2 datasets failed"),
            self._run("not_verified", message="d2 failed its read-back"),
        )
        assert "d0: last run interrupted (" in text and ": interrupted by SIGINT" in text, text
        assert "d1: last run incomplete (" in text and ", 2 dataset(s) missing" in text, text
        assert "d2: last run not_verified (" in text, text
        assert "missing" not in text.split("d1:", maxsplit=1)[0], (
            text
        )  # interrupted: 0 is not printed

    def test_nothing_for_the_others(self):
        text = self._out(self._run("completed"), self._run("aborted"), None)
        assert text == "", text

    def test_no_file_no_line(self):
        import commands.status as status_cmd  # pylint: disable=import-outside-toplevel

        with redirect_stdout(StringIO()) as out:
            status_cmd._last_runs(None, Log(os.devnull))  # pylint: disable=protected-access
        assert out.getvalue() == ""

    def test_run_prints_it_after_the_point_records(self):
        import commands.status as status_cmd  # pylint: disable=import-outside-toplevel

        doc = {"drives": [{"name": "bk", "last_run": self._run("interrupted")}]}
        with (
            patch.object(status_cmd.status_file, "refresh"),
            patch.object(status_cmd.status_file, "read", return_value=doc),
            patch.object(status_cmd, "_report"),
            patch.object(status_cmd, "_exclusions"),
            patch.object(status_cmd.Config, "load"),
            redirect_stdout(StringIO()) as out,
        ):
            status_cmd.run([])
        assert "bk: last run interrupted" in out.getvalue(), out.getvalue()
