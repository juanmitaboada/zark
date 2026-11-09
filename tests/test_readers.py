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


class TestPendingFile:  # pylint: disable=missing-function-docstring
    """lib.pending: pending_drives.json, validated and written atomically (M20-4 4a)."""

    @staticmethod
    def _cfg() -> Any:
        from lib.config import Config  # pylint: disable=import-outside-toplevel

        cfg = Config()
        cfg.config_dir = Path(tempfile.mkdtemp())
        return cfg

    @staticmethod
    def _item(name: str = "old", guid: str = "9") -> Any:
        from lib import pending  # pylint: disable=import-outside-toplevel

        entry = {"guid": guid, "drive_id": "ata-X", "last_backup_at": None, "autoeject": False}
        return pending.Pending(name, entry, "2026-11-09T10:00:00Z", "/dev/disk/by-id/ata-X-part1")

    def test_absent_is_none_and_a_round_trip_keeps_everything(self):
        from lib import pending  # pylint: disable=import-outside-toplevel

        cfg = self._cfg()
        assert not pending.read(cfg)
        pending.add(cfg, self._item())
        got = pending.read(cfg)
        assert got == {"old": self._item()}, got
        assert stat.S_IMODE(os.stat(pending.path(cfg)).st_mode) == 0o644
        assert got["old"].info.guid == "9" and got["old"].guid == "9"
        assert pending.by_guid(got, "9") == got["old"] and pending.by_guid(got, "1") is None

    def test_remove_and_an_empty_file_goes(self):
        from lib import pending  # pylint: disable=import-outside-toplevel

        cfg = self._cfg()
        pending.add(cfg, self._item("a", "1"))
        pending.add(cfg, self._item("b", "2"))
        assert pending.remove(cfg, "a") and not pending.remove(cfg, "a")
        assert list(pending.read(cfg)) == ["b"]
        assert pending.remove(cfg, "b") and not pending.path(cfg).exists()

    def test_what_is_refused(self):
        from lib import pending  # pylint: disable=import-outside-toplevel

        cfg = self._cfg()
        for text, why in (
            ("{", "invalid JSON"),
            ('{"schema_version": 2, "pending": {}}', "not a schema 1"),
            ('{"schema_version": 1, "pending": []}', "'pending' must be an object"),
            ('{"schema_version": 1, "pending": {"a": {"entry": {"guid": "x"}}}}', "guid"),
            (
                '{"schema_version": 1, "pending": {"a": {"entry": '
                '{"guid": "1", "drive_id": "d"}, "prepared_at": "t"}}}',
                "needs 'prepared_at' and 'device'",
            ),
        ):
            pending.path(cfg).write_text(text, encoding="utf-8")
            try:
                pending.read(cfg)
            except pending.PendingError as e:
                assert why in str(e), (why, e)
            else:
                raise AssertionError(f"accepted: {text}")


