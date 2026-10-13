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
Unit tests for zark — all modules and command logic.

Run:  python3 -m pytest tests/test_unit.py -v
  or: python3 tests/test_unit.py

Tests run WITHOUT root, ZFS, or real disks. All shell commands are mocked.
"""

# pylint: disable=too-many-lines
# Rationale: this is the single-file unit suite by design. A `main()` runner
# at the bottom of this file lets developers run tests without pytest, and
# splitting per-class would scatter that contract. The agreement is that
# only test code lives here — production modules stay under the standard
# line limit.

import atexit
import inspect
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

from unittest import SkipTest  # pylint: disable=wrong-import-position # noqa: E402
from unittest.mock import MagicMock, patch  # pylint: disable=wrong-import-position # noqa: E402

import commands.backup as backup_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.chroot as chroot_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.clean as clean_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.finish as finish_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.fix_rpool_mountpoint as fix_rpool_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.monitor as monitor_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.mount as mount_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.prepare as prepare_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.prune as prune_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.purge as purge_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.recover as recover_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.registry as registry_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.repair_boot as repair_boot_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.status as status_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.umount as umount_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.alerts as alerts_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.drives as drives_lib  # pylint: disable=wrong-import-position # noqa: E402
import lib.grub_cfg as grub_cfg_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.notify as notify_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.origin_prune as oprune  # pylint: disable=wrong-import-position # noqa: E402
import lib.replication as repl  # pylint: disable=wrong-import-position # noqa: E402
import lib.retention as ret  # pylint: disable=wrong-import-position # noqa: E402
import lib.sh as _sh  # pylint: disable=wrong-import-position # noqa: E402
import lib.status as status_lib  # pylint: disable=wrong-import-position # noqa: E402
from commands.backup import (  # pylint: disable=wrong-import-position # noqa: E402
    _check_target_space,
    _decide,
    _detect_live_usb,
    _parse_args as _backup_parse_args,
    _report_rpo,
    _show_table,
)
from commands.monitor import _draw_bar  # pylint: disable=wrong-import-position # noqa: E402
from commands.recover import (  # pylint: disable=wrong-import-position # noqa: E402
    RestorePlan,
    RestoreRow,
    _abort_missing_keystore,
    _absent_note,
    _check_sizes,
    _choose_point,
    _force_latest_signed_alternative,
    _mount_restored_system,
    _plan,
    _preflight,
    _target_candidates,
)
from commands.setup import (  # pylint: disable=wrong-import-position # noqa: E402
    _TEMPLATE_MINIMAL_EXPECTED,
    SanoidDiff,
    SanoidRule,
    _classify,
    _diff_rules,
    _discover_rules,
    _format_rule,
    _generate_sanoid_conf,
    _parse_sanoid_conf,
    _print_diff,
    _signed_alternative_status,
    ensure_bookmark_v2,
)
from commands.simulate import (  # pylint: disable=wrong-import-position # noqa: E402
    OVMF_CODE_CANDIDATES,
    _detect_gl,
    _disk_in_use_reasons,
    _list_candidate_disks,
    _parse_args,
)
from commands.umount import (  # pylint: disable=wrong-import-position # noqa: E402
    _umount_local_system,
)
from lib import (  # pylint: disable=wrong-import-position # noqa: E402
    apt_guard,
    engine,
)
from lib.cleanup import (  # pylint: disable=wrong-import-position # noqa: E402
    Cleanup,
    eject_device,
    flush_device_cache,
    prompt_eject_or_attach,
)
from lib.config import (  # pylint: disable=wrong-import-position # noqa: E402
    VERSION,
    Config,
    DriveInfo,
    now_utc_iso,
    parse_utc_iso,
)
from lib.drives import (  # pylint: disable=wrong-import-position # noqa: E402
    ConnectedDrive,
    drive_staleness_days,
    is_drive_stale,
    shared_snapshot_line,
    validate_external_block_device,
)
from lib.health import (  # pylint: disable=wrong-import-position # noqa: E402
    INFO,
    KNOWN_BAD_BRIDGES,
    OK,
    PROFILE_FAST,
    PROFILE_MEDIUM,
    PROFILE_SURFACE,
    SURFACE_CAP_BYTES,
    WARN,
    Finding,
    HealthReport,
    _check_fua,
    _check_transport,
    _transport_errors_since,
    estimate_seconds,
    generate_report,
    profile_target_bytes,
    run_destructive_test,
)
from lib.identity import (  # pylint: disable=wrong-import-position # noqa: E402
    DiskIdentity,
    IdentityError,
    PoolLabel,
    by_id_names,
    match_registry,
    preferred_by_id,
    protected_disks,
    read_pool_label,
    resolve_disk,
)
from lib.initrd import (  # pylint: disable=wrong-import-position # noqa: E402
    installed_kernels,
    regenerate_initrd,
)
from lib.keystore import (  # pylint: disable=wrong-import-position # noqa: E402
    Keystore,
    open_keystore,
)
from lib.log import Log  # pylint: disable=wrong-import-position # noqa: E402
from lib.mount import (  # pylint: disable=wrong-import-position # noqa: E402
    find_system_root_dataset,
    kernel_named_vdevs,
    mount_system_pools,
    rpool_mountpoint_lost,
)
from lib.mount_props import (  # pylint: disable=wrong-import-position # noqa: E402
    MountProps,
    choose as choose_props,
    parse_list_cache,
    receive_options,
    rpool_root_mountpoint,
)
from lib.registry import (  # pylint: disable=wrong-import-position # noqa: E402
    RegistryError,
    parse as registry_parse,
    serialize as registry_serialize,
    write_atomic as registry_write_atomic,
)
from lib.restore_points import (  # pylint: disable=wrong-import-position # noqa: E402
    COMPLETE_PROP,
    Snap,
    complete_points,
    legacy_complete,
    logical,
    parse_snapshots,
    point_name,
    resolve,
    restore_points,
    retention_points,
)
from lib.sanoid_retention import (  # pylint: disable=wrong-import-position # noqa: E402
    _guaranteed_days_of_template,
    guaranteed_lives,
    guaranteed_overlap_days,
)
from lib.sh import RunResult, part, run  # pylint: disable=wrong-import-position # noqa: E402
from lib.zfs import (  # pylint: disable=wrong-import-position # noqa: E402
    ZFS,
    DatasetInfo,
    PoolInfo,
    Readback,
    fix_grub_bpool_uuid,
)
from tests.fake_zfs import (  # pylint: disable=wrong-import-position # noqa: E402
    FakeZfs,
    FDataset,
    FSnap,
)
from tests.mock_sh import MockShell, patch_sh  # pylint: disable=wrong-import-position # noqa: E402

# ═════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════


def make_log() -> Log:
    """Create a Log that writes to /dev/null."""
    return Log(log_file="/dev/null")


# Commands under test build Log() with its default file: as root that is the
# machine's real /var/log/zark.log (or the live stick's). Redirect it for the
# whole module, under either runner; main() checks the real files stayed put.
_REAL_LOG_FILES = (
    "/var/log/zark.log",
    str(Path(__file__).resolve().parent.parent / "zark.log"),
)
_TEST_LOG_DIR = tempfile.mkdtemp(prefix="zark-test-log-")
atexit.register(shutil.rmtree, _TEST_LOG_DIR, ignore_errors=True)
_ = patch.object(
    Log,
    "default_file",
    classmethod(lambda _cls: os.path.join(_TEST_LOG_DIR, "zark.log")),
).start()


def _real_log_state() -> dict[str, tuple[int, int] | None]:
    """(size, mtime_ns) of each real log file, None when absent."""
    state: dict[str, tuple[int, int] | None] = {}
    for path in _REAL_LOG_FILES:
        try:
            st = os.stat(path)
            state[path] = (st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            state[path] = None
    return state


def make_config(**overrides) -> Config:
    """Create a Config with sane defaults for testing."""
    cfg = Config()
    cfg.config_dir = __import__("pathlib").Path(tempfile.mkdtemp())
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_mock_zfs() -> tuple[MockShell, ZFS]:
    """Create a MockShell pre-configured for common ZFS responses."""
    mock = MockShell()
    log = make_log()
    zfs = ZFS(log)
    return mock, zfs


# ═════════════════════════════════════════════════════════════════════════
#  lib/config.py
# ═════════════════════════════════════════════════════════════════════════


class TestConfig:
    """
    Tests for Config loading, saving, and drive registration.
    """

    def test_load_empty_dir(self):
        """Config loads cleanly when no file exists."""
        with tempfile.TemporaryDirectory() as td:
            os.environ["ZARK_CONFIG_DIR"] = td
            cfg = Config.load()
            assert len(cfg.known_drives) == 0
            del os.environ["ZARK_CONFIG_DIR"]

    def test_load_valid_json(self):
        """Config loads drives from JSON."""
        with tempfile.TemporaryDirectory() as td:
            data = {"backup": {"guid": "1234567890123456789", "drive_id": "usb-Micron-0:0"}}
            with open(os.path.join(td, "known_drives.json"), "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.environ["ZARK_CONFIG_DIR"] = td
            cfg = Config.load()
            assert "backup" in cfg.known_drives
            assert cfg.known_drives["backup"].guid == "1234567890123456789"
            del os.environ["ZARK_CONFIG_DIR"]

    def test_save_roundtrip(self):
        """Save and reload produces identical data."""
        with tempfile.TemporaryDirectory() as td:
            os.environ["ZARK_CONFIG_DIR"] = td
            cfg = Config.load()
            cfg.config_dir = __import__("pathlib").Path(td)
            cfg.known_drives["mypool"] = DriveInfo("mypool", "99999", "usb-Test-0:0")
            cfg.save_drives()

            cfg2 = Config.load()
            assert "mypool" in cfg2.known_drives
            assert cfg2.known_drives["mypool"].guid == "99999"
            del os.environ["ZARK_CONFIG_DIR"]

    def test_corrupt_json(self):
        """Config handles corrupt JSON gracefully."""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "known_drives.json"), "w", encoding="utf-8") as f:
                f.write("{corrupt!!!")
            os.environ["ZARK_CONFIG_DIR"] = td
            cfg = Config.load()
            assert len(cfg.known_drives) == 0
            del os.environ["ZARK_CONFIG_DIR"]

    def test_drive_registration_line(self):
        """DriveInfo produces correct registration JSON."""
        cfg = Config()
        line = cfg.drive_registration_line("pool1", "12345", "usb-X-0:0")
        assert '"pool1"' in line
        assert '"12345"' in line

    def test_portable_config_detection(self):
        """Config finds etc/ relative to project root."""
        root = Config.zark_root()
        etc = root / "etc"
        assert etc.name == "etc"

    def test_system_install_fallback(self):
        """
        When the package is installed under /usr/share/zark/ (the .deb
        layout), <zark_root>/etc/known_drives.json does not exist and
        the lookup falls back to /etc/zark/known_drives.json.

        We simulate this by:
          - pointing zark_root() at a tmp dir that has an empty etc/
          - stubbing Path("/etc/zark") to also be a tmp dir, this time
            with known_drives.json present.
        """
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)

            # Layout: zark_root with empty etc/ (= what the .deb ships)
            fake_zark_root = base / "usr_share_zark"
            (fake_zark_root / "etc").mkdir(parents=True)

            # Layout: simulated /etc/zark with known_drives.json present
            fake_etc_zark = base / "etc_zark"
            fake_etc_zark.mkdir()
            (fake_etc_zark / "known_drives.json").write_text("{}", encoding="utf-8")

            def _path_factory(p):
                # Redirect the hard-coded "/etc/zark" lookup to our fake;
                # everything else goes through the real Path constructor.
                if str(p) == "/etc/zark":
                    return fake_etc_zark
                return Path(p)

            # Make sure the env override is not set (otherwise step 1 wins).
            env_backup = os.environ.pop("ZARK_CONFIG_DIR", None)
            try:
                with (
                    patch.object(Config, "zark_root", return_value=fake_zark_root),
                    patch("lib.config.Path", side_effect=_path_factory),
                ):
                    result = Config.default_config_dir()
                assert result == fake_etc_zark, (
                    f"Expected fallback to /etc/zark (= {fake_etc_zark}), got {result}"
                )
            finally:
                if env_backup is not None:
                    os.environ["ZARK_CONFIG_DIR"] = env_backup

    def test_system_install_default_with_no_config(self):
        """
        Fresh .deb install before the user creates known_drives.json:
        zark_root is /usr/share/zark, neither <root>/etc/known_drives.json
        nor /etc/zark/known_drives.json exist yet. The default must point
        at /etc/zark (writable, where postinst created the directory),
        NOT at /usr/share/zark/etc which is dpkg-managed and read-only.
        """
        sys_install_root = Path("/usr/share/zark")
        env_backup = os.environ.pop("ZARK_CONFIG_DIR", None)
        try:
            with (
                patch.object(Config, "zark_root", return_value=sys_install_root),
                # Make every "exists()" return False so neither portable
                # nor system have a known_drives.json yet.
                patch.object(Path, "exists", return_value=False),
                patch.object(Path, "is_dir", return_value=False),
            ):
                result = Config.default_config_dir()
            assert result == Path(
                "/etc/zark",
            ), f"system install with no config should default to /etc/zark, got {result}"
        finally:
            if env_backup is not None:
                os.environ["ZARK_CONFIG_DIR"] = env_backup

    def test_portable_default_with_no_config(self):
        """
        Fresh portable run before the user creates known_drives.json:
        zark_root is e.g. /home/user/zark, neither <root>/etc/known_drives.json
        nor /etc/zark/known_drives.json exist yet. The default must
        point at <zark_root>/etc (writable, alongside the script).
        """
        portable_root = Path("/home/user/zark")
        env_backup = os.environ.pop("ZARK_CONFIG_DIR", None)
        try:
            with (
                patch.object(Config, "zark_root", return_value=portable_root),
                patch.object(Path, "exists", return_value=False),
                patch.object(Path, "is_dir", return_value=False),
            ):
                result = Config.default_config_dir()
            assert result == portable_root / "etc", (
                f"portable run with no config should default to <root>/etc, got {result}"
            )
        finally:
            if env_backup is not None:
                os.environ["ZARK_CONFIG_DIR"] = env_backup


# ═════════════════════════════════════════════════════════════════════════
#  lib/log.py
# ═════════════════════════════════════════════════════════════════════════


class TestLog:
    """Tests for Log ANSI stripping and fatal error handling."""

    def test_default_log_file_is_redirected_in_tests(self):
        """A Log() built by a command under test never targets a real log (R2-3)."""
        assert Log().log_file == os.path.join(_TEST_LOG_DIR, "zark.log")
        assert Log().log_file not in _REAL_LOG_FILES

    def test_passphrase_box_is_shown_before_the_prompt(self):
        """getpass writes to /dev/tty; with stdout piped (`| tee`, block
        buffered) the box came after "Passphrase:" (eli, N5)."""

        class Piped(StringIO):
            """A block-buffered pipe: only flushed text reaches the terminal."""

            def __init__(self) -> None:
                super().__init__()
                self.shown = ""

            def flush(self) -> None:
                self.shown = self.getvalue()

        out = Piped()
        seen: list[str] = []

        def prompt(_text: str) -> str:
            seen.append(out.shown)
            return "secret"

        with patch("lib.log.getpass.getpass", side_effect=prompt), redirect_stdout(out):
            assert make_log().ask_password("Passphrase for backup") == "secret"
        assert "Passphrase for backup" in seen[0]

    def test_strip_ansi(self):
        """Log strips ANSI codes for file output."""
        log = make_log()
        assert log._strip("\033[0;31mRED\033[0m") == "RED"  # pylint: disable=protected-access
        assert log._strip("no colors") == "no colors"  # pylint: disable=protected-access

    def test_strip_complex_ansi(self):
        """Strips nested and multi-code sequences."""
        log = make_log()
        raw = f"{log.BOLD}{log.G}✅ SUCCESS{log.N}"
        assert log._strip(raw) == "✅ SUCCESS"  # pylint: disable=protected-access

    def test_fatal_raises_systemexit(self):
        """fatal() raises SystemExit(1)."""
        log = make_log()
        with patch("builtins.input", return_value=""):
            try:
                log.fatal("test failure")
            except SystemExit as e:
                assert e.code == 1

    def test_banner_safe_unplug_contains_drive_name(self):
        """banner_safe_unplug emits a visible message naming the drive.
        Operators rely on this as the final signal that the USB drive
        can be physically disconnected."""
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            log.banner_safe_unplug("blue")
        out = buf.getvalue()
        assert "Safe to unplug" in out
        assert "blue" in out

    def test_banner_safe_unplug_writes_to_log_file(self):
        """The banner also lands in the log file (with ANSI stripped),
        so post-mortem inspection can confirm whether the operator was
        told it was safe to unplug."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".log") as f:
            log_path = f.name
        try:
            log = Log(log_file=log_path)
            with redirect_stdout(StringIO()):
                log.banner_safe_unplug("carmenblue")
            with open(log_path, encoding="utf-8") as f:
                contents = f.read()
            assert "Safe to unplug drive 'carmenblue'" in contents
        finally:
            os.unlink(log_path)

    def test_banner_drive_attached_contains_drive_name(self):
        """banner_drive_attached signals 'flushed but still attached' for
        the eject-declined path. Operator should see the drive name and
        the 'still attached' wording so they know the state."""
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            log.banner_drive_attached("blue")
        out = buf.getvalue()
        assert "still attached" in out
        assert "blue" in out
        # Distinct from the safe-to-unplug banner — operators must not
        # confuse the two states.
        assert "Safe to unplug" not in out


# ═════════════════════════════════════════════════════════════════════════
#  lib/sh.py
# ═════════════════════════════════════════════════════════════════════════


class TestSh:  # pylint: disable=missing-function-docstring
    """Tests for RunResult parsing and run() behavior with mocked commands."""

    def test_run_result_ok(self):
        r = RunResult(returncode=0, stdout="hello\nworld\n", stderr="", command="test")
        assert r.ok
        assert r.output == "hello\nworld"
        assert r.lines == ["hello", "world"]

    def test_run_result_fail(self):
        r = RunResult(returncode=1, stdout="", stderr="err", command="fail")
        assert not r.ok

    def test_run_basic(self):
        r = run("echo hello")
        assert r.ok
        assert r.output == "hello"

    def test_run_failure(self):
        r = run("false")
        assert not r.ok
        assert r.returncode == 1

    def test_run_timeout(self):
        r = run("sleep 10", timeout=1)
        assert r.returncode == 124

    def test_run_not_found(self):
        r = run("totally_nonexistent_command_zzz")
        assert not r.ok
        assert r.returncode == 127


class TestShRunPipe:  # pylint: disable=missing-function-docstring
    """Tests for run_pipe() pipeline behavior, especially p1-side failures."""

    def test_pipe_both_succeed(self):
        r = _sh.run_pipe("echo hello world", "tr a-z A-Z")
        assert r.ok
        assert r.output == "HELLO WORLD"

    def test_pipe_p2_fails(self):
        # p2 (false) exits non-zero; result must be non-zero
        r = _sh.run_pipe("echo data", "false")
        assert not r.ok
        assert r.returncode == 1

    def test_pipe_p1_fails_p2_clean_eof(self):
        # p1 fails AFTER writing some output; p2 reads it and exits clean.
        # Pre-fix run_pipe missed this case (returncode came only from p2).
        # The 'sh -c' wrapper writes one line to stdout, then exits 7.
        r = _sh.run_pipe("sh -c 'echo partial; exit 7'", "cat")
        assert not r.ok, "p1 failure must surface as non-zero returncode"
        assert r.returncode == 7

    def test_pipe_combines_stderr(self):
        # Both sides write to stderr; the combined stderr must contain both.
        r = _sh.run_pipe(
            "sh -c 'echo p1err 1>&2; echo data'",
            "sh -c 'cat; echo p2err 1>&2'",
        )
        assert r.ok  # both exit 0
        assert "p1err" in r.stderr
        assert "p2err" in r.stderr
        assert r.stderr_left.strip() == "p1err"
        assert r.stderr_right.strip() == "p2err"

    def test_pipe_p2_takes_precedence_when_both_fail(self):
        # If both sides fail, the downstream (p2) returncode is what
        # the caller usually cares about — it's the more direct symptom.
        r = _sh.run_pipe(
            "sh -c 'echo data; exit 5'",
            "sh -c 'cat >/dev/null; exit 9'",
        )
        assert not r.ok
        assert r.returncode == 9


class TestShIsEnospc:  # pylint: disable=missing-function-docstring
    """Tests for is_enospc() marker detection."""

    def test_empty(self):
        assert not _sh.is_enospc("")
        assert not _sh.is_enospc(None)  # type: ignore[arg-type]

    def test_unrelated_error(self):
        assert not _sh.is_enospc("permission denied")
        assert not _sh.is_enospc("cannot import 'rpool': pool already exists")

    def test_no_space_left_on_device(self):
        assert _sh.is_enospc("write: No space left on device")
        # case-insensitive
        assert _sh.is_enospc("WRITE: NO SPACE LEFT ON DEVICE")

    def test_zfs_receive_out_of_space(self):
        assert _sh.is_enospc(
            "cannot receive incremental stream: out of space",
        )
        assert _sh.is_enospc(
            "cannot receive new filesystem stream: out of space",
        )

    def test_enospc_literal(self):
        assert _sh.is_enospc("ENOSPC")
        assert _sh.is_enospc("syncoid: error: ENOSPC reported by zfs receive")

    def test_disk_quota_exceeded(self):
        assert _sh.is_enospc("write: Disk quota exceeded")

    def test_in_combined_pipeline_stderr(self):
        # Realistic combined stderr from `zfs send | zfs receive`
        combined = (
            "warning: cannot send 'rpool/foo@x': bla\n"
            "cannot receive incremental stream: out of space\n"
        )
        assert _sh.is_enospc(combined)


class TestShHumanizeBytes:  # pylint: disable=missing-function-docstring
    """Tests for humanize_bytes() byte-count formatting (IEC units)."""

    def test_zero(self):
        assert _sh.humanize_bytes(0) == "0B"

    def test_bytes_no_decimal(self):
        # Sub-KiB values: integer formatting, no decimal point
        assert _sh.humanize_bytes(1) == "1B"
        assert _sh.humanize_bytes(512) == "512B"
        assert _sh.humanize_bytes(1023) == "1023B"

    def test_kib_boundary(self):
        # Exactly 1024 = 1.0K
        assert _sh.humanize_bytes(1024) == "1.0K"

    def test_kib_range(self):
        assert _sh.humanize_bytes(1536) == "1.5K"
        assert _sh.humanize_bytes(2048) == "2.0K"

    def test_mib(self):
        assert _sh.humanize_bytes(1024**2) == "1.0M"
        assert _sh.humanize_bytes(int(1.5 * 1024**2)) == "1.5M"

    def test_gib(self):
        assert _sh.humanize_bytes(1024**3) == "1.0G"
        # 10 GiB — typical "1% of 1 TB" backup margin from our preventive
        # ENOSPC guard. Coherence-checks the formatting at that scale.
        assert _sh.humanize_bytes(10 * 1024**3) == "10.0G"

    def test_tib(self):
        assert _sh.humanize_bytes(1024**4) == "1.0T"
        assert _sh.humanize_bytes(2 * 1024**4) == "2.0T"

    def test_negative(self):
        # Used defensively (e.g. arithmetic underflow in callers)
        assert _sh.humanize_bytes(-512) == "-512B"
        assert _sh.humanize_bytes(-(2 * 1024**3)) == "-2.0G"


class TestShPart:  # pylint: disable=missing-function-docstring
    """Tests for part() function that generates partition device names."""

    def test_nvme(self):
        assert part("/dev/nvme0n1", 1) == "/dev/nvme0n1p1"
        assert part("/dev/nvme0n1", 4) == "/dev/nvme0n1p4"

    def test_sata(self):
        assert part("/dev/sda", 1) == "/dev/sda1"
        assert part("/dev/sda", 3) == "/dev/sda3"

    def test_virtio(self):
        assert part("/dev/vda", 2) == "/dev/vda2"

    def test_loop(self):
        assert part("/dev/loop0", 1) == "/dev/loop0p1"

    def test_mmcblk(self):
        assert part("/dev/mmcblk0", 1) == "/dev/mmcblk0p1"


# ═════════════════════════════════════════════════════════════════════════
#  lib/zfs.py
# ═════════════════════════════════════════════════════════════════════════


class TestZFS:  # pylint: disable=missing-function-docstring
    """Tests for ZFS pool and dataset management with mocked shell commands."""

    def test_pool_exists_true(self):
        mock, zfs = make_mock_zfs()
        mock.on("zpool list testpool").succeeds("testpool\t...\n")
        with patch_sh(mock):
            assert zfs.pool_exists("testpool")

    def test_pool_exists_false(self):
        mock, zfs = make_mock_zfs()
        mock.on("zpool list nopool").fails("no such pool")
        with patch_sh(mock):
            assert not zfs.pool_exists("nopool")

    def test_pool_info(self):
        mock, zfs = make_mock_zfs()
        mock.on("zpool list -H -o name,guid,health,size,alloc,free testpool").succeeds(
            "testpool\t12345\tONLINE\t1.82T\t500G\t1.32T",
        )
        mock.on("zfs list -H -o used,avail -p testpool").succeeds("536870912000\t1418440704000")
        with patch_sh(mock):
            info = zfs.pool_info("testpool")
            assert info is not None
            assert info.name == "testpool"
            assert info.guid == "12345"
            assert info.health == "ONLINE"
            assert info.pct_used == 27  # 536G / (536G+1418G)
            # Numeric byte fields parsed from `zfs list -p` (raw bytes).
            # size_bytes = used_bytes + avail_bytes.
            assert info.used_bytes == 536870912000
            assert info.avail_bytes == 1418440704000
            assert info.size_bytes == 536870912000 + 1418440704000

    def test_pool_info_not_imported(self):
        mock, zfs = make_mock_zfs()
        mock.on("zpool list -H -o name,guid,health,size,alloc,free gone").fails()
        with patch_sh(mock):
            assert zfs.pool_info("gone") is None

    def test_pool_export(self):
        mock, zfs = make_mock_zfs()
        mock.on("zpool list backup").succeeds()
        mock.on("zpool set cachefile=none backup").succeeds()
        mock.on("sleep 1").succeeds()
        mock.on("zpool export backup").succeeds()
        mock.on("sync").succeeds()
        with patch_sh(mock):
            assert zfs.pool_export("backup")
            assert mock.was_called("zpool set cachefile=none")

    def test_pool_export_not_imported(self):
        mock, zfs = make_mock_zfs()
        mock.on("zpool list gone").fails()
        with patch_sh(mock):
            assert zfs.pool_export("gone")  # returns True (nothing to do)

    def test_pool_export_force(self):
        mock, zfs = make_mock_zfs()
        mock.on("zpool list busy").succeeds()
        mock.on("zpool set cachefile=none busy").succeeds()
        mock.on("sleep 1").succeeds()
        mock.on("zpool export busy").fails("pool is busy")
        mock.on("zpool export -f busy").succeeds()
        mock.on("sync").succeeds()
        with patch_sh(mock):
            assert zfs.pool_export("busy")

    def test_pool_guid(self):
        mock, zfs = make_mock_zfs()
        mock.on("zpool get -H -o value guid rpool").succeeds("1234567890123456789")
        with patch_sh(mock):
            assert zfs.pool_guid("rpool") == "1234567890123456789"

    def test_list_datasets(self):
        mock, zfs = make_mock_zfs()
        mock.on(
            "zfs list -H -o name,mountpoint,used,refer,canmount,type "
            + "-t filesystem,volume -r rpool",
        ).succeeds(
            "rpool\tnone\t500G\t192K\toff\tfilesystem\n"
            "rpool/ROOT\tnone\t50G\t192K\toff\tfilesystem\n"
            "rpool/ROOT/ubuntu_8bt2zy\t/\t50G\t10G\tnoauto\tfilesystem\n"
            "rpool/ROOT/ubuntu_8bt2zy/home\t/home\t30G\t30G\ton\tfilesystem\n",
        )
        with patch_sh(mock):
            datasets = zfs.list_datasets("rpool")
            assert len(datasets) == 4
            assert datasets[2].name == "rpool/ROOT/ubuntu_8bt2zy"
            assert datasets[2].mountpoint == "/"
            assert datasets[2].type == "filesystem"

    def test_list_snapshots(self):
        mock, zfs = make_mock_zfs()
        mock.on("zfs list -H -o name -t snapshot -r rpool").succeeds(
            "rpool/ROOT/ubuntu@autosnap_2025-01-01\n"
            "rpool/ROOT/ubuntu@autosnap_2025-01-02\n"
            "rpool/ROOT/ubuntu@manual_test\n",
        )
        with patch_sh(mock):
            snaps = zfs.list_snapshots("rpool", pattern="autosnap")
            assert len(snaps) == 2

    def test_dataset_exists(self):
        mock, zfs = make_mock_zfs()
        mock.on("zfs list rpool/keystore").succeeds()
        mock.on("zfs list rpool/nonexistent").fails()
        with patch_sh(mock):
            assert zfs.dataset_exists("rpool/keystore")
            assert not zfs.dataset_exists("rpool/nonexistent")

    def test_dataset_used_bytes(self):
        mock, zfs = make_mock_zfs()
        # `zfs list -H -p -o used` returns raw bytes
        mock.on("zfs list -H -p -o used backup/rpool").succeeds("536870912000")
        with patch_sh(mock):
            assert zfs.dataset_used_bytes("backup/rpool") == 536870912000

    def test_dataset_used_bytes_failure(self):
        mock, zfs = make_mock_zfs()
        mock.on("zfs list -H -p -o used backup/missing").fails()
        with patch_sh(mock):
            assert zfs.dataset_used_bytes("backup/missing") == 0

    def test_dataset_used_bytes_unparseable(self):
        mock, zfs = make_mock_zfs()
        # Defensive: if the output isn't parseable as int, return 0
        mock.on("zfs list -H -p -o used backup/weird").succeeds("not-a-number")
        with patch_sh(mock):
            assert zfs.dataset_used_bytes("backup/weird") == 0

    def test_get_set_property(self):
        mock, zfs = make_mock_zfs()
        mock.on("zfs get -H -o value mountpoint rpool/ROOT/ubuntu").succeeds("/")
        mock.on("zfs set canmount=on rpool/ROOT/ubuntu").succeeds()
        with patch_sh(mock):
            assert zfs.get_property("rpool/ROOT/ubuntu", "mountpoint") == "/"
            assert zfs.set_property("rpool/ROOT/ubuntu", "canmount", "on")

    def test_scan_zfs_members(self):
        mock, zfs = make_mock_zfs()
        mock.on("blkid -t TYPE=zfs_member -o export").succeeds(
            "DEVNAME=/dev/sda1\n"
            "UUID=12345\n"
            "LABEL=backup\n"
            "\n"
            "DEVNAME=/dev/nvme0n1p3\n"
            "UUID=99999\n"
            "LABEL=rpool\n",
        )
        with patch_sh(mock):
            members = zfs.scan_zfs_members()
            assert len(members) == 2
            assert members[0]["label"] == "backup"
            assert members[1]["label"] == "rpool"

    def test_importable_pools(self):
        mock, zfs = make_mock_zfs()
        mock.on("zpool import").succeeds(
            "   pool: backup\n     id: 1234567890123456789\n  state: ONLINE\n",
        )
        with patch_sh(mock):
            pools = zfs.importable_pools()
            assert len(pools) == 1
            assert pools[0]["name"] == "backup"
            assert pools[0]["guid"] == "1234567890123456789"


# ═════════════════════════════════════════════════════════════════════════
#  lib/keystore.py
# ═════════════════════════════════════════════════════════════════════════


class TestKeystore:
    """Tests for Keystore zvol detection and mounting logic with mocked shell commands."""

    def test_find_zvol_no_rpool(self):
        """When rpool is not imported, first /dev/zd* is used."""
        mock = MockShell()
        log = make_log()
        ks = Keystore(log)

        mock.on("ls -1 /dev/zd*").succeeds("/dev/zd0\n/dev/zd16")
        mock.on("zfs list -H -o objsetid backup/keystore").fails()
        mock.on("zpool list rpool").fails()  # rpool NOT imported

        with patch_sh(mock):
            dev = ks.find_zvol_for_pool("backup")
            assert dev == "/dev/zd0"

    def test_find_zvol_with_rpool(self):
        """When rpool is imported, backup keystore is second device."""
        mock = MockShell()
        log = make_log()
        ks = Keystore(log)

        mock.on("ls -1 /dev/zd*").succeeds("/dev/zd0\n/dev/zd16")
        mock.on("zfs list -H -o objsetid backup/keystore").fails()
        mock.on("zpool list rpool").succeeds()

        with patch_sh(mock):
            dev = ks.find_zvol_for_pool("backup")
            assert dev == "/dev/zd16"

    def test_find_zvol_rpool_target(self):
        """When target is rpool itself, first device."""
        mock = MockShell()
        log = make_log()
        ks = Keystore(log)

        mock.on("ls -1 /dev/zd*").succeeds("/dev/zd0")
        mock.on("zfs list -H -o objsetid rpool/keystore").fails()
        mock.on("zpool list rpool").succeeds()

        with patch_sh(mock):
            dev = ks.find_zvol_for_pool("rpool")
            assert dev == "/dev/zd0"

    def test_find_zvol_none(self):
        """No zvol devices found."""
        mock = MockShell()
        log = make_log()
        ks = Keystore(log)

        mock.on("ls -1 /dev/zd*").fails("No such file")

        with patch_sh(mock):
            dev = ks.find_zvol_for_pool("backup")
            assert dev is None

    def test_mount_wrong_passphrase(self):
        """Mount fails with wrong passphrase."""
        mock = MockShell()
        log = make_log()
        ks = Keystore(log)

        mock.on("ls -1 /dev/zd*").succeeds("/dev/zd0")
        mock.on("zfs list -H -o objsetid").fails()
        mock.on("zpool list rpool").fails()
        mock.on("cryptsetup open").fails("No key available", rc=2)

        with patch_sh(mock):
            with patch("pathlib.Path.mkdir"):
                result = ks.mount("backup", "wrong_pass")
                assert result is False

    def test_umount(self):
        """Umount closes LUKS and unmounts."""
        mock = MockShell()
        log = make_log()
        ks = Keystore(log)
        ks._mapper_name = "zark_ks_backup"  # pylint: disable=protected-access
        ks._mounted = True  # pylint: disable=protected-access

        mock.on("umount").succeeds()
        mock.on("cryptsetup close zark_ks_backup").succeeds()

        with patch_sh(mock):
            with patch("pathlib.Path.is_mount", return_value=True):
                ks.umount()
                assert not ks._mounted  # pylint: disable=protected-access
                assert mock.was_called("cryptsetup close zark_ks_backup")


# ═════════════════════════════════════════════════════════════════════════
#  lib/cleanup.py
# ═════════════════════════════════════════════════════════════════════════


class TestCleanup:
    """Tests for Cleanup tracking and execution logic with mocked shell commands."""

    def test_track_and_run(self):
        """Cleanup exports pools and unmounts in reverse order."""
        mock = MockShell()
        log = make_log()
        cleanup = Cleanup(log)

        mock.on("zpool list backup").succeeds()
        mock.on("zfs unload-key -r backup").succeeds()
        mock.on("zpool export backup").succeeds()
        mock.on("sync").succeeds()
        mock.on("umount /mnt/a").succeeds()
        mock.on("umount /mnt/b").succeeds()

        cleanup.track_pool("backup")
        cleanup.track_mount("/mnt/a")
        cleanup.track_mount("/mnt/b")

        with patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0):
            with patch_sh(mock):
                with patch("pathlib.Path.is_mount", return_value=True):
                    cleanup.run()

        # /mnt/b should be unmounted before /mnt/a (reverse order)
        umount_calls = [c for c in mock.calls if c.startswith("umount")]
        assert umount_calls[0] == "umount /mnt/b"
        assert umount_calls[1] == "umount /mnt/a"

    def test_disable(self):
        """Disabled cleanup does nothing."""
        mock = MockShell()
        log = make_log()
        cleanup = Cleanup(log)
        cleanup.track_pool("backup")
        cleanup.disable()

        with patch_sh(mock):
            cleanup.run()

        assert mock.was_not_called("zpool export")

    def test_untrack(self):
        """Untracked pools are not exported."""
        mock = MockShell()
        log = make_log()
        cleanup = Cleanup(log)
        cleanup.track_pool("backup")
        cleanup.untrack_pool("backup")

        with patch_sh(mock):
            cleanup.run()

        assert mock.was_not_called("zpool export")

    def test_export_followed_by_sync(self):
        """A successful zpool export must be followed by `sync` before
        the cleanup returns. Without the sync, kernel write-back buffers
        for the bridge can be lost on a subsequent unplug.
        """
        mock = MockShell()
        log = make_log()
        cleanup = Cleanup(log)
        mock.on("zpool list backup").succeeds()
        mock.on("zfs unload-key -r backup").succeeds()
        mock.on("zpool export backup").succeeds()
        mock.on("sync").succeeds()

        cleanup.track_pool("backup")

        with patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0):
            with patch_sh(mock):
                cleanup.run()

        export_idx = next(i for i, c in enumerate(mock.calls) if c == "zpool export backup")
        sync_idx = next(i for i, c in enumerate(mock.calls) if c == "sync")
        assert sync_idx > export_idx

    def test_forced_export_also_flushes(self):
        """Both export paths (success and forced) must trigger the flush.
        Forced export was historically unprotected — the regression that
        motivated the device-flush window."""
        mock = MockShell()
        log = make_log()
        cleanup = Cleanup(log)
        mock.on("zpool list backup").succeeds()
        mock.on("zfs unload-key -r backup").succeeds()
        mock.on("zpool export backup").fails(stderr="busy", rc=1)
        mock.on("zpool export -f backup").succeeds()
        mock.on("sync").succeeds()

        cleanup.track_pool("backup")

        with patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0):
            with patch_sh(mock):
                cleanup.run()

        assert mock.was_called("sync")

    def test_cleanup_never_ejects(self):
        """Cleanup is intentionally device-agnostic — it never issues
        `eject`. The eject decision is the calling command's, made
        interactively after the success banner."""
        mock = MockShell()
        log = make_log()
        cleanup = Cleanup(log)
        mock.on("zpool list blue").succeeds()
        mock.on("zfs unload-key -r blue").succeeds()
        mock.on("zpool export blue").succeeds()
        mock.on("sync").succeeds()

        cleanup.track_pool("blue")

        with patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0):
            with patch_sh(mock):
                cleanup.run()

        assert mock.was_not_called("eject")

    def test_failed_export_no_flush(self):
        """If both export attempts fail, no flush is issued — the pool
        is still alive and may have dirty in-flight writes that must not
        be interrupted by a power-down."""
        mock = MockShell()
        log = make_log()
        cleanup = Cleanup(log)
        mock.on("zpool list backup").succeeds()
        mock.on("zfs unload-key -r backup").succeeds()
        mock.on("zpool export backup").fails(stderr="busy", rc=1)
        mock.on("zpool export -f backup").fails(stderr="busy", rc=1)

        cleanup.track_pool("backup")

        with patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0):
            with patch_sh(mock):
                cleanup.run()

        assert mock.was_not_called("eject")
        # `sync` may or may not have been issued (it is not on the
        # failed-export branch); the critical guarantee is "no eject".

    def test_exported_pools_query(self):
        """exported_pools() reports drives that were successfully exported
        — used by callers to know which pools were flushed."""
        mock = MockShell()
        log = make_log()
        cleanup = Cleanup(log)
        mock.on("zpool list blue").succeeds()
        mock.on("zfs unload-key -r blue").succeeds()
        mock.on("zpool export blue").succeeds()
        mock.on("sync").succeeds()

        cleanup.track_pool("blue")

        with patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0):
            with patch_sh(mock):
                cleanup.run()

        assert cleanup.exported_pools() == ["blue"]


class TestFlushAndEjectHelpers:
    """Tests for the module-level flush primitives used both inside
    Cleanup and by commands that perform their own teardown
    (prepare, purge, umount)."""

    def test_flush_device_cache_issues_sync(self):
        """flush_device_cache() always issues sync(2)."""
        mock = MockShell()
        log = make_log()
        mock.on("sync").succeeds()

        with patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0):
            with patch_sh(mock):
                flush_device_cache(log)

        assert mock.was_called("sync")

    def test_flush_device_cache_does_not_eject(self):
        """flush_device_cache() never ejects — that's eject_device's job.
        Splitting the two is what lets commands like prepare leave the
        drive attached for the followup backup."""
        mock = MockShell()
        log = make_log()
        mock.on("sync").succeeds()

        with patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0):
            with patch_sh(mock):
                flush_device_cache(log)

        assert mock.was_not_called("eject")

    def test_flush_device_cache_handles_sync_failure(self):
        """A failed sync is WARN, not raise. The on-disk data is already
        durable from zpool export; sync is belt-and-suspenders for the
        kernel-side flush window."""
        mock = MockShell()
        log = make_log()
        mock.on("sync").fails(stderr="not enough memory", rc=1)

        with patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0):
            with patch_sh(mock):
                flush_device_cache(log)
        # No exception, call returned normally.

    def test_eject_device_returns_true_on_success(self):
        """eject_device() returns True on a successful eject."""
        mock = MockShell()
        log = make_log()
        mock.on("eject /dev/sdb").succeeds()

        with patch_sh(mock):
            assert eject_device("/dev/sdb", log) is True
        assert mock.was_called("eject /dev/sdb")

    def test_eject_device_returns_false_on_failure(self):
        """eject_device() returns False on failure but does not raise.
        The on-disk data is already durable from the preceding sync;
        eject is belt-and-suspenders against the bridge cache layer."""
        mock = MockShell()
        log = make_log()
        mock.on("eject /dev/sdb").fails(stderr="device busy", rc=1)

        with patch_sh(mock):
            assert eject_device("/dev/sdb", log) is False


class TestPromptEjectOrAttach:
    """Tests for the interactive eject prompt + banner emission."""

    def test_prompt_yes_ejects_and_emits_safe_unplug(self):
        """Operator answers yes → eject_device called → banner_safe_unplug."""
        mock = MockShell()
        log = make_log()
        mock.on("eject /dev/sdb").succeeds()

        buf = StringIO()
        with patch_sh(mock):
            with patch("builtins.input", return_value="y"):
                with redirect_stdout(buf):
                    prompt_eject_or_attach("/dev/sdb", "blue", log, default_eject=True)

        assert mock.was_called("eject /dev/sdb")
        assert "Safe to unplug" in buf.getvalue()
        assert "still attached" not in buf.getvalue()

    def test_prompt_no_skips_eject_and_emits_attached(self):
        """Operator answers no → no eject → banner_drive_attached."""
        mock = MockShell()
        log = make_log()

        buf = StringIO()
        with patch_sh(mock):
            with patch("builtins.input", return_value="n"):
                with redirect_stdout(buf):
                    prompt_eject_or_attach("/dev/sdb", "blue", log, default_eject=True)

        assert mock.was_not_called("eject")
        assert "still attached" in buf.getvalue()
        assert "Safe to unplug" not in buf.getvalue()

    def test_prompt_default_eject_true_on_eof(self):
        """Non-interactive run (EOFError on input) uses default=True →
        eject. Matches `backup` / `umount` / `purge` / `recover` behaviour
        when invoked from a script or systemd timer."""
        mock = MockShell()
        log = make_log()
        mock.on("eject /dev/sdb").succeeds()

        with patch_sh(mock):
            with patch("builtins.input", side_effect=EOFError):
                with redirect_stdout(StringIO()):
                    prompt_eject_or_attach("/dev/sdb", "blue", log, default_eject=True)

        assert mock.was_called("eject /dev/sdb")

    def test_prompt_default_eject_false_on_eof(self):
        """Non-interactive run with default=False (prepare)
        does NOT eject. Matches the post-prepare workflow where the next
        step is `backup` against the same drive."""
        mock = MockShell()
        log = make_log()

        buf = StringIO()
        with patch_sh(mock):
            with patch("builtins.input", side_effect=EOFError):
                with redirect_stdout(buf):
                    prompt_eject_or_attach(
                        "/dev/sdb",
                        "blue",
                        log,
                        default_eject=False,
                    )

        assert mock.was_not_called("eject")
        assert "still attached" in buf.getvalue()

    def test_prompt_none_device_skips_and_attaches(self):
        """No resolvable device → no prompt, just banner_drive_attached
        with an explanatory WARN. Operator can clean up later via
        `zark umount`."""
        mock = MockShell()
        log = make_log()

        buf = StringIO()
        # input() must NOT be called when device is None.
        with patch_sh(mock):
            with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                with redirect_stdout(buf):
                    prompt_eject_or_attach(None, "blue", log, default_eject=True)

        assert mock.was_not_called("eject")
        assert "still attached" in buf.getvalue()

    def test_autoeject_non_tty_applies_default_immediately(self):
        """autoeject=True with no TTY ejects at once (no countdown), even when
        the command's own default is no-eject (the prepare/repair case)."""
        mock = MockShell()
        log = make_log()
        mock.on("eject /dev/sdb").succeeds()

        buf = StringIO()
        with patch_sh(mock):
            # ask_timeout checks sys.stdin.isatty(); force it False.
            with patch("sys.stdin") as fake_stdin:
                fake_stdin.isatty.return_value = False
                with redirect_stdout(buf):
                    prompt_eject_or_attach(
                        "/dev/sdb",
                        "blue",
                        log,
                        default_eject=False,  # command says no-eject...
                        autoeject=True,  # ...but auto-eject overrides to eject
                    )

        assert mock.was_called("eject /dev/sdb")
        assert "Safe to unplug" in buf.getvalue()

    def test_autoeject_false_uses_plain_prompt(self):
        """autoeject=False keeps the blocking prompt (input is consulted)."""
        mock = MockShell()
        log = make_log()

        buf = StringIO()
        with patch_sh(mock):
            with patch("builtins.input", return_value="n") as inp:
                with redirect_stdout(buf):
                    prompt_eject_or_attach(
                        "/dev/sdb",
                        "blue",
                        log,
                        default_eject=True,
                        autoeject=False,
                    )
                assert inp.called  # plain prompt path used input()
        assert mock.was_not_called("eject")
        assert "still attached" in buf.getvalue()

    def test_ask_timeout_expires_applies_default(self):
        """No input during the countdown -> default is applied, prompt shown
        exactly once (regression: it used to re-prompt)."""
        log = make_log()
        buf = StringIO()
        with patch("sys.stdin") as fake_stdin:
            fake_stdin.isatty.return_value = True
            # select never reports ready -> countdown runs out.
            with patch("lib.log.select.select", return_value=([], [], [])):
                with redirect_stdout(buf):
                    result = log.ask_timeout("Eject?", default=True, timeout=2)
        out = buf.getvalue()
        assert result is True
        assert "applying default (yes)" in out
        # The question banner must appear exactly once (no re-prompt).
        assert out.count("Eject?") == 1

    def test_ask_timeout_keypress_is_the_answer(self):
        """A line typed during the countdown IS the answer; no second prompt
        and no banner redraw (regression for the double-prompt bug)."""
        log = make_log()
        buf = StringIO()
        with patch("sys.stdin") as fake_stdin:
            fake_stdin.isatty.return_value = True
            fake_stdin.readline.return_value = "n\n"
            # First tick reports stdin ready.
            with patch("lib.log.select.select", return_value=([fake_stdin], [], [])):
                with redirect_stdout(buf):
                    result = log.ask_timeout("Eject?", default=True, timeout=10)
        out = buf.getvalue()
        assert result is False  # "n" -> no
        assert out.count("Eject?") == 1  # banner shown once only
        assert "applying default" not in out  # did not fall through to timeout

    def test_ask_timeout_empty_line_is_default(self):
        """Pressing Enter (empty line) during the countdown applies the
        default, like a normal prompt."""
        log = make_log()
        buf = StringIO()
        with patch("sys.stdin") as fake_stdin:
            fake_stdin.isatty.return_value = True
            fake_stdin.readline.return_value = "\n"
            with patch("lib.log.select.select", return_value=([fake_stdin], [], [])):
                with redirect_stdout(buf):
                    result = log.ask_timeout("Eject?", default=False, timeout=10)
        assert result is False  # empty -> default (False here)
        assert buf.getvalue().count("Eject?") == 1


class TestAutoejectConfig:  # pylint: disable=missing-function-docstring
    """Per-drive autoeject persistence and lookup in lib/config.py."""

    def test_autoeject_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["ZARK_CONFIG_DIR"] = td
            try:
                cfg = Config.load()
                cfg.config_dir = Path(td)
                cfg.known_drives["blue"] = DriveInfo(
                    "blue",
                    "111",
                    "usb-X-0:0",
                    autoeject=True,
                )
                cfg.known_drives["black"] = DriveInfo("black", "222", "usb-Y-0:0")
                cfg.save_drives()

                cfg2 = Config.load()
                assert cfg2.known_drives["blue"].autoeject is True
                assert cfg2.known_drives["black"].autoeject is False
            finally:
                del os.environ["ZARK_CONFIG_DIR"]

    def test_drive_autoeject_lookup(self):
        cfg = Config()
        cfg.known_drives["blue"] = DriveInfo("blue", "1", "x", autoeject=True)
        cfg.known_drives["black"] = DriveInfo("black", "2", "y")
        assert cfg.drive_autoeject("blue") is True
        assert cfg.drive_autoeject("black") is False
        # Unregistered pool -> False (recover/mount outside the registry).
        assert cfg.drive_autoeject("ghost") is False

    def test_autoeject_absent_defaults_false(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "known_drives.json"), "w", encoding="utf-8") as f:
                f.write('{"blue": {"guid": "1", "drive_id": "usb-X-0:0"}}')
            os.environ["ZARK_CONFIG_DIR"] = td
            try:
                cfg = Config.load()
                assert cfg.known_drives["blue"].autoeject is False
            finally:
                del os.environ["ZARK_CONFIG_DIR"]


# ═════════════════════════════════════════════════════════════════════════
#  lib/drives.py
# ═════════════════════════════════════════════════════════════════════════


class TestDrives:  # pylint: disable=missing-function-docstring
    """Tests for ConnectedDrive status labeling and registration JSON."""

    def test_connected_drive_status_known(self):
        d = ConnectedDrive(
            name="backup",
            guid="12345",
            drive_id="usb-Micron-0:0",
            dev_path="/dev/sda",
            known=True,
            guid_changed=False,
            renamed=False,
            state="exported",
        )
        assert d.status_label == "KNOWN"

    def test_connected_drive_status_guid_changed(self):
        d = ConnectedDrive(
            name="backup",
            guid="99999",
            drive_id="usb-Micron-0:0",
            dev_path="/dev/sda",
            known=False,
            guid_changed=True,
            renamed=False,
            state="exported",
        )
        assert d.status_label == "GUID CHANGED"

    def test_connected_drive_status_renamed(self):
        d = ConnectedDrive(
            name="newname",
            guid="12345",
            drive_id="usb-Micron-0:0",
            dev_path="/dev/sda",
            known=False,
            guid_changed=False,
            renamed=True,
            state="exported",
        )
        assert d.status_label == "RENAMED"

    def test_connected_drive_status_unknown(self):
        d = ConnectedDrive(
            name="mystery",
            guid="77777",
            drive_id="usb-X-0:0",
            dev_path="/dev/sdb",
            known=False,
            guid_changed=False,
            renamed=False,
            state="exported",
        )
        assert d.status_label == "UNKNOWN"

    def test_registration_json(self):
        d = ConnectedDrive(
            name="backup",
            guid="12345",
            drive_id="usb-Micron-0:0",
            dev_path="/dev/sda",
            known=True,
            guid_changed=False,
            renamed=False,
            state="exported",
        )
        j = d.registration_json
        assert '"backup"' in j
        assert '"12345"' in j
        assert '"usb-Micron-0:0"' in j


# ═════════════════════════════════════════════════════════════════════════
#  commands/backup.py — live USB detection
# ═════════════════════════════════════════════════════════════════════════


class TestBackupLiveUSBDetection:  # pylint: disable=missing-function-docstring
    """Tests for live USB detection logic in backup command with mocked shell commands."""

    def test_detect_live_usb_casper(self):
        """Detects live USB via boot=casper in cmdline."""
        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("BOOT_IMAGE=/casper/vmlinuz boot=casper quiet splash")
        with patch_sh(mock):
            assert _detect_live_usb()

    def test_detect_live_usb_rofs(self):
        """Detects live USB via /rofs directory."""
        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("BOOT_IMAGE=/vmlinuz root=/dev/mapper/root")
        mock.on("test -d /rofs").succeeds()
        with patch_sh(mock):
            assert _detect_live_usb()

    def test_detect_live_usb_no_rpool(self):
        """Detects live USB when rpool is not imported."""
        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("normal boot")
        mock.on("test -d /rofs").fails()
        mock.on("test -d /cow").fails()
        mock.on("zpool list rpool").fails()
        with patch_sh(mock):
            assert _detect_live_usb()

    def test_detect_normal_system(self):
        """Normal installed system is NOT live USB."""
        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("BOOT_IMAGE=/vmlinuz root=ZFS=rpool/ROOT/ubuntu")
        mock.on("test -d /rofs").fails()
        mock.on("test -d /cow").fails()
        mock.on("zpool list rpool").succeeds()
        with patch_sh(mock):
            assert not _detect_live_usb()


class TestBackupCheckTargetSpace:  # pylint: disable=missing-function-docstring
    """Tests for the preventive ENOSPC guard in backup (_check_target_space)."""

    @staticmethod
    def _make_pool_info(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        name: str,
        used_bytes: int,
        avail_bytes: int,
        used: str = "",
        avail: str = "",
        size: str = "",
    ) -> PoolInfo:
        return PoolInfo(
            name=name,
            used=used or _sh.humanize_bytes(used_bytes),
            avail=avail or _sh.humanize_bytes(avail_bytes),
            size=size or _sh.humanize_bytes(used_bytes + avail_bytes),
            used_bytes=used_bytes,
            avail_bytes=avail_bytes,
            size_bytes=used_bytes + avail_bytes,
        )

    def test_passes_when_plenty_of_space(self):
        """A target with abundant free space neither warns nor fatal."""
        src = self._make_pool_info("rpool", used_bytes=100 * 1024**3, avail_bytes=400 * 1024**3)
        dst = self._make_pool_info(
            "backup",
            used_bytes=200 * 1024**3,
            avail_bytes=1024 * 1024**3,  # 1 TiB free
        )
        log = make_log()
        # No raise expected
        _check_target_space(src, dst, "backup", "rpool", log)

    def test_low_space_is_left_to_the_estimate(self):
        """G4a: the old 1%-of-source guard is gone; whether a backup fits is
        decided after the questions, by its estimate and the drive's
        retention (M3 decision 4)."""
        src = self._make_pool_info("rpool", used_bytes=1024**4, avail_bytes=200 * 1024**3)
        dst = self._make_pool_info("backup", used_bytes=900 * 1024**3, avail_bytes=500 * 1024**2)
        with redirect_stdout(StringIO()):
            _check_target_space(src, dst, "backup", "rpool", make_log())

    def test_passes_just_above_threshold(self):
        """Boundary: 1% of 1 TiB source ≈ 10.24 GiB; 11 GiB free passes."""
        src = self._make_pool_info("rpool", used_bytes=1024**4, avail_bytes=200 * 1024**3)
        dst = self._make_pool_info("backup", used_bytes=900 * 1024**3, avail_bytes=11 * 1024**3)
        log = make_log()
        # No raise expected
        _check_target_space(src, dst, "backup", "rpool", log)

    def test_warns_when_target_smaller(self):
        """Target smaller than source warns (not fatal) — operator override."""
        # Source 2 TiB, target 1 TiB. Avail still > threshold → no fatal,
        # only the coherence warn fires.
        src = self._make_pool_info("rpool", used_bytes=100 * 1024**3, avail_bytes=2 * 1024**4)
        dst = self._make_pool_info(
            "backup",
            used_bytes=10 * 1024**3,
            avail_bytes=900 * 1024**3,
        )
        # dst.size_bytes ≈ 910 GiB < src.size_bytes ≈ 2148 GiB → warn
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            _check_target_space(src, dst, "backup", "rpool", log)
        out = buf.getvalue()
        assert "smaller than source" in out, f"Expected coherence warn, got: {out!r}"

    def test_silent_when_src_info_none(self):
        """If we cannot measure source, defer to the reactive handler — no fatal."""
        dst = self._make_pool_info("backup", used_bytes=10 * 1024**3, avail_bytes=1024**2)  # 1 MiB
        log = make_log()
        # Even with a near-empty target, no fatal: src_info is None
        _check_target_space(None, dst, "backup", "rpool", log)

    def test_silent_when_dst_info_none(self):
        """If we cannot measure target, defer to the reactive handler — no fatal."""
        src = self._make_pool_info("rpool", used_bytes=1024**4, avail_bytes=200 * 1024**3)
        log = make_log()
        _check_target_space(src, None, "backup", "rpool", log)

    def test_silent_when_used_bytes_zero(self):
        """If source used_bytes is unknown (zero), defer to reactive handler."""
        src = self._make_pool_info("rpool", used_bytes=0, avail_bytes=0)
        dst = self._make_pool_info("backup", used_bytes=0, avail_bytes=0)
        log = make_log()
        _check_target_space(src, dst, "backup", "rpool", log)


# ═════════════════════════════════════════════════════════════════════════
#  commands/backup.py — argument parsing
# ═════════════════════════════════════════════════════════════════════════


class TestBackupParseArgs:  # pylint: disable=missing-function-docstring
    """Tests for backup's lightweight argument parser.

    Default behaviour: take_snapshots=True (sanoid runs before syncoid).
    --no-snapshot is the only way to opt out — for cron/automation that
    has already taken snapshots by other means, or for re-runs after
    a transient failure where extra snapshots are unwanted.
    """

    def test_no_args_defaults_to_taking_snapshots(self):
        opts = _backup_parse_args([])
        assert opts.take_snapshots is True

    def test_no_snapshot_flag_disables(self):
        opts = _backup_parse_args(["--no-snapshot"])
        assert opts.take_snapshots is False

    def test_unknown_flags_ignored(self):
        opts = _backup_parse_args(["--whatever", "garbage", "--no-snapshot"])
        assert opts.take_snapshots is False


# ═════════════════════════════════════════════════════════════════════════
#  lib/drives.py — validate_external_block_device (shared by prepare/purge)
# ═════════════════════════════════════════════════════════════════════════


class TestValidateExternalBlockDevice:  # pylint: disable=missing-function-docstring
    """Tests for the external-drive safety validator (used by prepare and purge)."""

    @staticmethod
    def _run(mock: MockShell, dev: str, realpath: str) -> DiskIdentity:
        with (
            patch_sh(mock),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_block_device", return_value=True),
            patch("lib.identity.os.path.realpath", return_value=realpath),
            patch("builtins.input", return_value=""),
            redirect_stdout(StringIO()),
        ):
            return validate_external_block_device(dev, make_log(), command="prepare")

    def test_accepts_by_id_of_clean_usb_disk(self):
        mock = _identity_mock()
        mock.on("zpool list -H -o name").succeeds("")
        mock.on("findmnt -rn -o SOURCE,TARGET").succeeds("")
        mock.on("swapon --show=NAME --noheadings --raw").succeeds("")
        ident = self._run(mock, f"/dev/disk/by-id/{_KINGSTON_ID}", "/dev/sdb")
        assert ident.disk == "/dev/sdb"
        assert ident.by_id == _KINGSTON_ID

    def test_refuses_sata_system_disk(self):
        """Hallazgo 7: eli's internal disk is /dev/sda, not nvme."""
        mock = MockShell()
        mock.on("lsblk -dn -P -o NAME,TYPE,PKNAME,MODEL,SERIAL,SIZE,TRAN /dev/sda").succeeds(
            'NAME="sda" TYPE="disk" PKNAME="" MODEL="KINGSTON" SERIAL="5002" '
            'SIZE="476G" TRAN="sata"',
        )
        mock.on_prefix("find /dev/disk/by-id/").succeeds(_BY_ID_LISTING)
        mock.on("zpool list -H -o name").succeeds("rpool")
        mock.on("zpool list -vHP rpool").succeeds("rpool\t476G\n\t/dev/sda4\t476G")
        mock.on("lsblk -nrs -o NAME,TYPE /dev/sda4").succeeds("sda4 part\nsda disk")
        mock.on("findmnt -rn -o SOURCE,TARGET").succeeds("")
        mock.on("swapon --show=NAME --noheadings --raw").succeeds("")
        try:
            self._run(mock, "/dev/sda", "/dev/sda")
        except SystemExit:
            return
        raise AssertionError("Should have called fatal")

    def test_refuses_partition(self):
        mock = _identity_mock()
        try:
            self._run(mock, f"/dev/disk/by-id/{_KINGSTON_ID}-part1", "/dev/sdb1")
        except SystemExit:
            return
        raise AssertionError("Should have called fatal")

    def test_refuses_nvme(self):
        """Refuses internal NVMe drives."""
        log = make_log()
        mock = MockShell()
        mock.on("test -b /dev/nvme0n1").succeeds()

        with patch_sh(mock):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.is_block_device", return_value=True):
                    with patch("builtins.input", return_value=""):
                        try:
                            validate_external_block_device(
                                "/dev/nvme0n1",
                                log,
                                command="prepare",
                            )
                            raise AssertionError("Should have called fatal")
                        except SystemExit:
                            pass  # Expected — fatal raises SystemExit

    def test_refuses_nonexistent(self):
        """Refuses non-existent devices."""
        log = make_log()

        with patch("pathlib.Path.exists", return_value=False):
            with patch("builtins.input", return_value=""):
                try:
                    validate_external_block_device(
                        "/dev/sdz",
                        log,
                        command="prepare",
                    )
                    raise AssertionError("Should have called fatal")
                except SystemExit:
                    pass


# ═════════════════════════════════════════════════════════════════════════
#  commands/simulate.py — OVMF detection
# ═════════════════════════════════════════════════════════════════════════


class TestSimulate:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """Tests for OVMF candidate paths in simulate command."""

    def test_ovmf_candidates_exist(self):
        """OVMF candidate paths are defined."""
        assert len(OVMF_CODE_CANDIDATES) > 0
        assert any("OVMF_CODE" in p for p in OVMF_CODE_CANDIDATES)


class TestSimulateArgs:  # pylint: disable=missing-function-docstring
    """Tests for simulate argument parsing."""

    def test_no_args(self):
        opts = _parse_args([])
        assert opts.disk is None
        assert opts.rw is False
        # Defaults present
        assert opts.display_w > 0 and opts.display_h > 0

    def test_disk_only(self):
        opts = _parse_args(["/dev/sdb"])
        assert opts.disk == "/dev/sdb"
        assert opts.rw is False

    def test_rw_flag_alone(self):
        opts = _parse_args(["--rw"])
        assert opts.disk is None
        assert opts.rw is True

    def test_disk_and_rw(self):
        opts = _parse_args(["/dev/sdc", "--rw"])
        assert opts.disk == "/dev/sdc"
        assert opts.rw is True

    def test_ro_flag_silently_accepted(self):
        # --ro is a no-op now (read-only is the default), but the parser
        # should not choke on it for backwards compatibility.
        opts = _parse_args(["/dev/sdd", "--ro"])
        assert opts.disk == "/dev/sdd"
        assert opts.rw is False

    def test_display_custom(self):
        opts = _parse_args(["--display", "1920x1080"])
        assert opts.display_w == 1920
        assert opts.display_h == 1080

    def test_display_4k(self):
        opts = _parse_args(["--display", "3840x2160"])
        assert opts.display_w == 3840
        assert opts.display_h == 2160

    def test_display_uppercase_x(self):
        # Common typo — the parser should accept either x or X
        opts = _parse_args(["--display", "1920X1080"])
        assert opts.display_w == 1920
        assert opts.display_h == 1080

    def test_display_combined_with_disk_and_rw(self):
        opts = _parse_args(["/dev/sdb", "--rw", "--display", "2560x1440"])
        assert opts.disk == "/dev/sdb"
        assert opts.rw is True
        assert opts.display_w == 2560
        assert opts.display_h == 1440

    def test_display_bad_format(self):
        try:
            _parse_args(["--display", "lol"])
        except ValueError:
            return
        raise AssertionError("Expected ValueError for malformed --display spec")

    def test_display_zero_dimension(self):
        try:
            _parse_args(["--display", "0x1080"])
        except ValueError:
            return
        raise AssertionError("Expected ValueError for zero dimension")

    def test_display_too_large(self):
        # 16K is sci-fi territory — reject as a probable typo
        try:
            _parse_args(["--display", "15360x8640"])
        except ValueError:
            return
        raise AssertionError("Expected ValueError for >8K dimensions")

    def test_display_default_when_flag_absent(self):
        opts = _parse_args(["/dev/sdb"])
        # Defaults are sensible (>= HD)
        assert opts.display_w >= 1920
        assert opts.display_h >= 1080


class TestSimulateInUseDetection:  # pylint: disable=missing-function-docstring
    """Tests for _disk_in_use_reasons() — the safety-layer-1 detector."""

    def test_disk_with_zfs_root_pool(self):
        """A disk hosting the imported root ZFS pool is reported as in use."""
        mock = MockShell()
        # Live root is on rpool (ZFS dataset)
        mock.on("findmnt -no SOURCE /").succeeds("rpool/ROOT/ubuntu_xxx")
        # zpool status shows rpool backed by /dev/nvme0n1p4
        mock.on("zpool status -P rpool").succeeds(
            "  pool: rpool\n"
            " state: ONLINE\n"
            "config:\n"
            "\tNAME              STATE\n"
            "\trpool             ONLINE\n"
            "\t  /dev/nvme0n1p4  ONLINE\n",
        )
        # Parent of nvme0n1p4 is nvme0n1
        mock.on("lsblk -no PKNAME /dev/nvme0n1p4").succeeds("nvme0n1")
        # Partitions of nvme0n1
        mock.on("lsblk -lnp -o NAME,TYPE /dev/nvme0n1").succeeds(
            "/dev/nvme0n1   disk\n"
            "/dev/nvme0n1p1 part\n"
            "/dev/nvme0n1p2 part\n"
            "/dev/nvme0n1p3 part\n"
            "/dev/nvme0n1p4 part\n",
        )
        # No mounts on those partitions (root is via ZFS dataset, not a part)
        mock.on("findmnt -rno SOURCE").succeeds("rpool/ROOT/ubuntu_xxx\nrpool/USERDATA/foo")
        # zpool status -P (no args) lists the same vdev
        mock.on("zpool status -P").succeeds(
            "  pool: rpool\n\t  /dev/nvme0n1p4  ONLINE",
        )
        mock.on("cat /proc/swaps").succeeds(
            "Filename    Type    Size    Used    Priority\n",
        )
        with patch_sh(mock):
            reasons = _disk_in_use_reasons("/dev/nvme0n1")
        # At least one reason fired (root, or zfs-imported); both legitimate
        assert reasons, f"Expected at least one in-use reason, got {reasons}"
        joined = " | ".join(reasons)
        assert "live system root" in joined or "imported ZFS pools" in joined

    def test_disk_with_mounted_partition(self):
        """A disk with a mounted ext4 partition is reported as in use."""
        mock = MockShell()
        mock.on("findmnt -no SOURCE /").succeeds("/dev/nvme0n1p3")
        mock.on("lsblk -no PKNAME /dev/nvme0n1p3").succeeds("nvme0n1")
        mock.on("lsblk -lnp -o NAME,TYPE /dev/sdb").succeeds(
            "/dev/sdb  disk\n/dev/sdb1 part\n",
        )
        mock.on("findmnt -rno SOURCE").succeeds("/dev/sdb1\n/dev/nvme0n1p3")
        mock.on("zpool status -P").fails()
        mock.on("cat /proc/swaps").succeeds("Filename Type Size Used Priority\n")
        with patch_sh(mock):
            reasons = _disk_in_use_reasons("/dev/sdb")
        assert any("mounted partitions" in r for r in reasons)

    def test_disk_with_active_swap(self):
        """A disk with an active swap partition is reported as in use."""
        mock = MockShell()
        mock.on("findmnt -no SOURCE /").succeeds("/dev/nvme0n1p3")
        mock.on("lsblk -no PKNAME /dev/nvme0n1p3").succeeds("nvme0n1")
        mock.on("lsblk -lnp -o NAME,TYPE /dev/sdc").succeeds(
            "/dev/sdc  disk\n/dev/sdc1 part\n",
        )
        mock.on("findmnt -rno SOURCE").succeeds("/dev/nvme0n1p3")
        mock.on("zpool status -P").fails()
        mock.on("cat /proc/swaps").succeeds(
            "Filename       Type            Size       Used   Priority\n"
            "/dev/sdc1      partition       8388604    0      -2\n",
        )
        with patch_sh(mock):
            reasons = _disk_in_use_reasons("/dev/sdc")
        assert any("active swap" in r for r in reasons)

    def test_clean_disk_passes(self):
        """A spare disk with no mounts/pools/swap is reported as available."""
        mock = MockShell()
        # Root is on a different disk
        mock.on("findmnt -no SOURCE /").succeeds("/dev/nvme0n1p3")
        mock.on("lsblk -no PKNAME /dev/nvme0n1p3").succeeds("nvme0n1")
        # Spare disk with one untouched partition
        mock.on("lsblk -lnp -o NAME,TYPE /dev/sdz").succeeds(
            "/dev/sdz  disk\n/dev/sdz1 part\n",
        )
        # Nothing mounted from sdz
        mock.on("findmnt -rno SOURCE").succeeds("/dev/nvme0n1p3")
        # No imported pool uses sdz
        mock.on("zpool status -P").succeeds("  pool: rpool\n\t  /dev/nvme0n1p4  ONLINE\n")
        # No swap on sdz
        mock.on("cat /proc/swaps").succeeds("Filename Type Size Used Priority\n")
        with patch_sh(mock):
            reasons = _disk_in_use_reasons("/dev/sdz")
        assert not reasons, f"Expected clean disk, got reasons: {reasons}"


class TestSimulateCandidateList:  # pylint: disable=missing-function-docstring
    """Tests for _list_candidate_disks() — only eligible disks must be listed."""

    @staticmethod
    def _patch_devices_exist():
        """Pretend every '/dev/...' path exists during the test.

        _list_candidate_disks() guards against stale entries from lsblk
        with Path(dev).exists(). Under the unit tests there is no real
        /dev/sdz; we patch Path.exists to True for /dev/* paths so the
        gate doesn't filter our synthetic candidates.
        """
        return patch(
            "commands.simulate.Path.exists",
            new=lambda self: str(self).startswith("/dev/"),
        )

    def test_excludes_in_use_disk(self):
        """The in-use root disk must NOT appear in the candidate list."""
        mock = MockShell()
        # Two disks: nvme0n1 (in use) and sdz (clean).
        mock.on("lsblk -dn -o NAME,SIZE,MODEL").succeeds(
            "nvme0n1 1.8T NVMe Internal\nsdz     2.0T Spare USB\n",
        )
        # Wide-net responses for the in-use detection of each disk.
        mock.on("findmnt -no SOURCE /").succeeds("/dev/nvme0n1p3")
        mock.on("lsblk -no PKNAME /dev/nvme0n1p3").succeeds("nvme0n1")
        mock.on("lsblk -lnp -o NAME,TYPE /dev/nvme0n1").succeeds(
            "/dev/nvme0n1   disk\n/dev/nvme0n1p3 part\n",
        )
        mock.on("lsblk -lnp -o NAME,TYPE /dev/sdz").succeeds(
            "/dev/sdz  disk\n/dev/sdz1 part\n",
        )
        mock.on("findmnt -rno SOURCE").succeeds("/dev/nvme0n1p3")
        mock.on("zpool status -P").fails()
        mock.on("cat /proc/swaps").succeeds("Filename Type Size Used Priority\n")
        with patch_sh(mock), self._patch_devices_exist():
            cands = _list_candidate_disks()
        names = [d for d, _ in cands]
        assert "/dev/sdz" in names
        assert "/dev/nvme0n1" not in names, (
            "In-use disk leaked into candidate list — safety layer 2 broken"
        )

    def test_skips_loop_and_zd(self):
        """Loops and ZFS volumes (zd*) are filtered out of candidates."""
        mock = MockShell()
        mock.on("lsblk -dn -o NAME,SIZE,MODEL").succeeds(
            "loop0 100M\nzd0   8G\nsda   500G ExternalSpare\n",
        )
        # Make sda look completely clean
        mock.on("findmnt -no SOURCE /").succeeds("/dev/nvme0n1p3")
        mock.on("lsblk -no PKNAME /dev/nvme0n1p3").succeeds("nvme0n1")
        mock.on("lsblk -lnp -o NAME,TYPE /dev/sda").succeeds("/dev/sda disk\n")
        mock.on("findmnt -rno SOURCE").succeeds("/dev/nvme0n1p3")
        mock.on("zpool status -P").fails()
        mock.on("cat /proc/swaps").succeeds("Filename Type Size Used Priority\n")
        with patch_sh(mock), self._patch_devices_exist():
            cands = _list_candidate_disks()
        names = [d for d, _ in cands]
        assert "/dev/loop0" not in names
        assert "/dev/zd0" not in names


class TestSimulateGLDetection:  # pylint: disable=missing-function-docstring
    """Tests for _detect_gl() — host capability probe for virtio-vga-gl."""

    # Minimal fake `qemu-system-x86_64 -device help` output. Real output
    # is hundreds of lines; we keep just enough to exercise the substring
    # match in both directions.
    _DEVICE_HELP_WITH_GL = (
        "Display devices:\n"
        'name "VGA"\n'
        'name "virtio-vga"\n'
        'name "virtio-vga-gl"\n'
        'name "virtio-gpu-pci"\n'
    )
    _DEVICE_HELP_WITHOUT_GL = (
        "Display devices:\n" + 'name "VGA"\n' + 'name "virtio-vga"\n' + 'name "virtio-gpu-pci"\n'
    )

    @staticmethod
    def _patch_dri(present: bool, have_render_node: bool = True):
        """Patch /dev/dri inspection.

        present=True  → /dev/dri exists as a directory.
        have_render_node=True → it contains a 'renderD128' entry.
        """
        # Build a fake iterdir result of pathlib.Path-like objects with
        # a .name attribute. Path's iterdir yields Path instances; for
        # the purposes of _detect_gl() only the .name attribute is read.
        fake_entries = []
        if have_render_node:

            class _FakeEntry:  # pylint: disable=too-few-public-methods
                name = "renderD128"

            fake_entries = [_FakeEntry()]

        def _is_dir_for_dri(self):
            return present if str(self) == "/dev/dri" else False

        def _iterdir_for_dri(self):
            if str(self) == "/dev/dri":
                return iter(fake_entries)
            return iter([])

        return (
            patch.object(Path, "is_dir", _is_dir_for_dri),
            patch.object(Path, "iterdir", _iterdir_for_dri),
        )

    def test_gl_available_full_stack(self):
        """Happy path: render node + virtio-vga-gl in QEMU build."""
        mock = MockShell()
        mock.on("qemu-system-x86_64 -device help").succeeds(self._DEVICE_HELP_WITH_GL)
        is_dir_p, iterdir_p = self._patch_dri(present=True, have_render_node=True)
        with patch_sh(mock), is_dir_p, iterdir_p:
            ok, reason = _detect_gl()
        assert ok is True, f"GL should be available, got reason: {reason!r}"
        assert reason == ""

    def test_gl_unavailable_no_dri_directory(self):
        """No /dev/dri at all (e.g. headless/container host)."""
        mock = MockShell()
        # qemu probe should not even be reached — dri is checked first
        is_dir_p, iterdir_p = self._patch_dri(present=False)
        with patch_sh(mock), is_dir_p, iterdir_p:
            ok, reason = _detect_gl()
        assert ok is False
        assert "/dev/dri" in reason

    def test_gl_unavailable_no_render_node(self):
        """/dev/dri exists but has no renderD* node (rare but possible)."""
        mock = MockShell()
        is_dir_p, iterdir_p = self._patch_dri(present=True, have_render_node=False)
        with patch_sh(mock), is_dir_p, iterdir_p:
            ok, reason = _detect_gl()
        assert ok is False
        assert "renderD" in reason

    def test_gl_unavailable_qemu_minimal_build(self):
        """Render node OK, but the QEMU build lacks virtio-vga-gl."""
        mock = MockShell()
        mock.on("qemu-system-x86_64 -device help").succeeds(self._DEVICE_HELP_WITHOUT_GL)
        is_dir_p, iterdir_p = self._patch_dri(present=True, have_render_node=True)
        with patch_sh(mock), is_dir_p, iterdir_p:
            ok, reason = _detect_gl()
        assert ok is False
        assert "virtio-vga-gl" in reason

    def test_gl_unavailable_qemu_help_fails(self):
        """`qemu-system-x86_64 -device help` itself fails for some reason."""
        mock = MockShell()
        mock.on("qemu-system-x86_64 -device help").fails("boom")
        is_dir_p, iterdir_p = self._patch_dri(present=True, have_render_node=True)
        with patch_sh(mock), is_dir_p, iterdir_p:
            ok, reason = _detect_gl()
        assert ok is False
        assert "device help" in reason


# ═════════════════════════════════════════════════════════════════════════
#  commands/clean.py — patterns
# ═════════════════════════════════════════════════════════════════════════


class TestClean:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """Tests for mount patterns used in clean command."""

    def test_cleanup_patterns(self):
        """Clean targets the right mount patterns."""
        # Verify the patterns used in clean.py match expectations
        source = inspect.getsource(clean_mod.run)
        assert "/mnt/recover" in source
        assert "/mnt/zark" in source
        assert "/mnt/grub_" in source
        assert "zark_ks_" in source


# ═════════════════════════════════════════════════════════════════════════
#  commands/monitor.py — progress bar
# ═════════════════════════════════════════════════════════════════════════


class TestMonitor:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """Tests for progress bar drawing logic in monitor command."""

    def test_draw_bar(self):
        bar_0 = _draw_bar(0, width=10)
        bar_50 = _draw_bar(50, width=10)
        bar_100 = _draw_bar(100, width=10)
        assert bar_0 == "░" * 10
        assert bar_50 == "█" * 5 + "░" * 5
        assert bar_100 == "█" * 10


# ═════════════════════════════════════════════════════════════════════════
#  Mock framework self-tests
# ═════════════════════════════════════════════════════════════════════════


class TestMockShell:  # pylint: disable=missing-function-docstring
    """Self-tests for the MockShell framework to ensure it correctly simulates shell commands."""

    def test_basic_mock(self):
        mock = MockShell()
        mock.on("echo hello").succeeds("hello")
        with patch_sh(mock):
            r = _sh.run("echo hello")
            assert r.ok
            assert r.output == "hello"

    def test_failure_mock(self):
        mock = MockShell()
        mock.on("bad_cmd").fails("nope", rc=42)
        with patch_sh(mock):
            r = _sh.run("bad_cmd")
            assert not r.ok
            assert r.returncode == 42

    def test_unregistered_returns_127(self):
        mock = MockShell()
        with patch_sh(mock):
            r = _sh.run("unknown_command")
            assert r.returncode == 127

    def test_strict_mode(self):
        mock = MockShell(strict=True)
        with patch_sh(mock):
            # The guard lives in `else`, not inside the `try`. Raising it
            # from the try body would hand it straight to this very
            # `except AssertionError`, which is the clause meant to catch
            # the failure we are testing for.
            try:
                _sh.run("unknown_command")
            except AssertionError as e:
                assert "unexpected command" in str(e)
            else:
                raise AssertionError("strict MockShell should have rejected the command")

    def test_was_called(self):
        mock = MockShell()
        mock.on("zpool export backup").succeeds()
        with patch_sh(mock):
            _sh.run("zpool export backup")
        assert mock.was_called("zpool export backup")
        assert mock.was_not_called("zpool import")

    def test_call_count(self):
        mock = MockShell()
        mock.on("sync").succeeds()
        with patch_sh(mock):
            _sh.run("sync")
            _sh.run("sync")
            _sh.run("sync")
        assert mock.call_count("sync") == 3

    def test_regex_match(self):
        mock = MockShell()
        mock.on(r"zpool list \w+", regex=True).succeeds("pool_data")
        with patch_sh(mock):
            r = _sh.run("zpool list anything")
            assert r.ok

    def test_on_prefix(self):
        mock = MockShell()
        mock.on_prefix("syncoid").succeeds("syncing...")
        with patch_sh(mock):
            r = _sh.run("syncoid --recursive --raw rpool backup/rpool")
            assert r.ok


# ═════════════════════════════════════════════════════════════════════════
#  lib/zfs.py — fix_grub_bpool_uuid helper
# ═════════════════════════════════════════════════════════════════════════


class TestFixGrubBpoolUuid:
    """
    Tests for the grub.cfg bpool UUID rewriter.

    Real Ubuntu's update-grub emits `--set=root` (16 occurrences in a typical
    /boot/grub/grub.cfg, all sharing the same hex). The QEMU integration test
    fixture writes `--set=boot_fs`. The helper must handle both, plus arbitrary
    --set=NAME values, and must never touch hex strings outside `fs-uuid`.
    """

    OLD_HEX = "99cde6d3b95a6b11"
    NEW_HEX = "7318858f7ebb57e3"

    def _write(self, content: str):
        fd, path = tempfile.mkstemp(suffix=".cfg")
        os.close(fd)
        p = Path(path)
        p.write_text(content, encoding="utf-8")
        return p

    def test_replaces_set_root(self):
        """Real Ubuntu pattern: --set=root."""
        p = self._write(
            f"menuentry 'Ubuntu' {{\n"
            f"    search --no-floppy --fs-uuid --set=root {self.OLD_HEX}\n"
            f"    linux /boot/vmlinuz\n"
            f"}}\n",
        )
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        new = p.read_text(encoding="utf-8")
        assert self.NEW_HEX in new
        assert self.OLD_HEX not in new
        os.unlink(p)

    def test_replaces_set_boot_fs(self):
        """Test fixture pattern: --set=boot_fs."""
        p = self._write(
            f"menuentry 'Ubuntu Test' {{\n"
            f"    search --no-floppy --fs-uuid --set=boot_fs {self.OLD_HEX}\n"
            f"    linux ($boot_fs)/BOOT/foo/vmlinuz\n"
            f"}}\n",
        )
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        new = p.read_text(encoding="utf-8")
        assert f"--set=boot_fs {self.NEW_HEX}" in new
        assert self.OLD_HEX not in new
        os.unlink(p)

    def test_replaces_all_occurrences(self):
        """Real grub.cfg has the bpool UUID repeated many times."""
        line = f"    search --no-floppy --fs-uuid --set=root {self.OLD_HEX}\n"
        p = self._write("menuentry 'Ubuntu' {\n" + line * 16 + "}\n")
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        new = p.read_text(encoding="utf-8")
        assert new.count(self.NEW_HEX) == 16
        assert self.OLD_HEX not in new
        os.unlink(p)

    def test_already_correct_no_change(self):
        """If hex is already the new value, file is left untouched."""
        original = (
            f"menuentry 'Ubuntu' {{\n"
            f"    search --no-floppy --fs-uuid --set=root {self.NEW_HEX}\n"
            f"}}\n"
        )
        p = self._write(original)
        # mtime_before = p.stat().st_mtime_ns
        # Sleep is unnecessary — we compare contents, not just mtime.
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        assert p.read_text(encoding="utf-8") == original
        os.unlink(p)

    def test_missing_file_returns_false(self):
        """Missing grub.cfg should warn and return False (caller handles fallback)."""
        p = Path(tempfile.mkdtemp()) / "nope.cfg"
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is False

    def test_no_fs_uuid_lines_returns_true(self):
        """File with no fs-uuid lines is processed (warned) but doesn't error."""
        p = self._write("set timeout=5\nset default=0\n")
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        # Content unchanged
        assert "set timeout=5" in p.read_text(encoding="utf-8")
        os.unlink(p)

    def test_does_not_touch_unrelated_hex(self):
        """
        Hex strings that look like GUIDs but appear outside `fs-uuid --set=...`
        must NOT be replaced — e.g., a comment, a UUID in a different context.
        """
        unrelated = "deadbeefcafebabe"
        p = self._write(
            f"# a random hex: {unrelated}\n"
            f"menuentry 'Ubuntu' {{\n"
            f"    search --no-floppy --fs-uuid --set=root {self.OLD_HEX}\n"
            f"    set foo={unrelated}\n"
            f"}}\n",
        )
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        new = p.read_text(encoding="utf-8")
        assert unrelated in new  # untouched
        assert self.NEW_HEX in new  # bpool UUID rewritten
        assert self.OLD_HEX not in new
        os.unlink(p)

    def test_mixed_set_names_all_replaced(self):
        """Defensive: if a config mixes --set=root and --set=boot_fs (e.g.
        a partially-regenerated file), both are rewritten."""
        p = self._write(
            f"search --no-floppy --fs-uuid --set=root {self.OLD_HEX}\n"
            f"search --no-floppy --fs-uuid --set=boot_fs {self.OLD_HEX}\n",
        )
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        new = p.read_text(encoding="utf-8")
        assert new.count(self.NEW_HEX) == 2
        assert self.OLD_HEX not in new
        os.unlink(p)

    def test_replaces_lines_with_search_hints(self):
        """
        Real Ubuntu grub.cfg lines include `--hint-bios`, `--hint-efi`, and
        `--hint-baremetal` between `--set=<name>` and the UUID. Earlier
        versions of the regex required only whitespace there and silently
        skipped these lines, leaving the source machine's stale UUID intact
        — which broke boot whenever the recovered system's drive enumeration
        differed from the original (i.e. cross-host recovery).
        """
        p = self._write(
            f"menuentry 'Ubuntu 26.04 LTS' {{\n"
            f"    set root='hd2,gpt2'\n"
            f"    if [ x$feature_platform_search_hint = xy ]; then\n"
            f"        search --no-floppy --fs-uuid --set=root "
            f"--hint-bios=hd2,gpt2 --hint-efi=hd2,gpt2 "
            f"--hint-baremetal=ahci2,gpt2  {self.OLD_HEX}\n"
            f"    else\n"
            f"        search --no-floppy --fs-uuid --set=root {self.OLD_HEX}\n"
            f"    fi\n"
            f"    linux /BOOT/ubuntu_xxx@/vmlinuz-...\n"
            f"}}\n",
        )
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        new = p.read_text(encoding="utf-8")
        # Both occurrences (if-branch with hints AND else-branch without)
        # must be rewritten — regex must accept the hints in between.
        assert new.count(self.NEW_HEX) == 2
        assert self.OLD_HEX not in new
        # The hints themselves must remain (we only replace the UUID,
        # not the surrounding options)
        assert "--hint-bios=hd2,gpt2" in new
        assert "--hint-efi=hd2,gpt2" in new
        assert "--hint-baremetal=ahci2,gpt2" in new
        os.unlink(p)

    def test_does_not_touch_uuids_outside_fs_uuid_context(self):
        """
        The new permissive regex must still scope its replacements to lines
        that mention `--fs-uuid`. A 16-hex-char token elsewhere in the file
        (e.g. inside a `set foo=...` line, or a partition UUID) must remain
        untouched even if it happens to be the same as the old bpool UUID.
        """
        p = self._write(
            f"# Comment with hex like {self.OLD_HEX} should not be touched\n"
            f"set unrelated_var={self.OLD_HEX}\n"
            f"menuentry 'Ubuntu' {{\n"
            f"    search --no-floppy --fs-uuid --set=root {self.OLD_HEX}\n"
            f"}}\n",
        )
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        new = p.read_text(encoding="utf-8")
        # The fs-uuid line was rewritten
        assert f"--set=root {self.NEW_HEX}" in new
        # The unrelated hex tokens were preserved
        assert f"# Comment with hex like {self.OLD_HEX}" in new
        assert f"set unrelated_var={self.OLD_HEX}" in new
        os.unlink(p)


# ═════════════════════════════════════════════════════════════════════════
#  commands/recover.py — _abort_missing_keystore
# ═════════════════════════════════════════════════════════════════════════


class TestAbortMissingKeystore:  # pylint: disable=missing-function-docstring
    """
    Tests for the abort path when the keystore zvol cannot be restored.

    Validates that:
      - Each of the three failure modes raises SystemExit with code 1.
      - The reason string lands in the rendered banner so the user can
        diagnose the failure from the terminal output alone.
      - The pool name appears in the message (so the user knows which
        backup drive to re-prepare).
    """

    @staticmethod
    def _capture_abort(reason: str, pool: str) -> tuple[int, str]:
        """Run the abort and capture (exit_code, captured_stdout)."""
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            try:
                _abort_missing_keystore(reason, pool, log)
            except SystemExit as exc:
                return int(exc.code or 0), buf.getvalue()
        # Should never reach here — _abort_missing_keystore is NoReturn
        raise AssertionError("_abort_missing_keystore did not raise SystemExit")

    def test_no_dataset_aborts_with_exit_1(self):
        code, _ = self._capture_abort("no_dataset", "backup")
        assert code == 1

    def test_no_snapshot_aborts_with_exit_1(self):
        code, _ = self._capture_abort("no_snapshot", "backup")
        assert code == 1

    def test_send_failed_aborts_with_exit_1(self):
        code, _ = self._capture_abort("send_failed", "backup")
        assert code == 1

    def test_no_dataset_message_mentions_pool_and_dataset(self):
        _, output = self._capture_abort("no_dataset", "myvault")
        assert "myvault" in output
        assert "myvault/keystore" in output

    def test_no_snapshot_message_mentions_snapshots(self):
        _, output = self._capture_abort("no_snapshot", "backup")
        assert "snapshot" in output.lower()

    def test_send_failed_message_mentions_send_or_receive(self):
        _, output = self._capture_abort("send_failed", "backup")
        text = output.lower()
        assert "send" in text or "receive" in text

    def test_unknown_reason_still_aborts(self):
        # Forward-compat: a future caller passing a new reason string
        # must not crash with KeyError — the abort must still happen.
        code, output = self._capture_abort("totally_new_reason", "backup")
        assert code == 1
        assert "totally_new_reason" in output

    def test_message_explains_why_zark_refuses_to_continue(self):
        """The banner must explain the security rationale, not just fail."""
        _, output = self._capture_abort("no_dataset", "backup")
        # User must see actionable next steps and the security explanation
        assert "zark prepare" in output
        assert "How to recover" in output

    def test_abort_path_does_not_silently_warn(self):
        """Regression guard: the old behaviour was a misleading WARN.

        v1.0.4 emitted '[WARN] system.key will be embedded in initrd' and
        continued silently. v1.0.5 must abort instead — verified here by
        checking SystemExit is raised (not just logged).
        """
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            try:
                _abort_missing_keystore("no_dataset", "backup", log)
            except SystemExit:
                return  # expected
        raise AssertionError(
            "v1.0.4 silent-fallback regression: _abort_missing_keystore "
            "must raise SystemExit, not return.",
        )


# ═════════════════════════════════════════════════════════════════════════
#  Sanoid auto-discovery (test5+)
# ═════════════════════════════════════════════════════════════════════════


def _classified(name: str, ubuntu: str, type_: str = "filesystem") -> SanoidRule:
    """``_classify`` for a dataset that is expected NOT to be skipped.

    ``_classify`` returns None for datasets a parent's recursive rule
    already covers. Tests that go on to index the rule need that None
    narrowed away first; doing it here keeps the assertion out of every
    single test.
    """
    rule = _classify(DatasetInfo(name=name, type=type_), ubuntu)
    assert rule is not None, f"{name} should have produced a rule, not a skip"
    return rule


class TestSanoidClassification:  # pylint: disable=missing-function-docstring
    """Verify that _classify produces the right rule for each dataset shape.

    The rules are policy decisions encoded in code (zvols → autosnap=no, live
    system → production, unknown discovered datasets → minimal+recursive).
    Regressions here would silently change snapshot retention for users.
    """

    UBUNTU = "ubuntu_jqqq5u"

    def _ds(self, name: str, type_: str = "filesystem"):
        return DatasetInfo(name=name, type=type_)

    def test_pool_root_is_minimal_non_recursive(self):
        rule = _classified("rpool", self.UBUNTU)
        assert rule["template"] == "minimal"
        assert rule["recursive"] is False

    def test_bpool_root_is_minimal_non_recursive(self):
        rule = _classified("bpool", self.UBUNTU)
        assert rule["template"] == "minimal"
        assert rule["recursive"] is False

    def test_live_system_is_production_recursive(self):
        rule = _classified(f"rpool/ROOT/{self.UBUNTU}", self.UBUNTU)
        assert rule["template"] == "production"
        assert rule["recursive"] is True

    def test_userdata_is_production_recursive(self):
        rule = _classified("rpool/USERDATA", self.UBUNTU)
        assert rule["template"] == "production"
        assert rule["recursive"] is True

    def test_bpool_boot_is_production_recursive(self):
        """bpool/BOOT holds kernels and grub config — same retention horizon
        as user data so the user can roll back ~3 months after a bad update."""
        rule = _classified("bpool/BOOT", self.UBUNTU)
        assert rule["template"] == "production"
        assert rule["recursive"] is True

    def test_bpool_boot_children_are_skipped(self):
        """Children of bpool/BOOT (the named boot environment) must be
        skipped because the recursive rule on bpool/BOOT covers them."""
        rule = _classify(self._ds(f"bpool/BOOT/{self.UBUNTU}"), self.UBUNTU)
        assert rule is None

    def test_root_container_is_minimal_non_recursive(self):
        rule = _classified("rpool/ROOT", self.UBUNTU)
        assert rule["template"] == "minimal"
        assert rule["recursive"] is False

    def test_zvol_is_excluded_from_autosnap(self):
        """Critical: keystore (and any future zvol) must NOT be autosnapped.

        The keystore zvol is replicated by prepare/recover, not by sanoid.
        Snapshotting it accumulates useless snapshots that nobody consults.
        """
        rule = _classified("rpool/keystore", self.UBUNTU, type_="volume")
        assert rule["template"] is None  # signals autosnap=no

    def test_discovered_filesystem_is_minimal_recursive(self):
        """rpool/var, rpool/libvirt, anything else discovered → minimal+recursive.

        This is the agnostic default that protects users from forgetting
        to configure sanoid for new datasets they create.
        """
        for name in ("rpool/var", "rpool/libvirt", "rpool/data"):
            rule = _classified(name, self.UBUNTU)
            assert rule["template"] == "minimal", f"failed for {name}"
            assert rule["recursive"] is True, f"failed for {name}"

    def test_userdata_children_are_skipped(self):
        """Children of recursively-covered parents must NOT get their own rule.

        Two overlapping rules confuse sanoid (upstream issue 627). Children
        inherit the parent's recursive policy automatically.
        """
        for name in ("rpool/USERDATA/home_xyz", "rpool/USERDATA/root_xyz"):
            rule = _classify(self._ds(name), self.UBUNTU)
            assert rule is None, f"{name} should be skipped, got {rule}"

    def test_live_system_children_are_skipped(self):
        for name in (
            f"rpool/ROOT/{self.UBUNTU}/var",
            f"rpool/ROOT/{self.UBUNTU}/var/lib/docker",
        ):
            rule = _classify(self._ds(name), self.UBUNTU)
            assert rule is None

    def test_other_boot_environment_is_skipped(self):
        """If the user has an alternate BE (e.g. rpool/ROOT/ubuntu_old),
        we should NOT generate a rule for it — the named ubuntu_* rule
        only covers the live one."""
        rule = _classify(self._ds("rpool/ROOT/ubuntu_old"), self.UBUNTU)
        assert rule is None

    def test_format_zvol_rule_emits_autosnap_no(self):
        rule = _classified("rpool/keystore", self.UBUNTU, type_="volume")
        lines = _format_rule("rpool/keystore", rule)
        assert lines[0] == "[rpool/keystore]"
        assert "autosnap = no" in lines
        assert "autoprune = no" in lines

    def test_format_filesystem_rule_emits_template(self):
        rule = _classified(f"rpool/ROOT/{self.UBUNTU}", self.UBUNTU)
        lines = _format_rule(f"rpool/ROOT/{self.UBUNTU}", rule)
        assert lines[0] == f"[rpool/ROOT/{self.UBUNTU}]"
        assert "use_template = production" in lines
        assert "recursive = yes" in lines


class TestSanoidDiscoveryPruning:  # pylint: disable=missing-function-docstring
    """When _classify produces a recursive rule for a dataset, any descendants
    that would also receive a rule must be dropped. Two overlapping recursive
    rules trigger sanoid upstream bug 627 (the deeper rule may be ignored)."""

    UBUNTU = "ubuntu_jqqq5u"

    def _run_discovery(self, names_and_types):
        """Stub _discover_rules using a fake ZFS that returns the given list."""

        class FakeZFS:
            """Minimal ZFS stub: pool_exists + list_datasets only."""

            def pool_exists(self, name):
                return name in {n.split("/")[0] for n, _ in names_and_types}

            def list_datasets(self, root, recursive=True):
                del recursive
                return [
                    DatasetInfo(name=n, type=t)
                    for n, t in names_and_types
                    if n == root or n.startswith(root + "/")
                ]

        return _discover_rules(FakeZFS(), self.UBUNTU)

    def test_var_subtree_collapses_to_single_rule(self):
        """rpool/var, rpool/var/lib, rpool/var/lib/docker → only rpool/var
        appears in the output, because it covers the rest recursively."""
        rules = self._run_discovery(
            [
                ("rpool", "filesystem"),
                ("rpool/var", "filesystem"),
                ("rpool/var/lib", "filesystem"),
                ("rpool/var/lib/docker", "filesystem"),
            ],
        )
        names = [n for n, _ in rules]
        assert "rpool/var" in names
        assert "rpool/var/lib" not in names
        assert "rpool/var/lib/docker" not in names

    def test_bpool_boot_subtree_collapses(self):
        rules = self._run_discovery(
            [
                ("bpool", "filesystem"),
                ("bpool/BOOT", "filesystem"),
                (f"bpool/BOOT/{self.UBUNTU}", "filesystem"),
            ],
        )
        names = [n for n, _ in rules]
        assert "bpool" in names
        assert "bpool/BOOT" in names
        assert f"bpool/BOOT/{self.UBUNTU}" not in names

    def test_zvol_under_recursive_parent_keeps_explicit_rule(self):
        """If a zvol sits under a recursively-covered filesystem, its
        autosnap=no rule must REMAIN as an explicit override. Pruning it
        would let the ancestor's recursive autosnap=yes apply, defeating
        the zvol-exclusion policy and creating useless snapshots."""
        rules = self._run_discovery(
            [
                ("rpool", "filesystem"),
                ("rpool/data", "filesystem"),  # → minimal+recursive
                ("rpool/data/swap", "volume"),  # → autosnap=no override
            ],
        )
        names = [n for n, _ in rules]
        rules_dict = dict(rules)
        assert "rpool/data" in names
        assert "rpool/data/swap" in names, (
            "zvol must keep its explicit rule even under a recursive parent"
        )
        # Verify it kept the right policy
        assert rules_dict["rpool/data/swap"]["template"] is None  # autosnap=no


class TestSanoidDiff:  # pylint: disable=missing-function-docstring
    """Verify the comparator that decides whether to overwrite sanoid.conf.

    Bad diffs lead to either prompting the user when nothing changed (annoying)
    or skipping the prompt when something *did* change (dangerous). Both ends
    of that mistake are tested here.
    """

    UBUNTU = "ubuntu_jqqq5u"

    def _planned_for_carmen(self):
        """The rule list we'd generate for Juanmi's actual layout."""
        names = [
            ("rpool", "filesystem"),
            ("rpool/ROOT", "filesystem"),
            (f"rpool/ROOT/{self.UBUNTU}", "filesystem"),
            ("rpool/USERDATA", "filesystem"),
            ("rpool/keystore", "volume"),
            ("rpool/var", "filesystem"),
            ("bpool", "filesystem"),
            ("bpool/BOOT", "filesystem"),
        ]
        rules = []
        for name, type_ in names:
            ds = DatasetInfo(name=name, type=type_)
            rule = _classify(ds, self.UBUNTU)
            if rule is not None:
                rules.append((name, rule))
        return rules

    def _serialize(self, rules):
        return _generate_sanoid_conf(rules)

    def test_parse_extracts_sections_and_keys(self):
        text = (
            "# header\n"
            "[rpool/foo]\n"
            "use_template = minimal\n"
            "recursive = yes\n"
            "\n"
            "[template_minimal]\n"
            "daily = 2\n"
        )
        parsed = _parse_sanoid_conf(text)
        assert parsed["rpool/foo"]["use_template"] == "minimal"
        assert parsed["rpool/foo"]["recursive"] == "yes"
        assert parsed["template_minimal"]["daily"] == "2"

    def test_parse_ignores_comments_and_blank_lines(self):
        text = "\n# comment\n   # indented comment\n" + "[rpool/foo]\nuse_template = minimal\n"
        parsed = _parse_sanoid_conf(text)
        assert "rpool/foo" in parsed
        assert "# comment" not in parsed

    def test_diff_no_changes_when_conf_matches_rules(self):
        """Re-running setup against an already-consistent file must produce
        an empty diff so the prompt is skipped."""
        rules = self._planned_for_carmen()
        text = self._serialize(rules)
        current = _parse_sanoid_conf(text)
        diff = _diff_rules(current, rules)
        assert not diff["added"]
        assert not diff["removed"]
        assert not diff["changed"]
        assert not diff["manual"]

    def test_diff_detects_added_section(self):
        """An old conf that lacks a section we now generate (e.g. user just
        added rpool/var on disk) must show up as `added`."""
        # Old conf that's missing the new rpool/var rule
        old_text = (
            f"[rpool/ROOT/{self.UBUNTU}]\n"
            "use_template = production\nrecursive = yes\n\n"
            "[rpool/USERDATA]\nuse_template = production\nrecursive = yes\n\n"
            "[rpool]\nuse_template = minimal\nrecursive = no\n\n"
            "[rpool/ROOT]\nuse_template = minimal\nrecursive = no\n"
        )
        rules = self._planned_for_carmen()
        diff = _diff_rules(_parse_sanoid_conf(old_text), rules)
        added_names = [n for n, _ in diff["added"]]
        assert "rpool/var" in added_names
        assert "rpool/keystore" in added_names
        assert "bpool" in added_names

    def test_diff_detects_changed_settings(self):
        """If user manually changed a section's template/recursive, surface it."""
        # Same sections but with USERDATA downgraded to minimal (not what we'd plan)
        old_text = (
            f"[rpool/ROOT/{self.UBUNTU}]\n"
            "use_template = production\nrecursive = yes\n\n"
            "[rpool/USERDATA]\nuse_template = minimal\nrecursive = yes\n"
        )
        rules = self._planned_for_carmen()
        diff = _diff_rules(_parse_sanoid_conf(old_text), rules)
        changed_names = [n for n, _, _ in diff["changed"]]
        assert "rpool/USERDATA" in changed_names

    def test_diff_detects_removed_section(self):
        """If old conf has a managed section that's no longer in planned,
        show it as removed (e.g. rpool/old_thing that user destroyed)."""
        old_text = self._serialize(self._planned_for_carmen())
        old_text += "\n[rpool/old_thing]\nuse_template = minimal\nrecursive = yes\n"
        rules = self._planned_for_carmen()
        diff = _diff_rules(_parse_sanoid_conf(old_text), rules)
        removed_names = [n for n, _ in diff["removed"]]
        assert "rpool/old_thing" in removed_names

    def test_diff_flags_manual_unmanaged_sections(self):
        """User-added sections under non-managed pools (e.g. tank/games)
        must be reported as manual so the user doesn't lose them silently."""
        old_text = self._serialize(self._planned_for_carmen())
        old_text += "\n[tank/games]\nuse_template = production\nrecursive = yes\n"
        rules = self._planned_for_carmen()
        diff = _diff_rules(_parse_sanoid_conf(old_text), rules)
        manual_names = [n for n, _ in diff["manual"]]
        assert "tank/games" in manual_names

    def test_templates_never_appear_in_diff(self):
        """Templates are fixed in the generator; they must not produce diff
        entries even if the parser sees them."""
        old_text = self._serialize(self._planned_for_carmen())
        rules = self._planned_for_carmen()
        diff = _diff_rules(_parse_sanoid_conf(old_text), rules)
        all_names = (
            [n for n, _ in diff["added"]]
            + [n for n, _ in diff["removed"]]
            + [n for n, _, _ in diff["changed"]]
            + [n for n, _ in diff["manual"]]
        )
        for n in all_names:
            assert not n.startswith("template_"), f"template leaked into diff: {n}"


class TestSanoidPreserveManual:  # pylint: disable=missing-function-docstring
    """Manual sections (e.g. [tank/games]) must survive a sanoid.conf
    regeneration. Losing them would silently destroy user customisation
    every time setup is run, which would be a serious trust violation."""

    UBUNTU = "ubuntu_jqqq5u"

    def _planned(self):
        names = [
            ("rpool", "filesystem"),
            (f"rpool/ROOT/{self.UBUNTU}", "filesystem"),
            ("rpool/USERDATA", "filesystem"),
        ]
        rules = []
        for name, type_ in names:
            ds = DatasetInfo(name=name, type=type_)
            rule = _classify(ds, self.UBUNTU)
            if rule is not None:
                rules.append((name, rule))
        return rules

    def test_generated_with_no_manual_omits_preserve_section(self):
        """When there are no manual sections, the output must NOT contain the
        '# Preserved user sections' header — keep the file clean."""
        text = _generate_sanoid_conf(self._planned(), preserve_manual=None)
        assert "Preserved user sections" not in text

    def test_generated_with_manual_includes_them_verbatim(self):
        """The exact section name and key=value pairs must appear in the
        regenerated file, otherwise round-tripping would lose data."""
        manual = [
            ("tank/games", {"use_template": "production", "recursive": "yes"}),
        ]
        text = _generate_sanoid_conf(self._planned(), preserve_manual=manual)
        assert "[tank/games]" in text
        assert "use_template = production" in text
        assert "recursive = yes" in text

    def test_round_trip_preserves_manual_section(self):
        """Generate → parse → check that manual sections survived intact.

        This is the integration test that actually proves the promise:
        if a user has [tank/games], running setup again must leave it
        completely unchanged in the new file.
        """
        original_manual = [
            ("tank/games", {"use_template": "production", "recursive": "yes"}),
            ("data/scratch", {"autosnap": "no", "autoprune": "no"}),
        ]
        rules = self._planned()

        # Initial state: zark rules + manual entries
        v1 = _generate_sanoid_conf(rules, preserve_manual=original_manual)

        # Simulate "user re-runs setup": parse current, diff, regenerate
        current = _parse_sanoid_conf(v1)
        diff = _diff_rules(current, rules)

        # No managed changes expected
        assert not diff["added"]
        assert not diff["removed"]
        assert not diff["changed"]

        # Manual sections detected and reported
        manual_names = sorted(n for n, _ in diff["manual"])
        assert manual_names == ["data/scratch", "tank/games"]

        # Regenerate using the detected manuals and verify they're still there
        v2 = _generate_sanoid_conf(rules, preserve_manual=diff["manual"])
        current_v2 = _parse_sanoid_conf(v2)
        assert current_v2["tank/games"]["use_template"] == "production"
        assert current_v2["tank/games"]["recursive"] == "yes"
        assert current_v2["data/scratch"]["autosnap"] == "no"
        assert current_v2["data/scratch"]["autoprune"] == "no"

    def test_manual_sections_survive_when_managed_rules_change(self):
        """Even if zark rules genuinely change (added datasets, etc.),
        the manuals must still be preserved in the new file."""
        manual = [("tank/games", {"use_template": "production", "recursive": "yes"})]
        # First version: only base planned rules. Verify the manual section
        # is present here too — preservation must work regardless of what
        # zark's own rule set looks like.
        v1 = _generate_sanoid_conf(self._planned(), preserve_manual=manual)
        parsed_v1 = _parse_sanoid_conf(v1)
        assert "tank/games" in parsed_v1

        # Now simulate that we discovered an extra dataset: rpool/var
        new_rules = self._planned() + [
            ("rpool/var", _classified("rpool/var", self.UBUNTU)),
        ]
        v2 = _generate_sanoid_conf(new_rules, preserve_manual=manual)
        parsed = _parse_sanoid_conf(v2)
        assert "rpool/var" in parsed
        assert "tank/games" in parsed
        assert parsed["tank/games"]["use_template"] == "production"


# ═════════════════════════════════════════════════════════════════════════
#  commands/recover.py — Secure Boot .latest variant pinning
# ═════════════════════════════════════════════════════════════════════════


class TestForceLatestSignedAlternative:  # pylint: disable=missing-function-docstring
    """Tests for ``_force_latest_signed_alternative``.

    The function is the entry point used by ``recover`` to avoid copying
    the older shim/grub variant to the recovered system's ESP. Two
    behaviors must be guaranteed: (1) when the .latest variant exists
    in the chroot, run update-alternatives --set; (2) when it does not,
    do nothing — so older Ubuntu releases without the split keep working.
    """

    def test_no_op_when_latest_does_not_exist(self):
        """Older Ubuntu releases (or unusual chroots) without a .latest
        variant must not trigger update-alternatives — the function
        silently returns and lets the default behavior run."""
        mock = MockShell()
        with tempfile.TemporaryDirectory() as tmp:
            # No .latest file created; just an empty chroot
            with patch_sh(mock):
                _force_latest_signed_alternative(
                    tmp,
                    "shimx64.efi.signed",
                    "/usr/lib/shim/shimx64.efi.signed.latest",
                    make_log(),
                )
            assert not mock._calls  # pylint: disable=protected-access

    def test_runs_update_alternatives_when_latest_exists(self):
        """When the .latest file is present in the chroot, the function
        invokes update-alternatives --set inside the chroot."""
        mock = MockShell()
        with tempfile.TemporaryDirectory() as tmp:
            shim_dir = Path(tmp) / "usr/lib/shim"
            shim_dir.mkdir(parents=True)
            (shim_dir / "shimx64.efi.signed.latest").write_text("fake binary")
            resp = mock.on(
                f"chroot {tmp} update-alternatives --set shimx64.efi.signed "
                "/usr/lib/shim/shimx64.efi.signed.latest",
            ).succeeds("")
            with patch_sh(mock):
                _force_latest_signed_alternative(
                    tmp,
                    "shimx64.efi.signed",
                    "/usr/lib/shim/shimx64.efi.signed.latest",
                    make_log(),
                )
            assert resp.call_count == 1

    def test_logs_warning_but_continues_on_command_failure(self):
        """If update-alternatives itself fails (rare), the function logs
        a warning but does not raise — the subsequent dpkg-reconfigure
        will still run and use whatever default is configured."""
        mock = MockShell()
        with tempfile.TemporaryDirectory() as tmp:
            shim_dir = Path(tmp) / "usr/lib/shim"
            shim_dir.mkdir(parents=True)
            (shim_dir / "shimx64.efi.signed.latest").write_text("fake binary")
            mock.on(
                f"chroot {tmp} update-alternatives --set shimx64.efi.signed "
                "/usr/lib/shim/shimx64.efi.signed.latest",
            ).fails("alternative not registered")
            with patch_sh(mock):
                # Should not raise
                _force_latest_signed_alternative(
                    tmp,
                    "shimx64.efi.signed",
                    "/usr/lib/shim/shimx64.efi.signed.latest",
                    make_log(),
                )


# ═════════════════════════════════════════════════════════════════════════
#  commands/setup.py — Secure Boot alternatives status
# ═════════════════════════════════════════════════════════════════════════


class TestSignedAlternativeStatus:  # pylint: disable=missing-function-docstring
    """Tests for ``_signed_alternative_status``.

    The function returns ``(current_target, latest_path)`` for a given
    update-alternatives name. Both fields tell the caller different
    things, and the absence of either changes the recommended action.
    """

    def test_returns_none_when_alternative_not_installed(self):
        """When /etc/alternatives/<name> doesn't exist (e.g. on a system
        that never installed shim-signed), both fields are None and the
        caller skips the dataset entirely."""
        # We can't easily mock Path.exists for /etc/alternatives, so we
        # use a name that's guaranteed not to exist as an alternative.
        # The real behavior: alt_link.exists() returns False → current=None,
        # update-alternatives --query returns non-zero → latest=None.
        mock = MockShell()
        mock.on(
            "update-alternatives --query nonexistent.alternative.zark.test",
        ).fails("no alternatives for nonexistent.alternative.zark.test")
        with patch_sh(mock):
            current, latest = _signed_alternative_status(
                "nonexistent.alternative.zark.test",
            )
        assert current is None
        assert latest is None

    def test_latest_is_none_when_query_does_not_list_signed_latest(self):
        """If update-alternatives succeeds but returns no alternative
        path ending in '.signed.latest', the helper's latest field is
        None — meaning this Ubuntu release doesn't ship the split."""
        mock = MockShell()
        mock.on(
            "update-alternatives --query nonexistent.alternative.zark.test",
        ).succeeds(
            "Name: nonexistent.alternative.zark.test\n"
            "Link: /usr/lib/whatever\n"
            "Status: auto\n"
            "Best: /usr/lib/whatever.signed\n"
            "Value: /usr/lib/whatever.signed\n"
            "\n"
            "Alternative: /usr/lib/whatever.signed\n"
            "Priority: 100\n",
        )
        with patch_sh(mock):
            _, latest = _signed_alternative_status(
                "nonexistent.alternative.zark.test",
            )
        # No path ending in .signed.latest in the query output → None
        assert latest is None


# ═════════════════════════════════════════════════════════════════════════
#  lib/config.py — UTC timestamps
# ═════════════════════════════════════════════════════════════════════════


class TestConfigTimestamps:  # pylint: disable=missing-function-docstring
    """``now_utc_iso`` / ``parse_utc_iso`` — the single source of truth
    for the timestamp written to ``last_backup_at``."""

    def test_now_utc_iso_shape(self):
        """Z-suffix, second precision, ISO-8601, no microseconds."""
        s = now_utc_iso()
        # YYYY-MM-DDTHH:MM:SSZ — exactly 20 characters.
        assert len(s) == 20, f"expected 20 chars, got {len(s)}: {s!r}"
        assert s.endswith("Z"), f"missing Z suffix: {s!r}"
        assert s[4] == "-" and s[7] == "-" and s[10] == "T"
        assert s[13] == ":" and s[16] == ":"

    def test_parse_round_trip(self):
        """now_utc_iso() output must round-trip through parse_utc_iso()."""
        s = now_utc_iso()
        dt = parse_utc_iso(s)
        assert dt is not None
        assert dt.tzinfo is not None  # must be tz-aware

    def test_parse_accepts_z_suffix(self):
        dt = parse_utc_iso("2026-05-08T15:05:57Z")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 5 and dt.day == 8
        assert dt.hour == 15 and dt.minute == 5 and dt.second == 57

    def test_parse_accepts_explicit_offset(self):
        """``datetime.isoformat()`` emits ``+00:00`` rather than ``Z`` —
        the parser must accept both for robustness."""
        dt = parse_utc_iso("2026-05-08T15:05:57+00:00")
        assert dt is not None
        assert dt.hour == 15

    def test_parse_returns_none_on_garbage(self):
        assert parse_utc_iso("not a date") is None
        assert parse_utc_iso("") is None
        assert parse_utc_iso("2026-13-99T99:99:99Z") is None


# ═════════════════════════════════════════════════════════════════════════
#  lib/config.py — known_drives.json with last_backup_at
# ═════════════════════════════════════════════════════════════════════════


class TestConfigKnownDrivesTimestamp:  # pylint: disable=missing-function-docstring
    """``Config.load`` and ``save_drives`` must tolerate the new
    ``last_backup_at`` field's absence (legacy files) and round-trip
    it cleanly when present."""

    def test_load_legacy_file_without_field(self):
        """A known_drives.json without ``last_backup_at`` parses
        without errors and leaves the field as None."""
        cfg = make_config()
        (cfg.config_dir / "known_drives.json").write_text(
            json.dumps({"black": {"guid": "111", "drive_id": "drv-A"}}) + "\n",
        )
        with patch.object(Config, "default_config_dir", return_value=cfg.config_dir):
            loaded = Config.load()
        info = loaded.known_drives["black"]
        assert info.guid == "111"
        assert info.drive_id == "drv-A"
        assert info.last_backup_at is None

    def test_load_modern_file_with_field(self):
        cfg = make_config()
        (cfg.config_dir / "known_drives.json").write_text(
            json.dumps(
                {
                    "blue": {
                        "guid": "222",
                        "drive_id": "drv-B",
                        "last_backup_at": "2026-05-08T15:05:57Z",
                    },
                },
            )
            + "\n",
        )
        with patch.object(Config, "default_config_dir", return_value=cfg.config_dir):
            loaded = Config.load()
        info = loaded.known_drives["blue"]
        assert info.last_backup_at == "2026-05-08T15:05:57Z"

    def test_save_writes_null_when_none(self):
        """Every registry key is always written (I23): a drive that has
        never been backed up carries an explicit ``null``."""
        cfg = make_config()
        cfg.known_drives["black"] = DriveInfo(
            name="black",
            guid="111",
            drive_id="drv-A",
            last_backup_at=None,
        )
        cfg.save_drives()
        data = json.loads((cfg.config_dir / "known_drives.json").read_text())
        assert data["black"]["last_backup_at"] is None
        assert data["black"]["autoeject"] is False
        assert data["black"]["guid"] == "111"

    def test_save_preserves_field_when_set(self):
        cfg = make_config()
        cfg.known_drives["blue"] = DriveInfo(
            name="blue",
            guid="222",
            drive_id="drv-B",
            last_backup_at="2026-05-08T15:05:57Z",
        )
        cfg.save_drives()
        data = json.loads((cfg.config_dir / "known_drives.json").read_text())
        assert data["blue"]["last_backup_at"] == "2026-05-08T15:05:57Z"

    def test_load_ignores_non_string_field(self):
        """A garbage ``last_backup_at`` (e.g. None or int from a hand-
        edited file) is normalized to ``None`` rather than crashing."""
        cfg = make_config()
        (cfg.config_dir / "known_drives.json").write_text(
            json.dumps(
                {"black": {"guid": "1", "drive_id": "d", "last_backup_at": None}},
            )
            + "\n",
        )
        with patch.object(Config, "default_config_dir", return_value=cfg.config_dir):
            loaded = Config.load()
        assert loaded.known_drives["black"].last_backup_at is None


# ═════════════════════════════════════════════════════════════════════════
#  lib/drives.py — staleness helpers
# ═════════════════════════════════════════════════════════════════════════


class TestDrivesStaleness:  # pylint: disable=missing-function-docstring
    """``drive_staleness_days`` / ``is_drive_stale`` — the pre-flight
    arithmetic that gates ``zark backup``."""

    @staticmethod
    def _info(last: str | None) -> DriveInfo:
        return DriveInfo(name="x", guid="g", drive_id="d", last_backup_at=last)

    def test_staleness_none_when_field_absent(self):
        assert drive_staleness_days(self._info(None)) is None

    def test_staleness_none_when_field_malformed(self):
        """Malformed timestamps must not raise — the user shouldn't be
        blocked by a typo in known_drives.json."""
        assert drive_staleness_days(self._info("garbage")) is None

    def test_staleness_zero_when_just_now(self):

        now = datetime(2026, 5, 8, 15, 0, 0, tzinfo=UTC)
        info = self._info("2026-05-08T15:00:00Z")
        assert drive_staleness_days(info, now=now) == 0

    def test_staleness_counts_days(self):

        now = datetime(2026, 5, 8, 15, 0, 0, tzinfo=UTC)
        info = self._info("2026-04-08T15:00:00Z")  # 30 days earlier
        assert drive_staleness_days(info, now=now) == 30

    def test_is_stale_at_threshold_boundary_not_stale(self):
        """At exactly threshold_days, the drive is NOT yet stale —
        is_drive_stale uses strict ``>`` so the boundary is fresh."""

        now = datetime(2026, 5, 8, 15, 0, 0, tzinfo=UTC)
        info = self._info("2026-03-09T15:00:00Z")  # 60 days earlier
        assert drive_staleness_days(info, now=now) == 60
        assert not is_drive_stale(info, 60, now=now)

    def test_is_stale_beyond_threshold(self):

        now = datetime(2026, 5, 8, 15, 0, 0, tzinfo=UTC)
        info = self._info("2026-03-08T15:00:00Z")  # 61 days earlier
        assert is_drive_stale(info, 60, now=now)

    def test_is_stale_false_when_field_absent(self):
        """Drives with no ``last_backup_at`` are not considered stale —
        they auto-populate on first successful backup."""
        assert not is_drive_stale(self._info(None), 60)


# ═════════════════════════════════════════════════════════════════════════
#  commands/setup.py — template_minimal migration diff
# ═════════════════════════════════════════════════════════════════════════


class TestSetupTemplateDiff:  # pylint: disable=missing-function-docstring
    """``_diff_rules`` must surface a diff for ``[template_minimal]``
    when the parsed values disagree with the expected constants.
    Saying yes to the migration regenerates the file with the new
    values — verified via a parse-of-generator round-trip."""

    def test_no_diff_when_template_matches(self):
        """A file generated by the current ``_generate_sanoid_conf``
        must produce no template diff."""
        text = _generate_sanoid_conf([])
        parsed = _parse_sanoid_conf(text)
        diff = _diff_rules(parsed, [])
        assert not diff["templates"]

    def test_diff_when_template_is_old_1_0_8_values(self):
        """A file with the legacy daily=2/no-weekly/no-monthly values
        must show up as a template diff with daily 2 → 14."""
        old_text = (
            "[template_minimal]\n"
            "frequently = 0\nhourly = 0\ndaily = 2\nweekly = 0\n"
            "monthly = 0\nyearly = 0\nautoprune = yes\nautosnap = yes\n"
        )
        parsed = _parse_sanoid_conf(old_text)
        diff = _diff_rules(parsed, [])
        assert len(diff["templates"]) == 1
        name, before, after = diff["templates"][0]
        assert name == "template_minimal"
        assert before["daily"] == "2"
        assert after["daily"] == "14"

    def test_diff_when_template_section_missing(self):
        """A file missing ``[template_minimal]`` entirely must surface
        the migration so regeneration adds the section."""
        text_no_tm = "[rpool]\nuse_template = minimal\nrecursive = no\n"
        parsed = _parse_sanoid_conf(text_no_tm)
        diff = _diff_rules(parsed, [])
        # current[template_minimal] is missing (= {}) so it cannot equal
        # the expected map → template diff is reported.
        assert len(diff["templates"]) == 1

    def test_expected_constants_match_generator(self):
        """The constant ``_TEMPLATE_MINIMAL_EXPECTED`` must match what
        ``_generate_sanoid_conf`` actually emits. They are intentionally
        defined in two places (one is the on-disk source of truth, the
        other is the comparison target); this test prevents silent
        drift between them."""
        text = _generate_sanoid_conf([])
        parsed = _parse_sanoid_conf(text)
        assert parsed["template_minimal"] == _TEMPLATE_MINIMAL_EXPECTED

    def test_print_diff_includes_template_section(self):
        """``_print_diff`` must surface the template diff in its
        output, not silently drop it."""
        diff: SanoidDiff = {
            "added": [],
            "removed": [],
            "changed": [],
            "manual": [],
            "templates": [
                (
                    "template_minimal",
                    {"daily": "2", "weekly": "0"},
                    {"daily": "14", "weekly": "8"},
                ),
            ],
        }
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            _print_diff(log, diff)
        out = buf.getvalue()
        assert "template_minimal" in out
        assert "daily" in out
        assert "14" in out


# ═════════════════════════════════════════════════════════════════════════
#  commands/backup.py — staleness reporting (informative only)
# ═════════════════════════════════════════════════════════════════════════


class TestBackupRpoReporting:  # pylint: disable=missing-function-docstring
    """Decision 15: after a backup, how old every other drive's newest point is."""

    def test_other_drives_listed_with_their_age(self):
        old = (datetime.now(UTC) - timedelta(days=34)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cfg = make_config()
        cfg.known_drives.update(
            {
                "blue": DriveInfo("blue", "1", "d1", last_backup_at=old),
                "black": DriveInfo("black", "2", "d2"),
                "green": DriveInfo("green", "3", "d3", last_backup_at=old),
            },
        )
        buf = StringIO()
        with redirect_stdout(buf):
            _report_rpo(cfg, "green", make_log())
        out = buf.getvalue()
        assert "blue: 34 day(s) since its last backup" in out
        assert "black: no backup recorded" in out
        assert "green" not in out
        assert "divergence" not in out and "retention" not in out

    def test_drive_without_anchors_gets_the_horizon(self):
        """H21: only a drive with no anchor in origin is told how long it
        stays incremental."""
        old = (datetime.now(UTC) - timedelta(days=34)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cfg = make_config()
        cfg.known_drives.update(
            {
                "blue": DriveInfo("blue", "1", "d1", last_backup_at=old),
                "black": DriveInfo("black", "2", "d2", last_backup_at=old),
            },
        )
        buf = StringIO()
        with (
            redirect_stdout(buf),
            patch.object(drives_lib, "guaranteed_overlap_days", return_value=62),
        ):
            _report_rpo(cfg, "green", make_log(), anchored={"2"}, origin={})
        out = buf.getvalue()
        assert "blue: no anchor in origin: if it still shares snapshots with the source" in out
        assert "at least 28 more day(s)" in out and "otherwise it resends in full" in out
        assert "black: no anchor" not in out

    def test_silent_with_no_other_drive(self):
        cfg = make_config()
        cfg.known_drives["blue"] = DriveInfo("blue", "1", "d1")
        buf = StringIO()
        with redirect_stdout(buf):
            _report_rpo(cfg, "blue", make_log())
        assert buf.getvalue() == ""


# ═════════════════════════════════════════════════════════════════════════
#  lib/sanoid_retention.py — config parsing
# ═════════════════════════════════════════════════════════════════════════


class TestSanoidRetention:
    """H21: how long a drive anchored only on shared snapshots stays
    incremental: (n - 1) buckets of the longest type, the smallest over the
    templates in use."""

    @staticmethod
    def _conf(text: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "sanoid.conf"
        p.write_text(text, encoding="utf-8")
        return p

    def test_one_bucket_less_than_the_count(self) -> None:
        """sanoid prunes a type only past n periods and with more than n of
        it, so the newest of a type is guaranteed n - 1 periods more."""
        minimal = {"daily": "14", "weekly": "8", "monthly": "3"}
        production = {"hourly": "24", "daily": "7", "weekly": "4", "monthly": "3"}
        assert _guaranteed_days_of_template(minimal) == 62  # max(13, 49, 62)
        assert _guaranteed_days_of_template(production) == 62
        assert _guaranteed_days_of_template({"daily": "2"}) == 1
        assert abs(_guaranteed_days_of_template({"hourly": "24"}) - 23 / 24) < 1e-9
        assert _guaranteed_days_of_template({"yearly": "2"}) == 365.25
        assert _guaranteed_days_of_template({"daily": "0"}) == 0

    def test_the_smallest_template_in_use_decides(self) -> None:
        """A drive needs every dataset; the one with the shortest horizon
        diverges first."""
        text = (
            "[rpool]\nuse_template = production\nrecursive = no\n"
            "[bpool]\nuse_template = minimal\nrecursive = no\n"
            "[template_production]\ndaily = 7\nweekly = 4\nmonthly = 3\n"
            "[template_minimal]\ndaily = 2\nweekly = 0\nmonthly = 0\n"
        )
        assert guaranteed_overlap_days(make_log(), conf_path=self._conf(text)) == 1

    def test_carmens_templates(self) -> None:
        """Both of zark's templates guarantee 62 days, not "3 months"."""
        text = (
            "[rpool/ROOT/ubuntu]\nuse_template = production\n"
            "[rpool/var]\nuse_template = minimal\n"
            "[rpool/keystore]\nautosnap = no\nautoprune = no\n"
            "[template_production]\nhourly = 24\ndaily = 7\nweekly = 4\nmonthly = 3\n"
            "[template_minimal]\ndaily = 14\nweekly = 8\nmonthly = 3\n"
            "[template_unused]\nhourly = 1\n"
        )
        assert guaranteed_overlap_days(make_log(), conf_path=self._conf(text)) == 62

    def test_none_when_unknown(self) -> None:
        """No file (with a warning) or no managed section: no horizon."""
        with redirect_stdout(StringIO()):
            missing = Path("/tmp/zark-test-nonexistent.conf")
            assert guaranteed_overlap_days(make_log(), conf_path=missing) is None
        text = "[template_production]\ndaily = 7\nweekly = 4\nmonthly = 3\n"
        assert guaranteed_overlap_days(make_log(), conf_path=self._conf(text)) is None


class TestSharedSnapshotLine:
    """H21: what a drive anchored only on shared snapshots can expect."""

    NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

    def _info(self, days_ago: int | None) -> DriveInfo:
        if days_ago is None:
            return DriveInfo("blue", "1", "d")
        when = self.NOW - timedelta(days=days_ago)
        return DriveInfo("blue", "1", "d", last_backup_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_within_the_horizon(self) -> None:
        """Days left before the next backup stops being incremental."""
        line = shared_snapshot_line(self._info(34), 62, now=self.NOW)
        assert "at least 28 more day(s)" in line and "62 days after" in line

    def test_past_the_horizon(self) -> None:
        """Past it, the next backup may resend datasets in full."""
        line = shared_snapshot_line(self._info(70), 62, now=self.NOW)
        assert "past the 62-day horizon" in line and "in full" in line

    def test_no_backup_recorded(self) -> None:
        """Without last_backup_at nothing is promised."""
        assert "no backup recorded" in shared_snapshot_line(self._info(None), 62, now=self.NOW)


class TestNoAnchorLine:
    """M3 decision 5e: a drive with no anchor is judged by what it was
    recorded to share with origin at its last complete backup."""

    T = 1_790_000_000  # 2026-09-21
    DAY = 86400
    LIVES = {"hourly": 24 * 3600.0, "daily": 7 * 86400.0, "monthly": 93 * 86400.0}

    def _info(self, record: dict[str, list[list[object]]]) -> DriveInfo:
        return DriveInfo("blue", "1", "d", extra={"shared": record})

    def _origin(self, **snaps: list["repl.Ref"]) -> dict[str, "repl.Origin"]:
        return {
            rel.replace("_", "/"): repl.Origin(rel.replace("_", "/"), snaps=s)
            for rel, s in snaps.items()
        }

    def _record(self) -> dict[str, list[list[object]]]:
        return {
            "rpool/a": [
                ["autosnap_2026-09-21_00:00:00_daily", "11", self.T],
                ["autosnap_2026-09-01_00:00:00_monthly", "12", self.T - 20 * self.DAY],
            ],
            "rpool/b": [["autosnap_2026-09-21_10:00:00_hourly", "21", self.T]],
        }

    def test_every_dataset_still_shares_the_earliest_end_is_given(self) -> None:
        """Per dataset its longest-lived kept snapshot; the drive's date is
        the earliest of those, named with the snapshot that sets it."""
        origin = self._origin(
            rpool_a=[_ref("autosnap_2026-09-21_00:00:00_daily", "11", 5)],
            rpool_b=[_ref("autosnap_2026-09-21_10:00:00_hourly", "21", 6)],
        )
        line = shared_snapshot_line(
            self._info(self._record()), None, origin=origin, lives=self.LIVES
        )
        day = datetime.fromtimestamp(self.T + 24 * 3600, UTC).strftime("%Y-%m-%d")
        assert line == (
            f"blue: no anchor in origin; it still shares snapshots with it until about {day} "
            + "(rpool/b@autosnap_2026-09-21_10:00:00_hourly): its next backup asks to roll the "
            + "drive back to them (its backup points after them go) or to resend in full"
        )
        origin["rpool/a"].snaps.append(_ref("autosnap_2026-09-01_00:00:00_monthly", "12", 4))
        origin["rpool/b"].snaps.append(_ref("autosnap_2026-09-01_00:00:00_monthly", "22", 4))
        record = self._record()
        record["rpool/b"].append(["autosnap_2026-09-01_00:00:00_monthly", "22", self.T])
        line = shared_snapshot_line(self._info(record), None, origin=origin, lives=self.LIVES)
        day = datetime.fromtimestamp(self.T - 20 * self.DAY + 93 * self.DAY, UTC).strftime(
            "%Y-%m-%d"
        )
        assert f"until about {day} (rpool/a@autosnap_2026-09-01_00:00:00_monthly)" in line

    def test_after_a_recover_nothing_is_shared(self) -> None:
        """eli R8.9: origin restored from a point holds none of the recorded
        snapshots: the next backup resends every dataset in full."""
        origin = self._origin(
            rpool_a=[_ref("zark_2026-10-13_08:13:40Z", "91", 1)],
            rpool_b=[_ref("zark_2026-10-13_08:13:40Z", "92", 1)],
        )
        line = shared_snapshot_line(self._info(self._record()), 62, origin=origin, lives=self.LIVES)
        assert line == (
            "blue: no anchor in origin; 2 dataset(s) share no snapshot with it: "
            + "its next backup resends them in full"
        )

    def test_one_dataset_lost_and_one_gone_from_origin(self) -> None:
        """A dataset origin no longer has is not counted; one whose shared
        snapshots all went is."""
        origin = self._origin(rpool_a=[_ref("autosnap_2026-09-22_00:00:00_daily", "13", 7)])
        line = shared_snapshot_line(
            self._info(self._record()), None, origin=origin, lives=self.LIVES
        )
        assert "; 1 dataset(s) share no snapshot with it" in line

    def test_prepared_drive_shares_nothing(self) -> None:
        """prepare records empty lists: losing the anchors means a full resend."""
        origin = self._origin(rpool_a=[_ref("autosnap_2026-09-22_00:00:00_daily", "13", 7)])
        line = shared_snapshot_line(
            self._info({"rpool/a": []}), None, origin=origin, lives=self.LIVES
        )
        assert "1 dataset(s) share no snapshot" in line

    def test_residues_and_unknown_lives(self) -> None:
        """A syncoid residue has no date (zark prune keeps it while needed,
        M3F-3); without sanoid.conf the line gives no date."""
        record: dict[str, list[list[object]]] = {
            "rpool/a": [["syncoid_eli_2026-09-27:01:00:00-GMT02:00", "31", self.T]]
        }
        origin = self._origin(rpool_a=[_ref("syncoid_eli_2026-09-27:01:00:00-GMT02:00", "31", 3)])
        line = shared_snapshot_line(self._info(record), None, origin=origin, lives=self.LIVES)
        assert "until they are removed with zark prune: its next backup asks to roll" in line
        origin = self._origin(
            rpool_a=[_ref("autosnap_2026-09-21_00:00:00_daily", "11", 5)],
            rpool_b=[_ref("autosnap_2026-09-21_10:00:00_hourly", "21", 6)],
        )
        line = shared_snapshot_line(self._info(self._record()), None, origin=origin, lives=None)
        assert line == (
            "blue: no anchor in origin; it still shares snapshots with it: its next backup asks "
            + "to roll the drive back to them (its backup points after them go) or to resend in "
            + "full"
        )

    def test_without_a_record_the_horizon_is_a_condition(self) -> None:
        """An entry no complete backup has written since 5e: H21's horizon,
        worded as a condition (after a recover origin may share nothing)."""
        when = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        info = DriveInfo("blue", "1", "d", last_backup_at=when)
        line = shared_snapshot_line(info, 62, origin={}, lives=self.LIVES)
        assert line == (
            "blue: no anchor in origin: if it still shares snapshots with the source, its next "
            + "backup continues from the newest of them, asking first to roll the drive back "
            + "where its own newest snapshot is not shared (its backup points after it go), for "
            + "at least 52 more day(s) (62 days after its last backup); otherwise it resends in "
            + "full"
        )
        line = shared_snapshot_line(info, None, origin={}, lives=self.LIVES)
        assert "stays incremental" not in line and "asking first to roll the drive back" in line

    def test_lives_from_the_templates_in_use(self) -> None:
        """n * period per type, the smallest n >= 1 over the templates in use;
        frequently by frequent_period (15 min unless set); unkept types left out."""
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(
                "[rpool]\nuse_template = production\n"
                "[bpool]\nuse_template = minimal\n"
                "[template_production]\nfrequently = 4\nhourly = 24\ndaily = 7\nmonthly = 3\n"
                "[template_minimal]\nfrequent_period = 5\nfrequently = 2\ndaily = 14\n"
                "monthly = 3\nyearly = 1\nhourly = 0\n"
                "[template_unused]\ndaily = 1\n"
            )
        lives = guaranteed_lives(conf_path=Path(f.name))
        assert lives == {
            "frequently": 2 * 5 * 60.0,
            "hourly": 24 * 3600.0,
            "daily": 7 * 86400.0,
            "monthly": 3 * 31 * 86400.0,
            "yearly": 365.25 * 86400,
        }
        assert guaranteed_lives(conf_path=Path("/tmp/zark-test-nonexistent.conf")) is None


class TestPoolImportExactDevice:  # pylint: disable=missing-function-docstring
    """#4: pool_import must try the exact device path before any fallback.

    Regression guard for the bridge bogus-WWN bug: when a precise device is
    supplied, ``zpool import -d <device>`` must be the first command issued,
    so ZFS opens that exact partition rather than scanning a directory and
    resolving the vdev through a generic ``wwn-0x...`` alias.
    """

    DEV = "/dev/disk/by-id/usb-Micron_X_SERIAL-0:0-part1"

    def test_exact_device_tried_first(self):
        mock = MockShell()
        # Not already imported.
        mock.on("zpool list black").fails()
        # Exact-device import succeeds.
        mock.on(f"zpool import -d {self.DEV} black").succeeds()
        with patch_sh(mock):
            zfs = ZFS(make_log())
            assert zfs.pool_import("black", device=self.DEV)
        import_calls = [c for c in mock.calls if c.startswith("zpool import")]
        assert import_calls, "no import attempted"
        # First import attempt must be the exact device, no -f.
        assert import_calls[0] == f"zpool import -d {self.DEV} black"

    def test_falls_back_when_exact_device_fails(self):
        mock = MockShell()
        mock.on("zpool list black").fails()
        # Exact device (with and without -f) fails…
        mock.on(f"zpool import -d {self.DEV} black").fails("insufficient replicas")
        mock.on(f"zpool import -f -d {self.DEV} black").fails("insufficient replicas")
        # …directory fallback succeeds.
        mock.on("zpool import -d /dev/disk/by-id black").succeeds()
        with patch_sh(mock):
            zfs = ZFS(make_log())
            assert zfs.pool_import("black", device=self.DEV)
        import_calls = [c for c in mock.calls if c.startswith("zpool import")]
        # Exact device attempts come strictly before the directory fallback.
        assert import_calls[0] == f"zpool import -d {self.DEV} black"
        assert any("/dev/disk/by-id" in c for c in import_calls)

    def test_no_device_uses_fallbacks_only(self):
        mock = MockShell()
        mock.on("zpool list black").fails()
        mock.on("zpool import black").succeeds()
        with patch_sh(mock):
            zfs = ZFS(make_log())
            assert zfs.pool_import("black")  # device=None
        import_calls = [c for c in mock.calls if c.startswith("zpool import")]
        # No exact-device -d should appear when device is None.
        assert all("usb-Micron" not in c for c in import_calls)


class TestReadbackVerification:  # pylint: disable=missing-function-docstring
    """#1: verify_exported_pool_readback re-imports read-only and checks health.

    Guards against USB-SATA bridges that lie about FUA — an export that
    returns 0 over a pool that is no longer importable (``error=52``).
    """

    DEV = "/dev/disk/by-id/usb-Micron_X_SERIAL-0:0-part1"
    IMPORT = f"zpool import -o readonly=on -N -R /run/zark/altroot/black -d {DEV} black"

    def test_passes_when_reimport_online(self):
        mock = MockShell()
        mock.on("sync").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on(self.IMPORT).succeeds()
        mock.on("zpool list -H -o health black").succeeds("ONLINE")
        mock.on("zpool export black").succeeds()
        with patch_sh(mock):
            zfs = ZFS(make_log())
            assert zfs.verify_exported_pool_readback("black", device=self.DEV).ok
        # Cache must be dropped before the read-back import.
        assert mock.was_called("drop_caches")
        # Pool must be left exported again.
        assert mock.was_called("zpool export black")

    def test_fails_when_reimport_fails(self):
        mock = MockShell()
        mock.on("sync").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on(self.IMPORT).fails(
            "cannot import 'black': insufficient replicas",
        )
        with patch_sh(mock):
            zfs = ZFS(make_log())
            rb = zfs.verify_exported_pool_readback("black", device=self.DEV)
        assert not rb.ok and not rb.imported
        assert "insufficient replicas" in "\n".join(rb.describe())

    def test_fails_when_health_not_online(self):
        mock = MockShell()
        mock.on("sync").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on(self.IMPORT).succeeds()
        mock.on("zpool list -H -o health black").succeeds("DEGRADED")
        mock.on("zpool status -v black").succeeds("errors: 3 data errors\n  black/rpool/x:<0x1>")
        mock.on("zpool export black").succeeds()
        with patch_sh(mock):
            zfs = ZFS(make_log())
            rb = zfs.verify_exported_pool_readback("black", device=self.DEV)
        assert not rb.ok and rb.imported and rb.reexported
        text = "\n".join(rb.describe())
        # I6: says what happened, shows the errors, blames no bridge.
        assert "re-imported, but its health is DEGRADED" in text
        assert "3 data errors" in text
        assert "FUA" not in text and "bridge" not in text
        # Even on failure, the pool is re-exported to restore on-entry state.
        assert mock.was_called("zpool export black")

    def test_fails_when_reexport_fails(self):
        mock = MockShell()
        mock.on("sync").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on(self.IMPORT).succeeds()
        mock.on("zpool list -H -o health black").succeeds("ONLINE")
        mock.on("zpool export").fails("pool is busy")
        with patch_sh(mock):
            zfs = ZFS(make_log())
            rb = zfs.verify_exported_pool_readback("black", device=self.DEV)
        # I12: a pool left imported is not a verified backup.
        assert not rb.ok and not rb.reexported
        assert mock.was_called("zpool export -f black")


class TestHealthChecks:  # pylint: disable=missing-function-docstring
    """Non-destructive drive risk checks in lib/health.py."""

    def test_known_bad_bridge_listed(self):
        # Both Micron CT2000X10* enclosure bridges are catalogued.
        assert "0634:5604" in KNOWN_BAD_BRIDGES
        assert "0634:5607" in KNOWN_BAD_BRIDGES

    def test_fua_finding_when_dmesg_reports_no_fua(self):
        mock = MockShell()
        mock.on("dmesg").succeeds(
            "[ 1.0] sd 0:0:0:0: [sda] Write cache: enabled, read cache: "
            "enabled, doesn't support DPO or FUA\n",
        )
        with patch_sh(mock):
            finding = _check_fua("sda")
        assert finding is not None
        assert finding.level == WARN
        assert finding.see_hardware_doc

    def test_fua_no_finding_when_clean(self):
        mock = MockShell()
        mock.on("dmesg").succeeds("[ 1.0] sd 0:0:0:0: [sda] Attached SCSI disk\n")
        with patch_sh(mock):
            assert _check_fua("sda") is None

    def test_transport_uas_warns(self):
        mock = MockShell()
        mock.on("lsblk -dn -o TRAN /dev/sda").succeeds("usb")
        mock.on("scsi host").succeeds("[ 1.0] scsi host0: uas")
        with patch_sh(mock):
            finding = _check_transport("sda")
        assert finding is not None
        assert finding.level == WARN

    def test_transport_usb_storage_info(self):
        mock = MockShell()
        mock.on("lsblk -dn -o TRAN /dev/sda").succeeds("usb")
        mock.on("scsi host").succeeds(
            "[ 1.0] usb 2-1: UAS is ignored for this device, using usb-storage instead",
        )
        with patch_sh(mock):
            finding = _check_transport("sda")
        assert finding is not None
        assert finding.level == INFO

    def test_transport_silent_for_non_usb(self):
        mock = MockShell()
        mock.on("lsblk -dn -o TRAN /dev/nvme0n1").succeeds("nvme")
        with patch_sh(mock):
            assert _check_transport("nvme0n1") is None

    def test_report_worst_level_and_has_risk(self):
        rep = HealthReport(device="/dev/sda")
        rep.findings.append(Finding(level=OK, title="t", detail="d"))
        assert rep.worst_level == OK
        assert not rep.has_risk
        rep.findings.append(Finding(level=INFO, title="t", detail="d"))
        assert rep.worst_level == INFO
        rep.findings.append(Finding(level=WARN, title="t", detail="d"))
        assert rep.worst_level == WARN
        assert rep.has_risk


class _FakeHealthZFS:  # pylint: disable=too-few-public-methods
    """Stand-in ZFS for health destructive tests: pools always report ONLINE."""

    def pool_health(self, _name):
        """Return a constant healthy status for any pool name."""
        return "ONLINE"


class TestHealthDestructive:  # pylint: disable=missing-function-docstring
    """Destructive write-and-verify logic in lib/health.py (mocked shell)."""

    def test_profile_target_bytes_fast_medium(self):
        assert profile_target_bytes(PROFILE_FAST, "/dev/sda") == 2 * 1024**3
        assert profile_target_bytes(PROFILE_MEDIUM, "/dev/sda") == 15 * 1024**3

    def test_profile_target_bytes_surface_capped(self):
        mock = MockShell()
        # Huge disk -> capped.
        mock.on("lsblk -bdn -o SIZE /dev/sda").succeeds(str(4 * 1024**4))
        with patch_sh(mock):
            assert profile_target_bytes(PROFILE_SURFACE, "/dev/sda") == SURFACE_CAP_BYTES

    def test_estimate_seconds_pessimistic(self):
        # 2 GB at 100 MB/s -> ~20s sequential, *3 factor -> ~60s+.
        secs = estimate_seconds(PROFILE_FAST, "/dev/sda", 100.0)
        assert secs > 0
        # No speed -> no estimate.
        assert estimate_seconds(PROFILE_FAST, "/dev/sda", 0.0) == 0

    def test_destructive_test_passes_online(self):
        mock = MockShell()
        # Make every command succeed by default for this happy path.
        mock.on("zpool create").succeeds()
        mock.on("dd").succeeds()
        mock.on("sync").succeeds()
        mock.on("zpool export").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on("zpool import").succeeds()
        mock.on("zpool destroy").succeeds()
        mock.on("rm -rf").succeeds()
        with patch_sh(mock):
            result = run_destructive_test(
                "/dev/sda",
                PROFILE_FAST,
                False,
                make_log(),
                zfs=_FakeHealthZFS(),
            )
        assert result.passed
        # The test pool must always be destroyed (finally block).
        assert mock.was_called("zpool destroy")

    def test_destructive_test_fails_on_reimport(self):
        mock = MockShell()
        mock.on("zpool create").succeeds()
        mock.on("dd").succeeds()
        mock.on("sync").succeeds()
        mock.on("zpool export").succeeds()
        mock.on("drop_caches").succeeds()
        # Re-import fails with the FUA-lie signature.
        mock.on("zpool import -o readonly=on").fails(
            "cannot import: insufficient replicas",
        )
        mock.on("zpool destroy").succeeds()
        mock.on("rm -rf").succeeds()
        with patch_sh(mock):
            result = run_destructive_test(
                "/dev/sda",
                PROFILE_FAST,
                False,
                make_log(),
                zfs=_FakeHealthZFS(),
            )
        assert not result.passed
        assert mock.was_called("zpool destroy")

    def test_destructive_cold_waits_for_reconnect(self):
        called = {"reconnect": False, "eject": False}

        def _wait():
            called["reconnect"] = True

        mock = MockShell()
        mock.on("zpool create").succeeds()
        mock.on("dd").succeeds()
        mock.on("sync").succeeds()
        mock.on("zpool export").succeeds()
        mock.on("eject").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on("zpool import").succeeds()
        mock.on("zpool destroy").succeeds()
        mock.on("rm -rf").succeeds()
        with patch_sh(mock):
            result = run_destructive_test(
                "/dev/sda",
                PROFILE_FAST,
                True,
                make_log(),
                zfs=_FakeHealthZFS(),
                wait_for_reconnect=_wait,
            )
        assert result.passed
        assert called["reconnect"]  # cold pause was hit
        assert mock.was_called("eject /dev/sda")

    def test_destructive_pass_with_transport_errors(self):
        """A pool that reads back ONLINE but logged DID_ERROR during the test
        is reported as passed-but-with-transport-errors (the flaky-bridge
        case observed on 0634:5604 under UAS)."""
        mock = MockShell()
        mock.on("zpool create").succeeds()
        mock.on("dd").succeeds()
        mock.on("sync").succeeds()
        mock.on("zpool export").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on("zpool import").succeeds()
        mock.on("zpool destroy").succeeds()
        mock.on("rm -rf").succeeds()
        mock.on("wipefs").succeeds()
        mock.on("sgdisk").succeeds()
        # First dmesg call (the marker, tail -1) — old timestamp.
        # Subsequent dmesg call (the scan) — contains a DID_ERROR after it.
        mock.on("dmesg | tail -1").succeeds("[ 100.0] sd 0:0:0:0: [sda] ready")
        mock.on("dmesg").succeeds(
            "[ 100.0] sd 0:0:0:0: [sda] ready\n"
            "[ 200.5] sd 0:0:0:0: [sda] Synchronize Cache(10) failed: "
            "Result: hostbyte=DID_ERROR driverbyte=DRIVER_OK\n",
        )
        with patch_sh(mock):
            result = run_destructive_test(
                "/dev/sda",
                PROFILE_FAST,
                False,
                make_log(),
                zfs=_FakeHealthZFS(),
            )
        assert result.passed  # data survived
        assert result.transport_errors  # but the bus stumbled
        assert "transport errors" in result.detail.lower()

    def test_destructive_wipes_device_in_cleanup(self):
        """The finally block wipes signatures and the partition table so the
        device is left genuinely blank (no orphan zfs_member partitions)."""
        mock = MockShell()
        mock.on("zpool create").succeeds()
        mock.on("dd").succeeds()
        mock.on("sync").succeeds()
        mock.on("zpool export").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on("zpool import").succeeds()
        mock.on("zpool destroy").succeeds()
        mock.on("rm -rf").succeeds()
        mock.on("wipefs").succeeds()
        mock.on("sgdisk").succeeds()
        mock.on("dmesg").succeeds("")
        with patch_sh(mock):
            run_destructive_test(
                "/dev/sda",
                PROFILE_FAST,
                False,
                make_log(),
                zfs=_FakeHealthZFS(),
            )
        assert mock.was_called("wipefs -a /dev/sda")
        assert mock.was_called("sgdisk --zap-all /dev/sda")

    def test_transport_errors_ignores_clean_disconnect(self):
        """DID_NO_CONNECT (a clean unplug) must NOT be flagged as an error."""
        mock = MockShell()
        mock.on("dmesg").succeeds(
            "[ 300.0] sd 0:0:0:0: [sda] Synchronize Cache(10) failed: "
            "Result: hostbyte=DID_NO_CONNECT driverbyte=DRIVER_OK\n",
        )
        with patch_sh(mock):
            errs = _transport_errors_since("sda", since_ts=100.0)
        assert not errs  # clean disconnect is not a transport error

    def test_report_obfuscation(self):
        mock = MockShell()
        # Minimal environment responses; serial-like token in dmesg.
        mock.on("zark --version").succeeds("zark v1.0.12")
        mock.on("lsb_release").succeeds("Ubuntu 25.10")
        mock.on("zfs version").succeeds("zfs-2.2.6")
        mock.on("uname").succeeds("Linux carmen")
        mock.on("lsblk").succeeds("CT2000X10PROSSD9")
        mock.on("dmesg").succeeds("serial 2449E8CD1F15 attached")
        with patch_sh(mock):
            report = HealthReport(device="/dev/sda")
            clean = generate_report("/dev/sda", report, None, obfuscate=True)
            raw = generate_report("/dev/sda", report, None, obfuscate=False)
        assert "2449E8CD1F15" not in clean
        assert "<redacted>" in clean
        assert "2449E8CD1F15" in raw


class TestIsLiveUsb:  # pylint: disable=missing-function-docstring
    """lib.sh.is_live_usb — single source of truth for live-media detection."""

    def test_true_on_casper_cmdline(self):
        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("BOOT_IMAGE=/casper/vmlinuz boot=casper quiet")
        with patch_sh(mock):
            assert _sh.is_live_usb()

    def test_true_on_rofs_when_cmdline_silent(self):
        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("BOOT_IMAGE=/vmlinuz root=ZFS=rpool/ROOT/ubuntu")
        mock.on("test -d /rofs").succeeds()
        mock.on("test -d /cow").fails()
        with patch_sh(mock):
            assert _sh.is_live_usb()

    def test_false_on_installed_system(self):
        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("BOOT_IMAGE=/vmlinuz root=ZFS=rpool/ROOT/ubuntu_x")
        mock.on("test -d /rofs").fails()
        mock.on("test -d /cow").fails()
        with patch_sh(mock):
            assert not _sh.is_live_usb()


class TestRepairBootImport:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """repair-boot must import rpool/bpool with a -f fallback, not abort.

    Regression guard for the "boot left without a kernel, repair-boot then
    failed because the import lacked -f" incident: a pool left in-use by an
    unclean shutdown must still import (clean attempt first, then forced),
    routed through ZFS.pool_import so the operator never types -f by hand.
    """

    def test_forced_import_used_when_clean_fails(self):
        zfs = ZFS(make_log())
        mock = MockShell()
        mock.on("zpool list bpool").fails()  # not yet imported
        mock.on("zpool import -N -R /mnt/repair bpool").fails("pool was previously in use")
        mock.on("zpool import -f -N -R /mnt/repair bpool").succeeds()
        with patch_sh(mock):
            assert zfs.pool_import("bpool", altroot="/mnt/repair", no_mount=True)
        import_calls = [c for c in mock.calls if c.startswith("zpool import")]
        # Clean attempt strictly precedes the forced one.
        assert import_calls[0] == "zpool import -N -R /mnt/repair bpool"
        assert "zpool import -f -N -R /mnt/repair bpool" in import_calls


class _FakeKeystore:  # pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods # noqa: E501
    """Minimal stand-in for Keystore in system-mount tests (no real LUKS)."""

    bad_passphrase = False

    def __init__(self, loaded: int = 2):
        self._loaded = loaded

    def mount(self, pool, passphrase, *, readonly=False):  # pylint: disable=unused-argument
        return True

    def load_pool_keys(self, pool_root):  # pylint: disable=unused-argument
        return self._loaded

    def umount(self):
        """Never reached in these tests, but Cleanup.track_keystore will
        call it if the cleanup handler ever runs — so the stub must have
        it, and the KeystoreLike protocol now says so."""


class TestSystemMountHelpers:  # pylint: disable=missing-function-docstring
    """lib.mount system-layout helpers used by `zark chroot` / `zark mount local`."""

    def test_find_system_root_dataset(self):
        mock = MockShell()
        mock.on("zfs list -H -o name -r rpool/ROOT").succeeds(
            "rpool/ROOT\nrpool/ROOT/ubuntu_8bt2zy\n",
        )
        with patch_sh(mock):
            assert find_system_root_dataset() == "rpool/ROOT/ubuntu_8bt2zy"

    def test_mount_system_pools_imports_both_and_returns_root(self):
        mock = MockShell()
        # Neither pool imported yet.
        mock.on("zpool list rpool").fails()
        mock.on("zpool list bpool").fails()
        # Imports succeed (clean, no -f needed), scanning by-id first so the
        # pools record stable device names (eli 2026-09-30).
        mock.on("zpool import -N -R /mnt/zark/chroot -d /dev/disk/by-id rpool").succeeds()
        mock.on("zpool import -N -R /mnt/zark/chroot -d /dev/disk/by-id bpool").succeeds()
        # Root dataset discovery + mounts.
        mock.on("zfs list -H -o name -r rpool/ROOT").succeeds(
            "rpool/ROOT\nrpool/ROOT/ubuntu_x\n",
        )
        mock.on("zfs mount rpool/ROOT/ubuntu_x").succeeds()
        mock.on("zfs get -H -o value mountpoint rpool/ROOT/ubuntu_x").succeeds("/")
        # rpool dataset listing: container + BE + keystore zvol + a child.
        mock.on(
            "zfs list -H -o name,mountpoint,used,refer,canmount,type -t filesystem,volume -r rpool",
        ).succeeds(
            "rpool\t/\t1G\t1G\ton\tfilesystem\n"
            "rpool/ROOT\tnone\t1G\t1G\toff\tfilesystem\n"
            "rpool/ROOT/ubuntu_x\t/\t1G\t1G\tnoauto\tfilesystem\n"
            "rpool/keystore\t-\t20M\t20M\t-\tvolume\n"
            "rpool/USERDATA\t/home\t1G\t1G\ton\tfilesystem\n",
        )
        mock.on("zfs mount rpool/USERDATA").succeeds()
        mock.on("zfs get -H -o value mountpoint rpool/USERDATA").succeeds("/home")
        # bpool boot env.
        mock.on("zfs list bpool/BOOT/ubuntu_x").succeeds("bpool/BOOT/ubuntu_x")
        mock.on("zfs mount bpool/BOOT/ubuntu_x").succeeds()
        mock.on("zfs get -H -o value mountpoint bpool/BOOT/ubuntu_x").succeeds("/boot")

        cleanup = Cleanup(make_log())
        with patch_sh(mock), redirect_stdout(StringIO()):
            res = mount_system_pools(
                "/mnt/zark/chroot",
                "pp",
                make_log(),
                ZFS(make_log()),
                _FakeKeystore(),
                cleanup,
            )
        assert res == ("/mnt/zark/chroot", "ubuntu_x")
        # Both pools were imported (and thus tracked for clean export).
        imports = [c for c in mock.calls if c.startswith("zpool import")]
        assert imports == [
            "zpool import -N -R /mnt/zark/chroot -d /dev/disk/by-id rpool",
            "zpool import -N -R /mnt/zark/chroot -d /dev/disk/by-id bpool",
        ]
        # Keystore zvol must never be mounted as a filesystem.
        assert not mock.was_called("zfs mount rpool/keystore")
        # The helper must never touch mountpoint properties (project rule #1).
        assert not any("zfs set mountpoint" in c for c in mock.calls)


class TestUmountLocalSafety:  # pylint: disable=missing-function-docstring
    """umount local must refuse to export the *running* system's rpool.

    Discriminator: zark imports the system under an altroot beneath
    /mnt/zark; the live root has altroot '-'. Exporting the latter would
    pull the filesystem out from under a running machine.
    """

    def test_refuses_when_no_altroot(self):
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool get -H -o value altroot rpool").succeeds("-")
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                return_value="",
            ),
        ):
            try:
                _umount_local_system(make_log())
            except SystemExit:
                # Must NOT have exported anything.
                assert not mock.was_called("zpool export")
                return
        raise AssertionError("Expected SystemExit refusing to export the running system")

    def test_exports_when_zark_altroot(self):
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool get -H -o value altroot rpool").succeeds("/mnt/zark/system")
        mock.on("zpool list bpool").succeeds("bpool")
        mock.on("findmnt -rn -o TARGET,FSTYPE").succeeds(
            "/ overlay\n"
            "/mnt/zark/system zfs\n"
            "/mnt/zark/system/boot zfs\n"
            "/mnt/zark/system/proc proc\n"
            "/mnt/zark/system/run tmpfs\n"
            "/mnt/zark/system/run/user/1000 tmpfs\n"
            "/mnt/zark/system/home zfs\n"
            "/mnt/zark/systemx tmpfs\n"
            "/mnt/zark/backup zfs\n"
            "/mnt/zark/backup/proc proc",
        )
        mock.on("zfs unload-key -r bpool").succeeds()
        mock.on("zfs unload-key -r rpool").succeeds()
        mock.on("zpool export bpool").succeeds()
        mock.on("zpool export rpool").succeeds()
        mock.on("sync").succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            _umount_local_system(make_log())
        assert mock.was_called("zpool export rpool")
        assert mock.was_called("zpool export bpool")
        # R2-5: only the system's tree; a backup mounted alongside stays.
        # V-3: the non-ZFS mounts (binds) are made private and unmounted first,
        # each once at its top; the ZFS tree is unmounted without that, so the
        # unmount reaches its copies in other mount namespaces (eli, K6).
        tree = [c for c in mock.calls if c.startswith(("mount --make-rprivate", "umount -R"))]
        assert tree == [
            "mount --make-rprivate /mnt/zark/system/proc",
            "umount -R /mnt/zark/system/proc",
            "mount --make-rprivate /mnt/zark/system/run",
            "umount -R /mnt/zark/system/run",
            "umount -R /mnt/zark/system",
        ]
        assert mock.was_not_called("zfs unmount")

    def test_a_failed_export_is_not_reported_as_unmounted(self):
        # K6-1 (eli): "SYSTEM UNMOUNTED — exported cleanly" printed under two
        # "Could not export" warnings.
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool get -H -o value altroot rpool").succeeds("/mnt/zark/system")
        mock.on("zpool list bpool").succeeds("bpool")
        mock.on_prefix("zpool export").fails("pool is busy")
        buf = StringIO()
        with (
            patch_sh(mock),
            patch.object(umount_mod, "flush_device_cache"),
            redirect_stdout(buf),
        ):
            try:
                _umount_local_system(make_log())
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("a pool left imported must end with exit status 1")
        out = buf.getvalue()
        assert "SYSTEM NOT UNMOUNTED" in out and "still imported: bpool, rpool" in out
        assert "exported cleanly" not in out

    def test_refuses_an_altroot_that_climbs_out_of_mnt_zark(self):
        # V-7: a prefix match on the stored string let "/mnt/zark/../.." through.
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool get -H -o value altroot rpool").succeeds("/mnt/zark/../..")
        with patch_sh(mock), redirect_stdout(StringIO()), patch("builtins.input", return_value=""):
            try:
                _umount_local_system(make_log())
            except SystemExit:
                assert mock.was_not_called("umount") and mock.was_not_called("mount --make")
                return
        raise AssertionError("Expected SystemExit for an altroot resolving to /")

    def test_refuses_an_altroot_that_only_starts_like_mnt_zark(self):
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool get -H -o value altroot rpool").succeeds("/mnt/zarkfoo")
        with patch_sh(mock), redirect_stdout(StringIO()), patch("builtins.input", return_value=""):
            try:
                _umount_local_system(make_log())
            except SystemExit:
                assert mock.was_not_called("umount") and mock.was_not_called("zpool export")
                return
        raise AssertionError("Expected SystemExit for an altroot outside /mnt/zark/")

    def test_closes_keystore_before_export(self):
        """umount local must close the LUKS keystore (cryptsetup) before
        exporting rpool, else the held zvol hangs the export in taskq_wait.
        Regression for the live-USB hang found on hardware."""
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool get -H -o value altroot rpool").succeeds("/mnt/zark/system")
        mock.on("zpool list bpool").succeeds("bpool")
        mock.on("umount -R /mnt/zark/system").succeeds()
        mock.on("zfs unload-key -r bpool").succeeds()
        mock.on("zfs unload-key -r rpool").succeeds()
        mock.on("zpool export bpool").succeeds()
        mock.on("zpool export rpool").succeeds()
        mock.on("umount").succeeds()
        mock.on("cryptsetup close").succeeds()
        mock.on("sync").succeeds()
        # Pretend the keystore LUKS mapper is open (opened by a prior
        # `zark mount local`) and its mountpoint is active.
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch("lib.keystore.Path") as fake_path,
        ):
            inst = fake_path.return_value
            inst.exists.return_value = True
            inst.is_mount.return_value = True
            _umount_local_system(make_log())
        # The cryptsetup mapping must have been torn down before the export.
        assert mock.was_called("cryptsetup close")


class TestChrootSafety:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """zark chroot must refuse when rpool is already imported (running system)."""

    def test_refuses_when_rpool_imported(self):
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                return_value="",
            ),
        ):
            try:
                chroot_mod.run([])
            except SystemExit:
                return
        raise AssertionError("Expected SystemExit when rpool is already imported")


class TestAptGuard:  # pylint: disable=missing-function-docstring
    """The standalone apt/dpkg backup guard installed by setup/recover/finish.

    Covers the Python installer (writes three files, correct perms, overwrite
    semantics) and the cheap exit paths of the shell hook that can run without
    zpool/dpkg-deb present (the ZARK_INTERNAL escape and the no-sensitive-
    package short-circuit). The block path — external pool + boot-critical
    package — needs a real importable pool and is exercised by integration
    testing, not CI; here it is covered by content assertions on the script.
    """

    def test_install_writes_three_files(self):
        with tempfile.TemporaryDirectory() as td:
            apt_guard.install(target_root=td, log=make_log())
            hook = Path(td) / apt_guard.HOOK_RELATIVE_PATH
            conf = Path(td) / apt_guard.APT_CONF_RELATIVE_PATH
            motd = Path(td) / apt_guard.MOTD_RELATIVE_PATH
            for p in (hook, conf, motd):
                assert p.exists(), f"missing {p}"
                assert p.stat().st_mode & 0o111, f"not executable: {p}"
            # apt.conf registers the hook via Pre-Install-Pkgs at its abs path.
            conf_text = conf.read_text(encoding="utf-8")
            assert "DPkg::Pre-Install-Pkgs" in conf_text
            assert f"/{apt_guard.HOOK_RELATIVE_PATH}" in conf_text
            # Hook honours the ZARK_INTERNAL escape and detects external pools.
            hook_text = hook.read_text(encoding="utf-8")
            assert "ZARK_INTERNAL" in hook_text
            assert "zpool import" in hook_text

    def test_sensitive_globs_present_in_hook(self):
        # Every glob in the single source of truth must appear in the hook's
        # case pattern, so the constant and the script can never drift.
        for glob in apt_guard.SENSITIVE_GLOBS:
            assert glob in apt_guard.HOOK_SCRIPT, f"glob not wired into hook: {glob}"
        # The categories from the incident must all be covered.
        joined = " ".join(apt_guard.SENSITIVE_GLOBS)
        for needed in ("linux-image-*", "grub-*", "shim-*", "zfs-*"):
            assert needed in joined

    def test_overwrite_false_keeps_existing(self):
        with tempfile.TemporaryDirectory() as td:
            hook = Path(td) / apt_guard.HOOK_RELATIVE_PATH
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("SENTINEL", encoding="utf-8")
            apt_guard.install(target_root=td, log=make_log(), overwrite=False)
            assert hook.read_text(encoding="utf-8") == "SENTINEL"

    def test_install_is_noop_when_current(self):
        # Re-running install on an up-to-date tree must not rewrite anything,
        # so `zark setup` stays a true no-op (no mtime churn).
        with tempfile.TemporaryDirectory() as td:
            apt_guard.install(target_root=td, log=make_log())
            hook = Path(td) / apt_guard.HOOK_RELATIVE_PATH
            # First write created it; a second _write with identical content
            # is a no-op.
            assert (
                apt_guard._write(  # pylint: disable=protected-access
                    hook,
                    apt_guard.HOOK_SCRIPT,
                    overwrite=True,
                )
                is False
            )

    def _written_hook(self, td: str) -> str:
        apt_guard.install(target_root=td, log=make_log())
        return str(Path(td) / apt_guard.HOOK_RELATIVE_PATH)

    def test_hook_exits_zero_when_internal(self):
        # ZARK_INTERNAL=1 must short-circuit before any pool scan.
        with tempfile.TemporaryDirectory() as td:
            hook = self._written_hook(td)
            env = {**os.environ, "ZARK_INTERNAL": "1"}
            proc = subprocess.run(
                ["sh", hook],
                input="/var/cache/apt/archives/linux-image-x_1_amd64.deb\n",
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            assert proc.returncode == 0

    def test_hook_exits_zero_for_non_boot_package(self):
        # A non-boot-critical package must be allowed; the hook short-circuits
        # before the zpool scan, so this is safe without ZFS present.
        with tempfile.TemporaryDirectory() as td:
            hook = self._written_hook(td)
            env = {k: v for k, v in os.environ.items() if k != "ZARK_INTERNAL"}
            proc = subprocess.run(
                ["sh", hook],
                input="/var/cache/apt/archives/cowsay_3.03_all.deb\n",
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            assert proc.returncode == 0


# ═════════════════════════════════════════════════════════════════════════
#  lib/identity.py and lib/registry.py (M1: I20–I25, hallazgo 7)
# ═════════════════════════════════════════════════════════════════════════

_KINGSTON_ID = "usb-Kingston_DT_microDuo_3C_408D5C15CFA5E961091D0CCA-0:0"
_LSBLK_SDB = (
    'NAME="sdb" TYPE="disk" PKNAME="" MODEL="DT microDuo 3C" '
    'SERIAL="408D5C15CFA5E9610" SIZE="115.5G" TRAN="usb"'
)
_LSBLK_SDB1 = 'NAME="sdb1" TYPE="part" PKNAME="sdb" MODEL="" SERIAL="" SIZE="115.5G" TRAN=""'
_BY_ID_LISTING = (
    f"{_KINGSTON_ID}\t../../sdb\n"
    f"{_KINGSTON_ID}-part1\t../../sdb1\n"
    "wwn-0x5000000000000001\t../../sdb\n"
    "ata-KINGSTON_SKC600MS512G_50026B7784FC3319\t../../sda\n"
)
_ZDB_LABEL = (
    "------------------------------------\n"
    "LABEL 0\n"
    "------------------------------------\n"
    "    version: 5000\n"
    "    name: 'backup'\n"
    "    state: 1\n"
    "    pool_guid: 3446866051930726346\n"
    "    hostid: 1635764780\n"
    "    hostname: 'ubuntu'\n"
    "    vdev_children: 1\n"
)


def _identity_mock() -> MockShell:
    mock = MockShell()
    mock.on("lsblk -dn -P -o NAME,TYPE,PKNAME,MODEL,SERIAL,SIZE,TRAN /dev/sdb1").succeeds(
        _LSBLK_SDB1,
    )
    mock.on("lsblk -dn -P -o NAME,TYPE,PKNAME,MODEL,SERIAL,SIZE,TRAN /dev/sdb").succeeds(
        _LSBLK_SDB,
    )
    mock.on_prefix("find /dev/disk/by-id/").succeeds(_BY_ID_LISTING)
    mock.on(f"zdb -l /dev/disk/by-id/{_KINGSTON_ID}-part1").succeeds(_ZDB_LABEL)
    return mock


class TestIdentity:  # pylint: disable=missing-function-docstring
    """Single device-identity path shared by every command (I20/I25)."""

    def test_preferred_by_id_avoids_bogus_wwn(self):
        names = ["wwn-0x5000000000000001", _KINGSTON_ID]
        assert preferred_by_id(names) == _KINGSTON_ID

    def test_preferred_by_id_falls_back_to_wwn_when_alone(self):
        assert preferred_by_id(["wwn-0x5000000000000001"]) == "wwn-0x5000000000000001"

    def test_by_id_names_skips_partitions_and_other_disks(self):
        mock = _identity_mock()
        with patch_sh(mock):
            names = by_id_names("/dev/sdb")
        assert names == [_KINGSTON_ID, "wwn-0x5000000000000001"]

    def test_read_pool_label(self):
        mock = MockShell()
        mock.on("zdb -l /dev/sdb1").succeeds(_ZDB_LABEL)
        with patch_sh(mock):
            label = read_pool_label("/dev/sdb1")
        assert label == PoolLabel("backup", "3446866051930726346", "1635764780", "ubuntu")

    def test_read_pool_label_none_on_blank_disk(self):
        mock = MockShell()
        mock.on("zdb -l /dev/sdb1").fails("failed to unpack label 0")
        with patch_sh(mock):
            assert read_pool_label("/dev/sdb1") is None

    def test_resolve_by_id_argument(self):
        """The by-id spelling that broke 1.0.12 resolves to the real disk."""
        mock = _identity_mock()
        with (
            patch_sh(mock),
            patch("lib.identity.os.path.realpath", return_value="/dev/sdb"),
            patch("lib.identity.Path.exists", return_value=True),
        ):
            ident = resolve_disk(f"/dev/disk/by-id/{_KINGSTON_ID}")
        assert ident.disk == "/dev/sdb"
        assert ident.by_id == _KINGSTON_ID
        assert ident.model == "DT microDuo 3C"
        assert ident.transport == "usb"
        assert ident.part1 == f"/dev/disk/by-id/{_KINGSTON_ID}-part1"
        assert ident.label is not None
        assert ident.label.guid == "3446866051930726346"

    def test_resolve_refuses_partition(self):
        mock = _identity_mock()
        with (
            patch_sh(mock),
            patch("lib.identity.os.path.realpath", return_value="/dev/sdb1"),
        ):
            try:
                resolve_disk(f"/dev/disk/by-id/{_KINGSTON_ID}-part1")
            except IdentityError as e:
                assert "/dev/sdb" in str(e)
                return
        raise AssertionError("expected IdentityError")

    def test_resolve_partition_allowed(self):
        mock = _identity_mock()
        with (
            patch_sh(mock),
            patch("lib.identity.os.path.realpath", return_value="/dev/sdb1"),
            patch("lib.identity.Path.exists", return_value=True),
        ):
            ident = resolve_disk("/dev/sdb1", allow_partition=True)
        assert ident.disk == "/dev/sdb"

    def test_match_registry_by_label_guid_and_drive_id(self):
        drives = {
            "backup": DriveInfo("backup", "3446866051930726346", "<unknown>"),
            "stale": DriveInfo("stale", "111", _KINGSTON_ID),
            "other": DriveInfo("other", "222", "usb-Other-0:0"),
        }
        ident = DiskIdentity(
            disk="/dev/sdb",
            by_id=_KINGSTON_ID,
            label=PoolLabel("backup", "3446866051930726346"),
        )
        assert match_registry(ident, drives) == ["backup", "stale"]

    def test_protected_disks(self):
        mock = MockShell()
        mock.on("zpool list -H -o name").succeeds("rpool\nbpool\nbackup")
        mock.on("zpool list -vHP rpool").succeeds(
            "rpool\t476G\t10G\n\t/dev/disk/by-id/ata-X-part4\t476G\t10G",
        )
        mock.on("zpool list -vHP bpool").succeeds(
            "bpool\t2G\t1G\n\t/dev/disk/by-id/ata-X-part2\t2G\t1G",
        )
        mock.on("zpool list -vHP backup").succeeds(
            "backup\t115G\t9G\n\t/dev/disk/by-id/usb-K-part1\t115G\t9G",
        )
        mock.on("lsblk -nrs -o NAME,TYPE /dev/disk/by-id/ata-X-part4").succeeds(
            "sda4 part\nsda disk",
        )
        mock.on("lsblk -nrs -o NAME,TYPE /dev/disk/by-id/ata-X-part2").succeeds(
            "sda2 part\nsda disk",
        )
        mock.on("lsblk -nrs -o NAME,TYPE /dev/disk/by-id/usb-K-part1").succeeds(
            "sdb1 part\nsdb disk",
        )
        mock.on("findmnt -rn -o SOURCE,TARGET").succeeds(
            "rpool/ROOT/x /\n/dev/sdc1 /cdrom\n/dev/mapper/luks /media/j",
        )
        mock.on("lsblk -nrs -o NAME,TYPE /dev/sdc1").succeeds("sdc1 part\nsdc disk")
        mock.on("lsblk -nrs -o NAME,TYPE /dev/mapper/luks").succeeds(
            "luks crypt\nsdc4 part\nsdc disk",
        )
        mock.on("swapon --show=NAME --noheadings --raw").succeeds("")
        with patch_sh(mock):
            reasons = protected_disks(own_pool="backup")
        assert "rpool" in reasons["/dev/sda"]
        assert "/cdrom" in reasons["/dev/sdc"]
        assert "/dev/sdb" not in reasons  # own_pool exempts the target itself
        with patch_sh(mock):
            assert "/dev/sdb" in protected_disks()


class TestRegistry:  # pylint: disable=missing-function-docstring
    """known_drives.json: strict parse, all keys, atomic write (I23/I24)."""

    def test_invalid_json_reports_position(self):
        try:
            registry_parse('{"blue": {"guid": "1" "drive_id": "x"}}', Path("/k.json"))
        except RegistryError as e:
            assert "line 1" in str(e) and "column" in str(e)
            return
        raise AssertionError("expected RegistryError")

    def test_schema_rejects_non_numeric_guid(self):
        try:
            registry_parse('{"blue": {"guid": "abc", "drive_id": "x"}}', Path("/k.json"))
        except RegistryError as e:
            assert "guid" in str(e)
            return
        raise AssertionError("expected RegistryError")

    def test_schema_rejects_non_bool_autoeject(self):
        text = '{"blue": {"guid": "1", "drive_id": "x", "autoeject": "false"}}'
        try:
            registry_parse(text, Path("/k.json"))
        except RegistryError:
            return
        raise AssertionError("expected RegistryError")

    def test_serialize_writes_every_key_and_keeps_unknown(self):
        text = registry_serialize(
            {"blue": {"guid": "7", "drive_id": "usb-X", "future": {"a": 1}}},
            Path("/k.json"),
        )
        data = json.loads(text)
        assert data["blue"] == {
            "guid": "7",
            "drive_id": "usb-X",
            "last_backup_at": None,
            "autoeject": False,
            "future": {"a": 1},
        }

    def test_shared_and_version_keys_are_validated(self):
        """M3 decision 5e: the shapes written by backup and prepare pass; any
        other shape is refused before the file is touched."""
        good = {
            "guid": "7",
            "drive_id": "usb-X",
            "prepared_at": "2026-10-13T08:00:00Z",
            "prepared_version": "2.0.0-rc3",
            "last_backup_version": "2.0.0-rc3",
            "shared": {
                "rpool/var": [["autosnap_2026-10-02_00:00:00_daily", "5", 1700000005]],
                "bpool/BOOT/be": [],
            },
        }
        data = json.loads(registry_serialize({"blue": good}, Path("/k.json")))
        assert data["blue"]["shared"] == good["shared"]
        assert data["blue"]["prepared_version"] == "2.0.0-rc3"
        bad: list[dict[str, Any]] = [
            {"prepared_version": 3},
            {"shared": []},
            {"shared": {"rpool/var": "x"}},
            {"shared": {"rpool/var": [["n", "g", 1]]}},
            {"shared": {"rpool/var": [["n", "5"]]}},
            {"shared": {"rpool/var": [["n", "5", True]]}},
        ]
        for extra in bad:
            try:
                registry_parse(
                    json.dumps({"blue": {"guid": "7", "drive_id": "x", **extra}}), Path("/k.json")
                )
            except RegistryError:
                continue
            raise AssertionError(f"accepted {extra}")

    def test_write_atomic_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "known_drives.json"
            registry_write_atomic(path, {"b": {"guid": "1", "drive_id": "d"}})
            assert sorted(os.listdir(td)) == ["known_drives.json"]
            assert json.loads(path.read_text())["b"]["guid"] == "1"

    def test_malformed_registry_is_never_overwritten(self):
        """Regression: 1.0.12 prepare rewrote a malformed file with only
        the new drive, deleting every other registered disk."""
        cfg = make_config()
        path = cfg.config_dir / "known_drives.json"
        broken = '{"black": {"guid": "1", "drive_id": "a"}\n "blue": {}}'
        path.write_text(broken)
        with patch.object(Config, "default_config_dir", return_value=cfg.config_dir):
            loaded = Config.load()
        assert loaded.load_error is not None
        loaded.known_drives["new"] = DriveInfo("new", "9", "usb-New")
        try:
            loaded.save_drives()
        except RegistryError:
            assert path.read_text() == broken
            return
        raise AssertionError("save_drives must refuse")

    def test_check_registry_fatal_for_writers(self):
        cfg = make_config()
        cfg._load_error = "boom"  # pylint: disable=protected-access
        with redirect_stdout(StringIO()), patch("builtins.input", return_value=""):
            try:
                cfg.check_registry(make_log(), fatal=True)
            except SystemExit:
                return
        raise AssertionError("expected SystemExit")

    def test_check_registry_warns_for_readers(self):
        cfg = make_config()
        cfg._load_error = "boom"  # pylint: disable=protected-access
        buf = StringIO()
        with redirect_stdout(buf):
            cfg.check_registry(make_log(), fatal=False)
        assert "boom" in buf.getvalue()


class TestImportBackupPool:  # pylint: disable=missing-function-docstring
    """I-G: every backup-pool import is -N -R <altroot> -d <exact device>."""

    DEV = f"/dev/disk/by-id/{_KINGSTON_ID}-part1"

    def test_readonly_exact_device_under_altroot(self):
        mock = MockShell()
        mock.on("zpool list backup").fails()
        mock.on("mkdir -p /run/zark/altroot/backup").succeeds()
        cmd = f"zpool import -N -R /run/zark/altroot/backup -o readonly=on -d {self.DEV} backup"
        mock.on(cmd).succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert ZFS(make_log()).import_backup_pool("backup", self.DEV, readonly=True)
        imports = [c for c in mock.calls if c.startswith("zpool import")]
        assert imports == [cmd]

    def test_force_only_after_clean_attempt_and_never_scans(self):
        mock = MockShell()
        mock.on("zpool list backup").fails()
        mock.on("mkdir -p /mnt/zark/backup").succeeds()
        mock.on(f"zpool import -N -R /mnt/zark/backup -d {self.DEV} backup").fails("in use")
        mock.on(f"zpool import -N -R /mnt/zark/backup -f -d {self.DEV} backup").succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert ZFS(make_log()).import_backup_pool(
                "backup",
                self.DEV,
                altroot="/mnt/zark/backup",
            )
        imports = [c for c in mock.calls if c.startswith("zpool import")]
        assert len(imports) == 2
        assert all(f"-d {self.DEV} backup" in c and "-N -R" in c for c in imports)

    def test_refuses_without_device(self):
        mock = MockShell()
        mock.on("zpool list backup").fails()
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert not ZFS(make_log()).import_backup_pool("backup", None)
        assert mock.was_not_called("zpool import")

    def test_refuses_pool_already_imported_without_altroot(self):
        mock = MockShell()
        mock.on("zpool list backup").succeeds("backup")
        mock.on("zpool get -H -o value altroot backup").succeeds("-")
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert not ZFS(make_log()).import_backup_pool("backup", self.DEV)

    def test_accepts_pool_already_imported_under_altroot(self):
        mock = MockShell()
        mock.on("zpool list backup").succeeds("backup")
        mock.on("zpool get -H -o value altroot backup").succeeds("/mnt/zark/backup")
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert ZFS(make_log()).import_backup_pool("backup", self.DEV)


class TestKeystoreReadonlyAndRetry:  # pylint: disable=missing-function-docstring
    """Read-only keystore open (readonly pools) and passphrase re-prompt (I9)."""

    def test_readonly_mount_uses_readonly_luks_and_noload(self):
        mock = MockShell()
        mock.on("ls -1 /dev/zd*").succeeds("/dev/zd0")
        mock.on("zfs list -H -o objsetid backup/keystore").succeeds("12")
        mock.on("zpool list rpool").fails()
        mock.on("cryptsetup open --readonly /dev/zd0 zark_ks_backup").succeeds()
        mock.on("mount -o ro,noload /dev/mapper/zark_ks_backup /run/keystore/rpool").succeeds()
        ks = Keystore(make_log())
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch("lib.keystore.Path.mkdir"),
            patch("lib.keystore.Path.exists", return_value=True),
        ):
            assert ks.mount("backup", "pw", readonly=True)
        assert mock.was_called("cryptsetup open --readonly /dev/zd0")
        assert mock.was_called("mount -o ro,noload /dev/mapper/zark_ks_backup")

    def test_open_keystore_reprompts_on_wrong_passphrase(self):
        class _Ks:  # pylint: disable=too-few-public-methods
            bad_passphrase = False

            def __init__(self) -> None:
                self.calls: list[str] = []

            def mount(self, pool: str, passphrase: str, *, readonly: bool = False) -> bool:
                del pool, readonly
                self.calls.append(passphrase)
                self.bad_passphrase = passphrase != "right"
                return passphrase == "right"

            def load_pool_keys(self, pool_root: str) -> int:
                del pool_root
                return 0

            def umount(self) -> None:
                return None

        ks = _Ks()
        with (
            patch("lib.log.getpass.getpass", side_effect=["typo", "typo2", "right"]),
            redirect_stdout(StringIO()),
        ):
            assert open_keystore(ks, "backup", make_log())
        assert ks.calls == ["typo", "typo2", "right"]

    def test_open_keystore_gives_up_after_three(self):
        mock_ks = _FakeKeystore()
        mock_ks.bad_passphrase = True
        with (
            patch.object(_FakeKeystore, "mount", return_value=False),
            patch("lib.log.getpass.getpass", return_value="x") as gp,
            redirect_stdout(StringIO()),
        ):
            assert not open_keystore(mock_ks, "backup", make_log())
        assert gp.call_count == 3


def _purge_ident(label: PoolLabel | None) -> DiskIdentity:
    return DiskIdentity(
        disk="/dev/sdb",
        by_id=_KINGSTON_ID,
        model="DT microDuo 3C",
        size="115.5G",
        transport="usb",
        label=label,
    )


class TestPurgeIdentity:  # pylint: disable=missing-function-docstring
    """I21: purge recognises the registered drive and removes its entries."""

    def _run(self, cfg_dir: Path, ident: DiskIdentity, mock: MockShell) -> None:
        with (
            patch_sh(mock),
            patch.object(Config, "default_config_dir", return_value=cfg_dir),
            patch.object(purge_mod, "validate_external_block_device", return_value=ident),
            patch("builtins.input", side_effect=["yes", "sdb", "n"]),
            patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
            redirect_stdout(StringIO()),
        ):
            purge_mod.run([f"/dev/disk/by-id/{_KINGSTON_ID}"])

    def test_by_id_purge_destroys_pool_and_cleans_every_matching_entry(self):
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(
            cfg_dir / "known_drives.json",
            {
                "backup": {"guid": "3446866051930726346", "drive_id": "<unknown>"},
                "old": {"guid": "1", "drive_id": _KINGSTON_ID},
                "black": {"guid": "2", "drive_id": "usb-Micron-0:0"},
            },
        )
        mock = MockShell()
        mock.on("zpool list backup").fails()
        vdev = f"/dev/disk/by-id/{_KINGSTON_ID}-part1"
        mock.on(f"zpool import -N -R /run/zark/altroot/backup -d {vdev} backup").succeeds()
        mock.on("zpool destroy backup").succeeds()
        ident = _purge_ident(PoolLabel("backup", "3446866051930726346"))
        self._run(cfg_dir, ident, mock)
        assert mock.was_called(f"zpool import -N -R /run/zark/altroot/backup -d {vdev} backup")
        assert mock.was_called("zpool destroy backup")
        assert mock.was_called("sgdisk --zap-all /dev/sdb")
        data = json.loads((cfg_dir / "known_drives.json").read_text())
        assert list(data) == ["black"]

    def _purge_on_fake(self, fake: FakeZfs, cfg_dir: Path, guid: str) -> str:
        ident = _purge_ident(PoolLabel("black", guid))
        out = StringIO()
        with (
            patch_sh(fake),
            patch.object(Config, "default_config_dir", return_value=cfg_dir),
            patch.object(purge_mod, "validate_external_block_device", return_value=ident),
            patch.object(ZFS, "import_backup_pool", return_value=True),
            patch("builtins.input", side_effect=["yes", "sdb", "n"]),
            patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
            redirect_stdout(out),
        ):
            purge_mod.run([f"/dev/disk/by-id/{_KINGSTON_ID}"])
        return out.getvalue()

    def test_purge_drops_the_wiped_pools_anchors(self):
        """M2R-6 (E12a): the purged drive's anchors leave origin with it."""
        fake = _fresh_fake(("black", "blue"))
        assert _engine_run(fake, "black", "1111", P1).ok
        assert _engine_run(fake, "blue", "2222", P2).ok
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(
            cfg_dir / "known_drives.json",
            {
                "black": {"guid": "1111", "drive_id": _KINGSTON_ID},
                "blue": {"guid": "2222", "drive_id": "usb-Micron-0:0"},
            },
        )
        _ = self._purge_on_fake(fake, cfg_dir, "1111")
        origin = {ds: d for ds, d in fake.ds.items() if ds.split("/")[0] in ("rpool", "bpool")}
        names = [b.name for d in origin.values() for b in [*d.bookmarks, *d.snaps]]
        assert not [n for n in names if repl.anchor_disk(n) == "1111"]
        assert [n for n in names if repl.anchor_disk(n) == "2222"]

    def test_purge_outside_the_installed_system_says_how(self):
        """M2R-6 (M2F-4): with rpool not imported the banner gives the command."""
        fake = _fresh_fake(("black",))
        fake.pools.discard("rpool")
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        with patch("lib.log.Log.ask", return_value=True):
            out = self._purge_on_fake(fake, cfg_dir, "1111")
        assert "sudo zark registry forget 1111" in out

    def test_foreign_rpool_label_is_never_imported(self):
        cfg_dir = Path(tempfile.mkdtemp())
        mock = MockShell()
        ident = _purge_ident(PoolLabel("rpool", "99"))
        with patch("lib.log.Log.ask", return_value=True):
            self._run(cfg_dir, ident, mock)
        assert mock.was_not_called("zpool import")
        assert mock.was_not_called("zpool destroy")
        assert mock.was_called("wipefs -a /dev/sdb")


class TestPrepareIdentity:  # pylint: disable=missing-function-docstring
    """I16/I22/I23 + I-G in prepare."""

    def _prepare(self, fake: FakeZfs, cfg_dir: Path, **answers: str) -> int:
        """Run prepare; ``answers`` by the first word of an input question
        (Pool, Days); anything not given takes the default."""
        answers = {"Pool": "blue", **answers}
        ident = _purge_ident(None)
        report = type("R", (), {"has_risk": False, "findings": [], "device": "/dev/sdb"})()
        readback = Readback(imported=True, health="ONLINE", reexported=True)
        rc = 0
        with ExitStack() as stack:
            for cm in (
                patch_sh(fake),
                patch.object(Config, "default_config_dir", return_value=cfg_dir),
                patch.object(prepare_mod, "validate_external_block_device", return_value=ident),
                patch.object(prepare_mod, "check_device", return_value=report),
                patch.object(prepare_mod.Path, "exists", return_value=True),
                patch.object(ZFS, "verify_exported_pool_readback", return_value=readback),
                patch.object(ZFS, "pool_guid", return_value="777"),
                patch("lib.log.Log.ask", return_value=True),
                patch(
                    "lib.log.Log.ask_input",
                    side_effect=lambda q, d="", **_k: answers.get(q.split()[0], d),
                ),
                patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
                patch("builtins.input", return_value=""),
                patch.object(prepare_mod, "prompt_eject_or_attach"),
                patch.object(Cleanup, "register", lambda _self: None),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                stack.enter_context(cm)
            try:
                prepare_mod.run([f"/dev/disk/by-id/{_KINGSTON_ID}"])
            except SystemExit as e:
                rc = int(e.code or 0)
        return rc

    def test_prepare_by_id_replaces_stale_entry_and_uses_by_id_vdev(self):
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(
            cfg_dir / "known_drives.json",
            {
                "blue": {"guid": "1", "drive_id": _KINGSTON_ID},
                "black": {"guid": "2", "drive_id": "usb-Micron-0:0"},
            },
        )
        fake = _fresh_fake(())
        assert self._prepare(fake, cfg_dir) == 0
        create = next(c for c in fake.calls if c.startswith("zpool create"))
        assert "-R /run/zark/altroot/blue" in create
        assert create.endswith(f"blue /dev/disk/by-id/{_KINGSTON_ID}")
        data = json.loads((cfg_dir / "known_drives.json").read_text())
        assert data["blue"]["guid"] == "777"
        assert data["blue"]["drive_id"] == _KINGSTON_ID
        assert data["blue"]["autoeject"] is True
        assert data["blue"]["last_backup_at"]  # I13
        assert data["black"]["guid"] == "2"

    def test_prepare_on_the_engine(self):
        """Decision 2, I14/F9, R2§8.5, M1P.14: one point, no syncoid, no
        residues in origin, no native mount properties on the drive."""
        fake = _fresh_fake(())
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        assert self._prepare(fake, cfg_dir) == 0
        assert not any(c.startswith("syncoid") for c in fake.calls)
        sets = [c for c in fake.calls if c.startswith("zfs set")]
        assert not any("mountpoint=" in c or " canmount=" in c for c in sets)
        home = fake.names("blue/rpool/USERDATA/home")
        assert len(home) == 1 and repl.POINT_RE.match(home[0])  # the point only
        assert fake.ds["blue/rpool/ROOT/be"].props["canmount"] == "noauto"
        assert fake.ds["blue/rpool/var"].props["org.zark:canmount"] == "off"
        assert "blue/keystore" in fake.ds
        assert _origin_points(fake) == []
        assert not any(n.startswith("prepare_") for n in fake.names("rpool/keystore"))  # I-B
        assert fake.bookmarks("rpool/ROOT/be") == [repl.anchor_name("777", home[0])]
        assert fake.ds["blue"].props["org.zark:last-point"] == home[0]
        assert _marked(fake, "blue") == {home[0]}

    def test_prepare_records_versions_and_that_it_shares_nothing(self):
        """M3 decision 5e: prepare's full send carries only the point, so the
        drive shares nothing with origin yet; the versions are recorded in
        the registry and the preparing one on the drive."""
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        fake = _fresh_fake(())
        _tick(fake, "2026-10-07_09:00:00_hourly")
        assert self._prepare(fake, cfg_dir) == 0
        entry = json.loads((cfg_dir / "known_drives.json").read_text())["blue"]
        assert entry["shared"] and all(v == [] for v in entry["shared"].values())
        assert entry["prepared_version"] == VERSION == entry["last_backup_version"]
        assert entry["prepared_at"] == entry["last_backup_at"]
        assert fake.ds["blue"].props["org.zark:prepared-version"] == VERSION

    def test_prepare_asks_the_drives_retention(self):
        """M3 decision 2b: prepare asks the window and the maximum age with
        its other questions; Enter keeps the defaults, which are not written."""
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        assert self._prepare(_fresh_fake(()), cfg_dir) == 0
        assert "retention" not in json.loads((cfg_dir / "known_drives.json").read_text())["blue"]
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        asked: list[str] = []
        replies = iter(["60", "400"])
        real = prepare_mod._ask_retention  # pylint: disable=protected-access

        def reply(question: str, _default: str = "", **_k: object) -> str:
            asked.append(question)
            return next(replies)

        def ask(log: Log) -> dict[str, int]:
            with patch.object(Log, "ask_input", side_effect=reply):
                return real(log)

        with patch.object(prepare_mod, "_ask_retention", side_effect=ask):
            assert self._prepare(_fresh_fake(()), cfg_dir) == 0
        data = json.loads((cfg_dir / "known_drives.json").read_text())
        assert data["blue"]["retention"] == {"window_days": 60, "max_age_days": 400}
        assert len(asked) == 2

    def test_a_retention_answer_that_is_not_a_number_is_asked_again(self):
        """M3R-13 (X7): the questions are repeated until the values pass the
        registry's validation, so no refused entry reaches save_drives."""
        replies = iter(["abc", "off", "30", "400"])
        out = StringIO()
        with (
            patch.object(Log, "ask_input", side_effect=lambda *_a, **_k: next(replies)),
            redirect_stdout(out),
        ):
            values = prepare_mod._ask_retention(Log())  # pylint: disable=protected-access
        assert values == {"max_age_days": 400}
        assert "asked again" in out.getvalue()

    def test_unregistered_prepare_leaves_no_anchors(self):
        """M2R-6 (E12b): a prepare that ends DRIVE NOT REGISTERED drops the
        anchors its datasets got."""
        fake = _fresh_fake(())
        fake.inject["blue/rpool/USERDATA/home"] = ("interrupt", 0)
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        assert self._prepare(fake, cfg_dir) == 1
        names = [
            n
            for ds, d in fake.ds.items()
            if ds.split("/")[0] in ("rpool", "bpool")
            for n in [b.name for b in d.bookmarks] + fake.names(ds)
        ]
        assert not [n for n in names if repl.anchor_disk(n) == "777"]

    def test_prepare_drops_the_replaced_entrys_anchors(self):
        """M2R-6: the stale entry it replaces was this same disk."""
        fake = _fresh_fake(("old",))
        assert _engine_run(fake, "old", "1", P1).ok
        _ = [fake.ds.pop(n) for n in [n for n in fake.ds if n == "old" or n.startswith("old/")]]
        fake.pools.discard("old")
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(
            cfg_dir / "known_drives.json", {"blue": {"guid": "1", "drive_id": _KINGSTON_ID}}
        )
        assert self._prepare(fake, cfg_dir) == 0
        assert repl.anchor_name("1", P1) not in fake.bookmarks("rpool/ROOT/be")
        assert fake.bookmarks("rpool/ROOT/be") and all(
            repl.anchor_disk(b) == "777" for b in fake.bookmarks("rpool/ROOT/be")
        )

    def test_prepare_refuses_while_bpool_is_not_imported(self):
        """M2R-4 (E4b): a new drive would be prepared without bpool."""
        fake = _fresh_fake(())
        for n in [n for n in fake.ds if n == "bpool" or n.startswith("bpool/")]:
            _ = fake.ds.pop(n)
        fake.pools.discard("bpool")
        fake.importable.add("bpool")
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        assert self._prepare(fake, cfg_dir) == 1
        assert not any(c.startswith("zpool create") for c in fake.calls)

    def test_prepare_records_the_machine_id(self):
        """The drive-owner check reads what prepare wrote (2026-10-08)."""
        fake = _fresh_fake(())
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        mid = cfg_dir / "machine-id"
        mid.write_text("5ca1ab1e0000000000000000000000ff\n", encoding="ascii")
        with patch.object(engine, "MACHINE_ID_PATH", str(mid)):
            assert self._prepare(fake, cfg_dir) == 0
        props = fake.ds["blue"].props
        assert props["org.zark:origin-machine-id"] == "5ca1ab1e0000000000000000000000ff"

    def test_prepare_does_not_register_an_incomplete_drive(self):
        """H16: a dataset that did not land means no registration and exit 1."""
        fake = _fresh_fake(())
        fake.inject["blue/rpool/USERDATA/home"] = ("enospc", 0)
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        assert self._prepare(fake, cfg_dir) == 1
        assert "blue" not in json.loads((cfg_dir / "known_drives.json").read_text())

    def test_prepare_refuses_disk_without_by_id(self):
        ident = DiskIdentity(disk="/dev/sdb", by_id="")
        with (
            patch.object(prepare_mod, "validate_external_block_device", return_value=ident),
            patch.object(prepare_mod.Path, "exists", return_value=True),
            patch("builtins.input", return_value=""),
            redirect_stdout(StringIO()),
        ):
            try:
                prepare_mod.run(["/dev/sdb"])
            except SystemExit:
                return
        raise AssertionError("prepare must refuse to register <unknown>")


class TestRegistryCommand:  # pylint: disable=missing-function-docstring
    """I24: supported way to inspect and repair the registry."""

    @staticmethod
    def _cfg_dir(entries: dict[str, dict[str, str]]) -> Path:
        d = Path(tempfile.mkdtemp())
        registry_write_atomic(d / "known_drives.json", entries)
        return d

    def test_fix_rewrites_unknown_drive_id_from_connected_disk(self):
        d = self._cfg_dir({"blue": {"guid": "777", "drive_id": "<unknown>"}})
        mock = MockShell()
        mock.on("blkid -t TYPE=zfs_member -o export").succeeds(
            "DEVNAME=/dev/sdb1\nLABEL=blue\nUUID=777\nTYPE=zfs_member\n",
        )
        mock.on("zpool list -H -o name").succeeds("")
        mock.on("lsblk -dn -P -o NAME,TYPE,PKNAME,MODEL,SERIAL,SIZE,TRAN /dev/sdb1").succeeds(
            _LSBLK_SDB1,
        )
        mock.on("lsblk -dn -P -o NAME,TYPE,PKNAME,MODEL,SERIAL,SIZE,TRAN /dev/sdb").succeeds(
            _LSBLK_SDB,
        )
        mock.on_prefix("find /dev/disk/by-id/").succeeds(_BY_ID_LISTING)
        with (
            patch_sh(mock),
            patch.object(Config, "default_config_dir", return_value=d),
            patch("lib.identity.os.path.realpath", side_effect=lambda p: p),
            redirect_stdout(StringIO()),
        ):
            registry_mod.run(["fix"])
        data = json.loads((d / "known_drives.json").read_text())
        assert data["blue"]["drive_id"] == _KINGSTON_ID
        assert data["blue"]["autoeject"] is False

    def test_retention_settings_are_validated_written_and_read(self):
        """M3 decision 2b: per-drive retention in the registry; the rest of the
        entry is untouched, and a bad value is refused without writing."""
        d = self._cfg_dir({"blue": {"guid": "1", "drive_id": "a"}})
        before = (d / "known_drives.json").read_text()
        with (
            patch_sh(MockShell()),
            patch.object(Config, "default_config_dir", return_value=d),
            patch("builtins.input", side_effect=EOFError),
            redirect_stdout(StringIO()),
        ):
            try:
                registry_mod.run(["retention", "blue", "window=60", "max_age=30"])
            except SystemExit:
                pass
            assert (d / "known_drives.json").read_text() == before
            registry_mod.run(["retention", "blue", "window=60", "max_age=400", "margin=15"])
            assert Config.load().drive_policy("blue") == ret.Policy(60, 400, 15)
            registry_mod.run(["retention", "blue", "max_age=off"])
            assert Config.load().drive_policy("blue") == ret.Policy(60, 0, 15)
            assert Config.load().drive_policy("unknown") == ret.Policy()
        data = json.loads((d / "known_drives.json").read_text())
        assert data["blue"]["retention"] == {
            "window_days": 60,
            "max_age_days": 0,
            "margin_pct": 15,
        }
        assert data["blue"]["guid"] == "1" and data["blue"]["drive_id"] == "a"

    def test_retention_schema(self):
        """Unknown keys, non-integers and out-of-range values are refused."""
        for bad in (
            '{"window_days": "30"}',
            '{"window_days": 0}',
            '{"margin_pct": 60}',
            '{"weekly": 4}',
            '{"window_days": true}',
            "[]",
        ):
            text = '{"blue": {"guid": "1", "drive_id": "x", "retention": ' + bad + "}}"
            try:
                registry_parse(text, Path("/k.json"))
            except RegistryError:
                continue
            raise AssertionError(f"accepted {bad}")

    def test_forget_removes_only_that_entry(self):
        d = self._cfg_dir(
            {"blue": {"guid": "1", "drive_id": "a"}, "black": {"guid": "2", "drive_id": "b"}},
        )
        with (
            patch_sh(MockShell()),  # no rpool here: anchors are left alone
            patch.object(Config, "default_config_dir", return_value=d),
            patch("lib.log.Log.ask", return_value=True),
            redirect_stdout(StringIO()),
        ):
            registry_mod.run(["forget", "blue"])
        assert list(json.loads((d / "known_drives.json").read_text())) == ["black"]

    def test_forget_drops_that_disks_anchors(self):
        """D16 / M1 decision 4: forget also removes the disk's anchors, by GUID."""
        fake = _fresh_fake(("black", "blue"))
        assert _engine_run(fake, "black", "1", P1).ok
        assert _engine_run(fake, "blue", "2", P2).ok
        d = self._cfg_dir(
            {"black": {"guid": "1", "drive_id": "a"}, "blue": {"guid": "2", "drive_id": "b"}},
        )
        with (
            patch_sh(fake),
            patch.object(Config, "default_config_dir", return_value=d),
            patch("lib.log.Log.ask", return_value=True),
            redirect_stdout(StringIO()),
        ):
            registry_mod.run(["forget", "black"])
        assert fake.bookmarks("rpool/USERDATA/home") == [repl.anchor_name("2", P2)]
        assert not any(repl.anchor_disk(n) == "1" for n in fake.names("bpool/BOOT/be"))
        assert any(c.startswith("zfs destroy 'rpool/") and "#zark_1_" in c for c in fake.calls)

    def _registry(self, fake: FakeZfs, d: Path, args: list[str], answer: bool = True) -> str:
        out = StringIO()
        with (
            patch_sh(fake),
            patch.object(Config, "default_config_dir", return_value=d),
            patch("lib.log.Log.ask", return_value=answer),
            patch("builtins.input", return_value=""),
            redirect_stdout(out),
        ):
            registry_mod.run(args)
        return out.getvalue()

    def test_list_shows_anchors_without_an_entry_and_forget_takes_a_guid(self):
        """M2R-6 (M2F-3): a drive that left the registry another way is
        visible, and `forget <GUID>` drops its anchors."""
        fake = _fresh_fake(("black", "blue"))
        assert _engine_run(fake, "black", "1", P1).ok
        assert _engine_run(fake, "blue", "2", P2).ok
        d = self._cfg_dir({"blue": {"guid": "2", "drive_id": "b"}})
        out = self._registry(fake, d, ["list"])
        assert "drive GUID 1:" in out and "sudo zark registry forget 1" in out
        assert "drive GUID 2:" not in out
        _ = self._registry(fake, d, ["forget", "1"])
        assert fake.bookmarks("rpool/ROOT/be") == [repl.anchor_name("2", P2)]
        assert list(json.loads((d / "known_drives.json").read_text())) == ["blue"]

    def test_forget_by_the_guid_of_a_registered_drive(self):
        """A registered drive's GUID forgets its entry like its name."""
        fake = _fresh_fake(("black",))
        assert _engine_run(fake, "black", "1", P1).ok
        d = self._cfg_dir({"black": {"guid": "1", "drive_id": "a"}})
        _ = self._registry(fake, d, ["forget", "1"])
        assert not json.loads((d / "known_drives.json").read_text())
        assert not fake.bookmarks("rpool/ROOT/be")

    def test_forget_reports_anchors_it_could_not_drop(self):
        """Anchors first, entry last; a failure names the command to retry."""
        fake = _fresh_fake(("black",))
        assert _engine_run(fake, "black", "1", P1).ok
        fake.fail_cmds.add("rpool/ROOT/be#zark_1_")
        d = self._cfg_dir({"black": {"guid": "1", "drive_id": "a"}})
        destroys_before_save: list[int] = []
        save = Config.save_drives

        def recording_save(cfg: Config) -> None:
            destroys_before_save.append(sum(c.startswith("zfs destroy") for c in fake.calls))
            save(cfg)

        with patch.object(Config, "save_drives", recording_save):
            out = self._registry(fake, d, ["forget", "black"])
        assert "could not remove" in out and "sudo zark registry forget 1" in out
        assert not json.loads((d / "known_drives.json").read_text())
        assert destroys_before_save and destroys_before_save[0] > 0

    def test_forget_offers_leftover_points_when_no_drive_remains(self):
        """M2F-5: points of an interrupted run are offered for removal once the
        registry is empty, and only then."""
        fake = _fresh_fake(("black", "blue"))
        assert _engine_run(fake, "black", "1", P1).ok
        assert _engine_run(fake, "blue", "2", P1.replace("07", "06")).ok
        fake.inject["black/rpool/USERDATA/home"] = ("interrupt", 0)
        assert not _engine_run(fake, "black", "1", P2).ok
        assert _origin_points(fake)
        d = self._cfg_dir(
            {"black": {"guid": "1", "drive_id": "a"}, "blue": {"guid": "2", "drive_id": "b"}},
        )
        out = self._registry(fake, d, ["forget", "black"])
        assert "next complete backup or prepare" in out and _origin_points(fake)
        _ = self._registry(fake, d, ["forget", "blue"])
        assert _origin_points(fake) == []

    def test_forget_refuses_on_malformed_registry(self):
        d = Path(tempfile.mkdtemp())
        (d / "known_drives.json").write_text("{bad")
        with (
            patch.object(Config, "default_config_dir", return_value=d),
            patch("builtins.input", return_value=""),
            redirect_stdout(StringIO()),
        ):
            try:
                registry_mod.run(["forget", "blue"])
            except SystemExit:
                assert (d / "known_drives.json").read_text() == "{bad"
                return
        raise AssertionError("expected SystemExit")


# ═════════════════════════════════════════════════════════════════════════
#  M1 recover: restore points, resolution, mount properties, sizes (fixture)
# ═════════════════════════════════════════════════════════════════════════

_MANIFEST = Path(__file__).parent / "fixtures" / "phase0-stick-manifest.txt"
_BE = "ubuntu_g8v4da"


def _manifest_snapshot_lines() -> list[str]:
    """Snapshot lines in `zfs list -Hp -o name,guid,createtxg,creation` form."""
    return [ln for ln in _MANIFEST.read_text().splitlines() if "@" in ln.split("\t")[0]]


def _manifest_dataset_lines() -> list[tuple[str, str, str, str, str]]:
    """(name, used, referenced, mountpoint, canmount) of the frozen stick."""
    out = []
    for ln in _MANIFEST.read_text().splitlines():
        f = ln.split("\t")
        if "@" not in f[0] and len(f) == 5:
            out.append((f[0], f[1], f[2], f[3], f[4]))
    return out


def _stick_snaps() -> list[Snap]:
    return [
        s for s in parse_snapshots(_manifest_snapshot_lines(), "backup") if s.dataset != "keystore"
    ]


def _utc(p) -> str:
    return datetime.fromtimestamp(p.label, UTC).strftime("%H:%M:%S")


# Origin (eli) mount properties, as its zfs-list.cache would record them.
_ELI_CACHE_RPOOL = "\n".join(
    [
        "rpool\tnone\toff",
        "rpool/ROOT\tnone\toff",
        f"rpool/ROOT/{_BE}\t/\ton",
        f"rpool/ROOT/{_BE}/usr\t/usr\toff",
        f"rpool/ROOT/{_BE}/var\t/var\toff",
        f"rpool/ROOT/{_BE}/var/lib\t/var/lib\ton",
        "rpool/USERDATA\tnone\toff",
        "rpool/USERDATA/home_cgx8je\t/home\ton",
        "rpool/USERDATA/root_cgx8je\t/root\ton",
        "rpool/var\t/var\toff",
        "rpool/var/lib\t/var/lib\toff",
        "rpool/var/lib/docker\t/var/lib/docker\ton",
    ],
)
_ELI_CACHE_BPOOL = f"bpool\tnone\toff\nbpool/BOOT\tnone\toff\nbpool/BOOT/{_BE}\t/boot\ton"


class TestRestorePoints:  # pylint: disable=missing-function-docstring
    """P0-8: points by creation, grouped per run, from the frozen stick."""

    @staticmethod
    def _v2_lines() -> list[str]:
        """A v2 backup: two points, a carried hourly, a dataset only in the
        second point, and the BE's first lineage archived."""
        p1, p2 = "zark_2026-10-01_10:00:00Z", "zark_2026-10-08_10:00:00Z"
        t1, t2, th = 1_759_312_800, 1_759_917_600, 1_759_900_000
        be = f"backup/rpool/ROOT/{_BE}"
        return [
            f"{be}.archived-20261008@{p1}\t11\t10\t{t1}",
            f"backup/rpool@{p1}\t12\t11\t{t1}",
            f"backup/rpool@autosnap_2026-10-08_05:33:20_hourly\t13\t12\t{th}",
            f"backup/rpool@{p2}\t14\t13\t{t2}",
            f"{be}@{p2}\t15\t14\t{t2}",
            f"backup/rpool/new@{p2}\t16\t15\t{t2}",
        ]

    def test_archived_lineage_serves_older_points(self):
        snaps = parse_snapshots(self._v2_lines(), "backup")
        be = f"rpool/ROOT/{_BE}"
        assert logical(f"{be}.archived-20261008/var") == f"{be}/var"
        old = [x for x in snaps if x.name.endswith("10-01_10:00:00Z") and x.dataset == be]
        assert old and old[0].stored == f"{be}.archived-20261008"
        points = restore_points(snaps, be)
        assert [p.family for p in points] == ["zark_", "zark_"]
        first = resolve(points[0], snaps, [be, "rpool"])
        got = first[be]
        assert got is not None and got.source == f"{be}.archived-20261008"

    def test_backup_points_never_resurrect_a_destroyed_dataset(self):
        # Decision 18: a dataset whose lineage ends before a point (destroyed
        # in origin) is not restored for it; one created after it has nothing.
        p1, p2 = "zark_2026-10-01_10:00:00Z", "zark_2026-10-08_10:00:00Z"
        lines = [
            *self._v2_lines(),
            f"backup/rpool/gone@autosnap_2026-09-30_10:00:00_daily\t21\t9\t{1_759_226_400}",
        ]
        snaps = parse_snapshots(lines, "backup")
        points = restore_points(snaps, f"rpool/ROOT/{_BE}")
        assert [p.members[f"rpool/ROOT/{_BE}"][0].name for p in points] == [p1, p2]
        second = resolve(points[1], snaps, ["rpool/gone", "rpool/new"])
        assert second["rpool/gone"] is None
        first = resolve(points[0], snaps, ["rpool/new"])
        assert first["rpool/new"] is None

    def test_a_dataset_missed_by_a_run_falls_back(self):
        # Decision 14: a run that did not reach a dataset leaves the point
        # without it; the dataset still has later snapshots, so the point
        # restores it from its newest earlier one instead of leaving it out.
        p1, p2 = "zark_2026-10-01_10:00:00Z", "zark_2026-10-08_10:00:00Z"
        t0, t1, t2 = 1_759_226_400, 1_759_312_800, 1_759_917_600
        usr = f"backup/rpool/ROOT/{_BE}/usr"
        lines = [
            *self._v2_lines(),
            f"{usr}@autosnap_2026-09-30_10:00:00_daily\t31\t9\t{t0}",
            f"{usr}@{p2}\t32\t14\t{t2}",
        ]
        snaps = parse_snapshots(lines, "backup")
        points = restore_points(snaps, f"rpool/ROOT/{_BE}")
        assert points[0].members[f"rpool/ROOT/{_BE}"][0].name == p1
        first = resolve(points[0], snaps, [f"rpool/ROOT/{_BE}/usr"])
        got = first[f"rpool/ROOT/{_BE}/usr"]
        assert got is not None and got.name.startswith("autosnap_") and got.creation < t1

    def test_later_backup_points_are_not_evidence(self):
        # A dataset whose last snapshot is the first backup point and that a
        # later backup point lacks (destroyed afterwards) still belongs to a
        # carried run between the two: only backup points at or before the
        # chosen point say a lineage ended.
        p1, p2 = "zark_2026-10-01_10:00:00Z", "zark_2026-10-08_10:00:00Z"
        t1, th, t2 = 1_759_312_800, 1_759_600_000, 1_759_917_600
        be = f"backup/rpool/ROOT/{_BE}"
        lines = [
            f"{be}@{p1}\t1\t1\t{t1}",
            f"backup/rpool/old@{p1}\t2\t2\t{t1}",
            f"{be}@autosnap_2026-10-04_10:00:00_daily\t3\t3\t{th}",
            f"{be}@{p2}\t4\t4\t{t2}",
        ]
        snaps = parse_snapshots(lines, "backup")
        points = restore_points(snaps, f"rpool/ROOT/{_BE}")
        carried = next(p for p in points if p.family == "autosnap_")
        got = resolve(carried, snaps, ["rpool/old"])["rpool/old"]
        assert got is not None and got.name == p1
        assert resolve(points[-1], snaps, ["rpool/old"])["rpool/old"] is None

    def test_choice_labels_backup_points_and_carried_runs(self):
        snaps = parse_snapshots(self._v2_lines(), "backup")
        snaps.append(
            Snap(f"rpool/ROOT/{_BE}", "autosnap_2026-10-08_05:33:20_hourly", "17", 9, 1_759_900_000)
        )
        points = restore_points(snaps, f"rpool/ROOT/{_BE}")
        out = StringIO()
        with patch("builtins.input", return_value=""), redirect_stdout(out):
            assert _choose_point(points, make_log()) is points[-1]
        text = out.getvalue()
        assert "backup point" in text and "carried: hourly" in text

    def test_four_points_ordered_by_creation(self):
        points = restore_points(_stick_snaps(), f"rpool/ROOT/{_BE}")
        assert [_utc(p) for p in points] == ["15:52:42", "16:00:01", "16:12:50", "17:00:01"]
        assert [p.family for p in points] == ["autosnap_", "autosnap_", "syncoid_eli_", "autosnap_"]

    def test_default_is_the_newest(self):
        points = restore_points(_stick_snaps(), f"rpool/ROOT/{_BE}")
        with patch("builtins.input", return_value=""), redirect_stdout(StringIO()):
            assert _choose_point(points, make_log()) is points[-1]

    def test_invalid_selection_asks_again(self):
        points = restore_points(_stick_snaps(), f"rpool/ROOT/{_BE}")
        with (
            patch("builtins.input", side_effect=["x", "9", "2"]),
            redirect_stdout(StringIO()),
        ):
            assert _choose_point(points, make_log()) is points[1]

    def test_resolution_of_the_1700_point(self):
        snaps = _stick_snaps()
        points = restore_points(snaps, f"rpool/ROOT/{_BE}")
        datasets = sorted({s.dataset for s in snaps})
        got = {ds: s.name if s else None for ds, s in resolve(points[-1], snaps, datasets).items()}
        minimal = {
            "rpool": "syncoid_eli_2026-09-28:18:12:48-GMT02:00",
            "rpool/ROOT": "syncoid_eli_2026-09-28:18:12:49-GMT02:00",
            "rpool/var": "syncoid_eli_2026-09-28:18:23:31-GMT02:00",
            "rpool/var/lib": "syncoid_eli_2026-09-28:18:23:33-GMT02:00",
            "rpool/var/lib/docker": "syncoid_eli_2026-09-28:18:23:34-GMT02:00",
            "bpool": "syncoid_eli_2026-09-28:18:23:42-GMT02:00",
        }
        for ds, name in got.items():
            assert name == minimal.get(ds, "autosnap_2026-09-28_17:00:01_hourly"), (ds, name)

    def test_never_resolves_forward(self):
        snaps = _stick_snaps()
        points = restore_points(snaps, f"rpool/ROOT/{_BE}")
        p1600 = points[1]
        for ds, snap in resolve(p1600, snaps, sorted({s.dataset for s in snaps})).items():
            assert snap is not None and snap.creation <= p1600.end, ds
        got = resolve(p1600, snaps, ["rpool/var", "rpool/ROOT/" + _BE])
        var, root = got["rpool/var"], got["rpool/ROOT/" + _BE]
        assert var is not None and var.name.startswith("autosnap")
        assert root is not None and root.name == "autosnap_2026-09-28_16:00:01_hourly"

    def test_createtxg_across_datasets_is_not_time(self):
        """On a destination createtxg is receive order: bpool/BOOT@16:00:01
        has txg 452 but the syncoid run (txg 59) is 12 minutes newer."""
        snaps = {(s.dataset, s.name): s for s in _stick_snaps()}
        a = snaps[("bpool/BOOT", "autosnap_2026-09-28_16:00:01_hourly")]
        b = snaps[(f"rpool/ROOT/{_BE}", "syncoid_eli_2026-09-28:18:12:50-GMT02:00")]
        assert a.createtxg > b.createtxg and a.creation < b.creation

    @staticmethod
    def _eli_0910() -> list[Snap]:
        """eli on 2026-10-09 (UTC): a point, an interrupted run X1 that only
        reached rpool/var, P1 and P2 77 s apart; zt5b destroyed in origin
        after the 05:35:03 point; a sanoid hourly at 06:00:01."""
        base = 1_759_986_000  # 2026-10-09 05:00:00 UTC
        be, var, zt5b = f"rpool/ROOT/{_BE}", "rpool/var", "rpool/ROOT/zt5b"

        def at(hms: str) -> int:
            h, m, s = (int(x) for x in hms.split(":"))
            return base + (h - 5) * 3600 + m * 60 + s

        rows = [
            (be, "zark_2026-10-09_05:35:03Z", "05:35:03"),
            (var, "zark_2026-10-09_05:35:03Z", "05:35:03"),
            (zt5b, "zark_2026-10-09_05:35:03Z", "05:35:03"),
            (be, "zark_2026-10-09_05:36:29Z", "05:36:29"),
            (var, "zark_2026-10-09_05:36:29Z", "05:36:29"),
            (be, "zark_2026-10-09_05:40:47Z", "05:40:47"),
            (var, "zark_2026-10-09_05:40:47Z", "05:40:47"),
            (var, "zark_2026-10-09_05:43:39Z", "05:43:39"),
            (be, "autosnap_2026-10-09_06:00:01_hourly", "06:00:01"),
            (be, "zark_2026-10-09_07:37:46Z", "07:37:46"),
            (var, "zark_2026-10-09_07:37:46Z", "07:37:46"),
            (be, "zark_2026-10-09_07:39:03Z", "07:39:04"),
            (var, "zark_2026-10-09_07:39:03Z", "07:39:04"),
        ]
        return [Snap(ds, n, f"{i}", i, at(t)) for i, (ds, n, t) in enumerate(rows)]

    def test_backup_points_a_minute_apart_are_two_points(self):
        # O7: P1 and P2 77 s apart were one menu entry, so P1 could not be
        # chosen; X1 (no BE) joined the 05:40:47 entry and leaked into it.
        snaps = self._eli_0910()
        zark = [p for p in restore_points(snaps, f"rpool/ROOT/{_BE}") if p.family == "zark_"]
        names = [{s.name for v in p.members.values() for s in v} for p in zark]
        assert names == [
            {"zark_2026-10-09_05:35:03Z"},
            {"zark_2026-10-09_05:36:29Z"},
            {"zark_2026-10-09_05:40:47Z"},
            {"zark_2026-10-09_07:37:46Z"},
            {"zark_2026-10-09_07:39:03Z"},
        ]
        p1 = resolve(zark[3], snaps, ["rpool/var"])["rpool/var"]
        assert p1 is not None and p1.name == "zark_2026-10-09_07:37:46Z"

    def test_carried_point_does_not_resurrect_a_destroyed_dataset(self):
        # O12: zt5b's lineage ends at 05:35:03 and the 05:36:29 backup point
        # lacks it, so the 06:00:01 hourly does not bring it back; rpool/var,
        # absent from the hourly but present in every backup point, falls
        # back to its newest earlier snapshot (X1).
        snaps = self._eli_0910()
        hourly = next(
            p for p in restore_points(snaps, f"rpool/ROOT/{_BE}") if p.family == "autosnap_"
        )
        got = resolve(hourly, snaps, ["rpool/ROOT/zt5b", "rpool/var"])
        assert got["rpool/ROOT/zt5b"] is None
        var = got["rpool/var"]
        assert var is not None and var.name == "zark_2026-10-09_05:43:39Z"
        # Before it was destroyed, the same dataset resolves normally.
        first = restore_points(snaps, f"rpool/ROOT/{_BE}")[0]
        early = resolve(first, snaps, ["rpool/ROOT/zt5b"])["rpool/ROOT/zt5b"]
        assert early is not None and early.name == "zark_2026-10-09_05:35:03Z"

    def test_not_restored_note_tells_destroyed_from_not_yet_created(self):
        # O8: zt5b has snapshots before the hourly; its lineage ended there.
        snaps = self._eli_0910()
        hourly = next(
            p for p in restore_points(snaps, f"rpool/ROOT/{_BE}") if p.family == "autosnap_"
        )
        assert _absent_note("rpool/ROOT/zt5b", hourly, snaps) == (
            "destroyed in origin before this point (last snapshot zark_2026-10-09_05:35:03Z)"
        )
        assert _absent_note("rpool/later", hourly, snaps) == "no snapshot at or before this point"

    def test_dataset_without_earlier_snapshot_is_none(self):
        snaps = [
            Snap("rpool/ROOT/x", "autosnap_a", "1", 1, 100),
            Snap("rpool/new", "autosnap_b", "2", 2, 900),
        ]
        (point,) = restore_points(snaps[:1], "rpool/ROOT/x")
        assert resolve(point, snaps, ["rpool/new"])["rpool/new"] is None


class TestMountProps:  # pylint: disable=missing-function-docstring
    """D6/P0-11: where canmount/mountpoint come from, applied at receive."""

    def test_cache_wins_over_layout(self):
        cache = parse_list_cache(_ELI_CACHE_RPOOL)
        props = choose_props("rpool/var", _BE, zark={}, cache=cache, empty_with_children=False)
        assert props == MountProps("off", "/var", "cache")

    def test_zark_props_win_over_cache(self):
        cache = parse_list_cache(_ELI_CACHE_RPOOL)
        zark = {"rpool/var": ("noauto", "/srv/var")}
        props = choose_props("rpool/var", _BE, zark=zark, cache=cache, empty_with_children=False)
        assert props.source == "zark" and props.canmount == "noauto"

    def test_layout_for_boot_environment_children(self):
        props = choose_props(
            f"rpool/ROOT/{_BE}/usr", _BE, zark={}, cache={}, empty_with_children=True
        )
        assert props == MountProps("off", "/usr", "ubuntu")

    def test_inference_for_unknown_trees(self):
        top = choose_props("rpool/var", _BE, zark={}, cache={}, empty_with_children=True)
        mid = choose_props("rpool/var/lib", _BE, zark={}, cache={}, empty_with_children=True)
        leaf = choose_props(
            "rpool/var/lib/docker", _BE, zark={}, cache={}, empty_with_children=False
        )
        assert (top.canmount, top.mountpoint, top.source) == ("off", "/var", "inferred")
        assert (mid.canmount, mid.mountpoint) == ("off", "")
        assert (leaf.canmount, leaf.mountpoint) == ("on", "")

    def test_receive_options_keep_inheritance(self):
        assert receive_options(MountProps("off", "/var", "cache"), "none", "var") == [
            "-o canmount=off",
            "-o mountpoint=/var",
        ]
        assert receive_options(MountProps("off", "/var/lib", "cache"), "/var", "lib") == [
            "-o canmount=off",
        ]
        assert receive_options(MountProps("on", "/", "cache"), "none", _BE) == [
            "-o canmount=on",
            "-o mountpoint=/",
        ]


def _plan_mock(referenced: int = 1000) -> MockShell:
    mock = MockShell()
    types = [(d[0], "volume" if d[4] == "-" else "filesystem") for d in _manifest_dataset_lines()]
    mock.on("zfs list -Hp -o name,type -r backup/rpool").succeeds(
        "\n".join(f"{n}\t{t}" for n, t in types if n.startswith("backup/rpool")),
    )
    mock.on("zfs list -Hp -o name,type -r backup/bpool").succeeds(
        "\n".join(f"{n}\t{t}" for n, t in types if n.startswith("backup/bpool")),
    )
    mock.on_prefix("zfs get -Hp -s local -o name,property,value org.zark").succeeds("")

    def _refs(cmd: str) -> str:
        return "\n".join(f"{n}\t{referenced}" for n in cmd.split()[6:])

    mock.on("zfs list -Hp -t snapshot -o name,createtxg -s createtxg backup/keystore").succeeds(
        "backup/keystore@prepare_20260928_182338\t430",
    )
    mock._refs = _refs  # type: ignore[attr-defined] # pylint: disable=protected-access
    return mock


def _run_plan(cache: bool, empty: dict[str, bool] | None = None) -> RestorePlan:
    snaps = _stick_snaps()
    point = restore_points(snaps, f"rpool/ROOT/{_BE}")[-1]
    mock = _plan_mock()
    probe = (_ELI_CACHE_RPOOL, _ELI_CACHE_BPOOL, "617fca2c") if cache else ("", "", "617fca2c")

    def fake_referenced(names: list[str]) -> dict[str, int]:
        return dict.fromkeys(names, 1000)

    with (
        patch_sh(mock),
        patch.object(recover_mod, "_probe_be", return_value=probe),
        patch.object(recover_mod, "_referenced", side_effect=fake_referenced),
        patch.object(
            recover_mod,
            "_empty_root",
            side_effect=lambda full: (empty or {}).get(full.split("@")[0], False),
        ),
    ):
        return _plan("backup", "/dev/sdb1", _BE, point, snaps, make_log())


class TestRecoverPlan:  # pylint: disable=missing-function-docstring
    """Hallazgos 1/3 + P0-11 on the frozen stick, 17:00:01 point."""

    def test_first_level_tree_is_restored_with_origin_properties(self):
        plan = _run_plan(cache=True)
        rows = {r.rel: r for r in plan.rows}
        # rpool is created with mountpoint=/, so /var is inherited, not set.
        assert rows["rpool/var"].options == ["-o canmount=off"]
        assert rows["rpool/var"].effective == "/var"
        assert rows["rpool/var/lib"].options == ["-o canmount=off"]
        assert rows["rpool/var/lib/docker"].options == ["-o canmount=on"]
        assert rows["rpool/var/lib/docker"].effective == "/var/lib/docker"
        assert rows[f"rpool/ROOT/{_BE}"].options == ["-o canmount=on", "-o mountpoint=/"]
        assert rows[f"rpool/ROOT/{_BE}/var/lib"].effective == "/var/lib"
        assert rows["rpool/USERDATA/home_cgx8je"].effective == "/home"
        assert rows[f"bpool/BOOT/{_BE}"].options == ["-o canmount=on", "-o mountpoint=/boot"]
        assert plan.keystore_snap == "backup/keystore@prepare_20260928_182338"
        assert plan.hostid == "617fca2c"
        assert not plan.skipped

    def test_first_level_tree_without_cache_is_inferred(self):
        plan = _run_plan(
            cache=False,
            empty={"backup/rpool/var": True, "backup/rpool/var/lib": True},
        )
        rows = {r.rel: r for r in plan.rows}
        assert rows["rpool/var"].props is not None
        assert rows["rpool/var"].props.source == "inferred"
        # rpool is created with mountpoint=/, so /var is inherited, not set.
        assert rows["rpool/var"].options == ["-o canmount=off"]
        assert rows["rpool/var"].effective == "/var"
        assert rows["rpool/var/lib"].options == ["-o canmount=off"]
        assert rows["rpool/var/lib/docker"].options == ["-o canmount=on"]
        assert rows[f"rpool/ROOT/{_BE}/var"].options == ["-o canmount=off"]

    def test_snapshots_and_keystore_resolution(self):
        plan = _run_plan(cache=True)
        rows = {r.rel: r for r in plan.rows}
        assert rows["rpool/var/lib/docker"].snap is not None
        assert rows["rpool/var/lib/docker"].snap.name.startswith("syncoid_eli_")
        home = rows["rpool/USERDATA/home_cgx8je"].snap
        assert home is not None
        assert home.name == "autosnap_2026-09-28_17:00:01_hourly"
        assert not any("keystore" in r for r in rows)  # keystore resolves on its own


class TestRecoverAnchorLabel:  # pylint: disable=too-few-public-methods
    """M2X.5: another drive's bpool anchor on this drive is named as that
    drive's backup point, not as an anchor name."""

    def test_label(self) -> None:
        """Known GUID by its registry name, unknown by its GUID, others as they are."""
        name = repl.anchor_name("3277814611169134880", "zark_2026-10-11_12:22:15Z")
        label = recover_mod._snapshot_label  # pylint: disable=protected-access
        assert label(name, {"3277814611169134880": "backup2"}) == (
            "zark_2026-10-11_12:22:15Z (bpool anchor of drive backup2)"
        )
        assert label(name, {}) == (
            "zark_2026-10-11_12:22:15Z (bpool anchor of drive 3277814611169134880)"
        )
        assert label("zark_2026-10-11_12:22:15Z", {}) == "zark_2026-10-11_12:22:15Z"
        assert label("autosnap_2026-10-11_12:00:00_hourly", {}) == (
            "autosnap_2026-10-11_12:00:00_hourly"
        )


class TestRecoverPlanArchived:  # pylint: disable=missing-function-docstring
    """Decision 19: a dataset resolved from an archived lineage is read from
    where it is stored, and an archived lineage is never a dataset of its own."""

    P1, P2 = "zark_2026-10-01_10:00:00Z", "zark_2026-10-08_10:00:00Z"

    def _plan(self) -> tuple[RestorePlan, list[str], list[list[str]]]:
        be = f"rpool/ROOT/{_BE}"
        arch = f"{be}.archived-20261008"
        datasets = [
            "backup/rpool",
            "backup/rpool/ROOT",
            f"backup/{be}",
            f"backup/{be}/var",
            f"backup/{arch}",
            f"backup/{arch}/var",
        ]
        t1, t2 = 1_759_312_800, 1_759_917_600
        lines = [
            f"backup/{arch}@{self.P1}\t1\t1\t{t1}",
            f"backup/{arch}/var@{self.P1}\t2\t2\t{t1}",
            f"backup/{be}@{self.P2}\t3\t3\t{t2}",
            f"backup/{be}/var@{self.P2}\t4\t4\t{t2}",
        ]
        snaps = parse_snapshots(lines, "backup")
        point = restore_points(snaps, be)[0]
        mock = MockShell()
        mock.on("zfs list -Hp -o name,type -r backup/rpool").succeeds(
            "\n".join(f"{n}\tfilesystem" for n in datasets),
        )
        mock.on("zfs list -Hp -o name,type -r backup/bpool").fails()
        mock.on_prefix("zfs get -Hp -s local -o name,property,value org.zark").succeeds("")
        mock.on("zfs list -Hp -t snapshot -o name,createtxg -s createtxg backup/keystore").succeeds(
            "backup/keystore@prepare_20261001_100000\t9",
        )
        probed: list[str] = []
        sized: list[list[str]] = []

        def referenced(names: list[str]) -> dict[str, int]:
            sized.append(list(names))
            return dict.fromkeys(names, 1000)

        def empty_root(full: str) -> bool:
            probed.append(full)
            return False

        with (
            patch_sh(mock),
            patch.object(recover_mod, "_probe_be", return_value=("", "", "")),
            patch.object(recover_mod, "_referenced", side_effect=referenced),
            patch.object(recover_mod, "_empty_root", side_effect=empty_root),
            redirect_stdout(StringIO()),
        ):
            plan = _plan("backup", "/dev/sdb1", _BE, point, snaps, make_log())
        return plan, probed, sized

    def test_archived_snapshots_are_read_where_they_are_stored(self):
        plan, probed, sized = self._plan()
        arch = f"backup/rpool/ROOT/{_BE}.archived-20261008"
        assert probed == [f"{arch}@{self.P1}"]
        assert f"{arch}/var@{self.P1}" in sized[0] and f"{arch}@{self.P1}" in sized[0]
        row = plan.row(f"rpool/ROOT/{_BE}")
        assert row is not None and row.snap is not None and row.snap.source

    def test_archived_lineages_are_not_planned(self):
        plan, _probed, _sized = self._plan()
        assert not [r.rel for r in plan.rows if ".archived-" in r.rel]
        assert {r.rel for r in plan.rows} == {f"rpool/ROOT/{_BE}", f"rpool/ROOT/{_BE}/var"}


def _sized_plan(rpool: int, bpool: int, keystore: int = 16 * 1024**2) -> RestorePlan:
    snap = Snap("rpool/ROOT/x", "a", "1", 1, 1)
    rows = [
        RestoreRow("rpool/ROOT/x", snap, referenced=rpool),
        RestoreRow("bpool/BOOT/x", snap, referenced=bpool),
    ]
    point = restore_points([snap], "rpool/ROOT/x")[0]
    return RestorePlan("backup", "/dev/sdb1", "x", point, rows, "backup/keystore@k", keystore)


class TestRecoverSizeCheck:  # pylint: disable=missing-function-docstring
    """Hallazgo 14: size of the chosen point, fail-closed (hallazgo 5 spirit)."""

    @staticmethod
    def _check(plan: RestorePlan, disk_bytes: str | None) -> bool:
        mock = MockShell()
        if disk_bytes is None:
            mock.on("lsblk -bdn -o SIZE /dev/sda").fails()
        else:
            mock.on("lsblk -bdn -o SIZE /dev/sda").succeeds(disk_bytes)
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch("builtins.input", return_value=""),
        ):
            try:
                _check_sizes(plan, "/dev/sda", make_log())
            except SystemExit:
                return False
        return True

    def test_point_that_fits(self):
        assert self._check(_sized_plan(8 * 1024**3, 130 * 1024**2), str(476 * 1024**3))

    def test_point_too_big(self):
        assert not self._check(_sized_plan(470 * 1024**3, 130 * 1024**2), str(476 * 1024**3))

    def test_bpool_too_big(self):
        assert not self._check(_sized_plan(1024**3, 3 * 1024**3), str(476 * 1024**3))

    def test_fail_closed_when_unmeasurable(self):
        assert not self._check(_sized_plan(-1, 1), str(476 * 1024**3))
        assert not self._check(_sized_plan(1, 1), None)


class TestRecoverTargetDisks:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """Hallazgo 15: the live stick and the backup drive are never candidates."""

    def test_candidates(self):
        mock = MockShell()
        mock.on("lsblk -dn -P -o NAME,TYPE,SIZE,MODEL,SERIAL,TRAN").succeeds(
            'NAME="sda" TYPE="disk" SIZE="476.9G" MODEL="KINGSTON SKC600MS512G" '
            'SERIAL="50026B7784FC3319" TRAN="sata"\n'
            'NAME="sdb" TYPE="disk" SIZE="115.5G" MODEL="DT microDuo 3C" SERIAL="408D" TRAN="usb"\n'
            'NAME="sdc" TYPE="disk" SIZE="231G" MODEL="DT microDuo 3C" SERIAL="1C1B" TRAN="usb"\n'
            'NAME="zd0" TYPE="disk" SIZE="16M" MODEL="" SERIAL="" TRAN=""\n'
            'NAME="loop0" TYPE="loop" SIZE="2G" MODEL="" SERIAL="" TRAN=""',
        )
        with (
            patch_sh(mock),
            patch.object(recover_mod, "protected_disks", return_value={"/dev/sdc": "/cdrom"}),
        ):
            cands = _target_candidates("/dev/sdb")
        assert [c[0] for c in cands] == ["/dev/sda"]
        assert "50026B7784FC3319" in cands[0][1]


def _marks_unreadable(snaps: list[Snap]) -> Callable[..., list[Snap]]:
    """drive_snapshots on a drive whose marks listing fails: a strict read
    raises, a non-strict one silently returns the snapshots without marks."""

    def read(_pool: str, strict: bool = False) -> list[Snap]:
        if strict:
            raise engine.ReadError("could not read: zfs get")
        return snaps

    return read


class TestRecoverNoWipeBeforeYes:  # pylint: disable=missing-function-docstring
    """Hallazgo 15: nothing touches the internal disk before pre-flight + YES."""

    def _run(self, answer: str | None, preflight_fails: bool = False) -> MockShell:
        with tempfile.TemporaryDirectory() as td:
            return self._run_in(td, answer, preflight_fails)

    def _run_in(  # pylint: disable=too-many-locals
        self,
        work: str,
        answer: str | None,
        preflight_fails: bool = False,
        calls: dict[str, list[tuple[object, ...]]] | None = None,
        snaps_fail: bool = False,
    ) -> MockShell:
        """``answer`` None simulates a closed terminal (EOF) at the YES prompt.

        The system.key copy goes below ``work``; Cleanup's atexit run is
        simulated after the command returns. ``calls``, when given, receives
        the positional arguments of every ``_choose_point`` and ``_plan`` call.
        ``snaps_fail`` leaves the real ``_list_snapshots`` in place with the
        engine's listing of the drive failing.
        """
        source_key = os.path.join(work, "source.key")
        Path(source_key).write_bytes(b"k" * 32)
        cleanups: list[Cleanup] = []
        mock = MockShell()
        snaps = _stick_snaps()
        point = restore_points(snaps, f"rpool/ROOT/{_BE}")[-1]
        plan = RestorePlan("backup", "/dev/sdb1", _BE, point, [], "k@k", 1, "617fca2c")
        drive = type("D", (), {"name": "backup", "drive_id": _KINGSTON_ID, "guid": "1"})()

        def preflight(*_args: object) -> None:
            if preflight_fails:
                make_log().fatal("preflight")

        def spy(name: str, result: object) -> Callable[..., object]:
            def record(*args: object) -> object:
                if calls is not None:
                    calls.setdefault(name, []).append(args)
                return result

            return record

        patches: list[AbstractContextManager[object]] = [
            patch_sh(mock),
            patch.object(
                recover_mod.Cleanup,
                "register",
                autospec=True,
                side_effect=cleanups.append,
            ),
            patch.object(recover_mod, "SYSTEM_KEY_PATH", source_key),
            patch.object(
                recover_mod.tempfile,
                "mkdtemp",
                side_effect=lambda real=tempfile.mkdtemp, **_k: real(dir=work),
            ),
            patch.object(recover_mod, "_is_live_usb", return_value=True),
            patch.object(recover_mod, "scan_connected_drives", return_value=[drive]),
            patch.object(recover_mod, "select_drive", return_value=drive),
            patch.object(recover_mod, "backup_device", return_value="/dev/sdb1"),
            patch.object(ZFS, "import_backup_pool", return_value=True),
            patch.object(recover_mod, "open_keystore", return_value=True),
            patch.object(Keystore, "load_pool_keys", return_value=1),
            patch.object(recover_mod, "whole_disk", return_value="/dev/sdb"),
            patch.object(recover_mod, "_find_be", return_value=_BE),
            patch.object(engine, "drive_snapshots", side_effect=_marks_unreadable(snaps))
            if snaps_fail
            else patch.object(recover_mod, "_list_snapshots", return_value=snaps),
            patch.object(recover_mod, "_choose_point", side_effect=spy("_choose_point", point)),
            patch.object(recover_mod, "_plan", side_effect=spy("_plan", plan)),
            patch.object(recover_mod, "_select_target", return_value="/dev/sda"),
            patch.object(recover_mod, "_preflight", side_effect=preflight),
            patch("builtins.input", side_effect=EOFError)
            if answer is None
            else patch("builtins.input", return_value=answer),
            patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
            redirect_stdout(StringIO()),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            try:
                recover_mod.run([])
            except SystemExit:
                pass
            for c in cleanups:  # what atexit would do
                c.run()
        return mock

    def test_system_key_copy_is_private_and_removed_on_a_fatal(self):
        # Review §8: the /tmp copy used to survive every failure path.
        with tempfile.TemporaryDirectory() as td:
            mock = self._run_in(td, "YES", preflight_fails=True)
            key_dirs = [d for d in os.listdir(td) if d != "source.key"]
            assert len(key_dirs) == 1
            copy = Path(td, key_dirs[0], "system.key")
            assert copy.read_bytes() == b"k" * 32
            assert copy.stat().st_mode & 0o777 == 0o600
            assert mock.was_called(f"rm -rf {copy.parent}")

    def test_no_wipe_when_not_confirmed(self):
        mock = self._run("NO")
        assert mock.was_not_called("wipefs")
        assert mock.was_not_called("sgdisk")
        assert mock.was_not_called("zpool destroy")

    def test_no_wipe_when_preflight_fails(self):
        mock = self._run("YES", preflight_fails=True)
        assert mock.was_not_called("wipefs")

    def test_no_wipe_when_terminal_closes_at_yes(self):
        mock = self._run(None)
        assert mock.was_not_called("wipefs")
        assert mock.was_not_called("sgdisk")

    def test_a_failed_read_of_the_marks_stops_before_the_menu(self) -> None:
        """M3R-9: the drive's snapshots and marks are read strictly; a failed
        listing is fatal instead of turning the drive into a legacy one."""
        calls: dict[str, list[tuple[object, ...]]] = {}
        with tempfile.TemporaryDirectory() as td:
            mock = self._run_in(td, "YES", calls=calls, snaps_fail=True)
        assert "_choose_point" not in calls and "_plan" not in calls
        assert mock.was_not_called("wipefs") and mock.was_not_called("sgdisk")

    def test_completeness_reaches_the_menu_and_the_plan(self) -> None:
        """M2V-5 (N12a, N12b): the complete points read from the drive are
        what the menu and the plan see; without them an incomplete point is
        the default and its missing datasets read as destroyed (M2R-1)."""
        complete = {"zark_2026-10-07_10:00:00Z"}
        calls: dict[str, list[tuple[object, ...]]] = {}
        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(recover_mod, "complete_points", return_value=complete),
        ):
            _ = self._run_in(td, "NO", calls=calls)
        (menu,) = calls["_choose_point"]
        (plan,) = calls["_plan"]
        assert menu[-1] is complete
        assert plan[-1] is complete


class TestRecoverAltrootChecks:  # pylint: disable=missing-function-docstring
    """R2-13 and review §8: /mnt/recover is checked before the wipe, and rm stays on it."""

    @staticmethod
    def _preflight_with_mounts(table: str) -> tuple[MockShell, str]:
        """Run _preflight with ``table`` as findmnt's output; it always ends in a fatal
        (the tool checks are not mocked), so the output says which check stopped it."""
        mock = MockShell()
        mock.on("findmnt -rn -o TARGET").succeeds(table)
        point = restore_points(_stick_snaps(), f"rpool/ROOT/{_BE}")[-1]
        plan = RestorePlan("backup", "/dev/sdb1", _BE, point, [], "k@k", 1, "")
        buf = StringIO()
        with patch_sh(mock), redirect_stdout(buf), patch("builtins.input", return_value=""):
            try:
                recover_mod._preflight(plan, "/dev/sda", make_log())  # pylint: disable=protected-access
            except SystemExit:
                pass
        return mock, buf.getvalue()

    def test_preflight_refuses_a_mount_under_the_altroot(self):
        mock, out = self._preflight_with_mounts("/\n/mnt/recover\n/mnt/recover/dev")
        assert "still mounted under /mnt/recover" in out
        assert mock.was_not_called("which")  # stopped before the tool checks
        assert "sudo umount -R /mnt/recover" in out

    def test_preflight_refuses_a_mount_below_a_plain_altroot_directory(self):
        # V-2: /mnt/recover is a plain directory; `findmnt -R` on it lists nothing.
        mock, out = self._preflight_with_mounts("/\n/mnt/recover/dev\n/mnt/recovery\n/run")
        assert "still mounted under /mnt/recover" in out
        assert mock.was_not_called("which")
        assert "sudo umount -R /mnt/recover/dev" in out and "/mnt/recovery" not in out

    def test_preflight_ignores_mounts_elsewhere(self):
        mock, out = self._preflight_with_mounts("/\n/mnt/recovery\n/run")
        assert "still mounted" not in out
        assert mock.was_called("which sgdisk")  # went on to the next check

    def test_step_6_never_leaves_the_altroot_filesystem(self):
        src = Path(recover_mod.__file__).read_text(encoding="utf-8")
        assert "rm -rf {RECOVER_MNT}" not in src
        assert "rm -rf --one-file-system {RECOVER_MNT}" in src


class TestInitrd:  # pylint: disable=missing-function-docstring
    """P0-7: only installed kernels; dracut failures are reported."""

    @staticmethod
    def _root() -> str:
        root = Path(tempfile.mkdtemp())
        (root / "boot").mkdir()
        for v in ("7.0.0-31-generic", "7.0.0-34-generic"):
            (root / f"boot/vmlinuz-{v}").write_text("")
            (root / f"lib/modules/{v}").mkdir(parents=True)
            (root / f"lib/modules/{v}/modules.dep").write_text("")
        # Installer residue: live-ISO kernel modules dir without a vmlinuz.
        (root / "lib/modules/7.0.0-14-generic").mkdir(parents=True)
        (root / "lib/modules/7.0.0-14-generic/modules.dep").write_text("")
        (root / "usr/bin").mkdir(parents=True)
        (root / "usr/bin/dracut").write_text("")
        return str(root)

    def test_installed_kernels_skip_residue(self):
        assert installed_kernels(self._root()) == ["7.0.0-31-generic", "7.0.0-34-generic"]

    def test_dracut_per_kernel_and_failure_reported(self):
        root = self._root()
        mock = MockShell()
        mock.on(f"cat {root}/etc/machine-id").fails()
        mock.on(f"chroot {root} dracut --force --kver=7.0.0-31-generic").succeeds()
        mock.on(f"chroot {root} dracut --force --kver=7.0.0-34-generic").fails(rc=1)
        with patch_sh(mock), redirect_stdout(StringIO()):
            failures = regenerate_initrd(root, make_log())
        assert failures == ["dracut failed for 7.0.0-34-generic (rc=1)"]
        assert mock.was_not_called("--regenerate-all")
        assert mock.was_not_called("7.0.0-14")


class TestRepairBootCleanup:  # pylint: disable=missing-function-docstring
    """P0-6: every mount is tracked, /boot goes before rpool, banner is truthful."""

    @staticmethod
    def _run(
        export_rpool_ok: bool,
        grub_failure: str = "",
    ) -> tuple[MockShell, str, bool, list[str]]:
        mock = MockShell()
        order: list[str] = []
        mock.on("zpool import").succeeds("")
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool list bpool").succeeds("bpool")
        if export_rpool_ok:
            mock.on("zpool export rpool").succeeds()
        else:
            mock.on("zpool export rpool").fails("pool is busy")
            mock.on("zpool export -f rpool").fails("pool is busy")
        mock.on("zpool export bpool").succeeds()
        mock.on("zpool list -vHP rpool").succeeds("rpool\t1G\n\t/dev/sda4\t1G")
        mock.on("lsblk -nrs -o NAME,TYPE /dev/sda4").succeeds("sda4 part\nsda disk")
        mock.on("lsblk -nr -o NAME,PARTTYPE /dev/sda").succeeds(
            "sda\nsda1 c12a7328-f81f-11d2-ba4b-00a0c93ec93b\n"
            "sda4 6a898cc3-1dd2-11b2-99a6-080020736631",
        )
        mock.on("chroot /mnt/repair update-grub").fails()
        mock.on("mount /dev/sda1 /mnt/repair/boot/efi").succeeds()
        exported_after_rpool_state = {"rpool": export_rpool_ok}

        def fake_initrd(*_a: object) -> list[str]:
            order.append("initrd")
            return []

        def fake_guard(**_k: object) -> None:
            order.append("guard")

        def fake_grub(*_a: object) -> str:
            order.append("grub")
            return grub_failure

        def fake_mount(_alt, _pw, _log, _zfs, _ks, cleanup):
            cleanup.track_pool("rpool")
            cleanup.track_pool("bpool")
            cleanup.track_mount("/mnt/repair")
            cleanup.track_mount("/mnt/repair/boot")
            return ("/mnt/repair", "ubuntu_x")

        buf = StringIO()
        exited = False
        patches: list[AbstractContextManager[object]] = [
            patch_sh(mock),
            patch.object(repair_boot_mod.Cleanup, "register"),
            patch.object(repair_boot_mod.sh, "is_live_usb", return_value=True),
            patch.object(repair_boot_mod, "mount_system_pools", side_effect=fake_mount),
            patch.object(repair_boot_mod, "regenerate_initrd", side_effect=fake_initrd),
            patch.object(repair_boot_mod.grub_guard, "install", side_effect=fake_guard),
            patch.object(repair_boot_mod, "regenerate_grub_cfg", side_effect=fake_grub),
            patch.object(repair_boot_mod, "fix_grub_bpool_uuid"),
            # write_zpool_cache mkdirs under /mnt/repair: unprivileged runs fail
            patch.object(ZFS, "write_zpool_cache"),
            patch.object(repair_boot_mod.Path, "glob", return_value=[Path("vmlinuz-7")]),
            patch("lib.cleanup.Path.is_mount", return_value=True),
            patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
            patch.object(
                ZFS,
                "pool_exists",
                side_effect=lambda p: p == "rpool" and not exported_after_rpool_state["rpool"],
            ),
            redirect_stdout(buf),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            try:
                repair_boot_mod.run([])
            except SystemExit:
                exited = True
        return mock, buf.getvalue(), exited, order

    def test_boot_unmounted_before_rpool_export_and_success_banner(self):
        mock, out, exited, _ = self._run(export_rpool_ok=True)
        calls = mock.calls
        assert calls.index("umount /mnt/repair/boot") < calls.index("zpool export rpool")
        assert calls.index("umount /mnt/repair/boot/efi") < calls.index("umount /mnt/repair/boot")
        assert mock.was_called("mount /dev/sda1 /mnt/repair/boot/efi")
        assert "BOOT REPAIR COMPLETE" in out and not exited
        assert not any("zfs set mountpoint" in c for c in calls)

    def test_banner_does_not_lie_when_rpool_stays_imported(self):
        _, out, exited, _ = self._run(export_rpool_ok=False)
        assert exited
        assert "BOOT REPAIR INCOMPLETE" in out
        assert "Pools exported cleanly" not in out
        assert "rpool is still imported" in out

    def test_initrd_and_guard_come_before_update_grub(self):
        # 10_linux_zfs skips kernels without an initrd (E8 on eli).
        _, _, _, order = self._run(export_rpool_ok=True)
        assert order == ["guard", "initrd", "grub"]

    def test_banner_does_not_lie_when_grub_cfg_is_not_regenerated(self):
        _, out, exited, _ = self._run(export_rpool_ok=True, grub_failure="no entries")
        assert exited
        assert "BOOT REPAIR INCOMPLETE" in out
        assert "grub.cfg regenerated ✓" not in out
        assert "grub.cfg not regenerated: no entries" in out


class TestRepairBootGrubCfg:  # pylint: disable=missing-function-docstring
    """E8: a stale grub.cfg.pre-repair is never passed off as regenerated."""

    @staticmethod
    def _regenerate(
        root: Path,
        generated: str,
        update_grub_ok: bool = True,
        stderr: str = "boom",
    ) -> str:
        grub_cfg = root / "grub.cfg"

        def fake_run(cmd: str, **_kw: object) -> RunResult:
            if "update-grub" in cmd:
                grub_cfg.write_text(generated, encoding="utf-8")
                rc, err = (0, "") if update_grub_ok else (1, stderr)
                return RunResult(returncode=rc, stdout="", stderr=err, command=cmd)
            if cmd.startswith("cp "):
                _, src, dst = cmd.split()
                Path(dst).write_bytes(Path(src).read_bytes())
            return RunResult(returncode=0, stdout="", stderr="", command=cmd)

        with patch.object(grub_cfg_mod.sh, "run", side_effect=fake_run):
            return grub_cfg_mod.regenerate_grub_cfg(
                grub_cfg,
                "chroot /mnt/repair update-grub",
                root / "grub.cfg.pre-repair",
                make_log(),
            )

    def test_entries_found_is_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "grub.cfg").write_text("old vmlinuz-1", encoding="utf-8")
            with redirect_stdout(StringIO()):
                assert self._regenerate(root, "linux /vmlinuz-7") == ""

    def test_stale_backup_from_an_earlier_run_is_not_restored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "grub.cfg.pre-repair").write_text("STALE vmlinuz", encoding="utf-8")
            with redirect_stdout(StringIO()):
                reason = self._regenerate(root, "no kernels here")
            assert reason == "update-grub produced no kernel entries"
            assert (root / "grub.cfg").read_text(encoding="utf-8") == "no kernels here"

    def test_this_runs_copy_is_restored_but_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "grub.cfg").write_text("CURRENT vmlinuz", encoding="utf-8")
            with redirect_stdout(StringIO()):
                reason = self._regenerate(root, "", update_grub_ok=False)
            assert reason == "update-grub failed: boom"
            assert (root / "grub.cfg").read_text(encoding="utf-8") == "CURRENT vmlinuz"

    def test_guard_refusal_is_reported_by_its_error_line(self):
        # J on eli: the guard's ERROR line, not grub-mkconfig's chatter.
        stderr = (
            "Sourcing file `/etc/default/grub'\n"
            "Generating grub configuration file ...\n"
            "ERROR: External ZFS pool(s) detected: backup\n\n"
            "  Fix: disconnect the external drive(s) and run update-grub again.\n"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "grub.cfg").write_text("CURRENT vmlinuz", encoding="utf-8")
            with redirect_stdout(StringIO()):
                reason = self._regenerate(root, "partial", update_grub_ok=False, stderr=stderr)
            assert reason == "update-grub failed: ERROR: External ZFS pool(s) detected: backup"
            assert (root / "grub.cfg").read_text(encoding="utf-8") == "CURRENT vmlinuz"


class _FinishPath:  # pylint: disable=too-few-public-methods
    """Stand-in for Path in finish: only /etc/os-release exists (no host writes)."""

    def __init__(self, p: object) -> None:
        self.p = str(p)

    def __str__(self) -> str:
        return self.p

    def exists(self) -> bool:
        """Only the os-release check passes; nothing on the host is read or written."""
        return self.p == "/etc/os-release"


class TestFinishBanner:  # pylint: disable=missing-function-docstring
    """J: finish reports update-grub, update-initramfs and pool failures."""

    @staticmethod
    def _run(
        grub_failure: str = "",
        initramfs_ok: bool = True,
        health: str = "ONLINE",
    ) -> tuple[str, bool, list[tuple[object, ...]]]:
        grub_calls: list[tuple[object, ...]] = []

        def fake_run(cmd: str, **_kw: object) -> RunResult:
            rc = 1 if cmd.startswith("update-initramfs") and not initramfs_ok else 0
            return RunResult(returncode=rc, stdout="", stderr="", command=cmd)

        def fake_grub(*a: object) -> str:
            grub_calls.append(a)
            return grub_failure

        buf = StringIO()
        exited = False
        patches: list[AbstractContextManager[object]] = [
            patch("lib.sh.run", side_effect=fake_run),
            patch("lib.zfs.run", side_effect=fake_run),
            patch.object(finish_mod, "Path", _FinishPath),
            patch.object(finish_mod, "warn_rpool_mountpoint_lost"),
            patch.object(finish_mod, "warn_kernel_named_vdevs"),
            patch.object(finish_mod.grub_guard, "install"),
            patch.object(finish_mod.apt_guard, "install"),
            patch.object(finish_mod, "regenerate_grub_cfg", side_effect=fake_grub),
            patch.object(finish_mod, "fix_grub_bpool_uuid"),
            patch.object(ZFS, "pool_exists", return_value=True),
            patch.object(ZFS, "pool_guid", return_value=""),
            patch.object(ZFS, "pool_health", return_value=health),
            redirect_stdout(buf),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            try:
                finish_mod.run([])
            except SystemExit:
                exited = True
        return buf.getvalue(), exited, grub_calls

    def test_all_good_is_complete(self):
        out, exited, calls = self._run()
        assert not exited
        assert "FINISH COMPLETE" in out and "GRUB config updated ✓" in out
        assert [(str(a[0]), a[1], str(a[2])) for a in calls] == [
            ("/boot/grub/grub.cfg", "update-grub", "/boot/grub/grub.cfg.pre-finish"),
        ]

    def test_guard_refusal_is_incomplete(self):
        reason = "update-grub failed: ERROR: External ZFS pool(s) detected: backup"
        out, exited, _ = self._run(grub_failure=reason)
        assert exited
        assert "FINISH INCOMPLETE" in out
        assert "GRUB config updated ✓" not in out
        assert f"grub.cfg not regenerated: {reason}" in out

    def test_initramfs_failure_is_incomplete(self):
        out, exited, _ = self._run(initramfs_ok=False)
        assert exited
        assert "FINISH INCOMPLETE" in out and "initramfs updated ✓" not in out

    def test_degraded_pool_is_incomplete(self):
        out, exited, _ = self._run(health="DEGRADED")
        assert exited
        assert "✗ pool rpool: DEGRADED" in out


class TestFailClosedAndLogging:  # pylint: disable=missing-function-docstring
    """Hallazgo 5 (used=-1), hallazgo 22 (ask_choice EOF), I19 (log file)."""

    def test_ask_choice_aborts_on_eof(self):
        with (
            patch("builtins.input", side_effect=EOFError),
            redirect_stdout(StringIO()),
        ):
            try:
                make_log().ask_choice("Pick", ["a", "b"])
            except SystemExit:
                return
        raise AssertionError("ask_choice must abort on EOF, not loop")

    def test_verdicts_and_prompts_reach_the_log_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zark.log")
            log = Log(log_file=path)
            with (
                patch("builtins.input", return_value="y"),
                patch("lib.log.getpass.getpass", return_value="secret"),
                redirect_stdout(StringIO()),
            ):
                log.ask("Destroy the pool?")
                log.ask_password("Passphrase for blue")
                log.banner_ok("BACKUP COMPLETED", ["Duration: 1m"])
                log.banner_error("BACKUP NOT VERIFIED", ["do not rely on it"])
            text = Path(path).read_text(encoding="utf-8")
        assert "Destroy the pool? → 'y (yes)'" in text
        assert "Passphrase for blue" in text and "secret" not in text
        assert "[OK] ══ BACKUP COMPLETED ══" in text
        assert "[FAIL] ══ BACKUP NOT VERIFIED ══" in text

    def test_typed_confirmations_reach_the_log_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zark.log")
            log = Log(log_file=path)
            with redirect_stdout(StringIO()):
                with patch("builtins.input", return_value="YES"):
                    got = log.ask_text(
                        "    Type YES to proceed: ",
                        accept=("YES",),
                        label="Type YES to erase /dev/sda",
                    )
                with patch("builtins.input", side_effect=EOFError):
                    eof = log.ask_text(
                        "  Type IUNDERSTAND to continue anyway: ", accept=("IUNDERSTAND",)
                    )
            text = Path(path).read_text(encoding="utf-8")
        assert got == "YES" and eof == ""
        assert "Type YES to erase /dev/sda → 'YES'" in text
        assert "Type IUNDERSTAND to continue anyway: → '<empty>'" in text

    def test_other_answers_are_logged_by_length_only(self):
        # R2-2: a passphrase typed at the wrong prompt never reaches the log.
        secret = "correct horse battery staple"
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zark.log")
            log = Log(log_file=path)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with patch("builtins.input", return_value=secret):
                    assert not log.ask("Continue anyway?")
                    assert log.ask_text("  Type YES: ", accept=("YES",)) == secret
                    assert log.ask_input("Pool name", "backup1") == secret
                    assert log.ask_input("Confirm", accept=("DESTROY",)) == secret
                with patch("builtins.input", return_value="yes"):
                    assert log.ask_text("  Type YES: ", accept=("YES",)) == "yes"
                with patch("builtins.input", return_value="DESTROY"):
                    assert log.ask_input("Confirm", accept=("DESTROY",)) == "DESTROY"
            text = Path(path).read_text(encoding="utf-8")
        assert secret not in text
        assert f"<other answer, {len(secret)} chars>" in text
        assert "Continue anyway? → '<other answer, 28 chars> (no)'" in text
        assert "Type YES: → 'yes'" in text
        assert "Confirm → 'DESTROY'" in text

    def test_a_closed_terminal_does_not_stop_the_log(self):
        # R2-1: after SIGHUP a print raises EIO; teardown messages still reach the file.
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zark.log")
            with patch("builtins.print", side_effect=OSError(5, "Input/output error")):
                Log(log_file=path).ok("Pool rpool exported")
            assert "Pool rpool exported" in Path(path).read_text(encoding="utf-8")

    def test_log_file_is_private(self):
        # R2-2: created 0600 whatever the umask; an existing 0644 file is tightened.
        with tempfile.TemporaryDirectory() as td:
            new = os.path.join(td, "new.log")
            old = os.path.join(td, "old.log")
            Path(old).write_text("x\n", encoding="utf-8")
            os.chmod(old, 0o644)
            umask = os.umask(0o022)
            try:
                with redirect_stdout(StringIO()):
                    Log(log_file=new).info("hello")
                    Log(log_file=old).info("hello")
            finally:
                os.umask(umask)
            assert os.stat(new).st_mode & 0o777 == 0o600
            assert os.stat(old).st_mode & 0o777 == 0o600

    def test_destructive_confirmations_never_bypass_the_log(self):
        # A raw input() answer never reaches zark.log (I19).
        raw_input = re.compile(r"(?<![\w.])input\(")
        for name in ("recover", "purge", "repair_boot", "fix_rpool_mountpoint", "backup"):
            src = (Path(__file__).parent.parent / "commands" / f"{name}.py").read_text(
                encoding="utf-8",
            )
            assert not raw_input.search(src), f"commands/{name}.py reads input() directly"


class TestReviewFindings:  # pylint: disable=missing-function-docstring
    """Regression guards for the independent review of the M1 diff."""

    def test_root_is_mounted_before_bpool_and_mount_all(self):
        mock = MockShell()
        mock.on("zfs mount rpool/ROOT/x").succeeds()
        mock.on("zfs mount bpool/BOOT/x").succeeds()
        mock.on("findmnt -n /mnt/recover/boot").succeeds("/mnt/recover/boot bpool/BOOT/x")
        mock.on("zfs mount -a").succeeds()
        with (
            patch_sh(mock),
            patch.object(recover_mod.Path, "exists", return_value=True),
            patch.object(recover_mod.Path, "glob", return_value=[]),
            redirect_stdout(StringIO()),
        ):
            _mount_restored_system("x", True, make_log())
        mounts = [c for c in mock.calls if c.startswith("zfs mount") and "|" not in c]
        assert mounts == ["zfs mount rpool/ROOT/x", "zfs mount bpool/BOOT/x", "zfs mount -a"]

    def test_unmountable_root_is_fatal(self):
        mock = MockShell()
        mock.on("zfs mount rpool/ROOT/x").fails("cannot mount")
        with (
            patch_sh(mock),
            patch("builtins.input", return_value=""),
            redirect_stdout(StringIO()),
        ):
            try:
                _mount_restored_system("x", True, make_log())
            except SystemExit:
                return
        raise AssertionError("expected SystemExit")

    def test_preflight_refuses_imported_system_pools(self):
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool list bpool").fails()
        plan = _sized_plan(1, 1)
        with (
            patch_sh(mock),
            patch("builtins.input", return_value=""),
            redirect_stdout(StringIO()),
        ):
            try:
                _preflight(plan, "/dev/sda", make_log())
            except SystemExit:
                assert mock.was_not_called("zpool destroy")
                return
        raise AssertionError("expected SystemExit")

    def test_dataset_created_between_sanoid_runs_is_not_filed_earlier(self):
        snaps = [
            Snap("rpool/ROOT/x", "autosnap_a_hourly", "1", 1, 1000),
            Snap("rpool/new", "autosnap_b_hourly", "2", 2, 1440),  # next run, new dataset
            Snap("rpool/ROOT/x", "autosnap_b_hourly", "3", 3, 1441),
        ]
        points = restore_points(snaps, "rpool/ROOT/x")
        assert len(points) == 2
        assert resolve(points[0], snaps, ["rpool/new"])["rpool/new"] is None

    def test_syncoid_run_with_long_gaps_stays_one_point(self):
        snaps = [
            Snap("rpool", "syncoid_h_1", "1", 1, 1000),
            Snap("rpool/ROOT/x", "syncoid_h_2", "2", 2, 1002),
            Snap("rpool/USERDATA/home", "syncoid_h_3", "3", 3, 1002 + 4200),  # 70 min transfer
        ]
        (point,) = restore_points(snaps, "rpool/ROOT/x")
        assert set(point.members) == {"rpool", "rpool/ROOT/x", "rpool/USERDATA/home"}

    def test_already_imported_pool_from_another_disk_is_refused(self):
        mock = MockShell()
        mock.on("zpool list backup").succeeds("backup")
        mock.on("zpool get -H -o value altroot backup").succeeds("/run/zark/altroot/backup")
        mock.on("zpool get -H -o value readonly backup").succeeds("on")
        mock.on("zpool get -H -o value guid backup").succeeds("999")
        with patch_sh(mock), redirect_stdout(StringIO()):
            zfs = ZFS(make_log())
            assert not zfs.import_backup_pool("backup", "/dev/x", readonly=True, guid="111")
            assert zfs.import_backup_pool("backup", "/dev/x", readonly=True, guid="999")
            assert not zfs.import_backup_pool("backup", "/dev/x", readonly=False, guid="999")

    def test_registry_match_uses_every_by_id_alias(self):
        drives = {"blue": DriveInfo("blue", "1", "wwn-0x5000000000000001")}
        ident = DiskIdentity(
            disk="/dev/sdb",
            by_id=_KINGSTON_ID,
            aliases=(_KINGSTON_ID, "wwn-0x5000000000000001"),
        )
        assert match_registry(ident, drives) == ["blue"]

    def test_second_home_dataset_is_not_stacked_on_home(self):
        snaps = _stick_snaps()
        extra = [
            Snap(
                "rpool/USERDATA/home_zzz",
                "autosnap_2026-09-28_17:00:01_hourly",
                "9",
                999,
                1790614801,
            ),
        ]
        point = restore_points(snaps + extra, f"rpool/ROOT/{_BE}")[-1]
        mock = _plan_mock()
        types_extra = "backup/rpool/USERDATA/home_zzz\tfilesystem"
        base = mock._find("zfs list -Hp -o name,type -r backup/rpool")  # pylint: disable=protected-access
        assert base is not None
        base.stdout = base.stdout + types_extra + "\n"
        with (
            patch_sh(mock),
            patch.object(recover_mod, "_probe_be", return_value=("", "", "")),
            patch.object(recover_mod, "_referenced", side_effect=lambda n: dict.fromkeys(n, 1)),
            patch.object(recover_mod, "_empty_root", return_value=False),
            redirect_stdout(StringIO()),
        ):
            plan = _plan("backup", "/dev/sdb1", _BE, point, snaps + extra, make_log())
        rows = {r.rel: r for r in plan.rows}
        assert rows["rpool/USERDATA/home_cgx8je"].options[0] == "-o canmount=on"
        assert rows["rpool/USERDATA/home_zzz"].options[0] == "-o canmount=noauto"


class TestRpoolRootMountpoint:  # pylint: disable=missing-function-docstring
    """G4 on eli: recover left rpool at mountpoint=none; the installer uses /."""

    def test_root_mountpoint_from_cache_or_ubuntu_layout(self):
        assert rpool_root_mountpoint({"rpool": ("off", "/")}) == ("/", "cache")
        # none in origin's cache is what a 1.0.12 recover left behind
        assert rpool_root_mountpoint({"rpool": ("off", "none")}) == ("/", "ubuntu")
        assert rpool_root_mountpoint({}) == ("/", "ubuntu")
        assert rpool_root_mountpoint({"rpool": ("off", "/srv/x")}) == ("/srv/x", "cache")

    def test_rpool_is_created_with_the_root_mountpoint(self):
        cmd = recover_mod._rpool_create_cmd(  # pylint: disable=protected-access
            "/",
            "/tmp/k",
            "/dev/sda4",
        )
        assert "-O canmount=off -O mountpoint=/ " in cmd
        assert "mountpoint=none" not in cmd and "-m none" not in cmd
        assert cmd.endswith("-R /mnt/recover rpool /dev/sda4")

    @staticmethod
    def _lost(get_out: str, has_root: bool = True) -> bool:
        mock = MockShell()
        mock.on("zfs get -H -o property,value,source mountpoint,canmount rpool").succeeds(get_out)
        if has_root:
            mock.on("zfs list -H -o name rpool/ROOT").succeeds("rpool/ROOT")
        else:
            mock.on("zfs list -H -o name rpool/ROOT").fails("does not exist")
        with patch_sh(mock):
            return rpool_mountpoint_lost()

    def test_detects_only_the_recover_artefact(self):
        lost = "mountpoint\tnone\tlocal\ncanmount\toff\tlocal"
        assert self._lost(lost)
        assert not self._lost("mountpoint\t/\tlocal\ncanmount\toff\tlocal")
        assert not self._lost("mountpoint\tnone\tdefault\ncanmount\toff\tlocal")
        assert not self._lost("mountpoint\tnone\tlocal\ncanmount\ton\tdefault")
        assert not self._lost(lost, has_root=False)


@dataclass
class _FixRun:
    """What one fix-rpool-mountpoint run did."""

    mock: MockShell
    out: str
    code: int | str | None  # SystemExit code, or the exception's name; None = returned
    events: list[str]
    final: str  # zvol_inhibit_dev after the run


class TestFixRpoolMountpoint:  # pylint: disable=missing-function-docstring
    """fix-rpool-mountpoint: live only, no zvol devices, YES, always restores."""

    @staticmethod
    def _run(  # pylint: disable=too-many-arguments,too-many-locals
        *,
        answer: str = "YES",
        choices: tuple[str, ...] = (),
        inheritors: tuple[tuple[str, str], ...] = (),
        live: bool = True,
        imported: bool = False,
        import_ok: bool = True,
        export_ok: bool = True,
        zvols_before: list[str] | None = None,
        zvols_after: list[str] | None = None,
        before: tuple[str, str] = ("none", "local"),
        after: tuple[str, str] = ("/", "local"),
        during_fix: Callable[[], None] | None = None,
        during_export: Callable[[], None] | None = None,
    ) -> _FixRun:
        mock = MockShell()
        mock.on("zfs set mountpoint=/ rpool").succeeds()
        mock.on("modprobe zfs").succeeds()
        mock.on("zfs list -H -t volume -o name").succeeds("backup/keystore")
        events: list[str] = []
        handlers = {sig: signal.getsignal(sig) for sig in fix_rpool_mod.TEARDOWN_SIGNALS}
        with tempfile.TemporaryDirectory() as td:
            param = Path(td) / "zvol_inhibit_dev"
            param.write_text("0\n", encoding="utf-8")

            def fake_import(*_a: object, **_k: object) -> bool:
                events.append(f"import(inhibit={param.read_text(encoding='utf-8')})")
                return import_ok

            def fake_export(*_a: object, **_k: object) -> bool:
                if during_export:
                    during_export()
                events.append(f"export(inhibit={param.read_text(encoding='utf-8')})")
                return export_ok

            real_fix = fix_rpool_mod._fix  # pylint: disable=protected-access

            def fix(zfs: ZFS, log: Log, turned_off: list[str]) -> bool:
                if during_fix:
                    during_fix()
                return real_fix(zfs, log, turned_off)

            rpool_props = iter(
                [
                    {"mountpoint": before, "canmount": ("off", "local")},
                    {"mountpoint": after, "canmount": ("off", "local")},
                ],
            )

            def props(ds: str) -> dict[str, tuple[str, str]]:
                if ds == "rpool":
                    return next(rpool_props)
                off = mock.was_called(f"zfs set canmount=off {ds}")
                return {"canmount": ("off", "local") if off else ("on", "default")}

            buf = StringIO()
            code: int | str | None = None
            patches: list[AbstractContextManager[object]] = [
                patch_sh(mock),
                patch.object(fix_rpool_mod, "ZVOL_INHIBIT", param),
                patch.object(fix_rpool_mod, "_fix", side_effect=fix),
                patch.object(fix_rpool_mod.sh, "is_live_usb", return_value=live),
                patch.object(
                    fix_rpool_mod.glob,
                    "glob",
                    side_effect=[zvols_before or [], zvols_after or []],
                ),
                patch.object(fix_rpool_mod, "_props", side_effect=props),
                patch.object(
                    fix_rpool_mod,
                    "_inheriting_from_rpool",
                    return_value=list(inheritors),
                ),
                patch.object(ZFS, "pool_exists", return_value=imported),
                patch.object(ZFS, "pool_import", side_effect=fake_import),
                patch.object(ZFS, "pool_export", side_effect=fake_export),
                patch.object(ZFS, "dataset_exists", return_value=True),
                patch("builtins.input", side_effect=[*choices, answer]),
                redirect_stdout(buf),
            ]
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                try:
                    fix_rpool_mod.run([])
                except SystemExit as e:
                    code = e.code if e.code is not None else 0
                except KeyboardInterrupt:
                    code = "KeyboardInterrupt"
                except RuntimeError:
                    code = "RuntimeError"
            final = param.read_text(encoding="utf-8")
        # The runner's own signal handlers are back after the command.
        assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
        return _FixRun(mock, buf.getvalue(), code, events, final)

    def test_sets_mountpoint_with_zvols_inhibited_and_restores_the_parameter(self):
        r = self._run()
        assert r.events == ["import(inhibit=1)", "export(inhibit=1)"]
        assert r.mock.was_called("zfs set mountpoint=/ rpool")
        assert r.final == "0" and r.code is None
        assert "RPOOL MOUNTPOINT FIXED" in r.out

    def test_refuses_when_rpool_is_imported(self):
        r = self._run(imported=True)
        assert r.code == 1 and not r.events
        assert r.mock.was_not_called("zfs set")
        assert r.final == "0\n"  # never touched

    def test_refuses_outside_a_live_usb(self):
        r = self._run(live=False)
        assert r.code == 1 and not r.events and r.final == "0\n"
        assert "Run this from a live USB" in r.out

    def test_no_answer_changes_nothing_but_still_exports_and_restores(self):
        r = self._run(answer="NO")
        assert r.mock.was_not_called("zfs set")
        assert r.events[-1] == "export(inhibit=1)" and r.final == "0"
        assert r.code == 1 and "RPOOL MOUNTPOINT NOT FIXED" in r.out

    def test_refuses_before_touching_anything_when_zvol_devices_exist(self):
        # R2-10: another pool's zvols; the parameter and rpool are never touched.
        r = self._run(zvols_before=["/dev/zd0"])
        assert r.code == 1 and not r.events and r.final == "0\n"
        assert "sudo zpool export backup" in r.out

    def test_aborts_if_a_zvol_device_appears_after_the_import(self):
        r = self._run(zvols_after=["/dev/zd0"])
        assert r.code == 1
        assert r.mock.was_not_called("zfs set")
        assert r.events[-1] == "export(inhibit=1)" and r.final == "0"

    def test_import_failure_still_restores_the_parameter(self):
        r = self._run(import_ok=False)
        assert r.code == 1 and r.final == "0"
        assert r.mock.was_not_called("zfs set")

    def test_failed_export_is_in_the_verdict(self):
        r = self._run(export_ok=False)
        assert r.code == 1 and r.final == "0"
        assert "rpool is still imported" in r.out and "NOT FIXED" in r.out

    def test_a_raising_export_still_restores_the_parameter(self):
        # V-7: the restore sits in its own finally, after the export.
        def boom() -> None:
            raise RuntimeError("export blew up")

        r = self._run(during_export=boom)
        assert r.code == "RuntimeError" and r.final == "0"

    def test_an_exception_in_the_fix_still_exports_and_restores(self):
        def boom() -> None:
            raise RuntimeError("boom")

        r = self._run(during_fix=boom)
        assert r.code == "RuntimeError"
        assert r.events == ["export(inhibit=1)"] and r.final == "0"

    def test_mountpoint_is_read_without_the_import_altroot(self):
        # eli 2026-09-30: under -R, zfs get shows "/" as the altroot itself.
        mock = MockShell()
        mock.on("zfs get -H -o property,value,source mountpoint,canmount rpool").succeeds(
            f"mountpoint\t{fix_rpool_mod.ALTROOT}\tlocal\ncanmount\toff\tlocal",
        )
        mock.on("zfs get -H -o property,value,source mountpoint,canmount rpool/x").succeeds(
            f"mountpoint\t{fix_rpool_mod.ALTROOT}/srv\tlocal\ncanmount\ton\tdefault",
        )
        with patch_sh(mock):
            root = fix_rpool_mod._props("rpool")  # pylint: disable=protected-access
            child = fix_rpool_mod._props("rpool/x")  # pylint: disable=protected-access
        assert root["mountpoint"] == ("/", "local")
        assert child["mountpoint"] == ("/srv", "local")

    # eli after H5: the rpool/var tree inherits from rpool; only docker mounts.
    _ELI_TREE = (
        ("rpool/var", "off"),
        ("rpool/var/lib", "off"),
        ("rpool/var/lib/docker", "on"),
    )

    def test_inheritors_are_read_with_their_canmount(self):
        mock = MockShell()
        mock.on(
            "zfs get -H -r -t filesystem -o name,property,value,source mountpoint,canmount rpool",
        ).succeeds(
            "rpool\tmountpoint\t/run/zark/altroot/rpool\tlocal\n"
            "rpool\tcanmount\toff\tlocal\n"
            "rpool/ROOT\tmountpoint\tnone\tlocal\n"
            "rpool/ROOT\tcanmount\toff\tlocal\n"
            "rpool/var\tmountpoint\t/run/zark/altroot/rpool/var\tinherited from rpool\n"
            "rpool/var\tcanmount\toff\tlocal\n"
            "rpool/var/lib/docker\tmountpoint\t/x\tinherited from rpool\n"
            "rpool/var/lib/docker\tcanmount\ton\tdefault",
        )
        with patch_sh(mock):
            got = fix_rpool_mod._inheriting_from_rpool(make_log())  # pylint: disable=protected-access
        assert got == [("rpool/var", "off"), ("rpool/var/lib/docker", "on")]

    def test_the_inheritor_list_fails_closed(self):
        # V-4: an empty list here used to read as "No other dataset inherits".
        mock = MockShell()
        mock.on_prefix("zfs get -H -r -t filesystem").fails("cannot open 'rpool'")
        with patch_sh(mock), redirect_stdout(StringIO()), patch("builtins.input", return_value=""):
            try:
                fix_rpool_mod._inheriting_from_rpool(make_log())  # pylint: disable=protected-access
            except SystemExit:
                return
        raise AssertionError("a failed zfs get must be fatal")

    def test_an_unknown_canmount_is_asked_about_as_on(self):
        r = self._run(inheritors=(("rpool/x", "?"),), choices=("",), answer="unused")
        assert "rpool/x has canmount=?" in r.out
        assert r.code == 1 and r.mock.was_not_called("zfs set")

    def test_the_verdict_lists_canmount_changes_when_the_mountpoint_fails(self):
        r = self._run(inheritors=(("rpool/var", "on"),), choices=("2",), after=("none", "local"))
        assert r.mock.was_called("zfs set canmount=off rpool/var")
        assert "RPOOL MOUNTPOINT NOT FIXED" in r.out and "canmount=off: rpool/var" in r.out

    def test_inheritors_are_listed_with_their_canmount(self):
        r = self._run(inheritors=self._ELI_TREE, choices=("1", "rpool/var/lib/docker"))
        for ds, cm, target in (
            ("rpool/var", "off", "/var"),
            ("rpool/var/lib/docker", "on", "/var/lib/docker"),
        ):
            assert re.search(
                rf"{re.escape(ds)}\s+none → {re.escape(target)}\s+canmount={cm}", r.out
            )
        assert "rpool/var has canmount=on" not in r.out  # off datasets ask nothing

    def test_keeping_a_canmount_on_dataset_needs_its_name(self):
        r = self._run(inheritors=self._ELI_TREE, choices=("1", "rpool/var/lib/docker"))
        assert r.code is None and r.mock.was_called("zfs set mountpoint=/ rpool")
        assert r.mock.was_not_called("zfs set canmount")

    def test_a_wrong_name_changes_nothing(self):
        r = self._run(inheritors=self._ELI_TREE, choices=("1", "YES"), answer="unused")
        assert r.code == 1 and r.mock.was_not_called("zfs set")
        assert r.events[-1] == "export(inhibit=1)" and r.final == "0"

    def test_the_default_aborts(self):
        r = self._run(inheritors=self._ELI_TREE, choices=("",), answer="unused")
        assert r.code == 1 and r.mock.was_not_called("zfs set")

    def test_canmount_off_is_set_before_the_mountpoint(self):
        # R2-4: an empty rpool/var created later must not cover the BE's /var.
        r = self._run(inheritors=(("rpool/var", "on"),), choices=("2",))
        sets = [c for c in r.mock.calls if c.startswith("zfs set")]
        assert sets == ["zfs set canmount=off rpool/var", "zfs set mountpoint=/ rpool"]
        assert r.code is None and "RPOOL MOUNTPOINT FIXED" in r.out

    def test_success_is_judged_by_the_property_not_the_exit_code(self):
        r = self._run(after=("none", "local"))
        assert r.code == 1 and "RPOOL MOUNTPOINT NOT FIXED" in r.out


class TestFixRpoolSignals:  # pylint: disable=missing-function-docstring
    """R2-1, V-1, V-6: the teardown survives any mix of signals and a dead stdout.

    Each case runs tests/fix_rpool_harness.py in its own process: a regression
    kills that process, not the test runner.
    """

    @staticmethod
    def _run(scenario: str) -> tuple[int, str, list[str]]:
        harness = Path(__file__).parent / "fix_rpool_harness.py"
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [sys.executable, str(harness), td, scenario],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
            final = Path(td, "zvol_inhibit_dev").read_text(encoding="utf-8").strip()
            events_file = Path(td, "events")
            events = events_file.read_text(encoding="utf-8").split() if events_file.exists() else []
        return r.returncode, final, events

    def _assert_torn_down(self, scenario: str, *codes: int) -> None:
        rc, final, events = self._run(scenario)
        assert rc in codes and (final, events) == ("0", ["export(inhibit=1)"]), (
            scenario,
            rc,
            final,
            events,
        )

    def test_one_signal(self):
        self._assert_torn_down("sig:TERM", 128 + signal.SIGTERM)
        self._assert_torn_down("sig:HUP", 128 + signal.SIGHUP)

    def test_two_signals_pending_together_in_either_order(self):
        # V-1: logind sends SIGTERM and SIGHUP together when a session is ended.
        # CPython runs pending handlers lowest number first, whatever the send order.
        for pair, first in (
            ("INT,TERM", signal.SIGINT),
            ("TERM,INT", signal.SIGINT),
            ("HUP,INT", signal.SIGHUP),
            ("TERM,HUP", signal.SIGHUP),
            ("HUP,TERM", signal.SIGHUP),
        ):
            self._assert_torn_down(f"pending:{pair}", 128 + first)

    def test_signals_during_the_export_are_ignored(self):
        self._assert_torn_down("export:INT", 128 + signal.SIGINT)
        self._assert_torn_down("export:TERM,HUP", 128 + signal.SIGINT)

    def test_a_dead_stdout_does_not_kill_the_teardown(self):
        # V-6: `| tee` killed by the same Ctrl-C; zark runs with SIGPIPE at SIG_DFL.
        # The teardown must complete. Afterwards SIGPIPE is SIG_DFL again, and
        # Python's exit-time flush of the lines the teardown could not write may
        # kill the process (-SIGPIPE, seen on carmen's Python 3.14): harmless.
        after = -signal.SIGPIPE
        self._assert_torn_down("deadpipe:INT", 128 + signal.SIGINT, after)
        self._assert_torn_down("deadpipe:HUP", 128 + signal.SIGHUP, after)

    def test_a_reader_that_goes_away_with_no_signal_does_not_kill_the_run(self):
        # W-3: only the SIGPIPE ignore set before the parameter is written saves
        # this case; the teardown's own ignore comes too late. The run ends in
        # its NOT FIXED verdict (exit 1), or dies on SIGPIPE after the teardown.
        self._assert_torn_down("deadpipe:", 1, -signal.SIGPIPE)


class TestMountOriginLayout:  # pylint: disable=missing-function-docstring
    """G4: read-only `zark mount` rebuilds origin's tree without writing."""

    @staticmethod
    def _plan(cache: bool = True) -> dict[str, "mount_mod.LayoutMount"]:
        c = (
            {
                **parse_list_cache(_ELI_CACHE_RPOOL),
                **parse_list_cache(_ELI_CACHE_BPOOL),
            }
            if cache
            else {}
        )
        with (
            patch_sh(_plan_mock()),
            patch.object(mount_mod, "root_is_empty", return_value=False),
        ):
            plan = mount_mod._plan_origin_layout(  # pylint: disable=protected-access
                "backup",
                _BE,
                "/mnt/zark/backup",
                c,
            )
        return {m.rel: m for m in plan}

    def test_datasets_go_where_origin_mounts_them(self):
        plan = self._plan()
        assert plan["rpool/USERDATA/home_cgx8je"].target == "/mnt/zark/backup/home"
        assert plan["rpool/var/lib/docker"].target == "/mnt/zark/backup/var/lib/docker"
        assert plan[f"rpool/ROOT/{_BE}/var/lib"].target == "/mnt/zark/backup/var/lib"
        assert plan[f"bpool/BOOT/{_BE}"].target == "/mnt/zark/backup/boot"

    def test_containers_and_off_datasets_are_not_mounted(self):
        plan = self._plan()
        for rel in ("rpool", "rpool/ROOT", "rpool/USERDATA", f"rpool/ROOT/{_BE}"):
            assert rel not in plan  # created containers and the BE root itself
        assert plan["rpool/var"].target == "" and "canmount=off" in plan["rpool/var"].reason
        assert plan["rpool/var/lib"].target == ""
        assert plan[f"rpool/ROOT/{_BE}/usr"].target == ""

    def test_without_cache_the_ubuntu_layout_still_places_the_system(self):
        plan = self._plan(cache=False)
        assert plan["rpool/USERDATA/home_cgx8je"].target == "/mnt/zark/backup/home"
        assert plan[f"rpool/ROOT/{_BE}/var"].target == ""  # layout: off
        assert plan["rpool/var/lib/docker"].target == "/mnt/zark/backup/var/lib/docker"

    def test_mount_never_creates_directories_in_the_backup(self):
        mock = MockShell()
        mock.on("zfs list -H -o name -r backup/rpool/ROOT").succeeds(
            f"backup/rpool/ROOT\nbackup/rpool/ROOT/{_BE}",
        )
        mock.on_prefix("mount -t zfs -o ro,zfsutil").succeeds()
        mock.on("mkdir -p /mnt/zark/backup").succeeds()
        plan = [
            mount_mod.LayoutMount("rpool/var/lib/docker", "/mnt/zark/backup/var/lib/docker"),
            mount_mod.LayoutMount("rpool/USERDATA/home_x", "/mnt/zark/backup/home"),
            mount_mod.LayoutMount(f"rpool/ROOT/{_BE}/var/lib", "/mnt/zark/backup/var/lib"),
        ]
        present = {"/mnt/zark/backup/home", "/mnt/zark/backup/var/lib"}
        with (
            patch_sh(mock),
            patch.object(mount_mod, "_plan_origin_layout", return_value=plan),
            patch.object(mount_mod.Path, "is_file", return_value=False),
            patch.object(
                mount_mod.Path,
                "is_dir",
                autospec=True,
                side_effect=lambda self: str(self) in present,
            ),
            redirect_stdout(StringIO()),
        ):
            mounted, skipped = mount_mod._mount_origin_layout(  # pylint: disable=protected-access
                "backup",
                "/mnt/zark/backup",
                make_log(),
            )
        mounts = [c for c in mock.calls if c.startswith("mount -t zfs")]
        assert mounts == [
            f"mount -t zfs -o ro,zfsutil backup/rpool/ROOT/{_BE} /mnt/zark/backup",
            "mount -t zfs -o ro,zfsutil backup/rpool/USERDATA/home_x /mnt/zark/backup/home",
            f"mount -t zfs -o ro,zfsutil backup/rpool/ROOT/{_BE}/var/lib /mnt/zark/backup/var/lib",
        ]
        assert mounted == 3
        assert [s.rel for s in skipped] == ["rpool/var/lib/docker"]
        assert not any(c.startswith("mkdir") and "/mnt/zark/backup/" in c for c in mock.calls)

    def test_read_write_mount_uses_the_same_tree(self):
        # Decision 1: datasets on the backup are canmount=noauto with no
        # mountpoint of their own, so read-write mounts them explicitly too.
        mock = MockShell()
        mock.on("zfs list -H -o name -r backup/rpool/ROOT").succeeds(
            "\n".join(
                [
                    "backup/rpool/ROOT",
                    f"backup/rpool/ROOT/{_BE}.archived-1",
                    f"backup/rpool/ROOT/{_BE}",
                ],
            ),
        )
        mock.on_prefix("mount -t zfs -o zfsutil").succeeds()
        mock.on("mkdir -p /mnt/zark/backup").succeeds()
        plan = [mount_mod.LayoutMount("rpool/USERDATA/home_x", "/mnt/zark/backup/home")]
        with (
            patch_sh(mock),
            patch.object(mount_mod, "_plan_origin_layout", return_value=plan),
            patch.object(mount_mod.Path, "is_file", return_value=False),
            patch.object(mount_mod.Path, "is_dir", return_value=True),
            redirect_stdout(StringIO()),
        ):
            mounted, _ = mount_mod._mount_origin_layout(  # pylint: disable=protected-access
                "backup",
                "/mnt/zark/backup",
                make_log(),
                readonly=False,
            )
        mounts = [c for c in mock.calls if c.startswith("mount -t zfs")]
        assert mounts[0] == f"mount -t zfs -o zfsutil backup/rpool/ROOT/{_BE} /mnt/zark/backup"
        assert mounted == 2

    def test_archived_lineages_are_not_part_of_the_tree(self):
        with (
            patch_sh(_plan_mock()),
            patch.object(mount_mod, "root_is_empty", return_value=False),
            patch.object(
                mount_mod,
                "list_datasets",
                return_value={
                    "rpool/var/lib/docker": "filesystem",
                    "rpool/var/lib/docker.archived-20261001": "filesystem",
                },
            ),
        ):
            plan = mount_mod._plan_origin_layout(  # pylint: disable=protected-access
                "backup",
                _BE,
                "/mnt/zark/backup",
                {},
            )
        by_rel = {m.rel: m for m in plan}
        assert by_rel["rpool/var/lib/docker.archived-20261001"].target == ""
        assert "archived" in by_rel["rpool/var/lib/docker.archived-20261001"].reason

    def test_umount_never_unmounts_every_zfs_dataset(self):
        # `zfs unmount -a` would also hit the running system's datasets (R2-5: both branches).
        src = Path(umount_mod.__file__).read_text(encoding="utf-8")
        assert 'sh.run("zfs unmount -a")' not in src

    def test_targets_that_resolve_outside_the_altroot_are_skipped(self):
        # R2-6: an absolute symlink or a .. in the backup must not reach the live system.
        with tempfile.TemporaryDirectory() as td:
            mnt = os.path.join(td, "backup")
            outside = os.path.join(td, "live-big")
            for d in (f"{mnt}/srv/ok", f"{mnt}/etc", outside):
                os.makedirs(d)
            os.symlink(outside, f"{mnt}/data")  # /data -> /mnt/big on origin
            plan = [
                mount_mod.LayoutMount("rpool/srv", f"{mnt}/srv/ok"),
                mount_mod.LayoutMount("rpool/data", f"{mnt}/data"),
                mount_mod.LayoutMount("rpool/up", f"{mnt}/etc/../.."),
            ]
            mock = MockShell()
            mock.on("zfs list -H -o name -r backup/rpool/ROOT").succeeds(
                f"backup/rpool/ROOT\nbackup/rpool/ROOT/{_BE}",
            )
            mock.on_prefix("mount -t zfs -o ro,zfsutil").succeeds()
            with (
                patch_sh(mock),
                patch.object(mount_mod, "_plan_origin_layout", return_value=plan),
                redirect_stdout(StringIO()),
            ):
                mounted, skipped = mount_mod._mount_origin_layout(  # pylint: disable=protected-access
                    "backup",
                    mnt,
                    make_log(),
                )
            mounts = [c for c in mock.calls if c.startswith("mount -t zfs")]
            assert mounts[1:] == [
                f"mount -t zfs -o ro,zfsutil backup/rpool/srv {os.path.realpath(mnt)}/srv/ok",
            ]
            assert mounted == 2
            assert {x.rel for x in skipped} == {"rpool/data", "rpool/up"}
            assert all("resolves to" in x.reason for x in skipped)

    @staticmethod
    def _umount_backup(export_ok: bool) -> tuple[MockShell, str]:
        mock = MockShell()
        buf = StringIO()
        mock.on("zpool list backup").succeeds("backup")
        if export_ok:
            mock.on("zpool export backup").succeeds()
        else:
            mock.on("zpool export backup").fails("pool is busy")
            mock.on("zpool export -f backup").fails("pool is busy")
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "backup", "home"))
            with (
                patch_sh(mock),
                patch.object(umount_mod, "MNT_BASE", td),
                patch.object(umount_mod.Config, "load", return_value=make_config()),
                patch.object(umount_mod, "flush_device_cache"),
                patch("builtins.input", return_value="y"),
                redirect_stdout(buf),
            ):
                try:
                    umount_mod.run([])
                except SystemExit as e:
                    assert not export_ok and e.code == 1, e.code
                else:
                    assert export_ok, "a failed export must end with exit status 1"
        return mock, buf.getvalue()

    def test_umount_cleans_directories_only_after_the_export(self):
        # Review §8: with the pool still imported, find would delete inside the backup.
        assert self._umount_backup(export_ok=False)[0].was_not_called("find /")
        ok, _ = self._umount_backup(export_ok=True)
        assert any(c.startswith("find ") and " -xdev " in c for c in ok.calls)

    def test_a_failed_backup_export_is_not_reported_as_unmounted(self):
        # W-3: the exit status alone does not pin the banner the operator reads.
        _, out = self._umount_backup(export_ok=False)
        assert "BACKUP NOT UNMOUNTED" in out and "backup is still imported" in out, out
        assert "exported ✓" not in out, out

    def test_umount_lets_the_zfs_unmount_propagate(self):
        # eli K6: a private backup tree left its copies mounted in the service
        # and snap namespaces, and the export failed "busy".
        calls = self._umount_backup(export_ok=True)[0].calls
        assert not any(c.startswith("mount --make-rprivate") for c in calls)
        assert [c for c in calls if c.startswith("umount -R ")][-1].endswith("/backup")


class TestUmountPropagation:  # pylint: disable=missing-function-docstring
    """Review 2 §11.3: commands.umount._unmount_tree on real mounts, two namespaces.

    tests/umount_propagation_harness.py runs the function unmodified in a mount
    namespace of its own (tmpfs for ZFS, only findmnt's FSTYPE rewritten), next to
    a slave namespace that plays a systemd service or a snap. Mocked findmnt
    tables cannot show propagation: this is what would have caught commit 28
    before eli did. Needs root and mount namespaces; skipped otherwise.
    """

    _unavailable: str | None = None
    _probed = False

    @classmethod
    def _skip_unless_possible(cls) -> None:
        if not cls._probed:
            cls._probed = True
            if os.geteuid() != 0:
                cls._unavailable = "needs root (mount namespaces)"
            elif not shutil.which("unshare"):
                cls._unavailable = "unshare(1) not found"
            else:
                probe = subprocess.run(
                    [sys.executable, "-c", "import os; os.unshare(os.CLONE_NEWNS)"],
                    capture_output=True,
                    check=False,
                )
                if probe.returncode != 0:
                    cls._unavailable = "cannot create a mount namespace here"
        if cls._unavailable:
            raise SkipTest(cls._unavailable)

    def _run(self, case: str) -> dict[str, list[str]]:
        self._skip_unless_possible()
        harness = Path(__file__).parent / "umount_propagation_harness.py"
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [sys.executable, str(harness), td, case],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
        assert r.returncode == 0, (case, r.returncode, r.stderr[-2000:])
        result: dict[str, list[str]] = json.loads(r.stdout.strip().splitlines()[-1])
        return result

    def test_datasets_leave_nothing_in_another_namespace(self):
        # eli K6: a private tree (commit 28) left both datasets in the other namespace.
        r = self._run("datasets")
        assert r == {"other_left": [], "tree_left": [], "host_lost": []}, r

    def test_an_empty_bind_leaves_nothing_in_another_namespace(self):
        r = self._run("bind")
        assert r == {"other_left": [], "tree_left": [], "host_lost": []}, r

    def test_a_host_mount_below_a_bound_directory_survives(self):
        # V-3: a plain `umount -R` (commit 21) unmounted it through the shared bind.
        # The copies left in the other namespace are W-1 (M4, with hallazgo 12).
        r = self._run("bind-hostmount")
        assert r["host_lost"] == [], r

    def test_nested_binds_as_chroot_makes_lose_no_host_mount(self):
        # /dev with /dev/pts inside it. What stays in the other namespace, and the
        # stacked copy left on the host's /dev/pts, is W-1 (M4, with hallazgo 12).
        r = self._run("nested-bind")
        assert r["host_lost"] == [], r


class TestStableVdevs:  # pylint: disable=missing-function-docstring
    """eli 2026-09-30: kernel-named vdevs broke the boot with a USB stick plugged in."""

    @staticmethod
    def _vdev(links: str, exists: bool) -> tuple[str, str]:
        mock = MockShell()
        mock.on_prefix("find /dev/disk/by-id/").succeeds(links)
        buf = StringIO()
        with (
            patch_sh(mock),
            patch.object(recover_mod.Path, "exists", return_value=exists),
            redirect_stdout(buf),
        ):
            path = recover_mod._stable_vdev("/dev/sda", 4, make_log())  # pylint: disable=protected-access
        return path, buf.getvalue()

    def test_pools_are_created_on_the_by_id_partition(self):
        links = (
            "ata-KINGSTON_SKC600MS512G_50026B7784FC3319\t../../sda\n"
            "wwn-0x50026b7784fc3319\t../../sda\n"
            "ata-KINGSTON_SKC600MS512G_50026B7784FC3319-part4\t../../sda4\n"
        )
        path, _ = self._vdev(links, exists=True)
        assert path == "/dev/disk/by-id/ata-KINGSTON_SKC600MS512G_50026B7784FC3319-part4"

    def test_falls_back_to_the_kernel_name_with_a_warning(self):
        path, out = self._vdev("", exists=False)
        assert path == "/dev/sda4"
        assert "kernel name" in out

    def test_kernel_named_vdevs_are_detected(self):
        mock = MockShell()
        mock.on("zpool list -vHP rpool").succeeds("rpool\t464G\n\t/dev/sdb4\t464G")
        mock.on("zpool list -vHP bpool").succeeds("bpool\t2G\n\t/dev/disk/by-id/ata-X-part2\t2G")
        with patch_sh(mock):
            assert kernel_named_vdevs() == ["/dev/sdb4"]


# ── Replication planner (M2) ────────────────────────────────────────────────


def _ref(name: str, guid: str, txg: int) -> "repl.Ref":
    return repl.Ref(name, guid, txg, 1_700_000_000 + txg)


P = "zark_2026-10-07_18:00:00Z"


class TestReplicationNames:
    """Point, anchor and archive names."""

    def test_point_name_is_utc(self) -> None:
        """Points are named in UTC whatever the local zone."""
        tz = datetime(2026, 10, 7, 20, 0, 1).astimezone()
        assert repl.point_name(tz) == f"zark_{tz.astimezone(UTC):%Y-%m-%d_%H:%M:%S}Z"
        assert repl.POINT_RE.match(repl.point_name(tz))

    def test_anchor_round_trip(self) -> None:
        """An anchor carries its disk's GUID; points and sanoid names are not anchors."""
        a = repl.anchor_name("1622", P)
        assert a == "zark_1622_2026-10-07_18:00:00Z"
        assert repl.anchor_disk(a) == "1622"
        assert repl.anchor_disk(P) == ""
        assert repl.anchor_disk("autosnap_2026-10-07_18:00:00_hourly") == ""
        assert not repl.POINT_RE.match(a)

    def test_old_anchors_only_this_disk(self) -> None:
        """Dropping old anchors never touches another disk's."""
        mine_old = repl.anchor_name("1", "zark_2026-09-01_00:00:00Z")
        mine_new = repl.anchor_name("1", P)
        other = repl.anchor_name("2", "zark_2026-08-01_00:00:00Z")
        assert repl.old_anchors([mine_old, mine_new, other, P], "1", mine_new) == [mine_old]

    def test_archive_name(self) -> None:
        """Archive names are dated and never collide."""
        now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        assert repl.archive_name("rpool/var", set(), now) == "rpool/var.archived-20261007"
        taken = {"rpool/var.archived-20261007"}
        assert repl.archive_name("rpool/var", taken, now) == "rpool/var.archived-20261007-2"
        assert repl.is_archived("rpool/var.archived-20261007/lib")
        assert not repl.is_archived("rpool/var/lib")


class TestReplicationPlanner:
    """Per-dataset states (redesign §4; M2 decisions 5–10, 18)."""

    def _o(
        self, rel: str, snaps: list["repl.Ref"], bms: list["repl.Ref"] | None = None
    ) -> "repl.Origin":
        return repl.Origin(rel, snaps=snaps, bookmarks=bms or [])

    def test_structural_container_is_never_an_orphan(self) -> None:
        """M2R-4: a container origin does not list is never offered for
        destruction; what lies below it still is."""
        origin = {"rpool/var": repl.Origin("rpool/var")}
        dest = {
            "rpool": repl.Dest("rpool"),
            "rpool/USERDATA": repl.Dest("rpool/USERDATA"),
            "rpool/USERDATA/home": repl.Dest("rpool/USERDATA/home"),
            "rpool/var": repl.Dest("rpool/var"),
        }
        orphans = [p.rel for p in repl.plan(origin, dest, P) if p.state is repl.State.ORPHAN]
        assert orphans == ["rpool/USERDATA/home"]

    def test_keystore_never_expected(self) -> None:
        """The keystore travels apart, whatever its kind."""
        origin = {
            "rpool/keystore": repl.Origin("rpool/keystore", kind="filesystem"),
            "rpool/var": repl.Origin("rpool/var"),
        }
        assert repl.expected(origin) == ["rpool/var"]

    def test_new_dataset(self) -> None:
        """A dataset missing on the backup is sent in full."""
        o = self._o("rpool/x", [_ref(P, "9", 9)])
        assert repl.plan_dataset(o, None, P).state is repl.State.NEW

    def test_incremental_from_destination_newest(self) -> None:
        """The base is the destination's newest snapshot, found in origin by guid."""
        o = self._o("rpool/x", [_ref("a", "1", 1), _ref("b", "2", 2), _ref(P, "9", 9)])
        d = repl.Dest("rpool/x", snaps=[_ref("a", "1", 1), _ref("b-renamed-on-dest", "2", 2)])
        p = repl.plan_dataset(o, d, P)
        assert (p.state, p.base) == (repl.State.INCREMENTAL, "@b")

    def test_via_bookmark_after_range_destroy(self) -> None:
        """R2: the snapshot is gone but this disk's bookmark anchors the send."""
        bm = _ref(repl.anchor_name("7", "zark_2026-09-01_00:00:00Z"), "5", 5)
        o = self._o("rpool/x", [_ref("c", "6", 6), _ref(P, "9", 9)], [bm])
        d = repl.Dest(
            "rpool/x", snaps=[_ref("old", "1", 1), _ref("zark_2026-09-01_00:00:00Z", "5", 5)]
        )
        p = repl.plan_dataset(o, d, P)
        assert (p.state, p.base, p.first) == (repl.State.VIA_BOOKMARK, f"#{bm.name}", "@c")

    def test_never_bookmark_path_on_bpool(self) -> None:
        """I-F: bpool never anchors on a bookmark, even one somebody made."""
        o = self._o("bpool/BOOT", [_ref(P, "9", 9)], [_ref("bm", "5", 5)])
        d = repl.Dest("bpool/BOOT", snaps=[_ref("s", "5", 5)])
        assert repl.plan_dataset(o, d, P).state is repl.State.DIVERGED

    def test_rollback_lists_what_it_would_destroy(self) -> None:
        """H23/F4: an older common snapshot is never used silently."""
        o = self._o("rpool/x", [_ref("weekly", "3", 3), _ref(P, "9", 9)])
        d = repl.Dest(
            "rpool/x",
            used=10,
            snaps=[_ref("weekly", "3", 3), _ref("daily", "4", 4), _ref("hourly", "5", 5)],
        )
        p = repl.plan_dataset(o, d, P)
        assert p.state is repl.State.ROLLBACK
        assert (p.common, p.newer) == ("@weekly", ["@daily", "@hourly"])
        assert p.base == "@weekly"

    def test_diverged(self) -> None:
        """Nothing in common: a user decision, never a transfer."""
        o = self._o("rpool/x", [_ref(P, "9", 9)])
        d = repl.Dest("rpool/x", snaps=[_ref("a", "1", 1)])
        assert repl.plan_dataset(o, d, P).state is repl.State.DIVERGED
        empty = repl.Dest("rpool/x")
        assert repl.plan_dataset(o, empty, P).state is repl.State.DIVERGED

    def test_modified_head_is_a_decision(self) -> None:
        """M2R-9: a destination written since its newest snapshot refuses an
        incremental; asked about, the plan keeps the base it would use."""
        o = self._o("rpool/x", [_ref("a", "1", 1), _ref("b", "2", 2), _ref(P, "9", 9)])
        d = repl.Dest("rpool/x", written=4096, snaps=[_ref("a", "1", 1), _ref("b", "2", 2)])
        p = repl.plan_dataset(o, d, P)
        assert (p.state, p.base, p.common, p.used) == (repl.State.MODIFIED, "@b", "@b", 4096)
        assert p.state not in repl.TRANSFERABLE
        bm = _ref(repl.anchor_name("7", "zark_2026-09-01_00:00:00Z"), "5", 5)
        o = self._o("rpool/x", [_ref("c", "6", 6), _ref(P, "9", 9)], [bm])
        d = repl.Dest("rpool/x", written=1, snaps=[_ref("zark_2026-09-01_00:00:00Z", "5", 5)])
        p = repl.plan_dataset(o, d, P)
        assert (p.state, p.base, p.first) == (repl.State.MODIFIED, f"#{bm.name}", "@c")
        # M2V-1 (eli R10): a partial receive onto a written head is refused at
        # its end, so it is asked about too, planned as what it needs without
        # its partial state; nothing to estimate when that is no incremental.
        d = repl.Dest("rpool/x", token="1-abc", written=1, snaps=[_ref("a", "1", 1)])
        p = repl.plan_dataset(o, d, P)
        assert (p.state, p.partial, p.base, p.common) == (repl.State.MODIFIED, True, "", "@a")
        o = self._o("rpool/x", [_ref("a", "1", 1), _ref(P, "9", 9)])
        p = repl.plan_dataset(o, d, P)
        assert (p.state, p.partial, p.base, p.used) == (repl.State.MODIFIED, True, "@a", 1)
        # A partial full receive has no snapshot, so no `written`: still RESUME.
        d = repl.Dest("rpool/x", token="1-abc", written=0, snaps=[])
        assert repl.plan_dataset(o, d, P).state is repl.State.RESUME

    def test_resume_first(self) -> None:
        """Partial receive state is finished before anything else."""
        o = self._o("rpool/x", [_ref(P, "9", 9)])
        d = repl.Dest("rpool/x", token="1-abc", snaps=[_ref("a", "1", 1)])
        assert repl.plan_dataset(o, d, P).state is repl.State.RESUME

    def test_at_point(self) -> None:
        """A dataset already holding the point needs nothing."""
        o = self._o("rpool/x", [_ref(P, "9", 9)])
        d = repl.Dest("rpool/x", snaps=[_ref(P, "9", 9)])
        assert repl.plan_dataset(o, d, P).state is repl.State.AT_POINT

    def test_no_state_destroys_without_a_decision(self) -> None:
        """I-A: transferable states never need a destination snapshot removed."""
        o = self._o("rpool/x", [_ref("a", "1", 1), _ref(P, "9", 9)])
        for d in (
            None,
            repl.Dest("rpool/x", snaps=[_ref("a", "1", 1)]),
            repl.Dest("rpool/x", token="t"),
            repl.Dest("rpool/x", snaps=[_ref(P, "9", 9)]),
        ):
            p = repl.plan_dataset(o, d, P)
            assert p.state in repl.TRANSFERABLE
            assert not p.newer

    def test_expected_excludes_keystore_and_zvols(self) -> None:
        """Decision 10: zvols are listed apart; the keystore travels apart."""
        origin = {
            "rpool": repl.Origin("rpool"),
            "rpool/keystore": repl.Origin("rpool/keystore", kind="volume"),
            "rpool/vm": repl.Origin("rpool/vm", kind="volume"),
            "bpool": repl.Origin("bpool"),
        }
        assert not repl.expected(origin)  # pool roots are structural
        assert repl.excluded(origin) == [("rpool/vm", "zvol, not backed up")]
        states = {p.rel: p.state for p in repl.plan(origin, {}, P)}
        assert states["rpool/vm"] is repl.State.EXCLUDED
        assert "rpool/keystore" not in states

    def test_orphans_grouped_kept_and_archives_ignored(self) -> None:
        """Decision 18: one entry per orphan tree; kept marks; archives are not orphans."""
        origin = {"rpool": repl.Origin("rpool", snaps=[_ref(P, "9", 9)])}
        dest = {
            "rpool": repl.Dest("rpool", snaps=[_ref(P, "9", 9)]),
            "rpool/old": repl.Dest("rpool/old", used=5, orphan="kept@2026-10-01"),
            "rpool/old/child": repl.Dest("rpool/old/child"),
            "rpool/gone": repl.Dest("rpool/gone", used=7),
            "rpool/var.archived-20261001": repl.Dest("rpool/var.archived-20261001"),
        }
        orphans = {p.rel: p for p in repl.plan(origin, dest, P) if p.state is repl.State.ORPHAN}
        assert set(orphans) == {"rpool/old", "rpool/gone"}
        assert orphans["rpool/old"].kept and not orphans["rpool/gone"].kept

    def test_rename_in_origin_is_followed(self) -> None:
        """A rename in origin is recognised by guid; only the top of the tree is renamed."""
        origin = {
            "rpool": repl.Origin("rpool", snaps=[_ref(P, "9", 9)]),
            "rpool/new": repl.Origin("rpool/new", snaps=[_ref("s", "4", 4), _ref(P, "10", 10)]),
            "rpool/new/c": repl.Origin("rpool/new/c", snaps=[_ref("s", "5", 5), _ref(P, "11", 11)]),
        }
        dest = {
            "rpool": repl.Dest("rpool", snaps=[_ref(P, "9", 9)]),
            "rpool/old": repl.Dest("rpool/old", snaps=[_ref("s", "4", 4)]),
            "rpool/old/c": repl.Dest("rpool/old/c", snaps=[_ref("s", "5", 5)]),
        }
        orphans = [p for p in repl.plan(origin, dest, P) if p.state is repl.State.ORPHAN]
        assert [(p.rel, p.rename_to) for p in orphans] == [("rpool/old", "rpool/new")]

    def test_rename_needs_the_new_parent_on_the_backup(self) -> None:
        """No rename into a parent the backup does not have yet."""
        origin = {
            "rpool": repl.Origin("rpool", snaps=[_ref(P, "9", 9)]),
            "rpool/a": repl.Origin("rpool/a", snaps=[_ref(P, "8", 8)]),
            "rpool/a/x": repl.Origin("rpool/a/x", snaps=[_ref("s", "4", 4), _ref(P, "10", 10)]),
        }
        dest = {
            "rpool": repl.Dest("rpool", snaps=[_ref(P, "9", 9)]),
            "rpool/x": repl.Dest("rpool/x", snaps=[_ref("s", "4", 4)]),
        }
        orphans = [p for p in repl.plan(origin, dest, P) if p.state is repl.State.ORPHAN]
        assert [(p.rel, p.rename_to) for p in orphans] == [("rpool/x", "")]

    def test_reached_is_every_expected_dataset(self) -> None:
        """I-D: one dataset short of the point is not success."""
        at = repl.DatasetPlan("rpool", repl.State.AT_POINT)
        orphan = repl.DatasetPlan("rpool/gone", repl.State.ORPHAN)
        zvol = repl.DatasetPlan("rpool/vm", repl.State.EXCLUDED)
        assert repl.reached([at, orphan, zvol])
        assert not repl.reached([at, repl.DatasetPlan("rpool/x", repl.State.DIVERGED)])


# ── Replication engine against the in-memory ZFS model (M2) ────────────────


def _origin_fixture(fake: FakeZfs) -> None:
    """eli-like origin: encrypted rpool tree, keystore zvol, a VM zvol, bpool."""
    fake.pool("rpool")
    fake.pool("bpool", bookmark_v2="disabled")
    for name, props in (
        ("rpool/ROOT", {"canmount": "off", "mountpoint": "none"}),
        ("rpool/ROOT/be", {"canmount": "on", "mountpoint": "/"}),
        ("rpool/USERDATA", {"canmount": "off", "mountpoint": "none"}),
        ("rpool/USERDATA/home", {"canmount": "on", "mountpoint": "/home"}),
        ("rpool/var", {"canmount": "off", "mountpoint": "/var"}),
        ("rpool/var/lib", {"canmount": "off", "mountpoint": "/var/lib"}),
        ("rpool/var/lib/docker", {"canmount": "on", "mountpoint": "/var/lib/docker"}),
        ("bpool/BOOT", {"canmount": "off", "mountpoint": "none"}),
        ("bpool/BOOT/be", {"canmount": "on", "mountpoint": "/boot"}),
    ):
        fake.dataset(name, **props)
    fake.dataset("rpool/keystore", kind="volume", encryption="off")
    fake.dataset("rpool/vm", kind="volume")
    for ds in list(fake.ds):
        if fake.ds[ds].kind == "filesystem":
            fake.snap(ds, "autosnap_2026-10-01_00:00:00_daily")


def _tick(fake: FakeZfs, label: str) -> None:
    """sanoid takes a new snapshot of every filesystem."""
    for ds in list(fake.ds):
        if ds.split("/")[0] in ("rpool", "bpool") and fake.ds[ds].kind == "filesystem":
            fake.snap(ds, f"autosnap_{label}")


def _prune_all_autosnaps(fake: FakeZfs) -> None:
    """Origin loses every sanoid snapshot (gap beyond every bucket, or a % range)."""
    for ds in list(fake.ds):
        if ds.split("/")[0] in ("rpool", "bpool"):
            fake.ds[ds].snaps = [s for s in fake.ds[ds].snaps if not s.name.startswith("autosnap")]


def _engine_run(
    fake: FakeZfs,
    pool: str,
    guid: str,
    point: str,
    decide: "engine.Decide | None" = None,
) -> "engine.Result":
    with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        log = Log()
        eng = engine.Run(pool, guid, log, point, sleep=lambda _s: None)
        cleanup = Cleanup(log)
        return engine.execute(eng, cleanup, decide or (lambda _p, _r: True))


def _origin_points(fake: FakeZfs) -> list[str]:
    return sorted(
        f"{ds}@{s.name}"
        for ds, d in fake.ds.items()
        if ds.split("/")[0] in ("rpool", "bpool")
        for s in d.snaps
        if repl.POINT_RE.match(s.name)
    )


def _fresh_fake(pools: tuple[str, ...] = ("backup",)) -> FakeZfs:
    """The origin fixture plus empty backup pools."""
    fake = FakeZfs()
    _origin_fixture(fake)
    for p in pools:
        fake.pool(p)
    return fake


TODAY = datetime.now(UTC).strftime("%Y%m%d")
P1, P2, P3 = (
    "zark_2026-10-07_10:00:00Z",
    "zark_2026-10-08_10:00:00Z",
    "zark_2026-10-09_10:00:00Z",
)


class TestSharedSnapshots:
    """M3 decision 5e: what a drive shares with origin besides its anchors."""

    def test_newest_of_each_type_and_the_newest_residue(self) -> None:
        """Per dataset: the newest shared snapshot of each sanoid type and
        of the syncoid residues, oldest first; points, anchors, bookmarks,
        hand-made snapshots and snapshots only one side holds are left out."""
        snaps = [
            _ref("syncoid_eli_2026-09-01:10:00:00-GMT02:00", "1", 1),
            _ref("autosnap_2026-09-02_00:00:00_monthly", "2", 2),
            _ref("syncoid_eli_2026-09-03:10:00:00-GMT02:00", "3", 3),
            _ref("autosnap_2026-10-01_00:00:00_daily", "4", 4),
            _ref("autosnap_2026-10-02_00:00:00_daily", "5", 5),
            _ref("zark_2026-10-02_12:00:00Z", "6", 6),
            _ref("zark_111_2026-10-02_12:00:00Z", "7", 7),
            _ref("manual", "8", 8),
            _ref("autosnap_2026-10-03_00:00:00_hourly", "9", 9),
        ]
        origin = {
            "rpool/var": repl.Origin("rpool/var", snaps=snaps, bookmarks=[_ref("x", "6", 6)]),
            "rpool/keystore": repl.Origin("rpool/keystore", kind="volume", snaps=snaps[:2]),
        }
        on_drive = [s for s in snaps if s.guid != "9"]  # the hourly never reached it
        dest = {
            "rpool/var": repl.Dest("rpool/var", snaps=on_drive),
            "rpool/keystore": repl.Dest("rpool/keystore", snaps=snaps[:2]),
        }
        shared = repl.shared_snapshots(origin, dest)
        assert list(shared) == ["rpool/var"]
        assert [r.name for r in shared["rpool/var"]] == [
            "autosnap_2026-09-02_00:00:00_monthly",
            "syncoid_eli_2026-09-03:10:00:00-GMT02:00",
            "autosnap_2026-10-02_00:00:00_daily",
        ]

    def test_a_dataset_the_drive_lacks_or_holds_only_points_of(self) -> None:
        """A full send carries only the point: nothing is shared."""
        origin = {
            "rpool/var": repl.Origin(
                "rpool/var", snaps=[_ref("zark_2026-10-02_12:00:00Z", "6", 6)]
            ),
            "rpool/zt": repl.Origin(
                "rpool/zt", snaps=[_ref("autosnap_2026-10-02_00:00:00_daily", "5", 5)]
            ),
        }
        dest = {
            "rpool/var": repl.Dest("rpool/var", snaps=[_ref("zark_2026-10-02_12:00:00Z", "6", 6)])
        }
        assert repl.shared_snapshots(origin, dest) == {"rpool/var": [], "rpool/zt": []}

    def test_kind(self) -> None:
        """sanoid's types by name suffix, syncoid residues by prefix."""
        assert repl.shared_kind("autosnap_2026-10-02_00:00:00_frequently") == "frequently"
        assert repl.shared_kind("autosnap_2026-10-02_00:00:00_yearly") == "yearly"
        assert repl.shared_kind("syncoid_carmen_2026-09-27:01:00:00-GMT02:00") == "syncoid"
        assert repl.shared_kind("autosnap_2026-10-02_00:00:00_bogus") == ""
        assert repl.shared_kind("zark_2026-10-02_12:00:00Z") == ""


class TestEngine:  # pylint: disable=too-many-public-methods
    """The engine end to end against FakeZfs (redesign §4, I-A…I-G)."""

    def _fresh(self, pools: tuple[str, ...] = ("backup",)) -> FakeZfs:
        return _fresh_fake(pools)

    def test_first_run_brings_every_dataset_to_the_point(self) -> None:
        """NEW everywhere; keystore and zvols stay out; I-D holds."""
        fake = self._fresh()
        res = _engine_run(fake, "backup", "111", P1)
        assert res.ok, res
        assert P1 in fake.names("backup/rpool/var/lib/docker")
        assert P1 in fake.names("backup/bpool/BOOT/be")
        assert "backup/rpool/keystore" not in fake.ds
        assert "backup/rpool/vm" not in fake.ds
        excluded = [p.rel for p in res.plans if p.state is repl.State.EXCLUDED]
        assert excluded == ["rpool/vm"]

    def test_points_atomic_per_pool(self) -> None:
        """I-C: one snapshot call per pool, every expected dataset in it."""
        fake = self._fresh()
        _ = _engine_run(fake, "backup", "111", P1)
        snaps = [c for c in fake.calls if c.startswith("zfs snapshot")]
        assert len(snaps) == 2
        assert all(f"@{P1}" in c for c in snaps)
        assert "rpool/var/lib/docker@" in snaps[0] and "rpool/keystore@" not in snaps[0]
        assert "'rpool@" not in snaps[0] and "rpool/ROOT@" not in snaps[0]  # structural
        assert snaps[1].count("bpool") == 1

    def test_origin_keeps_only_bookmarks_and_bpool_anchors(self) -> None:
        """I-B, I-F: no point left in origin; bookmarks on rpool, none on bpool."""
        fake = self._fresh()
        _ = _engine_run(fake, "backup", "111", P1)
        assert _origin_points(fake) == []
        anchor = repl.anchor_name("111", P1)
        assert fake.bookmarks("rpool/USERDATA/home") == [anchor]
        assert all(not fake.bookmarks(ds) for ds in fake.ds if ds.startswith("bpool"))
        assert anchor in fake.names("bpool/BOOT/be")

    def test_destination_mount_properties(self) -> None:
        """Decision 1: canmount=noauto, inherited mountpoint, origin values as org.zark:*."""
        fake = self._fresh()
        _ = _engine_run(fake, "backup", "111", P1)
        props = fake.ds["backup/rpool/var"].props
        assert props["canmount"] == "noauto"
        assert "mountpoint" not in props
        assert (props["org.zark:canmount"], props["org.zark:mountpoint"]) == ("off", "/var")
        boot = fake.ds["backup/bpool/BOOT/be"].props
        assert (boot["canmount"], boot["org.zark:mountpoint"]) == ("noauto", "/boot")

    def test_second_run_via_bookmark_keeps_every_point(self) -> None:
        """G3/I-A: after origin lost every common snapshot, the next run is
        incremental (bookmark on rpool, anchor on bpool) and the backup keeps
        the first point."""
        fake = self._fresh()
        _ = _engine_run(fake, "backup", "111", P1)
        _tick(fake, "2026-10-07_12:00:00_hourly")
        _prune_all_autosnaps(fake)
        before = {ds: fake.names(ds) for ds in fake.ds if ds.startswith("backup/")}
        res = _engine_run(fake, "backup", "111", P2)
        assert res.ok, res
        states = {r: o.ok for r, o in res.outcomes.items()}
        assert all(states.values())
        for ds, names in before.items():
            assert fake.names(ds)[: len(names)] == names, ds  # nothing removed (I-A)
        assert P2 in fake.names("backup/rpool/USERDATA/home")
        assert fake.bookmarks("rpool/USERDATA/home") == [repl.anchor_name("111", P2)]
        assert any(" -i 'rpool/USERDATA/home#zark_111_" in c for c in fake.calls)

    def test_carried_intermediates(self) -> None:
        """D15: sanoid snapshots between the base and the point travel with -I."""
        fake = self._fresh()
        _ = _engine_run(fake, "backup", "111", P1)
        _tick(fake, "2026-10-07_12:00:00_hourly")
        res = _engine_run(fake, "backup", "111", P2)
        assert res.ok
        assert "autosnap_2026-10-07_12:00:00_hourly" in fake.names("backup/rpool/ROOT/be")

    def test_two_disks_keep_their_own_anchors(self) -> None:
        """§8 test 1: rotation with a gap beyond every bucket on both disks."""
        fake = self._fresh(("black", "blue"))
        assert _engine_run(fake, "black", "1", P1).ok
        assert _engine_run(fake, "blue", "2", P2).ok
        _prune_all_autosnaps(fake)
        assert _engine_run(fake, "black", "1", P3).ok
        marks = fake.bookmarks("rpool/ROOT/be")
        assert sorted(marks) == sorted([repl.anchor_name("2", P2), repl.anchor_name("1", P3)])
        assert P1 in fake.names("black/rpool/ROOT/be") and P3 in fake.names("black/rpool/ROOT/be")
        assert P2 in fake.names("blue/rpool/ROOT/be")

    def test_interrupted_transfer_resumes(self) -> None:
        """§8 test 3: a cut stream leaves a token and the point; the next run
        finishes it, reaches its own point and drops the leftover."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        res = _engine_run(fake, "backup", "111", P2)
        assert not res.ok
        assert fake.ds["backup/rpool/USERDATA/home"].token
        assert _origin_points(fake) == [f"rpool/USERDATA/home@{P2}"]  # kept for the resume
        res = _engine_run(fake, "backup", "111", P3)
        assert res.ok, res
        home = fake.names("backup/rpool/USERDATA/home")
        assert P2 in home and P3 in home
        assert _origin_points(fake) == []

    def test_new_clone_is_sent_from_its_origin_on_the_drive(self) -> None:
        """O3: a NEW clone whose origin snapshot the drive holds is sent with
        -I from that origin and received as a clone of the drive's copy;
        only what the clone changed travels."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        assert _engine_run(fake, "backup", "111", P2).ok
        src = "rpool/ROOT/be@autosnap_2026-10-07_12:00:00_hourly"
        fake.dataset("rpool/ROOT/be2", origin=src, mountpoint="/")
        res = _engine_run(fake, "backup", "111", P3)
        assert res.ok, res
        pipe = next(c for c in fake.calls if "rpool/ROOT/be2@" in c and " | " in c)
        assert pipe.startswith(f"zfs send -w -I {src} rpool/ROOT/be2@{P3} | zfs receive")
        assert f"-o origin=backup/{src}" in pipe and " -u " in pipe
        assert fake.ds["backup/rpool/ROOT/be2"].props["origin"] == f"backup/{src}"
        assert fake.names("backup/rpool/ROOT/be2") == [P3]
        assert _engine_run(fake, "backup", "111", "zark_2026-10-10_10:00:00Z").ok

    def test_new_clone_estimate_is_its_delta(self) -> None:
        """M3 decision 7a (eli R7): the estimate of a clone sent from its
        origin uses -i, which OpenZFS sizes as a delta; -nvP -I would report
        the whole clone. The send stays -I."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        assert _engine_run(fake, "backup", "111", P2).ok
        src = "rpool/ROOT/be@autosnap_2026-10-07_12:00:00_hourly"
        fake.dataset("rpool/ROOT/be2", origin=src, mountpoint="/")
        with patch_sh(fake), redirect_stdout(StringIO()):
            eng = engine.Run("backup", "111", Log(), P3, sleep=lambda _s: None)
            assert eng.take_point()
            clone = next(p for p in eng.survey() if p.rel == "rpool/ROOT/be2")
            assert clone.clone_from
            before = len(fake.calls)
            assert eng.estimate(clone) == 4096
            assert eng.estimate(clone, full=True) == 10**9
        assert fake.calls[before] == f"zfs send -nvP -w -i {src} rpool/ROOT/be2@{P3}"
        assert eng._send_args(clone) == [f"-w -I {src} rpool/ROOT/be2@{P3}"]  # pylint: disable=protected-access

    def test_new_clone_without_its_origin_on_the_drive_is_sent_in_full(self) -> None:
        """O3: the origin snapshot never reached the drive: a full send, as before."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        src = "rpool/ROOT/be@autosnap_2026-10-07_12:00:00_hourly"
        fake.dataset("rpool/ROOT/be2", origin=src, mountpoint="/")
        with patch_sh(fake), redirect_stdout(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            assert eng.take_point()
            plans = {p.rel: p for p in eng.survey()}
        assert plans["rpool/ROOT/be2"].state is repl.State.NEW
        assert not plans["rpool/ROOT/be2"].clone_from
        assert eng._send_args(plans["rpool/ROOT/be2"]) == [f"-w rpool/ROOT/be2@{P2}"]  # pylint: disable=protected-access

    def test_a_clone_origin_is_matched_by_guid_not_by_name(self) -> None:
        """O3 (M3R-12): a drive snapshot that only shares the origin's name is
        not a base; the clone is sent in full."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        assert _engine_run(fake, "backup", "111", P2).ok
        src = "rpool/ROOT/be@autosnap_2026-10-07_12:00:00_hourly"
        copy = next(s for s in fake.ds["backup/rpool/ROOT/be"].snaps if s.name == src.split("@")[1])
        copy.guid = "not-the-origins-guid"
        fake.dataset("rpool/ROOT/be2", origin=src, mountpoint="/")
        with patch_sh(fake), redirect_stdout(StringIO()):
            eng = engine.Run("backup", "111", Log(), P3, sleep=lambda _s: None)
            assert eng.take_point()
            plans = {p.rel: p for p in eng.survey()}
        assert plans["rpool/ROOT/be2"].state is repl.State.NEW
        assert not plans["rpool/ROOT/be2"].clone_from
        assert eng._send_args(plans["rpool/ROOT/be2"]) == [f"-w rpool/ROOT/be2@{P3}"]  # pylint: disable=protected-access

    def test_fake_refuses_a_resume_onto_a_written_destination(self) -> None:
        """eli R10 (M2V-1): ZFS receives a resumed stream onto a head written
        since its newest snapshot, refuses it at the end and keeps the
        state; the fake does the same, with libzfs's two-line text."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        assert not _engine_run(fake, "backup", "111", P2).ok
        home = fake.ds["backup/rpool/USERDATA/home"]
        token = home.token
        home.written = 12 << 20  # what a read-write mount leaves
        with patch_sh(fake):
            r = _sh.run_pipe(f"zfs send -t {token}", "zfs receive -s -u backup/rpool/USERDATA/home")
        assert (
            not r.ok
            and "has been modified" in r.stderr
            and "since most recent snapshot" in r.stderr
        )
        assert home.token == token

    def test_stale_token_is_discarded_not_snapshots(self) -> None:
        """M2R-10: a partial receive whose stream snapshot is gone from
        origin is aborted (receive -A); the dataset is planned again and no
        destination snapshot is lost."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        assert not _engine_run(fake, "backup", "111", P2).ok
        token = fake.ds["backup/rpool/USERDATA/home"].token
        named = token.split("|")[2]
        before = fake.names("backup/rpool/USERDATA/home")
        home = fake.ds["rpool/USERDATA/home"]
        home.snaps = [s for s in home.snaps if s.name != named]
        res = _engine_run(fake, "backup", "111", P3)
        assert res.ok, res
        assert any(c.startswith("zfs receive -A") for c in fake.calls)
        assert fake.names("backup/rpool/USERDATA/home")[: len(before)] == before

    def test_partial_receive_whose_source_is_gone_is_discarded(self) -> None:
        """M2R-10: the token's incremental source pruned in origin: the
        partial state cannot resume and goes; no snapshot is lost."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)  # the hourly lands
        assert not _engine_run(fake, "backup", "111", P2).ok
        before = fake.names("backup/rpool/USERDATA/home")
        _prune_all_autosnaps(fake)
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P3, sleep=lambda _s: None)
            _ = engine.execute(eng, Cleanup(Log()), lambda _p, _r: True)
        assert "partial receive discarded: cannot resume send: incremental source" in out.getvalue()
        assert not fake.ds["backup/rpool/USERDATA/home"].token
        assert fake.names("backup/rpool/USERDATA/home") == before

    def test_stale_partial_is_discarded_before_the_questions(self) -> None:
        """M2F-11a (eli N7, after a recover): a partial receive that can never
        resume goes before the questions, so the decision its dataset then
        needs (ROLLBACK) is asked in the same run, not the next one."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)  # the hourly lands
        assert not _engine_run(fake, "backup", "111", P2).ok
        named = fake.ds["backup/rpool/USERDATA/home"].token.split("|")[2]
        home = fake.ds["rpool/USERDATA/home"]
        # A recover of P1: origin has neither the received hourly nor the token's snapshot.
        home.snaps = [
            s for s in home.snaps if s.name not in (named, "autosnap_2026-10-07_12:00:00_hourly")
        ]
        asked: list[repl.State] = []

        def decide(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            p = next(q for q in plans if q.rel == "rpool/USERDATA/home")
            asked.append(p.state)
            return p.state is not repl.State.ROLLBACK or eng.rollback(p)

        res = _engine_run(fake, "backup", "111", P3, decide)
        assert asked == [repl.State.ROLLBACK]
        assert res.ok, res
        assert P3 in fake.names("backup/rpool/USERDATA/home")

    def test_partial_receive_kept_when_the_resume_fails_otherwise(self) -> None:
        """M2R-10 (E8): an I/O error on resume keeps the partial state."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "h1")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        assert not _engine_run(fake, "backup", "111", P2).ok
        token = fake.ds["backup/rpool/USERDATA/home"].token
        real_pipe = fake.mock_run_pipe
        failed: list[str] = []

        def pipe(cmd1: str, cmd2: str, log: object = None) -> RunResult:
            if " -t " in cmd1 and not failed:
                failed.append(cmd1)
                return RunResult(1, "", "cannot receive resume stream: I/O error\n", "")
            return real_pipe(cmd1, cmd2, log)

        fake.mock_run_pipe = pipe  # type: ignore[method-assign]
        res = _engine_run(fake, "backup", "111", P3)
        assert not res.ok
        assert not any(c.startswith("zfs receive -A") for c in fake.calls)
        assert fake.ds["backup/rpool/USERDATA/home"].token == token
        assert "partial receive kept" in res.outcomes["rpool/USERDATA/home"].error

    def test_enospc_destroys_nothing(self) -> None:
        """§8 test 4: per-dataset failure, nothing removed, not success."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        before = {ds: fake.names(ds) for ds in fake.ds if ds.startswith("backup/")}
        fake.inject["backup/rpool/ROOT/be"] = ("enospc", 0)
        res = _engine_run(fake, "backup", "111", P2)
        assert not res.ok and res.enospc
        for ds, names in before.items():
            assert fake.names(ds)[: len(names)] == names
        assert fake.bookmarks("rpool/ROOT/be") == [repl.anchor_name("111", P1)]  # unchanged

    def test_child_of_a_failed_new_parent_waits(self) -> None:
        """A NEW child is not attempted when its parent failed."""
        fake = self._fresh()
        fake.inject["backup/rpool/var"] = ("interrupt", 0)
        res = _engine_run(fake, "backup", "111", P1)
        assert not res.ok
        assert res.outcomes["rpool/var/lib"].error == "parent did not reach the point"

    def test_rollback_needs_a_decision(self) -> None:
        """H23/§7: with only an older common snapshot left, nothing transfers
        unless the user chooses; choosing rollback destroys only the listed ones."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "h1")
        assert _engine_run(fake, "backup", "111", P2).ok
        # A 1.0.12-style backup: the destination's newest is a snapshot origin
        # no longer has, no bookmark of it exists, an older one is common.
        fake.ds["rpool/ROOT/be"].bookmarks.clear()
        fake.snap("backup/rpool/ROOT/be", "autosnap_gone_hourly")
        res = _engine_run(fake, "backup", "111", P3)
        assert not res.ok
        assert "autosnap_gone_hourly" in fake.names("backup/rpool/ROOT/be")
        rb = [p for p in res.plans if p.state is repl.State.ROLLBACK]
        assert [(p.rel, p.common) for p in rb] == [("rpool/ROOT/be", "@autosnap_h1")]

        def roll(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            for p in plans:
                if p.state is repl.State.ROLLBACK:
                    assert p.newer == [f"@{P2}", "@autosnap_gone_hourly"]
                    assert eng.rollback(p)
            return True

        p4 = "zark_2026-10-10_10:00:00Z"
        assert _engine_run(fake, "backup", "111", p4, roll).ok
        names = fake.names("backup/rpool/ROOT/be")
        assert "autosnap_gone_hourly" not in names and P2 not in names
        assert P1 in names and p4 in names

    def test_diverged_archive_keeps_the_old_lineage(self) -> None:
        """D17: archiving renames the old lineage; the dataset is resent in full."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.ds["rpool/var/lib/docker"].snaps.clear()  # recreated in origin
        fake.ds["rpool/var/lib/docker"].bookmarks.clear()
        fake.snap("rpool/var/lib/docker", "autosnap_new")
        when = datetime(2026, 10, 9, tzinfo=UTC)

        def archive(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            for p in plans:
                if p.state is repl.State.DIVERGED:
                    assert eng.archive(p, when)
            return True

        res = _engine_run(fake, "backup", "111", P3, archive)
        assert res.ok, res
        assert P1 in fake.names("backup/rpool/var/lib/docker.archived-20261009")
        assert fake.names("backup/rpool/var/lib/docker") == [P3]

    def test_orphan_kept_is_not_asked_again_and_rename_followed(self) -> None:
        """Decision 18: keep marks the orphan; a rename in origin is followed."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.ds["rpool/USERDATA/home2"] = fake.ds.pop("rpool/USERDATA/home")
        fake.ds.pop("rpool/var/lib/docker")

        def decide(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            for p in plans:
                if p.state is repl.State.ORPHAN and p.rename_to:
                    assert (p.rel, p.rename_to) == ("rpool/USERDATA/home", "rpool/USERDATA/home2")
                    assert eng.follow_rename(p)
                elif p.state is repl.State.ORPHAN:
                    assert p.rel == "rpool/var/lib/docker"
                    assert eng.keep_orphan(p, "2026-10-09")
            return True

        res = _engine_run(fake, "backup", "111", P3, decide)
        assert res.ok, res
        home2 = fake.names("backup/rpool/USERDATA/home2")
        assert P1 in home2 and P3 in home2
        kept = [p for p in res.plans if p.state is repl.State.ORPHAN]
        assert [(p.rel, p.kept) for p in kept] == [("rpool/var/lib/docker", True)]

    def test_sanoid_paused_and_restored(self) -> None:
        """§8 test 10: the timer is stopped for the run and started after;
        a timer the user had stopped stays stopped."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        assert "systemctl stop sanoid.timer" in fake.calls
        assert fake.units["sanoid.timer"] == "active"
        fake.units["sanoid.timer"] = "inactive"
        assert _engine_run(fake, "backup", "111", P2).ok
        assert fake.units["sanoid.timer"] == "inactive"

    def _sanoid_ending(self, ending: str) -> FakeZfs:
        """One run whose sanoid wait ends with ``ending``, then the cleanup."""
        fake = self._fresh()
        fake.units["sanoid.service"] = "activating"

        def sleep(_s: float) -> None:
            if ending == "ctrl-c":
                raise KeyboardInterrupt
            if ending == "exit":
                raise SystemExit(143)

        with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            log = Log()
            cleanup = Cleanup(log)
            eng = engine.Run("backup", "111", log, P1, sleep=sleep)
            try:
                res = engine.execute(eng, cleanup, lambda _p, _r: True)
                assert res.aborted and "sanoid" in res.error
            except (KeyboardInterrupt, SystemExit):
                pass
            cleanup.run()
            assert not Path(engine.SANOID_MARKER).exists()
        return fake

    def test_sanoid_timer_back_on_every_ending_of_the_wait(self) -> None:
        """M2R-8 (E5): Ctrl-C, a fatal exit or the timeout while waiting for a
        running sanoid leave the timer active and no point."""
        for ending in ("ctrl-c", "exit", "timeout"):
            fake = self._sanoid_ending(ending)
            assert fake.units["sanoid.timer"] == "active", ending
            assert _origin_points(fake) == [], ending

    def test_waits_for_a_running_sanoid(self) -> None:
        """The point is taken only once sanoid's take/prune has finished,
        and the wait says so."""
        fake = self._fresh()
        fake.units["sanoid.service"] = "activating"
        waited: list[float] = []

        def sleep(s: float) -> None:
            waited.append(s)
            fake.units["sanoid.service"] = "inactive"

        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P1, sleep=sleep)
            assert engine.execute(eng, Cleanup(Log()), lambda _p, _r: True).ok
        assert waited
        assert "Waiting for sanoid to finish" in out.getvalue()

    def test_timer_left_stopped_by_a_dead_run_is_started(self) -> None:
        """A marker with the timer inactive: an earlier run died without its
        cleanup; this run starts the timer at its end and says so."""
        fake = self._fresh()
        fake.units["sanoid.timer"] = "inactive"
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            marker = Path(engine.SANOID_MARKER)
            marker.parent.mkdir(parents=True, exist_ok=True)
            _ = marker.write_text(P1 + "\n", encoding="ascii")
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            assert engine.execute(eng, Cleanup(Log()), lambda _p, _r: True).ok
            assert not marker.exists()
        assert fake.units["sanoid.timer"] == "active"
        assert "left stopped by an earlier zark run" in out.getvalue()

    def test_marker_present_while_the_timer_is_stopped(self) -> None:
        """The marker exists exactly while this run holds the timer stopped."""
        fake = self._fresh()
        seen: list[bool] = []

        def decide(_p: list[repl.DatasetPlan], _r: engine.Run) -> bool:
            seen.append(Path(engine.SANOID_MARKER).exists())
            return True

        with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P1, sleep=lambda _s: None)
            assert engine.execute(eng, Cleanup(Log()), decide).ok
            assert not Path(engine.SANOID_MARKER).exists()
        assert seen == [True]

    def test_no_point_left_when_bpool_snapshot_fails(self) -> None:
        """M2R-11 (E5): rpool's point is taken, bpool's fails: nothing is left."""
        fake = self._fresh()
        fake.fail_cmds.add("zfs snapshot bpool")
        res = _engine_run(fake, "backup", "111", P1)
        assert res.aborted
        assert _origin_points(fake) == []
        assert fake.units["sanoid.timer"] == "active"

    def test_abort_drops_the_point(self) -> None:
        """A run the user aborts leaves no point in origin and nothing on the backup."""
        fake = self._fresh()
        res = _engine_run(fake, "backup", "111", P1, lambda _p, _r: False)
        assert res.aborted
        assert _origin_points(fake) == []
        assert "backup/rpool" not in fake.ds

    def test_structural_containers_created_not_replicated(self) -> None:
        """Decision 13: containers exist on the backup, off and unmounted, with
        no snapshot; origin's containers get no point."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        for rel in repl.STRUCTURAL:
            d = fake.ds[f"backup/{rel}"]
            assert (d.props["canmount"], d.props["mountpoint"]) == ("off", "none"), rel
            assert not d.snaps, rel
            assert not fake.bookmarks(rel), rel
        assert P1 not in fake.names("rpool/ROOT")

    def test_backup_after_recover(self) -> None:
        """recover creates the containers anew (new guids): the next backup
        is incremental for the data and never asks about the containers."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        for rel in repl.STRUCTURAL:
            fake.ds[rel].snaps = []
            fake.ds[rel].bookmarks = []
            fake.snap(rel, "autosnap_after_recover")

        def no_questions(plans: list[repl.DatasetPlan], _eng: engine.Run) -> bool:
            asked = [
                p.rel
                for p in plans
                if p.state not in repl.TRANSFERABLE and p.state is not repl.State.EXCLUDED
            ]
            assert not asked, asked
            return True

        res = _engine_run(fake, "backup", "111", P2, no_questions)
        assert res.ok, res
        assert P1 in fake.names("backup/rpool/ROOT/be") and P2 in fake.names("backup/rpool/ROOT/be")

    def test_structural_container_with_data_is_reported(self) -> None:
        """A container that holds or could hold data is listed, not silently skipped."""
        fake = self._fresh()
        fake.ds["rpool/ROOT"].props["referenced"] = str(50 * 1024 * 1024)
        fake.ds["rpool/USERDATA"].props.update(canmount="on", mountpoint="/userdata")
        res = _engine_run(fake, "backup", "111", P1)
        assert res.ok
        notes = {p.rel: p.note for p in res.plans if p.state is repl.State.EXCLUDED}
        assert "holding data" in notes["rpool/ROOT"]
        assert "mountable at /userdata" in notes["rpool/USERDATA"]

    def test_failed_dataset_keeps_its_base(self) -> None:
        """After a recover the base is a point snapshot, with no bookmark. A
        dataset that fails keeps it, so the next run is still incremental."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        home = "rpool/USERDATA/home"
        fake.ds[home].bookmarks.clear()
        base = next(s for s in fake.ds[f"backup/{home}"].snaps if s.name == P1)
        fake.ds[home].snaps.append(FSnap(P1, base.guid, 1, base.creation))  # as recover leaves it
        fake.inject[f"backup/{home}"] = ("enospc", 0)
        assert not _engine_run(fake, "backup", "111", P2).ok
        assert P1 in fake.names(home)
        res = _engine_run(fake, "backup", "111", P3)
        assert res.ok, res
        assert P1 not in fake.names(home)  # superseded once the dataset is anchored

    def test_failed_anchor_keeps_point_and_old_anchor(self) -> None:
        """No verified new anchor: neither the point nor the old anchor goes."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.fail_cmds.add("zfs bookmark rpool/ROOT/be@")
        res = _engine_run(fake, "backup", "111", P2)
        assert "rpool/ROOT/be" in res.anchor_failed
        assert P2 in fake.names("rpool/ROOT/be")
        assert fake.bookmarks("rpool/ROOT/be") == [repl.anchor_name("111", P1)]
        fake.fail_cmds.clear()
        assert _engine_run(fake, "backup", "111", P3).ok
        assert fake.bookmarks("rpool/ROOT/be") == [repl.anchor_name("111", P3)]
        assert _origin_points(fake) == []

    def test_nothing_dropped_when_the_backup_cannot_be_read(self) -> None:
        """A listing of the backup that fails when drop_check reads it means
        no removal in origin at all."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            real_check = eng.drop_check

            def check() -> list[engine.Verdict]:
                fake.fail_cmds.add("-o name,guid,createtxg,creation -r backup/rpool")
                return real_check()

            eng.drop_check = check  # type: ignore[method-assign]
            _ = engine.execute(eng, Cleanup(Log()), lambda _p, _r: True)
        assert "Nothing removed in origin" in out.getvalue()
        assert P2 in fake.names("rpool/ROOT/be")
        assert repl.anchor_name("111", P1) in fake.bookmarks("rpool/ROOT/be")

    def _stopped_before_the_questions(self, failing: str) -> None:
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "h1")
        fake.fail_cmds.add(failing)
        asked: list[str] = []

        def decide(plans: list[repl.DatasetPlan], _r: engine.Run) -> bool:
            asked.extend(p.rel for p in plans)
            return True

        res = _engine_run(fake, "backup", "111", P2, decide)
        assert res.aborted and "could not" in res.error, failing
        assert not asked
        assert _origin_points(fake) == []
        assert repl.anchor_name("111", P1) in fake.bookmarks("rpool/ROOT/be")

    def test_a_listing_that_fails_stops_the_run_before_the_questions(self) -> None:
        """M2R-4 (E6a): what origin or the drive "has" is never a shrunken
        listing; the run stops, asks nothing and leaves only the anchors."""
        self._stopped_before_the_questions("-o name,guid,createtxg,creation -r backup/rpool")
        self._stopped_before_the_questions(
            "-o name,type,canmount,mountpoint,referenced,origin -r rpool"
        )

    def test_a_listing_that_fails_after_the_transfers_is_not_success(self) -> None:
        """M2R-4 (E6b): the final check cannot read origin: not complete,
        nothing anchored, nothing removed."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "h1")
        with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            real_transfer = eng.transfer
            done: list[str] = []

            def transfer(p: repl.DatasetPlan) -> engine.Outcome:
                o = real_transfer(p)
                done.append(p.rel)
                if len(done) == 6:
                    fake.fail_cmds.add(
                        "-o name,type,canmount,mountpoint,referenced,origin -r rpool"
                    )
                return o

            eng.transfer = transfer  # type: ignore[method-assign]
            res = engine.execute(eng, Cleanup(Log()), lambda _p, _r: True)
        assert not res.ok and "could not read" in res.unverified
        assert repl.anchor_name("111", P2) not in fake.bookmarks("rpool/ROOT/be")
        assert P2 in fake.names("rpool/ROOT/be")  # still a base for the next run

    def test_an_unreadable_final_check_is_not_success(self) -> None:
        """M2V-5 (N13): a result whose final check could not read either side
        is not complete, even when the plans it holds are all at the point."""
        plans = [
            repl.DatasetPlan("rpool/ROOT/be", repl.State.AT_POINT),
            repl.DatasetPlan("bpool/BOOT/be", repl.State.AT_POINT),
        ]
        assert engine.Result(plans=plans).ok
        assert not engine.Result(plans=plans, unverified="could not read: zfs list").ok

    def test_point_not_taken_when_origin_cannot_be_read(self) -> None:
        """M2V-5 (N21): take_point reads origin strictly; a failed listing of
        rpool takes no point at all (not bpool's alone)."""
        fake = self._fresh()
        fake.fail_cmds.add("-o name,type,canmount,mountpoint,referenced,origin -r rpool")
        with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P1, sleep=lambda _s: None)
            assert not eng.take_point()
        assert not any(c.startswith("zfs snapshot") for c in fake.calls)
        assert _origin_points(fake) == []

    def test_partial_state_kept_when_the_dry_run_fails_otherwise(self) -> None:
        """M2V-5 (N42): a resume whose dry run fails for a reason other than a
        snapshot or source that is gone (an I/O error) keeps the partial state:
        no `zfs receive -A`, and the transfer resumes."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        assert not _engine_run(fake, "backup", "111", P2).ok
        assert fake.ds["backup/rpool/USERDATA/home"].token
        fake.fail_cmds.add("zfs send -nvP -t")
        res = _engine_run(fake, "backup", "111", P3)
        assert not any(c.startswith("zfs receive -A") for c in fake.calls)
        assert res.ok, res
        home = fake.names("backup/rpool/USERDATA/home")
        assert P2 in home and P3 in home

    def test_a_pool_that_is_gone_is_not_an_empty_one(self) -> None:
        """M2R-3 (E3b): the backup pool exported under the run before the final
        survey: no point in origin is removed as "not on the backup"."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-02_08:00:00_hourly")
        hidden: dict[str, FDataset] = {}
        with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            real_transfer = eng.transfer
            done: list[str] = []

            def transfer(p: repl.DatasetPlan) -> engine.Outcome:
                o = real_transfer(p)
                done.append(p.rel)
                if len(done) == 6:
                    for name in [n for n in fake.ds if n == "backup" or n.startswith("backup/")]:
                        hidden[name] = fake.ds.pop(name)
                    fake.pools.discard("backup")
                return o

            eng.transfer = transfer  # type: ignore[method-assign]
            res = engine.execute(eng, Cleanup(Log()), lambda _p, _r: True)
        assert not res.ok
        assert sorted(p.split("@")[0] for p in _origin_points(fake)) == sorted(done)
        fake.ds.update(hidden)
        fake.pools.add("backup")
        _prune_all_autosnaps(fake)
        res = _engine_run(fake, "backup", "111", P3)
        assert res.ok, res  # every dataset incremental from P2, no rollback

    def _drive_changes_before_drop_check(self, what: str) -> None:
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            real_anchor = eng.anchor

            def anchor_then_lose(at_point: list[str]) -> list[str]:
                failed = real_anchor(at_point)
                names = [n for n in fake.ds if n == "backup" or n.startswith("backup/")]
                if what == "dataset":
                    names = ["backup/rpool/var/lib/docker"]
                for name in names:
                    _ = fake.ds.pop(name)
                if what == "pool":
                    fake.pools.discard("backup")
                return failed

            eng.anchor = anchor_then_lose  # type: ignore[method-assign]
            _ = engine.execute(eng, Cleanup(Log()), lambda _p, _r: True)
        assert "Nothing removed in origin" in out.getvalue(), what
        assert P2 in fake.names("rpool/ROOT/be"), what
        assert repl.anchor_name("111", P1) in fake.bookmarks("rpool/ROOT/be"), what

    def test_drop_check_stops_when_the_drive_changed_under_the_run(self) -> None:
        """M2R-3: the pool exported after the anchors and before drop_check
        (E3), or a dataset gone from it: nothing is removed in origin."""
        self._drive_changes_before_drop_check("pool")
        self._drive_changes_before_drop_check("dataset")

    def test_bpool_not_imported_stops_the_run(self) -> None:
        """M2R-4 (E4a): a drive holding bpool's backup while origin's bpool is
        not imported: no "only on the backup" question, no COMPLETED."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        for n in [n for n in fake.ds if n == "bpool" or n.startswith("bpool/")]:
            fake.ds.pop(n)
        fake.pools.discard("bpool")
        asked: list[str] = []

        def decide(plans: list[repl.DatasetPlan], _r: engine.Run) -> bool:
            asked.extend(p.rel for p in plans)
            return True

        res = _engine_run(fake, "backup", "111", P2, decide)
        assert res.aborted and "bpool is not imported" in res.error
        assert not asked
        assert _origin_points(fake) == []

    def test_every_verdict_is_logged(self) -> None:
        """Each removal and each conservation says why."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            _ = engine.execute(eng, Cleanup(Log()), lambda _p, _r: True)
        text = out.getvalue()
        assert f"kept rpool/USERDATA/home@{P2}: partial receive to resume" in text
        assert f"removed rpool/ROOT/be@{P2}: on the backup, anchored as" in text
        assert f"removed rpool/ROOT/be#{repl.anchor_name('111', P1)}: superseded by" in text

    def test_drop_disk_anchors(self) -> None:
        """registry forget: one disk's anchors go, the other's stay."""
        fake = self._fresh(("black", "blue"))
        assert _engine_run(fake, "black", "1", P1).ok
        assert _engine_run(fake, "blue", "2", P2).ok
        with patch_sh(fake), redirect_stdout(StringIO()):
            n, failed = engine.drop_disk_anchors("1", ["rpool", "bpool"], Log())
        assert n > 0 and not failed
        assert fake.bookmarks("rpool/ROOT/be") == [repl.anchor_name("2", P2)]
        assert repl.anchor_name("1", P1) not in fake.names("bpool/BOOT/be")
        assert repl.anchor_name("2", P2) in fake.names("bpool/BOOT/be")

    def test_every_receive_is_unmounted(self) -> None:
        """I-G in the engine: every receive carries -u (new, incremental, via
        bookmark and resume)."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-08_08:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        assert not _engine_run(fake, "backup", "111", P2).ok
        assert _engine_run(fake, "backup", "111", P3).ok
        _tick(fake, "2026-10-09_12:00:00_hourly")
        fake.ds["rpool/var"].snaps = [s for s in fake.ds["rpool/var"].snaps if s.name != P3]
        assert _engine_run(fake, "backup", "111", "zark_2026-10-10_10:00:00Z").ok
        assert any("-i 'rpool/var#" in c for c in fake.calls)  # via bookmark ran
        receives = [c.split(" | ", 1)[1] for c in fake.calls if " | zfs receive " in c]
        assert any("-t " in c for c in fake.calls if " | " in c)  # a resume ran
        assert receives and all(" -u " in r for r in receives), receives

    def test_removals_in_origin_are_by_exact_name(self) -> None:
        """No removal in origin is recursive: `zfs destroy -r ds@snap` would
        also take that name on every descendant."""
        fake = self._fresh(("black", "blue"))
        assert _engine_run(fake, "black", "1", P1).ok
        assert _engine_run(fake, "blue", "2", P2).ok
        assert _engine_run(fake, "black", "1", P3).ok
        with patch_sh(fake), redirect_stdout(StringIO()):
            assert engine.drop_disk_anchors("2", ["rpool", "bpool"], Log())[0] > 0
        destroys = [
            c
            for c in fake.calls
            if c.startswith("zfs destroy")
            and c.split()[-1].strip("'").split("/")[0] in ("rpool", "bpool")
        ]
        assert destroys and not any(" -r " in c for c in destroys), destroys

    def test_old_anchor_kept_when_the_new_one_is_not_on_the_backup(self) -> None:
        """drop_check verifies the new anchor by guid on the backup: when the
        drive lost the point after the transfer, the previous anchor stays."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        old = repl.anchor_name("111", P1)
        with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            real_anchor = eng.anchor

            def anchor_then_lose(at_point: list[str]) -> list[str]:
                failed = real_anchor(at_point)
                d = fake.ds["backup/rpool/var"]
                d.snaps = [s for s in d.snaps if s.name != P2]
                return failed

            eng.anchor = anchor_then_lose  # type: ignore[method-assign]
            _ = engine.execute(eng, Cleanup(Log()), lambda _p, _r: True)
        assert old in fake.bookmarks("rpool/var")
        assert old not in fake.bookmarks("rpool/ROOT/be")  # verified there: superseded

    def test_interrupted_run_keeps_the_point_where_a_receive_began(self) -> None:
        """Decision 13: after a Ctrl-C the point stays on every dataset whose
        receive began (the next run resumes or continues from it) and goes
        everywhere else."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-08_08:00:00_hourly")
        real_pipe = fake.mock_run_pipe

        def pipe(cmd1: str, cmd2: str, log: object = None) -> RunResult:
            if "rpool/USERDATA/home" in cmd1:
                raise KeyboardInterrupt
            return real_pipe(cmd1, cmd2, log)

        fake.mock_run_pipe = pipe  # type: ignore[method-assign]
        with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            log = Log()
            cleanup = Cleanup(log)
            eng = engine.Run("backup", "111", log, P2, sleep=lambda _s: None)
            try:
                _ = engine.execute(eng, cleanup, lambda _p, _r: True)
            except KeyboardInterrupt:
                pass
            cleanup.run()
        left = _origin_points(fake)
        assert f"rpool/USERDATA/home@{P2}" in left  # its receive began
        assert f"rpool/var/lib/docker@{P2}" not in left  # never started
        assert all(f"{rel}@{P2}" in left for rel in eng.started)


class TestCleanupActions:
    """Cleanup.track_action: undo steps run first, newest first, failures reported."""

    def test_actions_run_first_newest_first(self) -> None:
        """Order, survival of a failing action, and untracking."""
        order: list[str] = []
        mock = MockShell()
        mock.on("zpool list p").succeeds("p")
        mock.on("zfs unload-key -r p").succeeds()
        mock.on("zpool export p").succeeds()
        mock.on("sync").succeeds()

        def boom() -> None:
            order.append("boom")
            raise RuntimeError("x")

        def gone() -> None:
            order.append("gone")

        with patch_sh(mock), redirect_stdout(StringIO()), patch("time.sleep"):
            c = Cleanup(Log())
            c.track_pool("p")
            c.track_action(lambda: order.append("first"))
            c.track_action(boom)
            c.track_action(gone)
            c.untrack_action(gone)
            c.run()
        assert order == ["boom", "first"]
        assert c.exported_pools() == ["p"]

    def test_disable_drops_actions(self) -> None:
        """A disabled Cleanup runs no action."""
        order: list[str] = []
        with redirect_stdout(StringIO()):
            c = Cleanup(Log())
            c.track_action(lambda: order.append("x"))
            c.disable()
            c.run()
        assert not order


class TestSetupBookmarkV2:
    """setup enables feature@bookmark_v2 on rpool only after a confirmation (decision 4a)."""

    def _run(self, state: str, answer: bool, set_rc: int = 0) -> tuple[bool, MockShell]:
        mock = MockShell()
        mock.on("zpool get -H -o value feature@bookmark_v2 rpool").succeeds(state)
        mock.on("zpool set feature@bookmark_v2=enabled rpool").returns(
            set_rc,
            stderr="not specified in this pool's current compatibility set",
        )
        with (
            patch_sh(mock),
            patch.object(Log, "ask", return_value=answer),
            redirect_stdout(StringIO()),
        ):
            ok = ensure_bookmark_v2(Log())
        return ok, mock

    def test_already_there(self) -> None:
        """Enabled or active: nothing asked, nothing set."""
        for state in ("enabled", "active"):
            ok, mock = self._run(state, False)
            assert ok and mock.was_not_called("zpool set")

    def test_enabled_on_yes_only(self) -> None:
        """Disabled: set only when the user says yes."""
        ok, mock = self._run("disabled", True)
        assert ok and mock.was_called("zpool set feature@bookmark_v2=enabled rpool")
        ok, mock = self._run("disabled", False)
        assert not ok and mock.was_not_called("zpool set")

    def test_compatibility_refusal_is_reported(self) -> None:
        """A pool whose compatibility set lacks the feature stays as it is."""
        ok, _ = self._run("disabled", True, set_rc=1)
        assert not ok


class TestBackupDecide:
    """backup's questions before the first byte (decisions 7, 8, 18)."""

    def _plans(self, fake: FakeZfs) -> tuple[list[repl.DatasetPlan], engine.Run]:
        with patch_sh(fake), redirect_stdout(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            _ = eng.take_point()
            return eng.survey(), eng

    def _decide(
        self,
        fake: FakeZfs,
        choices: list[int],
        typed: str = "",
        avail: int = 10**12,
    ) -> tuple[bool, engine.Run]:
        plans, eng = self._plans(fake)
        dst = PoolInfo("backup", avail_bytes=avail)
        with (
            patch_sh(fake),
            patch.object(Log, "ask_choice", side_effect=choices),
            patch.object(Log, "ask_text", return_value=typed),
            redirect_stdout(StringIO()),
        ):
            return _decide(plans, eng, "backup", dst, Log()), eng

    @staticmethod
    def _mounted_read_write() -> FakeZfs:
        """P2 on eli: a read-write `zark mount` leaves the mounted datasets
        written since their newest snapshot, with nothing written by hand."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        for ds in ("rpool/var", "rpool/ROOT/be"):
            fake.ds[f"backup/{ds}"].written = 271 * 1024 * 1024
        return fake

    def test_modified_heads_are_skipped_by_default(self) -> None:
        """M2R-9: one question for every modified dataset; the default sends
        nothing to them and changes nothing on the drive (INCOMPLETE)."""
        fake = self._mounted_read_write()
        shown: list[str] = []

        def choice(question: str, _options: list[str], _default: int = 0) -> int:
            shown.append(question)
            return 0

        out = StringIO()

        def decide(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            with redirect_stdout(out):  # _engine_run swallows the rest
                return _decide(plans, eng, "backup", PoolInfo("backup", avail_bytes=10**12), Log())

        with patch.object(Log, "ask_choice", side_effect=choice):
            res = _engine_run(fake, "backup", "111", P3, decide)
        assert shown == ["How should this backup continue?"]
        assert "2 dataset(s) on backup changed since their newest snapshot" in out.getvalue()
        assert f"rpool/var: 271.0M written since {P1}" in out.getvalue()
        assert not res.ok
        states = {p.rel: p.state for p in res.plans}
        assert states["rpool/var"] is repl.State.MODIFIED
        assert states["rpool/var/lib"] is repl.State.AT_POINT
        assert not [c for c in fake.calls if c.startswith("zfs rollback")]
        sent = [c for c in fake.calls if "| zfs receive" in c]
        assert not [c for c in sent if c.endswith("backup/rpool/var") and P3 in c]
        assert fake.ds["backup/rpool/var"].written

    def test_modified_heads_discarded_with_the_typed_word(self) -> None:
        """M2R-9: "discard" is `zfs rollback` to the newest snapshot without
        -r (no snapshot destroyed), behind DISCARD; then the run completes."""
        fake = self._mounted_read_write()
        choice = MagicMock(side_effect=[1, 0])
        with (
            patch.object(Log, "ask_choice", choice),
            patch.object(Log, "ask_text", return_value="discard"),
            redirect_stdout(StringIO()),
        ):
            res = _engine_run(
                fake,
                "backup",
                "111",
                P3,
                lambda plans, eng: _decide(
                    plans, eng, "backup", PoolInfo("backup", avail_bytes=10**12), Log()
                ),
            )
        assert choice.call_count == 2 and not res.ok
        assert not [c for c in fake.calls if c.startswith("zfs rollback")]
        res = self._run_with_answers(fake, [1], typed="DISCARD", avail=10**12)
        assert res.ok, res
        rollbacks = sorted(c for c in fake.calls if c.startswith("zfs rollback"))
        assert rollbacks == [
            f"zfs rollback backup/rpool/ROOT/be@{P1}",
            f"zfs rollback backup/rpool/var@{P1}",
        ]
        assert P1 in fake.names("backup/rpool/var") and P3 in fake.names("backup/rpool/var")

    def test_discard_says_what_was_restored(self) -> None:
        """Eli N9b: the line says the drive's copy is back at its snapshot and
        nothing was destroyed, not that something was "discarded"."""
        fake = self._mounted_read_write()
        plans, eng = self._plans(fake)
        p = next(q for q in plans if q.rel == "rpool/var")
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out):
            assert eng.discard_changes(p)
        assert (
            f"Restored rpool/var on the backup to @{P1} "
            "(undid what a read-write mount changed; no snapshot destroyed)"
        ) in out.getvalue()

    def test_mount_read_write_says_the_next_backup_asks(self) -> None:
        """M2R-9: `zark mount` read-write warns what the next backup will ask."""
        out = StringIO()
        with redirect_stdout(out):
            mount_mod._warn_read_write(Log())  # pylint: disable=protected-access
        assert "the next backup asks whether to skip them or discard" in out.getvalue()

    def _with_rollback(self) -> FakeZfs:
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "h1")
        # The destination's newest (P1) is gone from origin with no bookmark,
        # and the destination also holds an older snapshot origin still has.
        fake.ds["rpool/ROOT/be"].bookmarks.clear()
        daily = fake.ds["rpool/ROOT/be"].snaps[0]
        fake.ds["backup/rpool/ROOT/be"].snaps.insert(
            0, FSnap(daily.name, daily.guid, 1, daily.creation)
        )
        return fake

    def test_destroy_lists_what_it_takes(self) -> None:
        """M2R-5 (E10): DESTROY of a diverged top counts what the drive holds
        below it and names, before the typed word, the archived lineage and
        the kept orphan it also takes."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        docker = fake.ds["rpool/var/lib/docker"]
        docker.snaps.clear()
        docker.bookmarks.clear()
        _ = fake.snap("rpool/var/lib/docker", "autosnap_new")

        def archive(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            for p in plans:
                if p.state is repl.State.DIVERGED:
                    assert eng.archive(p, datetime(2026, 10, 2, tzinfo=UTC))
            return True

        assert _engine_run(fake, "backup", "111", "zark_2026-10-02_10:00:00Z", archive).ok
        fake.dataset("rpool/var/lib/old", canmount="on", mountpoint="/var/lib/old")
        _ = fake.snap("rpool/var/lib/old", "autosnap_x")
        assert _engine_run(fake, "backup", "111", "zark_2026-10-03_10:00:00Z").ok
        _ = fake.ds.pop("rpool/var/lib/old")

        def keep(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            for p in plans:
                if p.state is repl.State.ORPHAN:
                    assert eng.keep_orphan(p, "2026-10-03")
            return True

        assert _engine_run(fake, "backup", "111", "zark_2026-10-04_10:00:00Z", keep).ok
        var = fake.ds["rpool/var"]
        var.snaps.clear()
        var.bookmarks.clear()
        _ = fake.snap("rpool/var", "autosnap_recreated")
        plans, eng = self._plans(fake)
        shown: list[str] = []

        def choice(_question: str, options: list[str], _default: int = 0) -> int:
            shown.extend(options)
            return 2

        out = StringIO()
        with (
            patch_sh(fake),
            patch.object(Log, "ask_choice", side_effect=choice),
            patch.object(Log, "ask_text", return_value="DESTROY"),
            redirect_stdout(out),
        ):
            assert _decide(plans, eng, "backup", PoolInfo("backup", avail_bytes=10**12), Log())
        assert any(o.startswith("destroy it (and 4 dataset(s) below)") for o in shown), shown
        text = out.getvalue()
        assert "DESTROY takes 5 dataset(s) on the backup:" in text
        assert "backup/rpool/var/lib/docker.archived-20261002" in text
        assert "archived lineage" in text and "kept, only on the backup" in text
        assert not [n for n in fake.ds if n.startswith("backup/rpool/var/")]

    def test_rollback_names_every_snapshot_in_the_log(self) -> None:
        """M2R-5 (M2F-7): the terminal shows three names and a count; zark.log
        gets every snapshot the rollback destroys."""
        fake = self._with_rollback()
        newer = [f"autosnap_extra_{i}" for i in range(5)]
        be = fake.ds["backup/rpool/ROOT/be"]
        for i, name in enumerate(newer):
            be.snaps.append(FSnap(name, f"9{i}", 900 + i, 2_000_000_000 + i))
        plans, eng = self._plans(fake)
        log_path = os.path.join(tempfile.mkdtemp(), "zark.log")
        out = StringIO()
        with (
            patch_sh(fake),
            patch.object(Log, "ask_choice", return_value=0),
            redirect_stdout(out),
        ):
            assert not _decide(
                plans, eng, "backup", PoolInfo("backup", avail_bytes=10**12), Log(log_path)
            )
        assert "…and 3 more (full list in zark.log)" in out.getvalue()
        logged = Path(log_path).read_text(encoding="utf-8")
        assert all(name in logged for name in newer)

    def test_rollback_default_aborts(self) -> None:
        """The default answer changes nothing."""
        fake = self._with_rollback()
        ok, _ = self._decide(fake, [0])
        assert not ok
        assert P1 in fake.names("backup/rpool/ROOT/be")

    def test_rollback_needs_the_typed_word(self) -> None:
        """A wrong word asks the question again (O10); abort then changes nothing."""
        fake = self._with_rollback()
        plans, eng = self._plans(fake)
        choice = MagicMock(side_effect=[2, 0])
        with (
            patch_sh(fake),
            patch.object(Log, "ask_choice", choice),
            patch.object(Log, "ask_text", return_value="rollback"),
            redirect_stdout(StringIO()),
        ):
            ok = _decide(plans, eng, "backup", PoolInfo("backup", avail_bytes=10**12), Log())
        assert not ok and choice.call_count == 2
        assert P1 in fake.names("backup/rpool/ROOT/be")
        ok, _ = self._decide(fake, [2], typed="ROLLBACK")
        assert ok
        assert P1 not in fake.names("backup/rpool/ROOT/be")

    def test_rollback_counts_the_incrementals_that_follow(self) -> None:
        """O11: after "roll back" the estimate holds each post-rollback incremental."""
        fake = self._with_rollback()
        fake.ds["backup"].props["org.zark:format"] = engine.FORMAT
        plans, eng = self._plans(fake)
        estimated: list[tuple[repl.State, bool]] = []
        real = eng.estimate

        def spy(p: repl.DatasetPlan, full: bool = False) -> int:
            estimated.append((p.state, full))
            return real(p, full)

        with (
            patch_sh(fake),
            patch.object(eng, "estimate", spy),
            patch.object(Log, "ask_choice", side_effect=[2]),
            patch.object(Log, "ask_text", return_value="ROLLBACK"),
            redirect_stdout(StringIO()) as out,
        ):
            assert _decide(plans, eng, "backup", PoolInfo("backup", avail_bytes=10**12), Log())
        assert (repl.State.ROLLBACK, False) in estimated
        # O6: on a v2 drive the warning names both causes.
        assert "recovered from an older point, or snapshots destroyed in origin" in out.getvalue()

    def test_archive_offered_only_when_it_fits(self) -> None:
        """Without room for a full resend, option 2 is the rollback."""
        fake = self._with_rollback()
        ok, _ = self._decide(fake, [1], typed="ROLLBACK", avail=10**6)
        assert ok
        assert not any(".archived-" in d for d in fake.ds)

    def test_orphan_keep_and_destroy(self) -> None:
        """Keep marks the dataset; destroy needs DESTROY typed."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.ds.pop("rpool/var/lib/docker")
        ok, _ = self._decide(fake, [1])
        assert ok
        assert fake.ds["backup/rpool/var/lib/docker"].props["org.zark:orphan"].startswith("kept@")
        fake.ds["backup/rpool/var/lib/docker"].props.pop("org.zark:orphan")
        # A wrong word asks again (O10); "ask again next time" then keeps it.
        ok, _ = self._decide(fake, [2, 0], typed="no")
        assert ok and "backup/rpool/var/lib/docker" in fake.ds
        ok, _ = self._decide(fake, [2], typed="DESTROY")
        assert ok and "backup/rpool/var/lib/docker" not in fake.ds

    def test_kept_orphan_back_in_origin_loses_its_mark(self) -> None:
        """O13: a kept orphan origin has again (after a recover) is replicated
        and unmarked, so a later removal in origin is asked about again."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "h1")
        docker = "backup/rpool/var/lib/docker"
        fake.ds[docker].props["org.zark:orphan"] = "kept@2026-10-09"
        res = _engine_run(fake, "backup", "111", P2)
        assert res.ok, res
        assert "org.zark:orphan" not in fake.ds[docker].props
        assert P2 in fake.names(docker)

    def test_no_space_is_fatal_before_any_change(self) -> None:
        """The space check runs after the questions and before applying them."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.ds.pop("rpool/var/lib/docker")
        try:
            with patch("builtins.input", return_value=""):
                _ = self._decide(fake, [2], typed="DESTROY", avail=1)
        except SystemExit:
            pass
        else:
            raise AssertionError("expected a fatal")
        assert "backup/rpool/var/lib/docker" in fake.ds

    @staticmethod
    def _var_tree_rolled_ahead() -> FakeZfs:
        """rpool/var and both children hold a snapshot origin never had, and
        their bookmarks are gone: ROLLBACK for the three, common autosnap_h1."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "h1")
        assert _engine_run(fake, "backup", "111", P2).ok
        for ds in ("rpool/var", "rpool/var/lib", "rpool/var/lib/docker"):
            fake.ds[ds].bookmarks.clear()
            fake.snap(f"backup/{ds}", "autosnap_gone")
        return fake

    @staticmethod
    def _run_with_answers(
        fake: FakeZfs,
        choices: list[int],
        typed: str = "",
        avail: int = 10**12,
    ) -> "engine.Result":
        def decide(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            return _decide(plans, eng, "backup", PoolInfo("backup", avail_bytes=avail), Log())

        with (
            patch.object(Log, "ask_choice", side_effect=choices),
            patch.object(Log, "ask_text", return_value=typed),
        ):
            return _engine_run(fake, "backup", "111", P3, decide)

    def test_archive_renames_only_the_top_of_a_subtree(self) -> None:
        """Archiving rpool/var moves its children with it: one rename, no
        failed renames, the three resent in full, every old snapshot kept."""
        fake = self._var_tree_rolled_ahead()
        res = self._run_with_answers(fake, [1])
        assert res.ok, res
        renames = [c for c in fake.calls if c.startswith("zfs rename backup/")]
        assert renames == [f"zfs rename backup/rpool/var backup/rpool/var.archived-{TODAY}"]
        archived = next(d for d in fake.ds if d.startswith("backup/rpool/var.archived-"))
        assert "autosnap_gone" in fake.names(f"{archived}/lib/docker")
        assert fake.names("backup/rpool/var/lib/docker") == [P3]

    def test_archive_space_counts_the_whole_subtree(self) -> None:
        """Room for the top alone is not room for its subtree: no archive option."""
        fake = self._var_tree_rolled_ahead()
        # Full sends are 1e9 each in the fake: 3e9 for the subtree, 1e9 for the top.
        res = self._run_with_answers(fake, [1], typed="ROLLBACK", avail=2 * 10**9)
        assert res.ok, res
        assert not any(".archived-" in d for d in fake.ds)  # option 1 was the rollback
        assert "autosnap_gone" not in fake.names("backup/rpool/var")

    def test_diverged_children_of_an_archived_top_are_not_asked(self) -> None:
        """One question for a diverged subtree; the children go with their parent."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        for ds in ("rpool/var", "rpool/var/lib", "rpool/var/lib/docker"):
            fake.ds[ds].snaps.clear()
            fake.ds[ds].bookmarks.clear()
            fake.snap(ds, "autosnap_recreated")
        asked: list[str] = []

        def choice(question: str, _opts: list[str], _default: int = 0) -> int:
            asked.append(question)
            return 1

        def decide(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            return _decide(plans, eng, "backup", PoolInfo("backup", avail_bytes=10**12), Log())

        with patch.object(Log, "ask_choice", side_effect=choice):
            res = _engine_run(fake, "backup", "111", P3, decide)
        assert res.ok, res
        assert asked == ["rpool/var has diverged:"]

    def test_table_names_every_dataset_short_of_the_point(self) -> None:
        """I-D table: failures and exclusions are warnings with their reason."""
        res = engine.Result(
            plans=[
                repl.DatasetPlan("rpool", repl.State.AT_POINT),
                repl.DatasetPlan("rpool/x", repl.State.RESUME),
                repl.DatasetPlan("rpool/vm", repl.State.EXCLUDED, note="zvol, not backed up"),
            ],
            outcomes={"rpool/x": engine.Outcome("rpool/x", False, "out of space")},
        )
        buf = StringIO()
        with redirect_stdout(buf):
            _show_table(res, P1, Log())
        out = buf.getvalue()
        assert "NOT at the point: out of space" in out
        assert "NOT BACKED UP (zvol, not backed up)" in out

    def test_table_tells_a_dataset_never_attempted(self) -> None:
        """O5: a dataset the run never reached is not shown as if it had failed."""
        res = engine.Result(
            plans=[repl.DatasetPlan("rpool/var", repl.State.VIA_BOOKMARK)],
            outcomes={},
        )
        buf = StringIO()
        with redirect_stdout(buf):
            _show_table(res, P1, Log())
        assert "NOT at the point: not attempted (via bookmark)" in buf.getvalue()

    def test_table_shows_the_whole_zfs_message(self) -> None:
        """M2R-12: libzfs's two-line refusals reach the table whole."""
        modified = (
            "cannot receive incremental stream: destination backup/rpool/x has been modified\n"
            "since most recent snapshot\n"
        )
        mismatch = (
            "warning: something earlier\n"
            "cannot receive incremental stream: most recent snapshot of backup/rpool/x does not\n"
            "match incremental source\n"
        )
        assert engine.last_message(modified) == (
            "cannot receive incremental stream: destination backup/rpool/x has been modified "
            "since most recent snapshot"
        )
        assert engine.last_message(mismatch).endswith("does not match incremental source")
        assert not engine.last_message(mismatch).startswith("warning")
        assert engine.last_message("plain error\n") == "plain error"
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        real_pipe = fake.mock_run_pipe

        def pipe(cmd1: str, cmd2: str, log: object = None) -> RunResult:
            if "rpool/var" in cmd2 and "rpool/var/" not in cmd2:
                return RunResult(1, "", modified, "")
            return real_pipe(cmd1, cmd2, log)

        fake.mock_run_pipe = pipe  # type: ignore[method-assign]
        res = _engine_run(fake, "backup", "111", P2)
        assert res.outcomes["rpool/var"].error.endswith(
            "has been modified since most recent snapshot"
        )

    def test_table_shows_both_sides_send_first(self) -> None:
        """M2R-12 (P3 on eli): receive's "failed to read from stream" alone
        hides why send stopped; both messages reach the table, send's first."""
        send = "cannot resume send: 'rpool/var@x' used in the initial send no longer exists\n"
        recv = "cannot receive: failed to read from stream\n"
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        real_pipe = fake.mock_run_pipe

        def pipe(cmd1: str, cmd2: str, log: object = None) -> RunResult:
            if "rpool/var" in cmd2 and "rpool/var/" not in cmd2:
                return RunResult(1, "", send + recv, "", stderr_left=send, stderr_right=recv)
            return real_pipe(cmd1, cmd2, log)

        fake.mock_run_pipe = pipe  # type: ignore[method-assign]
        res = _engine_run(fake, "backup", "111", P2)
        assert res.outcomes["rpool/var"].error == (
            "cannot resume send: 'rpool/var@x' used in the initial send no longer exists; "
            "cannot receive: failed to read from stream"
        )

    def test_report_keeps_dataset_names(self) -> None:
        """O1: "Rolled back rpool/ROOT/…@zark_…Z", not "rpool/root/…z"."""
        with patch_sh(_fresh_fake()), redirect_stdout(StringIO()) as buf:
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            eng._report(  # pylint: disable=protected-access
                RunResult(0, "", "", "zfs rollback"), f"rolled back rpool/ROOT/be to @{P1}"
            )
        assert f"Rolled back rpool/ROOT/be to @{P1}" in buf.getvalue()


class TestIncompleteBanner:
    """M2F-11b: BACKUP INCOMPLETE says, per dataset, what the next run does."""

    def test_each_dataset_says_what_the_next_backup_does(self) -> None:
        """Resume, decisions, a failure and a dataset not attempted each get
        their own line; datasets at the point or only on the drive get none."""
        plans = [
            repl.DatasetPlan("rpool/a", repl.State.AT_POINT),
            repl.DatasetPlan("rpool/home", repl.State.RESUME),
            repl.DatasetPlan("rpool/be", repl.State.ROLLBACK),
            repl.DatasetPlan("rpool/var", repl.State.MODIFIED),
            repl.DatasetPlan("rpool/docker", repl.State.INCREMENTAL),
            repl.DatasetPlan("rpool/srv", repl.State.VIA_BOOKMARK),
            repl.DatasetPlan("rpool/old", repl.State.ORPHAN),
        ]
        res = engine.Result(
            plans=plans,
            outcomes={"rpool/docker": engine.Outcome("rpool/docker", False, "cannot receive: I/O")},
        )
        lines, missing = backup_mod._incomplete_lines(res, P1)  # pylint: disable=protected-access
        assert missing == 5
        text = "\n".join(lines)
        assert f"5 dataset(s) did not reach {P1}:" in text
        assert "  partial transfer kept: the next backup resumes it (1):\n    rpool/home" in text
        assert "  needs a decision (rollback): the next backup asks (1):\n    rpool/be" in text
        assert "  changed on the drive: the next backup asks again (1):\n    rpool/var" in text
        assert "the next backup tries again (1):\n    rpool/docker" in text
        assert "  not attempted: the next backup sends it (1):\n    rpool/srv" in text
        assert "rpool/a" not in text and "rpool/old" not in text
        assert "an interrupted transfer resumes where it stopped" not in text

    def test_out_of_space(self) -> None:
        """The dataset that hit ENOSPC asks for space; the rest say why they waited."""
        plans = [
            repl.DatasetPlan("rpool/be", repl.State.INCREMENTAL),
            repl.DatasetPlan("rpool/home", repl.State.INCREMENTAL),
        ]
        res = engine.Result(
            plans=plans,
            outcomes={"rpool/be": engine.Outcome("rpool/be", False, "out of space", enospc=True)},
            enospc=True,
        )
        text = "\n".join(backup_mod._incomplete_lines(res, P1)[0])  # pylint: disable=protected-access
        assert "  the drive is full: free space, then run backup again (1):\n    rpool/be" in text
        assert "  not attempted after the drive filled up (1):\n    rpool/home" in text

    def test_datasets_with_the_same_step_share_one_line(self) -> None:
        """Eli N9a: 15 datasets changed by a read-write mount are one group,
        not 15 copies of the same line; names past the cap are counted."""
        plans = [repl.DatasetPlan(f"rpool/d{i:02}", repl.State.MODIFIED) for i in range(17)]
        lines, missing = backup_mod._incomplete_lines(  # pylint: disable=protected-access
            engine.Result(plans=plans), P1
        )
        text = "\n".join(lines)
        assert missing == 17
        assert text.count("changed on the drive") == 1
        assert "  changed on the drive: the next backup asks again (17):" in text
        assert "    rpool/d14" in text and "rpool/d15" not in text
        assert "    …and 2 more (see the table above)" in text


class TestBackupCommand:
    """`zark backup` end to end on FakeZfs (drive discovery and keystore stubbed)."""

    def _run(self, fake: FakeZfs, *extra: AbstractContextManager[object]) -> tuple[Config, int]:
        """``extra`` patches are entered last, so they override the defaults."""
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "usb-X-0:0", autoeject=False)
        drive = ConnectedDrive(
            "backup", "111", "usb-X-0:0", "/dev/sdb", True, False, False, "exported"
        )
        rc = 0
        readback = Readback(imported=True, health="ONLINE", reexported=True)
        info = PoolInfo("x", used_bytes=10**9, avail_bytes=10**13, size_bytes=10**13)
        with ExitStack() as stack:
            for cm in (
                patch_sh(fake),
                patch.object(Config, "load", return_value=cfg),
                patch.object(backup_mod, "_detect_live_usb", return_value=False),
                patch.object(backup_mod, "warn_rpool_mountpoint_lost"),
                patch.object(backup_mod, "warn_kernel_named_vdevs"),
                patch.object(backup_mod, "scan_connected_drives", return_value=[drive]),
                patch.object(backup_mod, "select_drive", return_value=drive),
                patch.object(backup_mod, "_heal_drive_id"),
                patch.object(backup_mod, "_notify"),
                patch.object(backup_mod, "prompt_eject_or_attach"),
                # No atexit/SIGTERM hook outliving the test: a refused run
                # leaves its Cleanup armed, and at exit it would act on a
                # real pool of the same name.
                patch.object(Cleanup, "register", lambda _self: None),
                patch.object(ZFS, "import_backup_pool", return_value=True),
                patch.object(ZFS, "pool_guid", return_value="111"),
                patch.object(ZFS, "pool_health", return_value="ONLINE"),
                patch.object(ZFS, "pool_info", return_value=info),
                patch.object(ZFS, "datasets_needing_key", return_value=[]),
                patch.object(ZFS, "verify_exported_pool_readback", return_value=readback),
                patch("lib.log.Log.ask", return_value=True),
                patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
                patch("builtins.input", return_value=""),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                stack.enter_context(cm)
            for override in extra:
                _ = stack.enter_context(override)
            try:
                backup_mod.run([])
            except SystemExit as e:
                rc = int(e.code or 0)
        return cfg, rc

    def test_complete_backup(self) -> None:
        """Every dataset at the point, metadata written, last_backup_at recorded."""
        fake = _fresh_fake()
        cfg, rc = self._run(fake)
        assert rc == 0
        points = [n for n in fake.names("backup/rpool/USERDATA/home") if repl.POINT_RE.match(n)]
        assert len(points) == 1
        meta = fake.ds["backup"].props
        assert meta["org.zark:format"] == "2" and meta["org.zark:last-point"] == points[0]
        assert _marked(fake) == {points[0]}
        assert cfg.known_drives["backup"].last_backup_at
        assert _origin_points(fake) == []
        assert fake.units["sanoid.timer"] == "active"
        assert not any(c.startswith("syncoid") or "sanoid --take" in c for c in fake.calls)

    def test_complete_backup_records_what_the_drive_shares(self) -> None:
        """M3 decision 5e: after a complete run the registry holds, per
        dataset, the sanoid snapshots the drive shares with origin (the
        intermediates the send carried) and the version that wrote it; the
        preparation fields come from the drive when the entry lacks them."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.ds["backup"].props["org.zark:prepared-at"] = "2026-10-01T10:00:00Z"
        fake.ds["backup"].props["org.zark:prepared-version"] = "2.0.0-rc2"
        # The fake's clock is in 2023: retention would remove the intermediates.
        cfg, rc = self._run(fake, patch.object(backup_mod, "_apply_retention", return_value=(0, 0)))
        assert rc == 0
        extra = cfg.known_drives["backup"].extra
        home = extra["shared"]["rpool/USERDATA/home"]
        assert [s[0] for s in home] == ["autosnap_2026-10-07_12:00:00_hourly"]
        guid = next(s.guid for s in fake.ds["rpool/USERDATA/home"].snaps if s.name == home[0][0])
        assert home[0][1] == guid and isinstance(home[0][2], int)
        with patch_sh(fake):
            replicated = repl.expected(engine.read_origin(["rpool", "bpool"]))
        assert set(extra["shared"]) == set(replicated)
        assert extra["last_backup_version"] == VERSION
        assert extra["prepared_at"] == "2026-10-01T10:00:00Z"
        assert extra["prepared_version"] == "2.0.0-rc2"
        saved = json.loads((cfg.config_dir / "known_drives.json").read_text())["backup"]
        assert saved["shared"] == extra["shared"]

    def test_shared_record_is_kept_when_unknown_and_not_written_when_incomplete(self) -> None:
        """M3 decision 5e: an incomplete run records nothing; a run that
        cannot read both sides afterwards keeps the previous record."""
        old = {"rpool/var": [["autosnap_2026-10-01_00:00:00_daily", "9", 1]]}
        fake = _fresh_fake()
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "usb-X-0:0", extra={"shared": old})
        _cfg, rc = self._run(fake, patch.object(Config, "load", return_value=cfg))
        assert rc == 1 and cfg.known_drives["backup"].extra == {"shared": old}
        fake = _fresh_fake()
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "usb-X-0:0", extra={"shared": old})
        _cfg, rc = self._run(
            fake,
            patch.object(Config, "load", return_value=cfg),
            patch.object(engine.Run, "shared_with_origin", return_value=None),
        )
        assert rc == 0
        assert cfg.known_drives["backup"].extra["shared"] == old
        assert cfg.known_drives["backup"].extra["last_backup_version"] == VERSION

    def _interrupted_then_written(self) -> FakeZfs:
        """eli R10: a run cut while sending home, then a read-write mount."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        assert not _engine_run(fake, "backup", "111", P2).ok
        fake.inject.clear()
        home = fake.ds["backup/rpool/USERDATA/home"]
        assert home.token
        home.written = 12 << 20
        return fake

    def test_partial_transfer_on_a_written_dataset_is_asked_and_discarded(self) -> None:
        """M2V-1: listed with the written datasets; DISCARD aborts the partial
        receive, rolls the head back and sends it in the same run."""
        fake = self._interrupted_then_written()
        before = len(fake.calls)
        out = StringIO()
        with patch.object(Log, "ask_text", return_value="DISCARD"):
            _cfg, rc = self._run(
                fake, patch.object(Log, "ask_choice", return_value=1), redirect_stdout(out)
            )
        text = " ".join(out.getvalue().split())
        assert "rpool/USERDATA/home: 12.0M written since" in text
        assert "(a partial transfer is discarded too)" in text
        calls = fake.calls[before:]
        abort = calls.index("zfs receive -A backup/rpool/USERDATA/home")
        rollback = next(
            i
            for i, c in enumerate(calls)
            if c.startswith("zfs rollback backup/rpool/USERDATA/home@")
        )
        assert abort < rollback and " -r " not in calls[rollback]
        assert rc == 0, text
        home = fake.ds["backup/rpool/USERDATA/home"]
        assert not home.token and not home.written
        assert repl.POINT_RE.match(home.snaps[-1].name)

    def test_partial_transfer_on_a_written_dataset_skipped_asks_again(self) -> None:
        """M2V-1: skipping (the default) keeps everything and says the next
        backup asks again, not that it resumes."""
        fake = self._interrupted_then_written()
        out = StringIO()
        _cfg, rc = self._run(
            fake, patch.object(Log, "ask_choice", return_value=0), redirect_stdout(out)
        )
        text = " ".join(out.getvalue().split())
        assert rc == 1
        assert "changed on the drive: the next backup asks again" in text
        assert "the next backup resumes it" not in text
        home = fake.ds["backup/rpool/USERDATA/home"]
        assert home.token and home.written
        assert not any(c.startswith("zfs send -t") for c in fake.calls[-200:])

    def test_retention_runs_after_a_complete_backup_only(self) -> None:
        """M3 decision 3a: the drive's policy is applied after a complete
        run, with that drive's settings, and not after an incomplete one."""
        calls: list[tuple[str, ret.Policy]] = []

        def spy(pool: str, policy: ret.Policy, _log: Log) -> tuple[int, int]:
            calls.append((pool, policy))
            return 3, 5 * 1024**2

        fake = _fresh_fake()
        out = StringIO()
        _cfg, rc = self._run(
            fake,
            patch.object(backup_mod, "_apply_retention", side_effect=spy),
            patch.object(Config, "drive_policy", return_value=ret.Policy(60)),
            redirect_stdout(out),
        )
        assert rc == 0 and calls == [("backup", ret.Policy(60))]
        assert "3 old snapshot(s) removed, 5.0M freed" in out.getvalue()
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        _tick(fake, "2026-10-09_08:00:00_hourly")
        _cfg, rc = self._run(fake, patch.object(backup_mod, "_apply_retention", side_effect=spy))
        assert rc == 1 and len(calls) == 1

    def test_removable_residues_are_pointed_at_after_a_backup(self) -> None:
        """11a: a backup never removes them; it says that prune can."""
        fake = _fresh_fake()
        fake.snap("rpool/keystore", "prepare_20260927_082338")
        out = StringIO()
        _cfg, rc = self._run(fake, redirect_stdout(out))
        assert rc == 0
        assert "1 legacy snapshot(s) in origin no registered drive needs" in out.getvalue()
        assert fake.names("rpool/keystore") == ["prepare_20260927_082338"]

    def test_a_failed_read_of_origin_after_the_transfer_says_so(self) -> None:
        """M3R-2: the final report does not call every other drive unanchored
        when origin cannot be listed; one line says so, the backup stays
        COMPLETED."""
        fake = _fresh_fake()
        fake.snap("rpool/keystore", "prepare_20260927_082338")

        def retention(_pool: str, _policy: ret.Policy, _log: Log) -> tuple[int, int]:
            fake.fail_cmds.add("zfs list -Hp -t snapshot,bookmark")  # from here on
            return 0, 0

        out = StringIO()
        _cfg, rc = self._run(
            fake,
            patch.object(backup_mod, "_apply_retention", side_effect=retention),
            patch.object(engine.Run, "shared_with_origin", return_value=None),
            patch.object(
                backup_mod.Config,
                "load",
                return_value=self._cfg_with_another_drive(),
            ),
            redirect_stdout(out),
        )
        text = out.getvalue()
        assert rc == 0 and "BACKUP COMPLETED" in text, text
        assert "Could not read the source pools" in text
        assert "no anchor" not in text and "legacy snapshot(s)" not in text
        assert "blue: " in text and "day(s) since its last backup" in text

    @staticmethod
    def _cfg_with_another_drive() -> Config:
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "usb-X-0:0", autoeject=False)
        cfg.known_drives["blue"] = DriveInfo(
            "blue", "222", "d", last_backup_at="2026-10-01T10:00:00Z"
        )
        return cfg

    def test_apply_retention_on_the_drive(self) -> None:
        """What goes after a complete run is the policy's, logged one by one."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        out = StringIO()
        with (
            patch_sh(fake),
            patch.object(backup_mod.time, "time", return_value=float(NOW)),
            redirect_stdout(out),
        ):
            count, freed = backup_mod._apply_retention(  # pylint: disable=protected-access
                "backup", ret.Policy(), Log()
            )
        assert count > 0 and freed == count * (1 << 20)
        assert P1 not in fake.names("backup/rpool/ROOT/be")
        assert f"removed backup/rpool/ROOT/be@{P1}" in out.getvalue()

    @staticmethod
    def _without_bpool(importable: bool) -> FakeZfs:
        fake = _fresh_fake()
        for n in [n for n in fake.ds if n == "bpool" or n.startswith("bpool/")]:
            _ = fake.ds.pop(n)
        fake.pools.discard("bpool")
        if importable:
            fake.importable.add("bpool")
        return fake

    def test_completed_banner_names_what_is_not_backed_up(self) -> None:
        """M2R-16: zvols and containers holding data are in the banner too."""
        fake = _fresh_fake()
        out = StringIO()
        _cfg, rc = self._run(fake, redirect_stdout(out))
        assert rc == 0
        text = out.getvalue()
        banner = text[text.index("BACKUP COMPLETED") :]
        assert "Not backed up:  1 dataset(s), by design:" in banner
        assert "rpool/vm (zvol, not backed up)" in banner

    def test_bpool_not_imported_is_refused(self) -> None:
        """M2R-4 (M2F-2): a bpool `zpool import` can see but that is not
        imported stops the backup before the drive is touched."""
        fake = self._without_bpool(importable=True)
        _cfg, rc = self._run(fake)
        assert rc == 1
        assert not any(c.startswith("zfs snapshot") for c in fake.calls)
        assert "backup/rpool" not in fake.ds

    def test_a_system_without_bpool_backs_up(self) -> None:
        """No bpool anywhere: the backup runs without it."""
        fake = self._without_bpool(importable=False)
        _cfg, rc = self._run(fake)
        assert rc == 0
        assert "backup/bpool/BOOT/be" not in fake.ds

    def test_incomplete_backup_exits_1_without_last_backup_at(self) -> None:
        """I-D/H2/I1: one dataset short is a failure, not a WARN plus success."""
        fake = _fresh_fake()
        fake.inject["backup/rpool/var/lib/docker"] = ("interrupt", 0)
        cfg, rc = self._run(fake)
        assert rc == 1
        assert not cfg.known_drives["backup"].last_backup_at
        assert "org.zark:last-point" not in fake.ds["backup"].props
        assert fake.ds["backup"].props["org.zark:format"] == "2"
        assert not _marked(fake)

    def _machine_id(self, value: str) -> str:
        path = os.path.join(tempfile.mkdtemp(), "machine-id")
        Path(path).write_text(value + "\n", encoding="ascii")
        return path

    def test_drive_records_this_machine_id(self) -> None:
        """Every backup writes origin-machine-id; the next one accepts it."""
        fake = _fresh_fake()
        with patch.object(engine, "MACHINE_ID_PATH", self._machine_id("aaaa1111")):
            _, rc = self._run(fake)
            assert rc == 0
            assert fake.ds["backup"].props["org.zark:origin-machine-id"] == "aaaa1111"
            _, rc = self._run(fake)
        assert rc == 0

    def test_drive_of_another_system_is_refused_untouched(self) -> None:
        """A mismatch is fatal before any question, key, point or sanoid change."""
        fake = _fresh_fake()
        fake.ds["backup"].props.update(
            {
                "org.zark:origin-machine-id": "bbbb2222",
                "org.zark:origin-host": "carmen",
                "org.zark:last-backup-at": "2026-10-01T10:00:00Z",
            },
        )
        out = StringIO()
        with (
            patch.object(engine, "MACHINE_ID_PATH", self._machine_id("aaaa1111")),
            patch("sys.stdout", out),
        ):
            _, rc = self._run(fake)
        assert rc == 1
        assert not any(
            c.startswith(("zfs snapshot", "systemctl stop", "zfs set")) for c in fake.calls
        )
        assert "backup/rpool" not in fake.ds

    def test_owner_is_checked_before_health_keys_and_questions(self) -> None:
        """The refusal comes before the health check, the keystore and the
        first question (review mutation O2)."""
        fake = _fresh_fake()
        fake.ds["backup"].props["org.zark:origin-machine-id"] = "bbbb2222"
        reached = AssertionError("reached past the owner check")
        with patch.object(engine, "MACHINE_ID_PATH", self._machine_id("aaaa1111")):
            _, rc = self._run(
                fake,
                patch.object(ZFS, "pool_health", side_effect=reached),
                patch.object(ZFS, "datasets_needing_key", side_effect=reached),
                patch("lib.log.Log.ask", side_effect=reached),
            )
        assert rc == 1

    def test_unreadable_machine_id_is_refused(self) -> None:
        """A drive that names an owner, and this system's machine-id cannot be
        read: refused, never taken for a match (mutation O3)."""
        fake = _fresh_fake()
        fake.ds["backup"].props["org.zark:origin-machine-id"] = "bbbb2222"
        missing = os.path.join(tempfile.mkdtemp(), "no-machine-id")
        with patch.object(engine, "MACHINE_ID_PATH", missing):
            _, rc = self._run(fake)
        assert rc == 1
        assert "backup/rpool" not in fake.ds

    def test_unreadable_metadata_is_refused(self) -> None:
        """M2R-7 (E11b): `zfs get` fails on a drive that names another system:
        refused, not taken for a drive without an owner."""
        fake = _fresh_fake()
        fake.ds["backup"].props["org.zark:origin-machine-id"] = "bbbb2222"
        fake.fail_cmds.add("zfs get -H -s local -o property,value all")
        with patch.object(engine, "MACHINE_ID_PATH", self._machine_id("aaaa1111")):
            _, rc = self._run(fake)
        assert rc == 1
        assert not any(c.startswith(("zfs snapshot", "systemctl stop")) for c in fake.calls)
        assert "backup/rpool" not in fake.ds

    def test_drive_without_machine_id_is_accepted(self) -> None:
        """A drive written by an older zark has no origin-machine-id: as before."""
        fake = _fresh_fake()
        with patch.object(engine, "MACHINE_ID_PATH", self._machine_id("aaaa1111")):
            _, rc = self._run(fake)
        assert rc == 0

    def test_owner_check_messages(self) -> None:
        """The refusal names the drive's host and date and the way out."""
        fake = _fresh_fake()
        fake.ds["backup"].props.update(
            {
                "org.zark:origin-machine-id": "bbbb2222",
                "org.zark:origin-host": "carmen",
                "org.zark:last-backup-at": "2026-10-01T10:00:00Z",
            },
        )
        out = StringIO()
        with (
            patch_sh(fake),
            patch.object(engine, "MACHINE_ID_PATH", self._machine_id("aaaa1111")),
            patch("builtins.input", return_value=""),
            redirect_stdout(out),
        ):
            try:
                engine.check_drive_owner("backup", Log())
            except SystemExit:
                pass
            else:
                raise AssertionError("expected a refusal")
        text = out.getvalue()
        assert "backup of another system" in text and "carmen" in text
        assert "2026-10-01T10:00:00Z" in text and "re-prepare" in text

    def test_refuses_without_bookmark_v2(self) -> None:
        """Decision 4a: no bookmark_v2 on rpool, no backup."""
        fake = _fresh_fake()
        fake.features["rpool"] = "disabled"
        _, rc = self._run(fake)
        assert rc == 1
        assert "backup/rpool" not in fake.ds


class TestDriveMetadata:
    """Decision 17a: drive metadata written by backup, shown by recover and mount."""

    def test_round_trip_and_display(self):
        """What write_metadata stores, read_metadata and log_metadata show."""
        fake = _fresh_fake()
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out):
            assert engine.write_metadata(
                "backup",
                {"format": "2", "version": "2.0.0-rc2", "origin-host": "eli", "last-point": P1},
                Log(),
            )
            meta = engine.log_metadata("backup", Log())
        assert meta["format"] == "2" and meta["origin-host"] == "eli"
        assert f"written by zark 2.0.0-rc2 on eli, last point {P1}" in out.getvalue()

    def test_pre_v2_drive(self):
        """A drive without org.zark:* is named as written by an older zark."""
        fake = _fresh_fake()
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out):
            assert not engine.log_metadata("backup", Log())
        assert "zark <= 2.0.0-rc1" in out.getvalue()


def _marked(fake: FakeZfs, pool: str = "backup") -> set[str]:
    """Names of the backup points complete on a drive, as recover reads them."""
    with patch_sh(fake):
        return complete_points(engine.drive_snapshots(pool), "")


def _record(fake: FakeZfs, point: str, pool: str = "backup") -> None:
    """record_complete for every expected dataset, as backup does after a complete run."""
    with patch_sh(fake), redirect_stdout(StringIO()):
        rels = repl.expected(engine.read_origin(["rpool", "bpool"]))
        assert engine.record_complete(pool, point, rels, Log())


# ── Destination retention policy (M3 decision 1) ─────────────────────────


NOW = 1_800_000_000  # 2027-01-15 08:00 UTC


def _days_ago(n: float) -> int:
    return int(NOW - n * ret.DAY)


def _pt(day: str) -> str:
    return f"zark_{day}_10:00:00Z"


def _drive(spec: dict[str, list[tuple[str, int]]], **orphans: str) -> dict[str, repl.Dest]:
    """{dataset: [(snapshot, creation)]} in createtxg order → a drive listing."""
    out: dict[str, repl.Dest] = {}
    txg = 0
    for rel, snaps in spec.items():
        refs = []
        for name, when in snaps:
            txg += 1
            refs.append(repl.Ref(name, f"{rel}@{name}", txg, when))
        out[rel] = repl.Dest(rel, snaps=refs, orphan=orphans.get(rel.replace("/", "_"), ""))
    return out


def _gone(p: ret.Prune) -> set[str]:
    return {r.target for r in p.snapshots}


class TestRetentionPolicy:
    """M3-1: window, one complete point per month, rule 3, max_age, 6c, 7a."""

    BE, HOME = "rpool/ROOT/be", "rpool/USERDATA/home"

    def _history(self) -> tuple[dict[str, repl.Dest], set[str]]:
        # Three points in November, two in December, one 10 days ago; an
        # hourly carried in November and one 2 days ago.
        points = [
            (_pt("2026-11-02"), _days_ago(74)),
            (_pt("2026-11-16"), _days_ago(60)),
            (_pt("2026-11-30"), _days_ago(46)),
            (_pt("2026-12-07"), _days_ago(39)),
            (_pt("2026-12-21"), _days_ago(25)),
            (_pt("2027-01-05"), _days_ago(10)),
        ]
        be = [*points[:2], ("autosnap_2026-11-20_10:00:00_hourly", _days_ago(56)), *points[2:]]
        home = [*points, ("autosnap_2027-01-13_10:00:00_hourly", _days_ago(2))]
        return _drive({self.BE: be, self.HOME: home}), {n for n, _ in points}

    def test_window_and_one_complete_point_per_month(self) -> None:
        """Inside the window everything stays; outside, the newest complete point of each month."""
        dest, complete = self._history()
        p = ret.plan_prune(dest, complete, ret.Policy(window_days=30), NOW)
        gone = _gone(p)
        for rel in (self.BE, self.HOME):
            assert f"{rel}@{_pt('2026-11-02')}" in gone
            assert f"{rel}@{_pt('2026-11-16')}" in gone
            assert f"{rel}@{_pt('2026-11-30')}" not in gone  # November's newest
            assert f"{rel}@{_pt('2026-12-07')}" in gone
            assert f"{rel}@{_pt('2026-12-21')}" not in gone  # inside the window
            assert f"{rel}@{_pt('2027-01-05')}" not in gone
        assert f"{self.BE}@autosnap_2026-11-20_10:00:00_hourly" in gone
        assert f"{self.HOME}@autosnap_2027-01-13_10:00:00_hourly" not in gone
        assert not p.datasets

    def test_a_point_goes_or_stays_whole(self) -> None:
        """Decided once per name: every dataset's snapshot of it shares the verdict."""
        dest, complete = self._history()
        gone = _gone(ret.plan_prune(dest, complete, ret.Policy(), NOW))
        by_point: dict[str, set[bool]] = {}
        for rel, d in dest.items():
            for s in d.snaps:
                if repl.POINT_RE.match(s.name):
                    by_point.setdefault(s.name, set()).add(f"{rel}@{s.name}" in gone)
        assert all(len(v) == 1 for v in by_point.values()), by_point

    def test_incomplete_points_leave_with_the_window(self) -> None:
        """An incomplete point outside the window goes; its month keeps the newest complete one."""
        dest, complete = self._history()
        complete.discard(_pt("2026-11-30"))
        gone = _gone(ret.plan_prune(dest, complete, ret.Policy(), NOW))
        assert f"{self.BE}@{_pt('2026-11-30')}" in gone
        assert f"{self.BE}@{_pt('2026-11-16')}" not in gone  # now November's newest complete

    def test_newest_complete_point_and_newest_snapshot_stay(self) -> None:
        """Rule 3: even far outside the window and past max_age."""
        dest = _drive(
            {
                self.BE: [(_pt("2026-06-01"), _days_ago(228)), (_pt("2026-06-02"), _days_ago(227))],
                self.HOME: [(_pt("2026-06-01"), _days_ago(228))],
            },
        )
        complete = {_pt("2026-06-01")}
        p = ret.plan_prune(dest, complete, ret.Policy(window_days=7, max_age_days=30), NOW)
        assert _gone(p) == set()  # 06-01: newest complete; 06-02: BE's newest (I-E)

    def test_max_age_removes_monthly_points(self) -> None:
        """max_age removes monthly points older than it, never the window."""
        dest, complete = self._history()
        gone = _gone(ret.plan_prune(dest, complete, ret.Policy(max_age_days=40), NOW))
        assert f"{self.BE}@{_pt('2026-11-30')}" in gone
        assert f"{self.BE}@{_pt('2026-12-21')}" not in gone

    def test_history_before_the_first_point_is_not_touched(self) -> None:
        """6c: a drive written by 1.x keeps its older history; one without any
        backup point is not pruned at all."""
        old = [
            ("autosnap_2026-03-01_00:00:00_monthly", _days_ago(320)),
            ("syncoid_carmen_2026-03-02:10:00:00-GMT01:00", _days_ago(319)),
        ]
        dest = _drive({self.BE: [*old, (_pt("2026-12-01"), _days_ago(45))]})
        assert ret.plan_prune(dest, {_pt("2026-12-01")}, ret.Policy(), NOW).empty
        assert ret.plan_prune(_drive({self.BE: old}), set(), ret.Policy(), NOW).empty

    def test_other_names_pinned_and_protected_stay(self) -> None:
        """Manual names, clone origins, holds and this run's bases are never removed."""
        dest, complete = self._history()
        dest[self.BE].snaps.insert(0, repl.Ref("manual-before-upgrade", "m", 0, _days_ago(70)))
        clone = f"{self.BE}@{_pt('2026-11-02')}"
        base = f"{self.HOME}@{_pt('2026-11-16')}"
        p = ret.plan_prune(
            dest,
            complete,
            ret.Policy(),
            NOW,
            pinned={clone: "origin of a clone on the drive"},
            protected={base},
        )
        gone = _gone(p)
        assert f"{self.BE}@manual-before-upgrade" not in gone
        assert clone not in gone and base not in gone
        assert (clone, "origin of a clone on the drive") in p.kept

    def test_other_drives_bpool_anchors_are_intermediates(self) -> None:
        """Another drive's bpool anchor carried by a -I stream goes like an intermediate."""
        anchor = repl.anchor_name("222", _pt("2026-11-10"))
        dest = _drive(
            {
                "bpool/BOOT/be": [
                    (_pt("2026-11-02"), _days_ago(74)),
                    (anchor, _days_ago(66)),
                    (_pt("2027-01-05"), _days_ago(10)),
                ],
            },
        )
        gone = _gone(ret.plan_prune(dest, {_pt("2027-01-05")}, ret.Policy(), NOW))
        assert f"bpool/BOOT/be@{anchor}" in gone

    def test_archived_lineage_left_with_nothing_goes_whole(self) -> None:
        """7a: no I-E for an archived lineage; with all its snapshots gone the
        subtree is destroyed whole. A live dataset keeps its newest."""
        arch = f"{self.BE}.archived-20261101"
        dest, complete = self._history()
        dest |= _drive(
            {
                arch: [(_pt("2026-10-05"), _days_ago(102))],
                f"{arch}/var": [(_pt("2026-10-05"), _days_ago(102))],
            },
        )
        complete.add(_pt("2026-10-05"))
        p = ret.plan_prune(dest, complete, ret.Policy(), NOW)
        assert f"{arch}@{_pt('2026-10-05')}" not in _gone(p)  # October's newest complete
        p = ret.plan_prune(dest, complete, ret.Policy(max_age_days=90), NOW)
        assert f"{arch}@{_pt('2026-10-05')}" in _gone(p)
        assert [t for t, _ in p.datasets] == [arch]

    def test_kept_orphan_goes_whole_undecided_orphan_keeps_its_newest(self) -> None:
        """7a: a kept orphan with nothing left goes whole; one still asked about keeps it."""
        old = [(_pt("2026-10-05"), _days_ago(102))]
        kept, asked = "rpool/USERDATA/old", "rpool/USERDATA/tmp"
        dest = _drive({kept: old, asked: old}, rpool_USERDATA_old="kept@2026-11-01")
        dest |= _drive({self.BE: [*old, (_pt("2027-01-05"), _days_ago(10))]})
        p = ret.plan_prune(
            dest, {_pt("2026-10-05"), _pt("2027-01-05")}, ret.Policy(max_age_days=60), NOW
        )
        assert f"{kept}@{_pt('2026-10-05')}" in _gone(p)
        assert f"{asked}@{_pt('2026-10-05')}" not in _gone(p)  # I-E: may become a base
        assert [t for t, _ in p.datasets] == [kept]

    def test_space_candidates_are_the_oldest_monthly_points(self) -> None:
        """M3-4: beyond the policy, the monthly points outside the window, oldest first."""
        dest, complete = self._history()
        dest[self.BE].snaps.insert(0, repl.Ref(_pt("2026-10-05"), "x", 0, _days_ago(102)))
        complete.add(_pt("2026-10-05"))
        policy = ret.Policy()
        p = ret.plan_prune(dest, complete, policy, NOW)
        names = ret.space_candidates(dest, complete, policy, NOW, p)
        assert names == [_pt("2026-10-05"), _pt("2026-11-30")]
        removals = ret.point_removals(dest, _pt("2026-11-30"))
        assert {r.target for r in removals} == {
            f"{self.BE}@{_pt('2026-11-30')}",
            f"{self.HOME}@{_pt('2026-11-30')}",
        }

    def test_the_space_plan_keeps_a_live_datasets_newest_snapshot(self) -> None:
        """I-E in the space plan: point_removals leaves a dataset whose newest
        snapshot is the point, and takes the archived copy (R14)."""
        arch = f"{self.HOME}.archived-20261101"
        dest = _drive(
            {
                self.BE: [(_pt("2026-11-02"), _days_ago(74)), (_pt("2027-01-05"), _days_ago(10))],
                self.HOME: [(_pt("2026-11-02"), _days_ago(74))],
                arch: [(_pt("2026-11-02"), _days_ago(74))],
            },
        )
        removals = {r.target for r in ret.point_removals(dest, _pt("2026-11-02"))}
        assert removals == {f"{self.BE}@{_pt('2026-11-02')}", f"{arch}@{_pt('2026-11-02')}"}

    def test_inside_the_window_everything_stays(self) -> None:
        """Rule 1: an intermediate that is not its dataset's newest and two
        points of one month stay while inside the window (M3R-6, R10)."""
        dest, complete = self._history()
        dest[self.BE].snaps.append(
            repl.Ref("autosnap_2026-12-26_10:00:00_hourly", "h", 99, _days_ago(20))
        )
        for day, age in ((_pt("2027-01-01"), 14), (_pt("2027-01-05"), 10)):
            dest[self.BE].snaps.append(repl.Ref(day, f"j{age}", 100 + age, _days_ago(age)))
        dest[self.BE].snaps = sorted(dest[self.BE].snaps, key=lambda s: s.creation)
        complete.add(_pt("2027-01-01"))
        gone = _gone(ret.plan_prune(dest, complete, ret.Policy(window_days=30), NOW))
        assert f"{self.BE}@autosnap_2026-12-26_10:00:00_hourly" not in gone
        assert f"{self.BE}@{_pt('2027-01-01')}" not in gone  # not January's newest, but inside
        assert f"{self.BE}@{_pt('2027-01-05')}" not in gone

    def test_a_child_that_keeps_a_point_blocks_the_whole_destroy(self) -> None:
        """7a: an archived top whose own snapshots all go is not destroyed
        with -r while a child keeps its month's newest complete point (M3R-6, R8)."""
        top = f"{self.BE}.archived-20261001"
        child = f"{top}/var"
        dest = _drive(
            {
                self.BE: [(_pt("2026-11-16"), _days_ago(60)), (_pt("2027-01-10"), _days_ago(5))],
                top: [(_pt("2026-11-02"), _days_ago(74))],
                child: [(_pt("2026-11-02"), _days_ago(74)), (_pt("2026-11-16"), _days_ago(60))],
            },
        )
        complete = {_pt("2026-11-02"), _pt("2026-11-16"), _pt("2027-01-10")}
        p = ret.plan_prune(dest, complete, ret.Policy(window_days=30), NOW)
        assert _gone(p) == {f"{top}@{_pt('2026-11-02')}", f"{child}@{_pt('2026-11-02')}"}
        assert p.datasets == []
        assert ret.whole_datasets(dest, p.snapshots) == []

    def test_a_month_with_no_whole_point_keeps_its_newest_run_complete_one(self) -> None:
        """M3F-5 5b: the month's point is its newest whole point; with none
        left, its newest run-complete point stays instead of the month
        losing every point."""
        dest, complete = self._history()
        whole = complete - {_pt("2026-11-16"), _pt("2026-11-30")}
        gone = _gone(ret.plan_prune(dest, complete, ret.Policy(), NOW, whole=whole))
        assert f"{self.BE}@{_pt('2026-11-30')}" in gone  # not whole: November's is 11-02 now
        assert f"{self.BE}@{_pt('2026-11-16')}" in gone
        assert f"{self.BE}@{_pt('2026-11-02')}" not in gone
        whole -= {_pt("2026-11-02")}
        gone = _gone(ret.plan_prune(dest, complete, ret.Policy(), NOW, whole=whole))
        assert f"{self.BE}@{_pt('2026-11-30')}" not in gone  # the newest run-complete one
        assert f"{self.BE}@{_pt('2026-11-16')}" in gone and f"{self.BE}@{_pt('2026-11-02')}" in gone
        candidates = ret.space_candidates(dest, complete, ret.Policy(), NOW, ret.Prune(), whole)
        assert _pt("2026-11-30") in candidates and _pt("2026-11-16") not in candidates


class TestRetentionOnDrive:
    """M3: the policy applied to a drive written by the engine."""

    def _drive_with_history(self) -> FakeZfs:
        """Four complete backups; their snapshots dated 70, 60, 40 and 5 days
        before NOW (P1 and P2 in one month, P3 in the next)."""
        fake = _fresh_fake()
        points = (P1, P2, P3, "zark_2026-10-10_10:00:00Z")
        for i, point in enumerate(points):
            _tick(fake, f"2026-10-0{i + 1}_08:00:00_hourly")
            assert _engine_run(fake, "backup", "111", point).ok
            _record(fake, point)
        ages = {P1: 70, P2: 66, P3: 40, points[3]: 5}
        for ds, d in fake.ds.items():
            for snap in d.snaps:
                if ds.startswith("backup/"):
                    base = next((a for p, a in ages.items() if snap.name == p), None)
                    snap.creation = _days_ago(base if base is not None else 67)
        return fake

    def _plan(self, fake: FakeZfs, policy: ret.Policy | None = None) -> ret.Prune:
        with patch_sh(fake):
            dest = engine.read_dest("backup", strict=True)
            complete = engine.drive_complete("backup")
            return ret.plan_prune(
                dest,
                complete.run,
                policy or ret.Policy(),
                NOW,
                whole=complete.whole,
                pinned=engine.drive_pins("backup"),
            )

    def test_prune_by_exact_name_keeps_bases_and_marks_true(self) -> None:
        """Every removal is one `zfs destroy` of one exact name; the next
        backup is still incremental; what is left of a pruned point is not
        complete (M2V-4)."""
        fake = self._drive_with_history()
        prune = self._plan(fake)
        gone = {r.target for r in prune.snapshots}
        assert f"rpool/ROOT/be@{P1}" in gone and f"rpool/ROOT/be@{P2}" not in gone
        before = len(fake.calls)
        with patch_sh(fake), redirect_stdout(StringIO()):
            count, failed = engine.apply_prune("backup", prune, Log())
        destroys = [c for c in fake.calls[before:] if c.startswith("zfs destroy")]
        assert count == len(prune.snapshots) and not failed
        assert len(destroys) == count
        assert all("%" not in c and "," not in c and " -r " not in c for c in destroys)
        for rel in (
            "rpool/ROOT/be",
            "rpool/USERDATA/home",
            "rpool/var",
            "rpool/var/lib",
            "rpool/var/lib/docker",
            "bpool/BOOT/be",
        ):
            assert fake.ds[f"backup/{rel}"].snaps, rel  # I-E: every dataset keeps its newest
        assert P1 not in _marked(fake)
        assert _engine_run(fake, "backup", "111", "zark_2026-10-11_10:00:00Z").ok

    def test_a_dataset_destroyed_on_the_drive_does_not_take_the_history(self) -> None:
        """A DESTROY the user confirmed for one dataset leaves the older points
        short of it; retention still judges them by their run's mark and keeps
        each month's point on the other datasets."""
        fake = self._drive_with_history()
        for n in [n for n in fake.ds if n.startswith("backup/rpool/var")]:
            _ = fake.ds.pop(n)
        with patch_sh(fake), redirect_stdout(StringIO()):
            assert _marked(fake) == set()  # recover: none of them is whole any more
        prune = self._plan(fake)
        gone = {r.target for r in prune.snapshots}
        assert f"rpool/ROOT/be@{P2}" not in gone and f"rpool/ROOT/be@{P3}" not in gone
        assert f"rpool/ROOT/be@{P1}" in gone

    def test_a_point_rolled_back_on_one_dataset_yields_to_the_whole_one(self) -> None:
        """M3R-4: P2 lost its bpool snapshot (a rollback or a hand destroy)
        while bpool stayed: retention keeps the whole P1 as the month's point
        and removes what is left of P2; recover labels P2 incomplete."""
        fake = self._drive_with_history()
        boot = fake.ds["backup/bpool/BOOT/be"]
        boot.snaps = [s for s in boot.snaps if s.name != P2]
        with patch_sh(fake), redirect_stdout(StringIO()):
            assert P2 not in _marked(fake) and P1 in _marked(fake)
        prune = self._plan(fake)
        gone = {r.target for r in prune.snapshots}
        assert f"rpool/ROOT/be@{P2}" in gone and f"rpool/ROOT/be@{P1}" not in gone
        out = StringIO()
        with (
            patch_sh(fake),
            patch.object(backup_mod.time, "time", return_value=float(NOW)),
            redirect_stdout(out),
        ):
            count, _freed = backup_mod._apply_retention("backup", ret.Policy(), Log())  # pylint: disable=protected-access
        assert count == len(prune.snapshots)
        assert P1 in fake.names("backup/rpool/ROOT/be") and P2 not in fake.names(
            "backup/rpool/ROOT/be"
        )

    def test_retention_stops_when_the_marks_cannot_be_read(self) -> None:
        """A failed read of the marks would make every point look incomplete:
        nothing is removed instead."""
        fake = self._drive_with_history()
        fake.fail_cmds.add(
            "-s local -t snapshot -o name,value org.zark:point-complete backup/bpool"
        )
        before = {ds: fake.names(ds) for ds in fake.ds}
        out = StringIO()
        with (
            patch_sh(fake),
            patch.object(backup_mod.time, "time", return_value=float(NOW)),
            redirect_stdout(out),
        ):
            assert backup_mod._apply_retention("backup", ret.Policy(), Log()) == (0, 0)  # pylint: disable=protected-access
        assert "Retention skipped" in out.getvalue()
        assert before == {ds: fake.names(ds) for ds in fake.ds}

    def test_retention_stops_when_the_pins_cannot_be_read(self) -> None:
        """M3R-8: without the pins the plan would include clone origins and
        held snapshots ZFS refuses one by one; nothing is removed instead."""
        fake = self._drive_with_history()
        fake.fail_cmds.add("-o name,clones,userrefs -r backup/rpool")
        before = {ds: fake.names(ds) for ds in fake.ds if ds.startswith("backup/")}
        out = StringIO()
        with (
            patch_sh(fake),
            patch.object(backup_mod.time, "time", return_value=float(NOW)),
            redirect_stdout(out),
        ):
            assert backup_mod._apply_retention("backup", ret.Policy(), Log()) == (0, 0)  # pylint: disable=protected-access
        assert "Retention skipped" in out.getvalue()
        assert before == {ds: fake.names(ds) for ds in fake.ds if ds.startswith("backup/")}
        with patch_sh(fake), redirect_stdout(StringIO()):
            eng = engine.Run(
                "backup", "111", Log(), "zark_2026-10-11_10:00:00Z", sleep=lambda _s: None
            )
            assert eng.take_point()
            plans = eng.survey()
            with (
                patch.object(backup_mod.time, "time", return_value=float(NOW)),
                patch("builtins.input", side_effect=EOFError),
                redirect_stdout(out),
            ):
                try:
                    _decide(
                        plans,
                        eng,
                        "backup",
                        PoolInfo("backup", avail_bytes=0, size_bytes=10**12),
                        Log(),
                        ret.Policy(),
                    )
                except SystemExit:
                    pass
        assert before == {ds: fake.names(ds) for ds in fake.ds if ds.startswith("backup/")}

    def test_clone_origin_and_held_snapshot_stay(self) -> None:
        """ZFS would refuse both destroys: the policy leaves them and reports the clone origin."""
        fake = self._drive_with_history()
        be = fake.ds["backup/rpool/ROOT/be"]
        home = fake.ds["backup/rpool/USERDATA/home"]
        next(x for x in be.snaps if x.name == P1).clones = "backup/rpool/ROOT/be2"
        next(x for x in home.snaps if x.name == P1).userrefs = 1
        prune = self._plan(fake)
        gone = {r.target for r in prune.snapshots}
        assert f"rpool/ROOT/be@{P1}" not in gone
        assert f"rpool/USERDATA/home@{P1}" not in gone
        assert (
            f"rpool/ROOT/be@{P1}",
            "origin of a clone on the drive (backup/rpool/ROOT/be2)",
        ) in (prune.kept)

    def test_reclaim_is_measured_by_a_dry_run_per_dataset(self) -> None:
        """What a plan frees comes from `zfs destroy -nvp`, one call per dataset, never a range."""
        fake = self._drive_with_history()
        prune = self._plan(fake)
        before = len(fake.calls)
        with patch_sh(fake):
            freed = engine.reclaim("backup", prune.snapshots)
        dry = [c for c in fake.calls[before:] if c.startswith("zfs destroy -nvp")]
        assert freed == len(prune.snapshots) * (1 << 20)
        assert len(dry) == len({r.rel for r in prune.snapshots})
        assert not any("%" in c for c in dry)

    def test_a_subtree_destroy_names_every_dataset_it_takes(self) -> None:
        """M3R-11: a dataset below the top with no snapshot, and one with a
        partial receive, are logged before the -r; with the listing failing
        the subtree stays and is reported."""
        fake = self._drive_with_history()
        arch = "backup/rpool/var.archived-20260801"
        fake.dataset(arch)
        fake.snap(arch, "zark_2026-08-01_10:00:00Z").creation = _days_ago(200)
        fake.dataset(f"{arch}/empty")
        fake.dataset(f"{arch}/partial")
        fake.ds[f"{arch}/partial"].token = "1-abc"
        prune = self._plan(fake, ret.Policy(max_age_days=100))
        assert [t for t, _ in prune.datasets] == ["rpool/var.archived-20260801"]
        with tempfile.TemporaryDirectory() as td:
            logf = os.path.join(td, "zark.log")
            with patch_sh(fake), redirect_stdout(StringIO()):
                count, failed = engine.apply_prune("backup", prune, Log(logf))
            logged = Path(logf).read_text(encoding="utf-8")
        assert not failed and count == len(prune.snapshots)
        assert f"{arch}@zark_2026-08-01_10:00:00Z goes with {arch}" in logged
        assert f"{arch}/empty (no snapshot) goes with {arch}" in logged
        assert f"{arch}/partial and its partial receive go with {arch}" in logged
        assert logged.index("(no snapshot) goes with") < logged.index("and everything below it:")
        assert arch not in fake.ds
        # The listing fails: nothing of the subtree is destroyed.
        fake = self._drive_with_history()
        fake.dataset(arch)
        fake.snap(arch, "zark_2026-08-01_10:00:00Z").creation = _days_ago(200)
        prune = self._plan(fake, ret.Policy(max_age_days=100))
        fake.fail_cmds.add(f"-t filesystem,volume -r {arch}")
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out):
            _count, failed = engine.apply_prune("backup", prune, Log())
        assert failed == [arch] and arch in fake.ds
        assert "its datasets could not be listed" in out.getvalue()
        assert not any(c.startswith("zfs destroy -r") for c in fake.calls)

    def test_archived_lineage_with_nothing_kept_is_destroyed_whole(self) -> None:
        """7a: the lineage goes with one `zfs destroy -r`, after its snapshots are named."""
        fake = self._drive_with_history()
        arch = "backup/rpool/var.archived-20260801"
        fake.dataset(arch)
        old = fake.snap(arch, "zark_2026-08-01_10:00:00Z")
        old.creation = _days_ago(200)
        prune = self._plan(fake, ret.Policy(max_age_days=100))
        assert prune.datasets == [
            (
                "rpool/var.archived-20260801",
                "archived lineage with nothing left that the policy keeps",
            ),
        ]
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out):
            _ = engine.apply_prune("backup", prune, Log())
        assert arch not in fake.ds
        assert f"zfs destroy -r {arch}" in fake.calls


class TestBackupSpace:
    """M3 decisions 1 and 4: the space check before the transfer."""

    NEXT = "zark_2026-10-11_10:00:00Z"

    def _decide(
        self,
        fake: FakeZfs,
        avail: int,
        size: int,
        answer: bool,
        policy: ret.Policy,
        point: str = NEXT,
    ) -> tuple[bool | None, list[str], str]:
        """Run backup's questions on the history drive; returns the result
        (None for a fatal), the yes/no questions asked and the output."""
        asked: list[str] = []

        def ask(question: str, default: bool = False) -> bool:
            _ = default
            asked.append(question)
            return answer

        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), point, sleep=lambda _s: None)
            assert eng.take_point()
            plans = eng.survey()
            dst = PoolInfo("backup", avail_bytes=avail, size_bytes=size)
            with (
                patch.object(Log, "ask", side_effect=ask),
                patch.object(backup_mod.time, "time", return_value=float(NOW)),
                patch("builtins.input", side_effect=EOFError),
            ):
                try:
                    ok: bool | None = _decide(plans, eng, "backup", dst, Log(), policy)
                except SystemExit:
                    ok = None
        return ok, asked, out.getvalue()

    @staticmethod
    def _history() -> FakeZfs:
        return TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access

    def test_policy_removals_are_applied_without_a_question(self) -> None:
        """Short of space: what the policy removes anyway goes first, asked nothing."""
        fake = self._history()
        ok, asked, _ = self._decide(fake, 0, 10**12, True, ret.Policy(margin_pct=0))
        assert ok and not asked
        assert P1 not in fake.names("backup/rpool/ROOT/be")
        assert P2 in fake.names("backup/rpool/ROOT/be")

    def test_beyond_the_policy_the_oldest_points_go_only_on_a_yes(self) -> None:
        """The plan lists the oldest monthly points; no (the default) with a
        transfer that does not fit stops the backup; yes removes them."""
        fake = self._history()
        with patch_sh(fake), redirect_stdout(StringIO()):
            dest = engine.read_dest("backup", strict=True)
            prune = ret.plan_prune(dest, engine.drive_complete("backup").run, ret.Policy(), NOW)
            _ = engine.apply_prune("backup", prune, Log())
        policy = ret.Policy(margin_pct=0)
        ok, asked, out = self._decide(fake, 0, 10**12, False, policy)
        assert ok is None and asked == ["Remove these 1 backup point(s) from backup?"]
        assert P2 in out and P2 in fake.names("backup/rpool/ROOT/be")
        ok, asked, _ = self._decide(fake, 0, 10**12, True, policy, "zark_2026-10-12_10:00:00Z")
        assert ok and P2 not in fake.names("backup/rpool/ROOT/be")
        assert P3 in fake.names("backup/rpool/ROOT/be")

    def test_margin_is_a_goal_not_a_requirement(self) -> None:
        """The transfer fits but the margin cannot be reached: the backup goes
        on with a warning, even when the plan is declined."""
        fake = self._history()
        ok, asked, out = self._decide(fake, 10**9, 10**15, False, ret.Policy())
        assert ok and asked
        assert "less than 10% free after this backup" in out
        assert P2 in fake.names("backup/rpool/ROOT/be")

    def test_a_new_clones_origin_survives_the_space_plan(self) -> None:
        """The snapshot a new clone will be received from is a base of this run."""
        fake = self._history()
        src = "rpool/ROOT/be@autosnap_2026-10-02_08:00:00_hourly"
        assert "autosnap_2026-10-02_08:00:00_hourly" in fake.names("backup/rpool/ROOT/be")
        fake.dataset("rpool/ROOT/be2", origin=src, mountpoint="/")
        ok, _asked, _ = self._decide(fake, 0, 10**12, False, ret.Policy(margin_pct=0))
        assert ok
        assert "autosnap_2026-10-02_08:00:00_hourly" in fake.names("backup/rpool/ROOT/be")
        assert "autosnap_2026-10-01_08:00:00_hourly" not in fake.names("backup/rpool/ROOT/be")

    def test_nothing_is_removed_with_room(self) -> None:
        """With room for the transfer and the margin, nothing on the drive changes."""
        fake = self._history()
        before = {ds: fake.names(ds) for ds in fake.ds if ds.startswith("backup/")}
        ok, asked, _ = self._decide(fake, 10**12, 10**12, True, ret.Policy())
        assert ok and not asked
        assert before == {ds: fake.names(ds) for ds in fake.ds if ds.startswith("backup/")}

    def test_the_newest_complete_point_is_never_offered(self) -> None:
        """Even outside the window, with a newer incomplete point on some
        datasets making it removable there (M3R-6, R12)."""
        fake = self._history()
        p4, p5 = "zark_2026-10-10_10:00:00Z", "zark_2026-10-14_10:00:00Z"
        _tick(fake, "2026-10-14_08:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        assert not _engine_run(fake, "backup", "111", p5).ok
        fake.inject.clear()
        assert p5 in fake.names("backup/rpool/ROOT/be") and p5 not in fake.names(
            "backup/rpool/USERDATA/home"
        )
        with patch.object(engine.Run, "estimate", return_value=10**15):
            ok, asked, out = self._decide(
                fake, 0, 10**12, True, ret.Policy(window_days=1, margin_pct=0)
            )
        assert ok is None and asked  # nothing fits: fatal after the plan
        assert p4 not in out
        for rel in ("rpool/ROOT/be", "rpool/USERDATA/home", "rpool/var", "bpool/BOOT/be"):
            assert p4 in fake.names(f"backup/{rel}"), rel

    def test_the_space_plan_defaults_to_no(self) -> None:
        """Enter and EOF at "Remove these N backup point(s)?" remove nothing (M3R-7, B2)."""
        for answer in (["", EOFError], EOFError):  # the fatal's "Press Enter" prompt follows
            fake = self._history()
            out = StringIO()
            with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
                eng = engine.Run("backup", "111", Log(), self.NEXT, sleep=lambda _s: None)
                assert eng.take_point()
                plans = eng.survey()
                with (
                    patch.object(backup_mod.time, "time", return_value=float(NOW)),
                    patch("builtins.input", side_effect=answer) as asked,
                    patch.object(engine.Run, "estimate", return_value=10**15),
                ):
                    try:
                        _decide(
                            plans,
                            eng,
                            "backup",
                            PoolInfo("backup", avail_bytes=0, size_bytes=10**12),
                            Log(),
                            ret.Policy(margin_pct=0),
                        )
                    except SystemExit:
                        pass
            assert "Remove these" in out.getvalue()
            assert "[y/N]" in asked.call_args_list[0].args[0]
            assert P2 in fake.names("backup/rpool/ROOT/be") and P3 in fake.names(
                "backup/rpool/ROOT/be"
            )

    def test_an_archived_lineage_the_space_plan_empties_goes_whole(self) -> None:
        """7a in the space plan (M3R-3, E4): an archived lineage whose only
        snapshot is the monthly point removed is destroyed whole, named in
        the question, and its `used` counts in what the plan frees."""
        fake = self._history()
        arch = "backup/rpool/var.archived-20260801"
        fake.dataset(arch)
        fake.snap(arch, P2).creation = _days_ago(66)
        with patch.object(engine.Run, "estimate", return_value=5 << 20):  # P2 alone frees it
            ok, asked, out = self._decide(fake, 0, 10**12, True, ret.Policy(margin_pct=0))
        assert ok and asked == ["Remove these 1 backup point(s) from backup?"]
        assert f"{arch} and everything below it go too" in out
        assert arch not in fake.ds
        assert f"zfs destroy -r {arch}" in fake.calls
        assert P2 not in fake.names("backup/rpool/ROOT/be")


class TestOriginInventory:  # pylint: disable=too-few-public-methods
    """M3-9/M3-11: what `zark prune` sees in origin, by kind."""

    def test_kinds_and_the_residue_rule(self) -> None:
        """A syncoid residue goes only where every registered drive has an
        anchor; the keystore's prepare_* always; points and bpool anchors apart."""
        home, var = "rpool/USERDATA/home", "rpool/var"

        def ref(name: str, n: int) -> repl.Ref:
            return repl.Ref(name, str(n), n, 1000 + n)

        origin = {
            home: repl.Origin(
                home,
                snaps=[
                    ref("autosnap_2026-10-01_00:00:00_daily", 1),
                    ref("autosnap_2026-10-01_01:00:00_hourly", 2),
                    ref("syncoid_carmen_2026-09-27:10:28:08-GMT02:00", 3),
                    ref("zark_2026-10-02_10:00:00Z", 4),
                ],
                bookmarks=[
                    ref("zark_111_2026-10-01_10:00:00Z", 5),
                    ref("zark_222_2026-10-01_10:00:00Z", 6),
                ],
            ),
            var: repl.Origin(
                var,
                snaps=[ref("syncoid_carmen_2026-09-27:10:28:08-GMT02:00", 7)],
                bookmarks=[ref("zark_111_2026-10-01_10:00:00Z", 8)],
            ),
            "rpool/keystore": repl.Origin(
                "rpool/keystore", "volume", snaps=[ref("prepare_20260927_082338", 9)]
            ),
            "bpool/BOOT/be": repl.Origin(
                "bpool/BOOT/be", snaps=[ref("zark_222_2026-10-01_10:00:00Z", 10)]
            ),
            "rpool/ROOT/be": repl.Origin("rpool/ROOT/be", snaps=[ref("before-upgrade", 11)]),
        }
        inv = oprune.inventory(origin, {"111": "black", "222": "blue"})
        assert sorted(inv.autosnaps) == ["daily", "hourly"]
        assert [i.target for i in inv.residues] == [
            f"{home}@syncoid_carmen_2026-09-27:10:28:08-GMT02:00",
            "rpool/keystore@prepare_20260927_082338",
        ]
        kept = inv.residues_kept
        assert [i.target for i in kept] == [f"{var}@syncoid_carmen_2026-09-27:10:28:08-GMT02:00"]
        assert "blue has no anchor" in kept[0].reason
        assert [i.target for i in inv.points] == [f"{home}@zark_2026-10-02_10:00:00Z"]
        on_root = repl.Origin(
            "rpool", snaps=[ref("syncoid_carmen_2026-09-27:10:28:08-GMT02:00", 12)]
        )
        container = oprune.inventory({"rpool": on_root}, {"222": "blue"})
        assert [i.target for i in container.residues] == [
            "rpool@syncoid_carmen_2026-09-27:10:28:08-GMT02:00"
        ]
        assert [i.target for i in inv.anchors["222"]] == [
            "bpool/BOOT/be@zark_222_2026-10-01_10:00:00Z"
        ]
        names = [i.target for items in inv.autosnaps.values() for i in items]
        assert not any("before-upgrade" in n for n in names + [i.target for i in inv.residues])


class TestPruneLegacyHistory:  # pylint: disable=too-few-public-methods
    """What `zark prune` offers as a drive's 1.x history."""

    def test_only_sanoid_and_syncoid_snapshots_before_the_first_point(self) -> None:
        """The first point counts from its newest snapshot (rpool's copy is
        taken before bpool's), so none of it is history; manual names stay;
        each live dataset keeps its newest."""
        first = _pt("2026-03-01")
        dest = _drive(
            {
                "rpool/ROOT/be": [
                    ("autosnap_2026-02-01_00:00:00_monthly", 500),
                    ("syncoid_carmen_2026-02-02:10:00:00-GMT01:00", 600),
                    ("before-upgrade", 700),
                    (first, 1000),
                    ("autosnap_2026-03-02_00:00:00_daily", 2000),
                ],
                "bpool/BOOT/be": [("autosnap_2026-02-01_00:00:00_monthly", 500), (first, 1001)],
            },
        )
        got = {r.target for r in prune_mod._legacy_history(dest, {}).snapshots}  # pylint: disable=protected-access
        assert got == {
            "rpool/ROOT/be@autosnap_2026-02-01_00:00:00_monthly",
            "rpool/ROOT/be@syncoid_carmen_2026-02-02:10:00:00-GMT01:00",
            "bpool/BOOT/be@autosnap_2026-02-01_00:00:00_monthly",
        }

    def test_an_archived_lineage_left_empty_goes_whole(self) -> None:
        """An archived lineage holding only 1.x history is destroyed whole
        (decision 7a: its head would keep its data out of recover's reach);
        one with a pinned snapshot keeps it and stays."""
        first = _pt("2026-03-01")
        old = "rpool/ROOT/be.archived-20260301"
        pinned = "rpool/ROOT/be2.archived-20260301"
        dest = _drive(
            {
                "rpool/ROOT/be": [("autosnap_2026-02-01_00:00:00_monthly", 500), (first, 1000)],
                old: [("autosnap_2026-01-01_00:00:00_monthly", 100)],
                f"{old}/var": [("autosnap_2026-01-01_00:00:00_monthly", 100)],
                pinned: [
                    ("autosnap_2026-01-01_00:00:00_monthly", 100),
                    ("autosnap_2026-01-02_00:00:00_daily", 200),
                ],
            },
        )
        pins = {f"{pinned}@autosnap_2026-01-02_00:00:00_daily": "origin of a clone"}
        got = prune_mod._legacy_history(dest, pins)  # pylint: disable=protected-access
        assert got.datasets == [(old, "archived lineage with nothing left that the policy keeps")]
        assert f"{pinned}@autosnap_2026-01-01_00:00:00_monthly" in _gone(got)
        assert not any(r.target in pins for r in got.snapshots)


class TestPruneCommand:
    """`zark prune` on FakeZfs: origin and a drive."""

    @staticmethod
    def _answers(
        choices: list[int], inputs: list[str], typed: str, yes: bool
    ) -> list[AbstractContextManager[object]]:
        return [
            patch.object(Log, "ask_choice", side_effect=choices),
            patch.object(Log, "ask_input", side_effect=inputs),
            patch.object(Log, "ask_text", return_value=typed),
            patch.object(Log, "ask", return_value=yes),
            patch("builtins.input", side_effect=EOFError),
        ]

    def _origin(self, fake: FakeZfs, cfg: Config, *answers: AbstractContextManager[object]) -> str:
        out = StringIO()
        with ExitStack() as stack:
            for cm in (patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()), *answers):
                stack.enter_context(cm)
            prune_mod._prune_origin(cfg, Log())  # pylint: disable=protected-access
        return out.getvalue()

    def test_sanoid_snapshots_by_age_and_the_next_backup_stays_incremental(self) -> None:
        """No floor: every sanoid snapshot older than the cutoff goes, one
        exact name per destroy; the drive's bookmarks carry the next backup."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "d")
        now = fake.clock + 10 * ret.DAY
        before = len(fake.calls)
        out = self._origin(
            fake,
            cfg,
            *self._answers([1, 0], ["5"], "PRUNE", False),
            patch.object(prune_mod.time, "time", return_value=float(now)),
        )
        assert "sanoid daily" in out
        destroys = [
            c for c in fake.calls[before:] if c.startswith("zfs destroy ") and "-nvp" not in c
        ]
        assert destroys and all("%" not in c and "," not in c and " -r" not in c for c in destroys)
        assert not any(n.startswith("autosnap_") for n in fake.names("rpool/USERDATA/home"))
        assert fake.bookmarks("rpool/USERDATA/home")
        with patch_sh(fake), redirect_stdout(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            assert eng.take_point()
            plans = {p.rel: p.state for p in eng.survey()}
        assert plans["rpool/USERDATA/home"] is repl.State.VIA_BOOKMARK

    def test_mistyped_word_removes_nothing(self) -> None:
        """A typed word that is not PRUNE removes nothing."""
        fake = _fresh_fake()
        before = {ds: fake.names(ds) for ds in fake.ds}
        out = self._origin(fake, make_config(), *self._answers([1, 0], ["0"], "prune", True))
        assert before == {ds: fake.names(ds) for ds in fake.ds}
        assert "Not confirmed (PRUNE was not typed) — nothing removed" in out

    def test_interrupted_points_warn_of_both_effects(self) -> None:
        """M3 decision 8a (eli R8.7): removing an interrupted run's point
        costs a rollback on a drive that received part of it, and a resend
        on one whose transfer it started; the warning names both."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        assert not _engine_run(fake, "backup", "111", P2).ok
        assert _origin_points(fake)

        picked: list[str] = []

        def pick(_q: str, options: list[str], _d: int = 0) -> int:
            wanted = (
                "remove backup points left in origin (interrupted runs, or a run whose anchor "
                + "failed)"
            )
            if wanted in options and not picked:
                picked.append(wanted)
                return options.index(wanted)
            return 0

        out = self._origin(
            fake,
            make_config(),
            patch.object(Log, "ask_choice", side_effect=pick),
            patch.object(Log, "ask_text", return_value="prune"),
            patch("builtins.input", side_effect=EOFError),
        )
        text = " ".join(out.split())
        assert (
            "A drive that received part of one of these rolls those datasets back at its next "
            + "backup (it asks first; only snapshots of that incomplete point go), a drive "
            + "whose transfer was interrupted discards it and sends it again, and a drive whose "
            + "backup completed but could not be anchored on a dataset rolls that dataset back "
            + "too (its newest backup point goes on it)"
        ) in text
        assert _origin_points(fake)

    def test_residues_need_a_yes_and_follow_the_rule(self) -> None:
        """11a: removed after a yes; one a registered drive may need stays."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        for ds in ("rpool/USERDATA/home", "rpool/var"):
            fake.snap(ds, "syncoid_carmen_2026-09-27:10:28:08-GMT02:00")
        fake.snap("rpool/keystore", "prepare_20260927_082338")
        fake.ds["rpool/var"].bookmarks = []  # this drive's anchor on var is gone
        cfg = make_config()
        cfg.known_drives["black"] = DriveInfo("black", "111", "d")
        out = self._origin(fake, cfg, *self._answers([2, 0], [], "", True))
        assert "syncoid_carmen" not in " ".join(fake.names("rpool/USERDATA/home"))
        assert not fake.names("rpool/keystore")
        assert "syncoid_carmen_2026-09-27:10:28:08-GMT02:00" in fake.names("rpool/var")
        assert "black has no anchor on rpool/var" in out

    def test_a_drives_bpool_anchors_go_and_its_next_backup_asks(self) -> None:
        """10b: the consequence is said first; that drive's bpool is then no
        longer incremental."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _prune_all_autosnaps(fake)
        cfg = make_config()
        cfg.known_drives["blue"] = DriveInfo("blue", "111", "d")
        out = self._origin(fake, cfg, *self._answers([1, 1, 0], [], "PRUNE", False))
        assert "asks to roll them back (the drive's newest backup point goes on them)" in out
        assert not [n for n in fake.names("bpool/BOOT/be") if repl.anchor_disk(n)]
        with patch_sh(fake), redirect_stdout(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            assert eng.take_point()
            plans = {p.rel: p.state for p in eng.survey()}
        assert plans["bpool/BOOT/be"] in (repl.State.ROLLBACK, repl.State.DIVERGED)
        assert plans["rpool/ROOT/be"] is repl.State.VIA_BOOKMARK

    def test_drive_policy_then_the_oldest_points(self) -> None:
        """A drive: its policy on a yes, then the oldest monthly point on a
        typed PRUNE; the newest complete point is never offered."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "d")
        drive = ConnectedDrive("backup", "111", "d", "/dev/sdb", True, False, False, "exported")
        out = StringIO()
        with ExitStack() as stack:
            for cm in (
                patch_sh(fake),
                redirect_stdout(out),
                redirect_stderr(StringIO()),
                patch.object(ZFS, "import_backup_pool", return_value=True),
                patch.object(prune_mod, "backup_device", return_value="/dev/sdb1"),
                patch.object(prune_mod, "prompt_eject_or_attach"),
                patch.object(Cleanup, "register", lambda _self: None),
                patch.object(prune_mod.time, "time", return_value=float(NOW)),
                *self._answers([], ["1"], "PRUNE", True),
            ):
                stack.enter_context(cm)
            prune_mod._prune_drive(cfg, drive, Log())  # pylint: disable=protected-access
        be = fake.names("backup/rpool/ROOT/be")
        assert P1 not in be and P2 not in be and P3 in be
        assert "zark_2026-10-10_10:00:00Z" in be

    def test_beyond_the_policy_lists_only_points_with_something_to_remove(self) -> None:
        """M3 decision 6a (eli R5): a point held only by an orphan's newest
        snapshot (I-E) frees nothing and is not offered. Since M3F-5 5b the
        other datasets, which hold a point before and after it, make it not
        whole, so its month's point is the whole one before it (M3R-4)."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        for ds in [d for d in fake.ds if d.startswith("backup/")]:
            fake.ds[ds].snaps = [s for s in fake.ds[ds].snaps if s.name != P2]
        fake.dataset("backup/rpool/zt9")
        held = fake.snap("backup/rpool/zt9", P2)
        held.creation = _days_ago(66)
        held.props[COMPLETE_PROP] = "7"  # still its month's newest complete point
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "d")
        drive = ConnectedDrive("backup", "111", "d", "/dev/sdb", True, False, False, "exported")
        out = StringIO()
        with ExitStack() as stack:
            for cm in (
                patch_sh(fake),
                redirect_stdout(out),
                redirect_stderr(StringIO()),
                patch.object(ZFS, "import_backup_pool", return_value=True),
                patch.object(prune_mod, "backup_device", return_value="/dev/sdb1"),
                patch.object(prune_mod, "prompt_eject_or_attach"),
                patch.object(Cleanup, "register", lambda _self: None),
                patch.object(prune_mod.time, "time", return_value=float(NOW)),
                *self._answers([], ["0"], "", False),
            ):
                stack.enter_context(cm)
            prune_mod._prune_drive(cfg, drive, Log())  # pylint: disable=protected-access
        listed = [line for line in out.getvalue().splitlines() if "(with the ones above:" in line]
        assert listed and P2 not in "".join(listed)
        assert f"1) {P1}" in listed[0] and f"2) {P3}" in listed[1]
        assert P2 in fake.names("backup/rpool/zt9")
        assert P1 in fake.names("backup/rpool/ROOT/be")  # its month's newest whole point now

    def test_legacy_history_boundaries(self) -> None:
        """6c: only names from before the first point go; a legacy snapshot
        after it stays, and on a drive with no point every live dataset
        keeps its newest while the older legacy names go (M3R-6, P3, P4)."""
        be, home = "rpool/ROOT/be", "rpool/USERDATA/home"
        dest = _drive(
            {
                be: [
                    ("autosnap_2026-10-01_00:00:00_daily", _days_ago(100)),
                    (_pt("2026-12-01"), _days_ago(45)),
                    ("autosnap_2026-12-06_00:00:00_daily", _days_ago(40)),
                    (_pt("2027-01-05"), _days_ago(10)),
                ],
            },
        )
        legacy = prune_mod._legacy_history(dest, {})  # pylint: disable=protected-access
        assert {r.target for r in legacy.snapshots} == {f"{be}@autosnap_2026-10-01_00:00:00_daily"}
        dest = _drive(
            {
                be: [
                    ("autosnap_2026-10-01_00:00:00_daily", _days_ago(100)),
                    ("syncoid_carmen_2026-10-11:10:00:00-GMT02:00", _days_ago(90)),
                ],
                home: [
                    ("autosnap_2026-10-01_00:00:00_daily", _days_ago(100)),
                    ("autosnap_2026-11-20_00:00:00_daily", _days_ago(50)),
                ],
            },
        )
        legacy = prune_mod._legacy_history(dest, {})  # pylint: disable=protected-access
        assert {r.target for r in legacy.snapshots} == {
            f"{be}@autosnap_2026-10-01_00:00:00_daily",
            f"{home}@autosnap_2026-10-01_00:00:00_daily",
        }
        assert legacy.datasets == []

    def test_the_residue_question_defaults_to_no(self) -> None:
        """Enter and EOF at "Remove these N residue(s)?" remove nothing (M3R-7, P5)."""
        for answer in ([""], EOFError):
            fake = _fresh_fake()
            assert _engine_run(fake, "backup", "111", P1).ok
            fake.snap("rpool/keystore", "prepare_20260927_082338")
            with patch("builtins.input", side_effect=answer) as asked:
                out = self._origin(
                    fake, make_config(), patch.object(Log, "ask_choice", side_effect=[2, 0])
                )
            assert "Remove these 1 residue(s)" in out
            assert "[y/N]" in asked.call_args_list[0].args[0]
            assert fake.names("rpool/keystore") == ["prepare_20260927_082338"]

    def test_the_drive_policy_question_defaults_to_no(self) -> None:
        """Enter and EOF at "Apply <pool>'s retention now?" remove nothing (M3R-7, P6)."""
        for answer in ([""], EOFError):
            fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
            cfg = make_config()
            cfg.known_drives["backup"] = DriveInfo("backup", "111", "d")
            drive = ConnectedDrive("backup", "111", "d", "/dev/sdb", True, False, False, "exported")
            out = StringIO()
            with ExitStack() as stack:
                for cm in (
                    patch_sh(fake),
                    redirect_stdout(out),
                    redirect_stderr(StringIO()),
                    patch.object(ZFS, "import_backup_pool", return_value=True),
                    patch.object(prune_mod, "backup_device", return_value="/dev/sdb1"),
                    patch.object(prune_mod, "prompt_eject_or_attach"),
                    patch.object(Cleanup, "register", lambda _self: None),
                    patch.object(prune_mod.time, "time", return_value=float(NOW)),
                    patch.object(Log, "ask_input", return_value="0"),
                ):
                    stack.enter_context(cm)
                asked = stack.enter_context(patch("builtins.input", side_effect=answer))
                prune_mod._prune_drive(cfg, drive, Log())  # pylint: disable=protected-access
            assert "Apply backup's retention now?" in out.getvalue()
            assert "[y/N]" in asked.call_args_list[0].args[0]
            assert P1 in fake.names("backup/rpool/ROOT/be")

    def test_beyond_policy_removals_keep_pins(self) -> None:
        """A clone origin in the oldest monthly point is left out of what the
        typed PRUNE removes, so nothing is refused (M3R-13, P7)."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        with patch_sh(fake), redirect_stdout(StringIO()):
            dest = engine.read_dest("backup", strict=True)
            prune = ret.plan_prune(dest, engine.drive_complete("backup").run, ret.Policy(), NOW)
            _ = engine.apply_prune("backup", prune, Log())
        home = fake.ds["backup/rpool/USERDATA/home"]
        next(x for x in home.snaps if x.name == P2).clones = "backup/rpool/USERDATA/home2"
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "d")
        drive = ConnectedDrive("backup", "111", "d", "/dev/sdb", True, False, False, "exported")
        out = StringIO()
        with ExitStack() as stack:
            for cm in (
                patch_sh(fake),
                redirect_stdout(out),
                redirect_stderr(StringIO()),
                patch.object(ZFS, "import_backup_pool", return_value=True),
                patch.object(prune_mod, "backup_device", return_value="/dev/sdb1"),
                patch.object(prune_mod, "prompt_eject_or_attach"),
                patch.object(Cleanup, "register", lambda _self: None),
                patch.object(prune_mod.time, "time", return_value=float(NOW)),
                *self._answers([], ["1"], "PRUNE", False),
            ):
                stack.enter_context(cm)
            prune_mod._prune_drive(cfg, drive, Log())  # pylint: disable=protected-access
        assert P2 not in fake.names("backup/rpool/ROOT/be")
        assert P2 in fake.names("backup/rpool/USERDATA/home")
        assert "could not remove" not in out.getvalue()

    def test_prune_is_fatal_when_the_pins_cannot_be_read(self) -> None:
        """M3R-8: zark prune on a drive stops before any removal."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        fake.fail_cmds.add("-o name,clones,userrefs -r backup/rpool")
        before = {ds: fake.names(ds) for ds in fake.ds}
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "d")
        drive = ConnectedDrive("backup", "111", "d", "/dev/sdb", True, False, False, "exported")
        out = StringIO()
        with ExitStack() as stack:
            for cm in (
                patch_sh(fake),
                redirect_stdout(out),
                redirect_stderr(StringIO()),
                patch.object(ZFS, "import_backup_pool", return_value=True),
                patch.object(prune_mod, "backup_device", return_value="/dev/sdb1"),
                patch.object(prune_mod, "prompt_eject_or_attach"),
                patch.object(Cleanup, "register", lambda _self: None),
                patch.object(prune_mod.time, "time", return_value=float(NOW)),
                *self._answers([], ["1"], "PRUNE", True),
            ):
                stack.enter_context(cm)
            try:
                prune_mod._prune_drive(cfg, drive, Log())  # pylint: disable=protected-access
                raise AssertionError("prune went on without the pins")
            except SystemExit:
                pass
        assert "Cannot read backup" in out.getvalue()
        assert before == {ds: fake.names(ds) for ds in fake.ds}

    def test_an_archived_lineage_a_beyond_policy_point_empties_goes_whole(self) -> None:
        """7a beyond the policy (M3R-3): the lineage is named before the
        typed PRUNE and destroyed whole with the point."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        arch = "backup/rpool/var.archived-20260801"
        fake.dataset(arch)
        fake.snap(arch, P2).creation = _days_ago(66)
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "d")
        drive = ConnectedDrive("backup", "111", "d", "/dev/sdb", True, False, False, "exported")
        out = StringIO()
        with ExitStack() as stack:
            for cm in (
                patch_sh(fake),
                redirect_stdout(out),
                redirect_stderr(StringIO()),
                patch.object(ZFS, "import_backup_pool", return_value=True),
                patch.object(prune_mod, "backup_device", return_value="/dev/sdb1"),
                patch.object(prune_mod, "prompt_eject_or_attach"),
                patch.object(Cleanup, "register", lambda _self: None),
                patch.object(prune_mod.time, "time", return_value=float(NOW)),
                *self._answers([], ["1"], "PRUNE", False),
            ):
                stack.enter_context(cm)
            prune_mod._prune_drive(cfg, drive, Log())  # pylint: disable=protected-access
        text = out.getvalue()
        assert f"{arch} and everything below it go too" in text
        assert arch not in fake.ds and f"zfs destroy -r {arch}" in fake.calls
        assert P2 not in fake.names("backup/rpool/ROOT/be") and P1 in fake.names(
            "backup/rpool/ROOT/be"
        )


class TestStatus:
    """M3 decisions 12 and 13: CAP, RPO, the MOTD and the notifications."""

    def test_cap_levels(self) -> None:
        """Warning from 80 %, critical from 90 %."""
        assert [status_lib.cap_level(p) for p in (79, 80, 89, 90)] == [
            "",
            "warning",
            "warning",
            "critical",
        ]

    def test_motd_lines(self) -> None:
        """CAP lines only at a level; the RPO line always, highlighted when the
        newest backup of all is older than 7 days; H21 and residue lines."""
        st = status_lib.Status(
            caps={"rpool": 86, "bpool": 40},
            ages={"black": 3, "blue": 34},
            shared=["blue: no anchor in origin; 1 dataset(s) share no snapshot with it: …"],
            residues=23,
        )
        text = status_lib.motd_text(st)
        assert "rpool is 86% full (warning from 80%): sudo zark prune" in text
        assert "bpool" not in text
        assert "zark: last backups: black 3 day(s) ago, blue 34 day(s) ago" in text
        assert "blue: no anchor in origin" in text and "23 legacy snapshot(s)" in text

    def test_a_failed_read_of_origin_says_so(self) -> None:
        """M3R-2 (E3): with the snapshot listing failing, no drive is called
        unanchored; one line says the source pools could not be read."""
        fake = _fresh_fake()
        _tick(fake, "2026-10-06_12:00:00_hourly")
        assert _engine_run(fake, "backup", "111", P1).ok
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo(
            "backup", "111", "d", last_backup_at="2026-10-08T10:00:00Z"
        )
        with patch_sh(fake), redirect_stdout(StringIO()):
            good = status_lib.gather(cfg, Log())
            fake.fail_cmds.add("zfs list -Hp -t snapshot,bookmark")
            bad = status_lib.gather(cfg, Log())
        assert good.shared == [] and not good.read_error
        assert bad.shared == [] and bad.read_error.startswith("could not read the source pools")
        assert bad.ages == good.ages and bad.residues == 0
        text = status_lib.motd_text(bad)
        assert "could not read the source pools" in text and "no anchor" not in text
        out = StringIO()
        with redirect_stdout(out):
            status_mod._report(bad, Log())  # pylint: disable=protected-access
        assert "could not read the source pools" in out.getvalue()
        stale = status_lib.motd_text(status_lib.Status(ages={"black": 9}))
        assert "\033[1;33mzark:\033[0m last backups: black 9 day(s) ago" in stale
        assert not status_lib.motd_text(status_lib.Status())

    def test_notifications_once_a_day_per_pool_and_level(self) -> None:
        """A pool notified today at a level is not notified again today."""
        st = status_lib.Status(caps={"rpool": 92, "bpool": 50})
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        due = status_lib.due_notifications(st, set(), now)
        assert [k for k, _ in due] == ["rpool critical 2026-10-12"]
        assert not status_lib.due_notifications(st, {"rpool critical 2026-10-12"}, now)

    def test_refresh_writes_the_motd_file(self) -> None:
        """status --refresh: CAP from zpool, ages from the registry; the file
        is replaced, readable by anyone."""
        fake = _fresh_fake()
        fake.capacity["rpool"] = 91
        cfg = make_config()
        cfg.known_drives["black"] = DriveInfo("black", "111", "d")
        with patch_sh(fake), redirect_stdout(StringIO()):
            st = status_lib.refresh(cfg, Log(os.devnull))
            text = status_lib.MOTD_FILE.read_text(encoding="utf-8")
            mode = status_lib.MOTD_FILE.stat().st_mode & 0o777
        assert st.caps == {"rpool": 91, "bpool": 50}
        assert "rpool is 91% full (critical from 90%)" in text and mode == 0o644
        assert "black never" in text

    def test_status_checks_what_an_unanchored_drive_shared(self) -> None:
        """M3 decision 5e on the real reading of origin: a drive with no
        anchor is told until when it shares snapshots, then, once origin
        lost them (a recover, a prune), that it resends in full."""
        fake = _fresh_fake()
        home = next(s for s in fake.ds["rpool/USERDATA/home"].snaps if "daily" in s.name)
        cfg = make_config()
        cfg.known_drives["blue"] = DriveInfo(
            "blue",
            "999",
            "d",
            extra={"shared": {"rpool/USERDATA/home": [[home.name, home.guid, home.creation]]}},
        )
        lives = {"daily": 7 * 86400.0}
        with patch_sh(fake), patch.object(drives_lib, "guaranteed_lives", return_value=lives):
            st = status_lib.gather(cfg, Log(os.devnull))
        day = datetime.fromtimestamp(home.creation + 7 * 86400, UTC).strftime("%Y-%m-%d")
        assert st.shared == [
            f"blue: no anchor in origin; it still shares snapshots with it until about {day} "
            + f"(rpool/USERDATA/home@{home.name}){drives_lib.ROLLBACK_NOTE}"
        ]
        fake.ds["rpool/USERDATA/home"].snaps.remove(home)
        with patch_sh(fake), patch.object(drives_lib, "guaranteed_lives", return_value=lives):
            st = status_lib.gather(cfg, Log(os.devnull))
        assert st.shared == [
            "blue: no anchor in origin; 1 dataset(s) share no snapshot with it: "
            + "its next backup resends them in full"
        ]

    def test_notify_reaches_active_desktop_sessions(self) -> None:
        """loginctl's active Wayland and X11 sessions get notify-send as
        their own user, on their own session bus."""
        mock = MockShell()
        mock.on("loginctl list-sessions --no-legend").succeeds(
            "2 1000 br0th3r seat0 tty2\n3 0 root - pts/0\n"
        )
        mock.on("loginctl show-session 2 -p Type -p Active -p Name -p User").succeeds(
            "Type=wayland\nActive=yes\nName=br0th3r\nUser=1000\n"
        )
        mock.on("loginctl show-session 3 -p Type -p Active -p Name -p User").succeeds(
            "Type=tty\nActive=yes\nName=root\nUser=0\n"
        )
        mock.on_prefix("runuser").succeeds("")
        with patch_sh(mock):
            assert notify_mod.notify("zark: disk space", "rpool is 92% full") == 1
        call = next(c for c in mock.calls if c.startswith("runuser"))
        assert call.startswith(
            "runuser -u br0th3r -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"
        )
        assert "notify-send" in call and "'rpool is 92% full'" in call

    def test_inactive_sessions_are_not_notified(self) -> None:
        """A locked seat or a greeter (Active=no) gets nothing (M3R-13, A5)."""
        mock = MockShell()
        mock.on("loginctl list-sessions --no-legend").succeeds(
            "2 1000 br0th3r seat0 tty2\n4 1001 guest seat0 tty3\n"
        )
        mock.on("loginctl show-session 2 -p Type -p Active -p Name -p User").succeeds(
            "Type=wayland\nActive=yes\nName=br0th3r\nUser=1000\n"
        )
        mock.on("loginctl show-session 4 -p Type -p Active -p Name -p User").succeeds(
            "Type=wayland\nActive=no\nName=guest\nUser=1001\n"
        )
        mock.on_prefix("runuser").succeeds("")
        with patch_sh(mock):
            assert notify_mod.notify("zark: disk space", "rpool is 92% full") == 1
        calls = [c for c in mock.calls if c.startswith("runuser")]
        assert len(calls) == 1 and "-u br0th3r " in calls[0]

    def test_status_command_notifies_once_a_day(self) -> None:
        """The timer's `status --refresh --notify` twice: one notification."""
        fake = _fresh_fake()
        fake.capacity["rpool"] = 85
        sent: list[str] = []

        def notify(_title: str, body: str) -> int:
            sent.append(body)
            return 1

        with (
            patch_sh(fake),
            patch.object(Config, "load", return_value=make_config()),
            patch.object(notify_mod, "notify", side_effect=notify),
            redirect_stdout(StringIO()),
        ):
            status_mod.run(["--refresh", "--notify"])
            status_mod.run(["--refresh", "--notify"])
        assert sent == ["rpool is 85% full — free space with: sudo zark prune"]


class TestAlerts:
    """M3 decision 13a: what `zark alerts install|remove` (and setup) write."""

    def test_install_and_remove(self) -> None:
        """MOTD script that only prints the status file; a service running this
        zark by absolute path, guarded by its existence; an hourly timer;
        idempotent; remove leaves nothing."""
        mock = MockShell()
        mock.on_prefix("systemctl").succeeds("")
        with tempfile.TemporaryDirectory() as root, patch_sh(mock), redirect_stdout(StringIO()):
            alerts_mod.install(make_log(), root, "/opt/zark/zark")
            motd = Path(root, alerts_mod.MOTD_SCRIPT_PATH)
            service = Path(root, alerts_mod.SERVICE_PATH).read_text(encoding="utf-8")
            timer = Path(root, alerts_mod.TIMER_PATH).read_text(encoding="utf-8")
            assert motd.stat().st_mode & 0o777 == 0o755
            assert "cat /run/zark/status.motd" in motd.read_text(encoding="utf-8")
            assert 'ExecStart="/opt/zark/zark" status --refresh --notify' in service
            assert "ConditionPathExists=/opt/zark/zark" in service
            assert "OnUnitActiveSec=1h" in timer and "OnBootSec=2min" in timer
            reloads = mock.calls.count("systemctl daemon-reload")
            alerts_mod.install(make_log(), root, "/opt/zark/zark")
            assert mock.calls.count("systemctl daemon-reload") == reloads  # nothing changed
            assert "systemctl enable --now zark-status.timer" in mock.calls
            alerts_mod.remove(make_log(), root)
            assert not any(
                Path(root, p).exists()
                for p in (
                    alerts_mod.MOTD_SCRIPT_PATH,
                    alerts_mod.SERVICE_PATH,
                    alerts_mod.TIMER_PATH,
                )
            )
            assert "systemctl disable --now zark-status.timer" in mock.calls

    def test_a_path_with_spaces_and_percent(self) -> None:
        """zark run from a stick mounted under its label: ExecStart quotes the
        path, both settings escape systemd's % specifiers."""
        unit = alerts_mod.service_unit("/run/media/u/Juanmi Taboada/50%/zark")
        assert 'ExecStart="/run/media/u/Juanmi Taboada/50%%/zark" status' in unit
        assert "ConditionPathExists=/run/media/u/Juanmi Taboada/50%%/zark\n" in unit

    def test_a_path_systemd_refuses_installs_nothing(self) -> None:
        """M3R-14 (M3F-9 9a): a quote, a backslash or a control character in
        the path makes systemd refuse the unit, so install warns and writes
        nothing instead of a unit that never loads."""
        for path in ('/run/media/u/a"b/zark', "/run/media/u/it's/zark", "/x\\y/zark", "/x\ty/zark"):
            mock = MockShell()
            mock.on_prefix("systemctl").succeeds("")
            out = StringIO()
            with tempfile.TemporaryDirectory() as root, patch_sh(mock), redirect_stdout(out):
                alerts_mod.install(make_log(), root, path)
                assert not Path(root, alerts_mod.SERVICE_PATH).exists()
                assert not Path(root, alerts_mod.TIMER_PATH).exists()
            assert "systemd refuses in ExecStart" in out.getvalue(), path
            assert not mock.calls
        assert alerts_mod.unsafe_char("/run/media/u/Juanmi Taboada/50%/zark") == ""

    def test_remove_takes_the_files_the_timer_wrote(self) -> None:
        """M3R-10 (M3F-7 7a): the MOTD file and the notified marker go; the
        directory stays."""
        mock = MockShell()
        mock.on_prefix("systemctl").succeeds("")
        with tempfile.TemporaryDirectory() as root, patch_sh(mock), redirect_stdout(StringIO()):
            run_dir = Path(root, "run/zark")
            run_dir.mkdir(parents=True)
            motd, notified, other = (
                run_dir / n for n in ("status.motd", "notified", "sanoid-paused")
            )
            for f in (motd, notified, other):
                f.write_text("x", encoding="utf-8")
            with (
                patch.object(status_lib, "MOTD_FILE", motd),
                patch.object(status_lib, "NOTIFIED_FILE", notified),
            ):
                alerts_mod.install(make_log(), root, "/opt/zark/zark")
                alerts_mod.remove(make_log(), root)
            assert not motd.exists() and not notified.exists()
            assert other.exists() and run_dir.is_dir()

    def test_the_debian_package_removes_them(self) -> None:
        """prerm runs `zark alerts remove` on remove, not on upgrade."""
        prerm = (Path(__file__).resolve().parent.parent / "debian/prerm").read_text(
            encoding="utf-8"
        )
        assert "remove)" in prerm and "/usr/share/zark/zark alerts remove" in prerm
        assert "upgrade)" not in prerm.split("remove)")[0]


class TestCompletePoints:
    """M2R-1: which backup points are complete is recorded on the drive."""

    def test_marks_every_snapshot_of_the_point(self) -> None:
        """Every dataset's snapshot of the point carries the mark, nothing else."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _record(fake, P1)
        marked = {
            f"{ds}@{s.name}": s.props[COMPLETE_PROP]
            for ds, d in fake.ds.items()
            for s in d.snaps
            if COMPLETE_PROP in s.props
        }
        rels = [
            "rpool/ROOT/be",
            "rpool/USERDATA/home",
            "rpool/var",
            "rpool/var/lib",
            "rpool/var/lib/docker",
            "bpool/BOOT/be",
        ]
        assert marked == {f"backup/{rel}@{P1}": str(len(rels)) for rel in rels}
        assert not any(COMPLETE_PROP in d.props for d in fake.ds.values())  # never a filesystem

    def test_a_point_a_dataset_skipped_is_not_whole(self) -> None:
        """M3R-4 (E5): a marked point that lost one dataset's snapshot while
        the dataset stayed is run-complete but not whole, so its month keeps
        the whole point before it and the policy removes the partial one."""
        be, boot = "rpool/ROOT/be", "bpool/BOOT/be"
        pa, pb, pc = _pt("2026-11-02"), _pt("2026-11-16"), _pt("2027-01-10")
        snaps = [
            Snap(be, pa, "g1", 1, _days_ago(74), mark="2"),
            Snap(boot, pa, "g2", 2, _days_ago(74), mark="2"),
            Snap(be, pb, "g3", 3, _days_ago(60), mark="2"),  # boot's copy of pb is gone
            Snap(be, pc, "g5", 5, _days_ago(5), mark="2"),
            Snap(boot, pc, "g6", 6, _days_ago(5), mark="2"),
        ]
        complete = retention_points(snaps, pc)
        assert complete.run == {pa, pb, pc}
        assert complete.whole == {pa, pc}
        assert complete_points(snaps, pc) == {pa, pc}  # recover agrees
        dest = _drive(
            {
                be: [(pa, _days_ago(74)), (pb, _days_ago(60)), (pc, _days_ago(5))],
                boot: [(pa, _days_ago(74)), (pc, _days_ago(5))],
            },
        )
        p = ret.plan_prune(dest, complete.run, ret.Policy(), NOW, whole=complete.whole)
        assert _gone(p) == {f"{be}@{pb}"}
        # A dataset destroyed whole skips nothing (commit 12's case).
        assert retention_points([s for s in snaps if s.dataset == be], pc).whole == {pa, pb, pc}

    def test_legacy_drive_is_marked_once_by_its_last_point(self) -> None:
        """A drive written before marks existed: points up to org.zark:last-point
        are marked unless their run missed a dataset that continues across
        them; points after it are incomplete."""
        fake = _fresh_fake()
        p0 = "zark_2026-10-06_10:00:00Z"
        assert _engine_run(fake, "backup", "111", p0).ok
        _tick(fake, "2026-10-07_08:00:00_hourly")
        # No partial state, so origin drops P1 there and no later run carries it.
        fake.inject["backup/rpool/USERDATA/home"] = ("enospc", 0)
        assert not _engine_run(fake, "backup", "111", P1).ok  # home missed P1
        assert _engine_run(fake, "backup", "111", P2).ok
        _tick(fake, "2026-10-08_12:00:00_hourly")
        fake.inject["backup/rpool/var/lib/docker"] = ("interrupt", 0)
        assert not _engine_run(fake, "backup", "111", P3).ok  # after the last complete run
        fake.ds["backup"].props["org.zark:last-point"] = P2
        p4 = "zark_2026-10-10_10:00:00Z"
        assert _engine_run(fake, "backup", "111", p4).ok
        _record(fake, p4)
        assert _marked(fake) == {p0, P2, p4}

    def test_a_marked_drive_is_not_marked_again(self) -> None:
        """Once a drive carries marks, only the new point is marked."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _record(fake, P1)
        _tick(fake, "2026-10-08_08:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        assert not _engine_run(fake, "backup", "111", P2).ok
        fake.ds["backup"].props["org.zark:last-point"] = P2  # even if it were wrong
        assert _engine_run(fake, "backup", "111", P3).ok
        _record(fake, P3)
        assert _marked(fake) == {P1, P3}

    def test_complete_points_pure(self) -> None:
        """Marks win; without marks the legacy rule; without last-point, none."""
        be, home = "rpool/ROOT/be", "rpool/USERDATA/home"
        snaps = [
            Snap(be, P1, "1", 1, 100),
            Snap(home, P1, "2", 2, 100),
            Snap(be, P2, "3", 3, 200),  # home missed it
            Snap(be, P3, "4", 4, 300),
            Snap(home, P3, "5", 5, 300),
        ]
        assert legacy_complete(snaps, P3) == {P1, P3}
        assert legacy_complete(snaps, P1) == {P1}
        assert legacy_complete(snaps, "") == set()
        marked = [*snaps[:2], Snap(be, P3, "4", 4, 300, mark="1")]
        assert complete_points(marked, P1) == {P3}
        assert complete_points(snaps, P3) == {P1, P3}

    def test_a_point_that_lost_part_of_its_snapshots_is_not_complete(self) -> None:
        """M2V-4: the mark records how many datasets the point had; with one
        of them gone (a prune, a rollback, a destroy) the point is incomplete.
        An rc2 mark has no count and is taken as it is."""
        be, home = "rpool/ROOT/be", "rpool/USERDATA/home"
        whole = [Snap(be, P1, "1", 1, 100, mark="2"), Snap(home, P1, "2", 2, 100, mark="2")]
        assert complete_points(whole, "") == {P1}
        assert complete_points(whole[:1], "") == set()
        unmarked = [whole[0], Snap(home, P1, "2", 2, 100)]
        assert complete_points(unmarked, "") == set()
        assert complete_points([Snap(be, P1, "1", 1, 100, mark="yes")], "") == {P1}
        assert complete_points([Snap(be, P1, "1", 1, 100, mark="on")], "") == set()

    def test_a_point_cut_on_the_drive_is_no_longer_complete(self) -> None:
        """M2V-4 (E15): a complete point whose snapshot on one dataset is
        destroyed (a rollback, a prune) reads as incomplete, with nothing
        written to the snapshots that are left."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _record(fake, P1)
        assert _engine_run(fake, "backup", "111", P2).ok
        _record(fake, P2)
        home = fake.ds["backup/rpool/USERDATA/home"]
        home.snaps = [s for s in home.snaps if s.name != P1]
        assert _marked(fake) == {P2}

    def test_parse_takes_the_mark_from_the_snapshot_itself(self) -> None:
        """M2V-3: the mark comes from the `-s local` listing, joined by name;
        nothing is read from the `zfs list` columns."""
        lines = [
            f"backup/rpool/ROOT/be@{P1}\t1\t1\t100",
            f"backup/rpool/ROOT/be@{P2}\t2\t2\t200\t1",
        ]
        snaps = parse_snapshots(lines, "backup", {f"backup/rpool/ROOT/be@{P1}": "1"})
        assert [s.mark for s in snaps] == ["1", ""]

    def test_a_mark_set_on_a_filesystem_is_not_read(self) -> None:
        """M2V-3 (E16): a value set by hand on a filesystem or the pool root
        shows on every snapshot below it in `zfs list`, but recover's view is
        unchanged: the incomplete point stays incomplete."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _record(fake, P1)
        _tick(fake, "2026-10-08_08:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        assert not _engine_run(fake, "backup", "111", P2).ok
        for ds in ("backup/rpool/ROOT/be", "backup/rpool", "backup"):
            fake.ds[ds].props[COMPLETE_PROP] = "yes"
            assert _marked(fake) == {P1}, ds

    def test_rc2_marks_get_their_count_once(self) -> None:
        """Marks written by 2.0.0-rc2 (`yes`) are rewritten with the number of
        datasets that hold the point at the next complete run."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        for ds, d in fake.ds.items():
            for s in d.snaps:
                if ds.startswith("backup/") and s.name == P1:
                    s.props[COMPLETE_PROP] = "yes"
        assert _engine_run(fake, "backup", "111", P2).ok
        _record(fake, P2)
        values = {
            s.props.get(COMPLETE_PROP)
            for ds, d in fake.ds.items()
            if ds.startswith("backup/")
            for s in d.snaps
            if s.name in (P1, P2)
        }
        assert values == {"6"}
        assert _marked(fake) == {P1, P2}


class TestRecoverIncompletePoint:
    """M2R-1: a backup point left by an incomplete run is labelled, never the
    default, and never evidence that a dataset was destroyed in origin."""

    BE, HOME, DOCKER = "rpool/ROOT/be", "rpool/USERDATA/home", "rpool/var/lib/docker"

    def _e1(self) -> list[Snap]:
        # P1 complete; P2 a week later, cut on home after its carried hourly.
        t1, t2 = 1_760_000_000, 1_760_600_000
        return [
            Snap(self.BE, P1, "g1", 10, t1, mark="3"),
            Snap(self.HOME, P1, "g2", 11, t1, mark="3"),
            Snap(self.DOCKER, P1, "g3", 12, t1, mark="3"),
            Snap(self.HOME, "autosnap_2026-10-08_08:00:00_hourly", "g4", 20, t2 - 7200),
            Snap(self.BE, P2, "g5", 21, t2),
            Snap(self.DOCKER, P2, "g6", 22, t2),
        ]

    def test_incomplete_point_is_not_evidence(self) -> None:
        """E1: choosing the incomplete point restores home from its earlier
        snapshot instead of reporting it destroyed."""
        snaps = self._e1()
        points = restore_points(snaps, self.BE)
        newest = points[-1]
        complete = complete_points(snaps, "")
        assert complete == {P1}
        got = resolve(newest, snaps, [self.BE, self.HOME, self.DOCKER], complete)
        home = got[self.HOME]
        assert home is not None and home.name.startswith("autosnap_")
        be = got[self.BE]
        assert be is not None and be.name == P2

    def test_incomplete_point_is_labelled_and_not_the_default(self) -> None:
        """The menu marks the incomplete point and defaults to the complete one."""
        snaps = self._e1()
        points = restore_points(snaps, self.BE)
        shown: dict[str, object] = {}

        def choice(question: str, options: list[str], default: int = 0) -> int:
            shown.update(question=question, options=options, default=default)
            return default

        with patch.object(Log, "ask_choice", side_effect=choice), redirect_stdout(StringIO()):
            chosen = _choose_point(points, make_log(), complete_points(snaps, ""))
        assert chosen is points[0]
        options = shown["options"]
        assert isinstance(options, list)
        assert "INCOMPLETE" in options[-1] and "INCOMPLETE" not in options[0]
        assert "complete backup point is the default" in str(shown["question"])

    def test_drive_written_by_the_engine(self) -> None:
        """E1b end to end: run 1 complete and marked, run 2 cut on home; then
        recover's own listing, choice and resolution."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _record(fake, P1)
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        assert not _engine_run(fake, "backup", "111", P2).ok
        self._check_recover_view(fake, "")

    def test_legacy_drive_reads_last_point(self) -> None:
        """A drive written before the marks: last-point alone tells the
        incomplete newest point apart."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        assert not _engine_run(fake, "backup", "111", P2).ok
        self._check_recover_view(fake, P1)

    def test_plan_of_the_incomplete_point_restores_what_its_run_missed(self) -> None:
        """M2V-5 (N12c): recover's own plan for the incomplete point takes home
        from its earlier snapshot and never reports it destroyed in origin."""
        be, home = f"rpool/ROOT/{_BE}", "rpool/USERDATA/home"
        t1, t2 = 1_760_000_000, 1_760_600_000
        lines = [
            f"backup/{be}@{P1}\t1\t10\t{t1}",
            f"backup/{home}@{P1}\t2\t11\t{t1}",
            f"backup/{home}@autosnap_2026-10-08_08:00:00_hourly\t3\t20\t{t2 - 7200}",
            f"backup/{be}@{P2}\t4\t21\t{t2}",
        ]
        snaps = parse_snapshots(lines, "backup")
        newest = restore_points(snaps, be)[-1]
        assert point_name(newest) == P2
        mock = MockShell()
        mock.on("zfs list -Hp -o name,type -r backup/rpool").succeeds(
            "\n".join(
                f"{n}\tfilesystem"
                for n in (
                    "backup/rpool",
                    "backup/rpool/ROOT",
                    f"backup/{be}",
                    "backup/rpool/USERDATA",
                    f"backup/{home}",
                )
            ),
        )
        mock.on("zfs list -Hp -o name,type -r backup/bpool").fails()
        mock.on_prefix("zfs get -Hp -s local -o name,property,value org.zark").succeeds("")
        mock.on("zfs list -Hp -t snapshot -o name,createtxg -s createtxg backup/keystore").succeeds(
            "backup/keystore@prepare_20261001_100000\t9",
        )
        with (
            patch_sh(mock),
            patch.object(recover_mod, "_probe_be", return_value=("", "", "")),
            patch.object(recover_mod, "_referenced", side_effect=lambda n: dict.fromkeys(n, 1)),
            patch.object(recover_mod, "_empty_root", return_value=False),
            redirect_stdout(StringIO()),
        ):
            plan = _plan("backup", "/dev/sdb1", _BE, newest, snaps, make_log(), {P1})
        row = plan.row(home)
        assert row is not None and row.restored, row
        assert row.snap is not None and row.snap.name.startswith("autosnap_")
        assert "destroyed" not in row.note

    def _check_recover_view(self, fake: FakeZfs, last_point: str) -> None:
        with patch_sh(fake):
            snaps = recover_mod._list_snapshots("backup", Log())  # pylint: disable=protected-access
        complete = complete_points(snaps, last_point)
        points = restore_points(snaps, self.BE)
        with patch("builtins.input", return_value=""), redirect_stdout(StringIO()):
            chosen = _choose_point(points, make_log(), complete)
        assert point_name(chosen) == P1
        newest = points[-1]
        assert point_name(newest) == P2
        wanted = sorted({s.dataset for s in snaps})
        got = resolve(newest, snaps, wanted, complete)
        assert all(got[ds] is not None for ds in wanted), got
        home = got[self.HOME]
        assert home is not None and home.creation < newest.end


class TestMonitorLatest:
    """M1P.3: the monitor's "latest" is the newest snapshot by creation."""

    def test_latest_by_creation_not_name(self):
        """A local-time sanoid name sorting after a newer UTC point loses."""
        mock = MockShell()
        mock.on("zfs list -Hp -t snapshot -o name,guid,createtxg,creation -r blue/rpool").succeeds(
            "blue/rpool@autosnap_2026-10-07_19:00:00_hourly\t1\t10\t1759856400\n"
            "blue/rpool@zark_2026-10-07_18:30:00Z\t2\t11\t1759861800\n",
        )
        with patch_sh(mock):
            latest = monitor_mod._latest("blue")  # pylint: disable=protected-access
        assert latest.startswith("zark_2026-10-07_18:30:00Z")

    def test_running_means_a_zark_backup_or_prepare(self):
        """Transfers are zark's own children now, not a syncoid process."""
        pattern = re.compile(monitor_mod._RUNNING)  # pylint: disable=protected-access
        assert pattern.search("/usr/bin/python3 /usr/share/zark/zark backup")
        assert pattern.search("python3 ./zark prepare /dev/sdb")
        assert not pattern.search("python3 ./zark monitor")


class TestRunner:
    """The suite's own runner (M1P.26)."""

    def test_system_exit_from_a_test_is_a_failure(self) -> None:
        """A SystemExit(0) raised by a test fails it instead of ending the run."""

        def exits() -> None:
            raise SystemExit(0)

        out = StringIO()
        with redirect_stdout(out):
            outcome = _run_one("test_exits", exits)
        assert outcome == "failed"
        assert "SystemExit(0)" in out.getvalue()

    def test_outcomes(self) -> None:
        """Pass, skip and failure are reported as such."""

        def skips() -> None:
            raise SkipTest("why")

        def fails() -> None:
            raise AssertionError("no")

        with redirect_stdout(StringIO()):
            assert _run_one("t", lambda: None) == "passed"
            assert _run_one("t", skips) == "skipped"
            assert _run_one("t", fails) == "failed"

    def test_summary_survives_an_escaping_exception(self) -> None:
        """An interrupt still prints the summary and counts the run as failed."""
        out = StringIO()
        with (
            patch.object(sys.modules[__name__], "_run_one", side_effect=KeyboardInterrupt),
            redirect_stdout(out),
        ):
            try:
                _ = main()
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("main() swallowed the interrupt")
        assert re.search(r"\d+ passed, 1 failed", out.getvalue())


def _run_one(name: str, method: Callable[[], object]) -> str:
    """Run one test and print its line; return "passed", "skipped" or "failed".

    A ``SystemExit`` escaping a test is a failure: caught by nothing else, it
    would end the whole run with that exit status and no summary line.
    """
    try:
        method()
    except SkipTest as e:
        print(f"    \033[0;33m-\033[0m {name}: skipped ({e})")
        return "skipped"
    except SystemExit as e:
        print(f"    \033[0;31m✗\033[0m {name}: raised SystemExit({e.code!r})")
        return "failed"
    except Exception as e:  # pylint: disable=broad-except
        print(f"    \033[0;31m✗\033[0m {name}: {e}")
        return "failed"
    print(f"    \033[0;32m✓\033[0m {name}")
    return "passed"


def main() -> int:
    """Discover and run every Test* class in this module.

    Returns the number of failed tests (0 on full success), suitable as a
    process exit status.
    """
    passed = failed = skipped = 0
    real_logs = _real_log_state()
    test_classes = [
        obj
        for name, obj in sorted(globals().items())
        if isinstance(obj, type) and name.startswith("Test")
    ]

    try:
        for cls in test_classes:
            instance = cls()
            methods = [
                (name, getattr(instance, name))
                for name in sorted(dir(instance))
                if name.startswith("test_")
            ]
            if methods:
                print(f"\n  {cls.__name__}")
            for name, method in methods:
                outcome = _run_one(name, method)
                passed += outcome == "passed"
                skipped += outcome == "skipped"
                failed += outcome == "failed"
    except KeyboardInterrupt:
        # An interrupted run is a failed run, and it still says how far it got.
        failed += 1
        raise
    except BaseException as e:  # pylint: disable=broad-exception-caught
        # Anything else that escaped a test (a SystemExit not caught by
        # _run_one) ends the run as failed instead of with its own status.
        print(f"\n    \033[0;31m✗\033[0m run aborted: {e!r}")
        failed += 1
    finally:
        changed = [p for p, st in _real_log_state().items() if st != real_logs[p]]
        if changed:
            print(f"\n    \033[0;31m✗\033[0m the run wrote a real log file: {', '.join(changed)}")
            failed += 1
        print(f"\n  {passed} passed, {failed} failed" + (f", {skipped} skipped" if skipped else ""))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