class TestBackupDriveOption:  # pylint: disable=missing-function-docstring
    """Z4: ``backup --drive NAME`` picks among the connected registered drives,
    in place of select_drive's question; refusals touch nothing."""

    @staticmethod
    def _cfg(names: tuple[str, ...] = ("alpha", "beta")) -> Any:
        from lib.config import Config, DriveInfo  # pylint: disable=import-outside-toplevel

        cfg = Config()
        cfg.config_dir = Path(tempfile.mkdtemp())
        for i, name in enumerate(names, 1):
            cfg.known_drives[name] = DriveInfo(name, f"{i}{i}{i}", f"usb-{name}-0:0")
        return cfg

    @staticmethod
    def _drive(name: str, guid: str) -> Any:
        from lib.drives import ConnectedDrive  # pylint: disable=import-outside-toplevel

        return ConnectedDrive(
            name, guid, f"usb-{name}-0:0", f"/dev/{name}", True, False, False, "exported"
        )

    def _run(
        self, args: list[str], cfg: Any, drives: list[Any]
    ) -> tuple[int, str, list[tuple[str, str | None, str]], list[str]]:
        """Run backup until its import, which is refused: (exit status, text,
        the imports asked for, select_drive's calls)."""
        # pylint: disable=import-outside-toplevel,too-many-locals
        from contextlib import ExitStack, redirect_stderr

        from commands import backup as backup_mod
        from lib.zfs import ZFS

        imports: list[tuple[str, str | None, str]] = []
        selected: list[str] = []
        scanned: list[int] = []

        def _import(_self: Any, name: str, device: str | None, *, guid: str = "") -> bool:
            imports.append((name, device, guid))
            return False

        def _select(known: list[Any], _log: Any, known_only: bool = True) -> Any:
            selected.append(",".join(d.name for d in known) + f":{known_only}")
            return known[0]

        def _scan(_cfg: Any, _log: Any) -> list[Any]:
            scanned.append(1)
            return drives

        out = StringIO()
        rc = 0
        with ExitStack() as stack:
            for cm in (
                patch.object(backup_mod.Config, "load", return_value=cfg),
                patch.object(backup_mod, "_detect_live_usb", return_value=False),
                patch.object(backup_mod, "warn_rpool_mountpoint_lost"),
                patch.object(backup_mod, "warn_kernel_named_vdevs"),
                patch.object(backup_mod.certificates, "show_warnings"),
                patch.object(backup_mod.survey, "require_bookmark_v2"),
                patch.object(backup_mod.survey, "require_boot_layout", return_value="bpool"),
                patch.object(backup_mod.pool_record, "capture", return_value="{}"),
                patch.object(backup_mod.pool_record, "require"),
                patch.object(backup_mod, "scan_connected_drives", side_effect=_scan),
                patch.object(backup_mod, "select_drive", side_effect=_select),
                patch.object(backup_mod, "backup_device", side_effect=lambda d: d.dev_path),
                patch.object(backup_mod.progress, "set_drive"),
                patch.object(backup_mod.progress, "on_end"),
                patch.object(ZFS, "import_backup_pool", _import),
                patch.object(backup_mod.Cleanup, "register", lambda _self: None),
                patch_sh(MockShell()),
                patch("builtins.input", side_effect=EOFError),
                redirect_stdout(out),
                redirect_stderr(out),
            ):
                stack.enter_context(cm)
            try:
                backup_mod.run(args)
            except SystemExit as e:
                rc = int(e.code or 0)
        if not scanned:
            imports.append(("not scanned", None, ""))
        return rc, " ".join(out.getvalue().split()), imports, selected

    def test_each_connected_drive_is_picked_without_a_question(self):
        cfg = self._cfg()
        drives = [self._drive("alpha", "111"), self._drive("beta", "222")]
        for name, guid in (("alpha", "111"), ("beta", "222")):
            rc, text, imports, selected = self._run(["--drive", name], cfg, drives)
            assert rc == 1 and "Cannot import pool" in text, text
            assert imports == [(name, f"/dev/{name}", guid)], imports
            assert not selected, selected

    def test_without_the_option_the_question_is_today_s(self):
        cfg = self._cfg()
        drives = [self._drive("alpha", "111"), self._drive("beta", "222")]
        _, _, imports, selected = self._run([], cfg, drives)
        assert selected == ["alpha,beta:True"], selected
        assert imports == [("alpha", "/dev/alpha", "111")], imports

    def test_one_drive_connected_and_named(self):
        cfg = self._cfg()
        _, _, imports, selected = self._run(["--drive", "beta"], cfg, [self._drive("beta", "222")])
        assert imports == [("beta", "/dev/beta", "222")] and not selected, (imports, selected)

    def test_a_registered_drive_not_connected_is_refused(self):
        cfg = self._cfg()
        # An unknown pool of that name on another disk is not it.
        stranger = self._drive("beta", "999")
        stranger.known = False
        rc, text, imports, selected = self._run(
            ["--drive", "beta"], cfg, [self._drive("alpha", "111"), stranger]
        )
        assert rc == 1 and "beta is not connected" in text, text
        assert "Connected registered drives: alpha" in text, text
        assert not imports and not selected, (imports, selected)

    def test_a_registered_prefix_of_a_connected_name_is_not_it(self):
        cfg = self._cfg(("back", "backup"))
        rc, text, imports, _ = self._run(["--drive", "back"], cfg, [self._drive("backup", "222")])
        assert rc == 1 and "back is not connected" in text, text
        assert not imports, imports

    def test_an_unknown_name_and_a_prefix_are_refused(self):
        cfg = self._cfg()
        drives = [self._drive("alpha", "111")]
        for name in ("gamma", "alp"):
            rc, text, imports, _ = self._run(["--drive", name], cfg, drives)
            assert rc == 1 and f"{name} is not a registered drive" in text, text
            assert "Registered: alpha, beta" in text, text
            assert "registry verify" not in text, text
            assert not imports, imports

    def test_a_pending_name_is_refused_with_the_verify_command(self):
        from lib import pending  # pylint: disable=import-outside-toplevel

        cfg = self._cfg()
        entry = {"guid": "333", "drive_id": "ata-X", "last_backup_at": None, "autoeject": False}
        pending.add(cfg, pending.Pending("gamma", entry, "2026-11-09T10:00:00Z", "/dev/gamma1"))
        rc, text, imports, _ = self._run(["--drive", "gamma"], cfg, [self._drive("alpha", "111")])
        assert rc == 1 and "gamma is not a registered drive" in text, text
        assert "gamma is waiting for a read-back" in text, text
        assert "registry verify gamma" in text, text
        assert not imports, imports

    def test_a_missing_value_is_a_usage_error_before_the_scan(self):
        cfg = self._cfg()
        drives = [self._drive("alpha", "111")]
        for args in (["--drive"], ["--drive", "--no-snapshot"], ["--drive", ""]):
            rc, text, imports, selected = self._run(args, cfg, drives)
            assert rc == 2 and "backup: --drive needs a drive name" in text, (args, text)
            assert imports == [("not scanned", None, "")] and not selected, imports

    def test_with_no_snapshot_in_either_order(self):
        from commands.backup import _parse_args  # pylint: disable=import-outside-toplevel

        for args in (["--no-snapshot", "--drive", "beta"], ["--drive", "beta", "--no-snapshot"]):
            got = _parse_args(args)
            assert got.drive == "beta" and not got.take_snapshots, (args, got)
        assert _parse_args([]).drive == ""

    def test_completion_offers_the_option_and_the_registered_names(self):
        from lib import completion  # pylint: disable=import-outside-toplevel

        d = Path(tempfile.mkdtemp())
        (d / "known_drives.json").write_text('{"alpha": {"guid": "1", "drive_id": "x"}}')
        with patch.dict(os.environ, {"ZARK_CONFIG_DIR": str(d)}):
            opts = completion.complete("zark backup --", Path("/nonexistent"))
            names = completion.complete("zark backup --drive ", Path("/nonexistent"))
        assert "--drive" in opts[1], opts
        assert names == ("words", ["alpha"]), names


class TestInvokingUser:  # pylint: disable=missing-function-docstring
    """Z2: zark.log names the uid behind each run, before the lock."""

    @staticmethod
    def _who(env: dict[str, str], *, getuid: int = 0) -> tuple[int, str, str]:
        from lib import invoker  # pylint: disable=import-outside-toplevel

        def _pw(uid: int) -> Any:
            if uid == 4242:
                raise KeyError(uid)
            return type("Pw", (), {"pw_name": {0: "root", 1000: "juanmi", 1001: "eli"}[uid]})

        with (
            patch.object(invoker.os, "getuid", return_value=getuid),
            patch.object(invoker.pwd, "getpwuid", side_effect=_pw),
        ):
            return invoker.invoker(env)

    def test_each_source(self):
        assert self._who({"PKEXEC_UID": "1000"}) == (1000, "juanmi", "pkexec")
        assert self._who({"SUDO_UID": "1001"}) == (1001, "eli", "sudo")
        assert self._who({}) == (0, "root", "root")

    def test_pkexec_comes_before_sudo(self):
        got = self._who({"PKEXEC_UID": "1000", "SUDO_UID": "1001"})
        assert got == (1000, "juanmi", "pkexec"), got

    def test_a_value_that_is_not_an_integer_is_ignored(self):
        for bad in ("", "x", "-1", "10 00", "²"):
            got = self._who({"PKEXEC_UID": bad, "SUDO_UID": "1001"})
            assert got == (1001, "eli", "sudo"), (bad, got)
            assert self._who({"SUDO_UID": bad}) == (0, "root", "root"), bad

    def test_a_uid_with_no_passwd_entry_is_its_number(self):
        assert self._who({"SUDO_UID": "4242"}) == (4242, "4242", "sudo")

    def test_the_line(self):
        from lib import invoker  # pylint: disable=import-outside-toplevel

        with patch.object(invoker, "invoker", return_value=(1000, "juanmi", "sudo")):
            line = invoker.run_line("backup", ["--drive", "alpha"])
        assert line == "zark backup --drive alpha run by uid 1000 (juanmi) via sudo", line

    def test_the_entry_point_logs_it_before_the_lock_and_prints_nothing(self):
        # pylint: disable=import-outside-toplevel
        import importlib.machinery
        import importlib.util
        import sys
        from contextlib import ExitStack

        from lib import lock

        path = str(Path(__file__).resolve().parent.parent / "zark")
        loader = importlib.machinery.SourceFileLoader("zark_entry", path)
        spec = importlib.util.spec_from_file_location("zark_entry", path, loader=loader)
        assert spec is not None
        entry = importlib.util.module_from_spec(spec)
        loader.exec_module(entry)
        order: list[str] = []
        out = StringIO()

        def _held(*_a: Any) -> None:
            order.append("guard")
            raise SystemExit(75)

        with ExitStack() as stack:
            for cm in (
                patch.object(sys, "argv", ["zark", "prune", "--dry-run"]),
                patch("os.geteuid", return_value=0),
                patch.dict(os.environ, {"SUDO_UID": "1000"}),
                patch.object(Log, "file", lambda _s, m: order.append(f"file:{m}")),
                patch.object(lock, "guard", side_effect=_held),
                redirect_stdout(out),
            ):
                stack.enter_context(cm)
            try:
                entry.main()
            except SystemExit as e:
                assert e.code == 75, e.code
        assert len(order) == 2 and order[1] == "guard", order
        assert order[0].startswith("file:zark prune --dry-run run by uid 1000 ("), order
        assert order[0].endswith(") via sudo"), order
        assert out.getvalue() == "", out.getvalue()
