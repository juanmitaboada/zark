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

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

from unittest.mock import patch  # pylint: disable=wrong-import-position # noqa: E402

import commands.chroot as chroot_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.clean as clean_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.finish as finish_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.fix_rpool_mountpoint as fix_rpool_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.mount as mount_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.prepare as prepare_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.purge as purge_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.recover as recover_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.registry as registry_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.repair_boot as repair_boot_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.umount as umount_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.grub_cfg as grub_cfg_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.sh as _sh  # pylint: disable=wrong-import-position # noqa: E402
from commands.backup import (  # pylint: disable=wrong-import-position # noqa: E402
    _check_target_space,
    _detect_live_usb,
    _parse_args as _backup_parse_args,
    _report_staleness_at_end,
)
from commands.monitor import _draw_bar  # pylint: disable=wrong-import-position # noqa: E402
from commands.recover import (  # pylint: disable=wrong-import-position # noqa: E402
    RestorePlan,
    RestoreRow,
    _abort_missing_keystore,
    _check_sizes,
    _choose_point,
    _force_latest_signed_alternative,
    _mount_restored_system,
    _plan,
    _preflight,
    _target_candidates,
)
from commands.repair_divergent import (  # pylint: disable=wrong-import-position # noqa: E402
    DOUBLE_CONFIRM_BYTES,
    _destroy_loop,
    _hint_for,
    _prompt_action,
    _prompt_double_confirm,
    _prompt_failure_policy,
    _shared_snapshot_with_source,
    _snapshot_creation_dates,
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
    repair,
)
from lib.cleanup import (  # pylint: disable=wrong-import-position # noqa: E402
    Cleanup,
    eject_device,
    flush_device_cache,
    prompt_eject_or_attach,
)
from lib.config import (  # pylint: disable=wrong-import-position # noqa: E402
    Config,
    DriveInfo,
    now_utc_iso,
    parse_utc_iso,
)
from lib.drives import (  # pylint: disable=wrong-import-position # noqa: E402
    ConnectedDrive,
    drive_staleness_days,
    drives_in_danger_zone,
    is_drive_stale,
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
from lib.repair import (  # pylint: disable=wrong-import-position # noqa: E402
    SIZE_LIMIT_BYTES,
    DivergentDataset,
    find_divergent,
    is_divergence_error,
)
from lib.restore_points import (  # pylint: disable=wrong-import-position # noqa: E402
    Snap,
    parse_snapshots,
    resolve,
    restore_points,
)
from lib.sanoid_retention import (  # pylint: disable=wrong-import-position # noqa: E402
    _retention_days_of_template,
    worst_case_retention_days,
)
from lib.sh import RunResult, part, run  # pylint: disable=wrong-import-position # noqa: E402
from lib.zfs import (  # pylint: disable=wrong-import-position # noqa: E402
    ZFS,
    DatasetInfo,
    PoolInfo,
    fix_grub_bpool_uuid,
    syncoid_exclude_flag,
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

    def test_unique_snap_names(self):
        mock, zfs = make_mock_zfs()
        mock.on("zfs list -H -o name -t snapshot -r rpool").succeeds(
            "rpool/ROOT/ubuntu@autosnap_2025-01-01\n"
            "rpool/ROOT/ubuntu/home@autosnap_2025-01-01\n"
            "rpool/ROOT/ubuntu@autosnap_2025-01-02\n"
            "rpool/ROOT/ubuntu/home@autosnap_2025-01-02\n",
        )
        with patch_sh(mock):
            names = zfs.unique_snap_names("rpool")
            assert names == ["autosnap_2025-01-01", "autosnap_2025-01-02"]

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
        """Non-interactive run with default=False (prepare / repair-divergent)
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

    def test_fatal_when_below_floor(self):
        """Below the 1 GiB floor, fatal is raised even for a tiny source."""
        # Source uses only 10 MB (1% would be 100 KB). Floor of 1 GiB
        # dominates. Target has 500 MiB free → below floor → fatal.
        src = self._make_pool_info("rpool", used_bytes=10 * 1024**2, avail_bytes=100 * 1024**3)
        dst = self._make_pool_info("backup", used_bytes=10 * 1024**3, avail_bytes=500 * 1024**2)
        log = make_log()
        # log.fatal() raises SystemExit after an input() prompt; we patch
        # input() to short-circuit the prompt under pytest's stdin capture.
        with redirect_stdout(StringIO()), patch("builtins.input", return_value=""):
            try:
                _check_target_space(src, dst, "backup", "rpool", log)
            except SystemExit:
                return
        raise AssertionError("Expected SystemExit from log.fatal()")

    def test_fatal_when_below_one_pct_of_source(self):
        """1% of a 1 TiB source dominates the 1 GiB floor → 10 GiB margin."""
        # Source uses ~1 TiB; 1% threshold = ~10.24 GiB.
        # Target has 5 GiB free → below threshold → fatal.
        src = self._make_pool_info("rpool", used_bytes=1024**4, avail_bytes=200 * 1024**3)
        dst = self._make_pool_info("backup", used_bytes=900 * 1024**3, avail_bytes=5 * 1024**3)
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value=""):
            try:
                _check_target_space(src, dst, "backup", "rpool", log)
            except SystemExit:
                return
        raise AssertionError("Expected SystemExit from log.fatal()")

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
#  lib/zfs.py — syncoid_exclude_flag helper
# ═════════════════════════════════════════════════════════════════════════


class TestSyncoidExcludeFlag:
    """Tests for the syncoid version-aware exclude-flag helper.

    syncoid 2.3.0 (Ubuntu 26.04) renamed --exclude to --exclude-datasets.
    On Ubuntu 22.04 - 25.10 (sanoid 2.1.0 - 2.2.0-2), only --exclude exists
    and the new name aborts syncoid mid-run with "Unknown option:
    exclude-datasets". The helper picks the right flag at runtime by
    inspecting `syncoid --help`.
    """

    HELP_2_3 = """\
syncoid [options]... SOURCE TARGET

  --exclude=REGEX           DEPRECATED. Equivalent to --exclude-datasets.
  --exclude-datasets=REGEX  Exclude specific datasets which match the given regex.
  --exclude-snaps=REGEX     Exclude specific snapshots that match the given regex.
"""

    HELP_2_2 = """\
syncoid [options]... SOURCE TARGET

  --exclude=REGEX           Exclude specific datasets which match the given regex.
                            Can be specified multiple times
  --sendoptions=OPTIONS     Use advanced options for zfs send.
"""

    def test_returns_exclude_datasets_on_syncoid_2_3(self):
        """syncoid 2.3+ help text mentions --exclude-datasets explicitly."""
        mock = MockShell()
        mock.on("syncoid --help").succeeds(self.HELP_2_3)
        with patch_sh(mock):
            assert syncoid_exclude_flag() == "--exclude-datasets"

    def test_returns_exclude_on_syncoid_2_2(self):
        """syncoid 2.2 (Ubuntu 22.04 - 25.10) only knows --exclude."""
        mock = MockShell()
        mock.on("syncoid --help").succeeds(self.HELP_2_2)
        with patch_sh(mock):
            assert syncoid_exclude_flag() == "--exclude"

    def test_returns_exclude_when_help_emitted_to_stderr(self):
        """syncoid emits --help text to stderr, not stdout. Helper must
        check both fields so detection works regardless of which side
        the version chose to use."""
        mock = MockShell()
        mock.on("syncoid --help").fails(self.HELP_2_3)  # rc!=0, output via stderr
        with patch_sh(mock):
            assert syncoid_exclude_flag() == "--exclude-datasets"


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


class TestLibRepair:  # pylint: disable=missing-function-docstring
    """Tests for lib/repair's detection logic.

    Used by both ``zark backup`` (silent path) and ``zark repair-divergent``
    (interactive path). Safety hinges on find_divergent correctly identifying
    datasets without shared snapshots — false positives would destroy
    real data, false negatives would leave the user with a broken backup.
    """

    def _setup_mock(  # pylint: disable=too-many-locals
        self,
        target_pool: str,
        target_datasets: list[tuple[str, str]],  # (name, type)
        target_snaps: dict[str, list[str]],  # dataset → snapshot suffixes
        source_snaps: dict[str, list[str]],
        source_exists: set[str] | None = None,
        used_bytes: dict[str, int] | None = None,
    ) -> tuple[MockShell, ZFS]:
        """Wire up a MockShell that answers all the queries find_divergent makes."""
        mock, zfs = make_mock_zfs()

        # list_datasets call
        rows = []
        for name, type_ in target_datasets:
            rows.append(f"{name}\tnone\t8K\t8K\toff\t{type_}")
        mock.on(
            "zfs list -H -o name,mountpoint,used,refer,canmount,type "
            + f"-t filesystem,volume -r {target_pool}",
        ).succeeds("\n".join(rows) + "\n")

        # snapshots and `dataset_exists` per (target, source) pair
        all_targets = [n for n, t in target_datasets if t == "filesystem"]
        for tgt in all_targets:
            snaps = target_snaps.get(tgt, [])
            mock.on(f"zfs list -H -o name -t snapshot {tgt}").succeeds(
                "\n".join(f"{tgt}@{s}" for s in snaps) + ("\n" if snaps else ""),
            )

        # Compute source counterparts (target_pool/X → X)
        for tgt in all_targets:
            if not tgt.startswith(target_pool + "/"):
                continue
            src = tgt[len(target_pool) + 1 :]
            # `zfs list <source>` for dataset_exists
            if source_exists is None or src in source_exists:
                mock.on(f"zfs list {src}").succeeds(f"{src}\t-\t-\t-\t-\n")
            else:
                mock.on(f"zfs list {src}").fails(f"cannot open '{src}': dataset does not exist")
            # snapshots on source
            snaps = source_snaps.get(src, [])
            mock.on(f"zfs list -H -o name -t snapshot {src}").succeeds(
                "\n".join(f"{src}@{s}" for s in snaps) + ("\n" if snaps else ""),
            )
            # used size — both -p numeric and human-readable
            ub = (used_bytes or {}).get(tgt, 8 * 1024)  # default 8K
            mock.on(f"zfs get -H -p -o value used {tgt}").succeeds(f"{ub}\n")
            # human form: anything plausible — caller doesn't parse
            mock.on(f"zfs get -H -o value used {tgt}").succeeds(f"{ub}B\n")

        return mock, zfs

    def test_no_divergence_when_snapshots_overlap(self):
        """Common snapshot present → not divergent."""
        mock, zfs = self._setup_mock(
            target_pool="blue",
            target_datasets=[
                ("blue", "filesystem"),
                ("blue/rpool", "filesystem"),
                ("blue/rpool/var", "filesystem"),
            ],
            target_snaps={
                "blue": [],
                "blue/rpool": ["snap_A"],
                "blue/rpool/var": ["snap_A", "snap_B"],
            },
            source_snaps={
                "rpool": ["snap_A"],
                "rpool/var": ["snap_B", "snap_C"],
            },
        )
        with patch_sh(mock):
            divergent = find_divergent(zfs, "rpool", "blue", make_log())
        assert not divergent

    def test_detects_dataset_with_no_shared_snapshots(self):
        """Target has a snapshot, source has different ones, no overlap → divergent."""
        mock, zfs = self._setup_mock(
            target_pool="blue",
            target_datasets=[
                ("blue", "filesystem"),
                ("blue/rpool", "filesystem"),
                ("blue/rpool/var", "filesystem"),
            ],
            target_snaps={
                "blue/rpool": ["shared_anchor"],
                "blue/rpool/var": ["old_april_snap"],
            },
            source_snaps={
                "rpool": ["shared_anchor"],
                "rpool/var": ["may_snap_1", "may_snap_2"],  # disjoint from old_april_snap
            },
        )
        with patch_sh(mock):
            divergent = find_divergent(zfs, "rpool", "blue", make_log())
        names = [d.target for d in divergent]
        assert "blue/rpool/var" in names
        assert "blue/rpool" not in names  # this one has overlap

    def test_skips_dataset_with_no_target_snapshots(self):
        """A target with no snapshots at all is a different bug — don't flag it.

        Without snapshots there's nothing meaningful to compare; the user
        should investigate why the dataset exists empty (e.g. someone ran
        `zfs create blue/rpool/foo` manually).
        """
        mock, zfs = self._setup_mock(
            target_pool="blue",
            target_datasets=[
                ("blue", "filesystem"),
                ("blue/rpool", "filesystem"),
                ("blue/rpool/empty", "filesystem"),
            ],
            target_snaps={"blue/rpool": ["s1"], "blue/rpool/empty": []},
            source_snaps={"rpool": ["s1"], "rpool/empty": ["src_snap"]},
        )
        with patch_sh(mock):
            divergent = find_divergent(zfs, "rpool", "blue", make_log())
        assert not divergent

    def test_skips_dataset_when_source_missing(self):
        """A target dataset whose source doesn't exist is not divergent —
        it's orphaned. Different problem, not handled here."""
        mock, zfs = self._setup_mock(
            target_pool="blue",
            target_datasets=[
                ("blue", "filesystem"),
                ("blue/rpool", "filesystem"),
                ("blue/rpool/orphan", "filesystem"),
            ],
            target_snaps={"blue/rpool": ["s1"], "blue/rpool/orphan": ["snap"]},
            source_snaps={"rpool": ["s1"]},  # rpool/orphan absent below
            source_exists={"rpool"},  # explicitly: rpool/orphan does NOT exist
        )
        with patch_sh(mock):
            divergent = find_divergent(zfs, "rpool", "blue", make_log())
        assert not divergent

    def test_skips_zvols(self):
        """Zvols are replicated by prepare/recover, never by syncoid → ignore."""
        mock, zfs = self._setup_mock(
            target_pool="blue",
            target_datasets=[
                ("blue", "filesystem"),
                ("blue/keystore", "volume"),
            ],
            target_snaps={"blue/keystore": ["whatever"]},
            source_snaps={"rpool/keystore": ["different"]},
        )
        with patch_sh(mock):
            divergent = find_divergent(zfs, "rpool", "blue", make_log())
        assert not divergent

    def test_size_limit_constant_is_64mb(self):
        """The SIZE_LIMIT_BYTES constant is the safety threshold the command
        documents to the user; pinning it here so a careless edit becomes
        a failing test."""
        assert SIZE_LIMIT_BYTES == 64 * 1024 * 1024

    def test_used_bytes_recorded_in_divergent(self):
        """The DivergentDataset records the destination's actual size, not
        just the human-readable form. The size guards the 64MB safety
        check, so it must be parsed numerically."""
        mock, zfs = self._setup_mock(
            target_pool="blue",
            target_datasets=[
                ("blue", "filesystem"),
                ("blue/rpool", "filesystem"),
                ("blue/rpool/var", "filesystem"),
            ],
            target_snaps={"blue/rpool": ["s1"], "blue/rpool/var": ["old"]},
            source_snaps={"rpool": ["s1"], "rpool/var": ["new"]},
            used_bytes={"blue/rpool/var": 8192},  # 8 KB
        )
        with patch_sh(mock):
            divergent = find_divergent(zfs, "rpool", "blue", make_log())
        assert len(divergent) == 1
        assert divergent[0].used_bytes == 8192

    # ── is_divergence_error ─────────────────────────────────────────────

    def test_is_divergence_error_matches_cowardly_refusing(self):
        """The exact phrase syncoid prints on divergence-protected aborts."""
        s = (
            "CRITICAL ERROR: Target blue/rpool/var exists but has no "
            "snapshots matching with rpool/var! Replication to target would "
            "require destroying existing target. Cowardly refusing to "
            "destroy your existing target."
        )
        assert is_divergence_error(s)

    def test_is_divergence_error_matches_no_snapshots_matching(self):
        """The other phrasing syncoid uses for the same condition."""
        s = "no snapshots matching with rpool/var"
        assert is_divergence_error(s)

    def test_is_divergence_error_is_case_insensitive(self):
        """Defensive: never trust exact case from third-party stderr."""
        s = "COWARDLY REFUSING to destroy your existing target"
        assert is_divergence_error(s)

    def test_is_divergence_error_does_not_match_other_failures(self):
        """Real failures unrelated to divergence must not trigger auto-repair."""
        assert not is_divergence_error("CRITICAL ERROR: out of space")
        assert not is_divergence_error("syncoid succeeded")
        assert not is_divergence_error("")

    # ── auto_repair_under_64mb ──────────────────────────────────────────

    def test_auto_repair_returns_ok_true_when_no_divergence(self):
        """No divergent datasets → return (True, []) immediately."""
        mock, zfs = self._setup_mock(
            target_pool="blue",
            target_datasets=[("blue", "filesystem"), ("blue/rpool", "filesystem")],
            target_snaps={"blue/rpool": ["autosnap_2026-01-01"]},
            source_snaps={"rpool": ["autosnap_2026-01-01"]},  # overlap
        )
        with patch_sh(mock):
            ok, too_big = repair.auto_repair_under_64mb(
                zfs,
                "rpool",
                "blue",
                make_log(),
            )
        assert ok is True
        assert too_big == []

    def test_auto_repair_destroys_small_divergent_datasets(self):
        """All divergent < 64MB → destroy each, return (True, [])."""
        mock, zfs = self._setup_mock(
            target_pool="blue",
            target_datasets=[("blue", "filesystem"), ("blue/rpool/var", "filesystem")],
            target_snaps={"blue/rpool/var": ["X"]},
            source_snaps={"rpool/var": ["Y"]},  # no overlap → divergent
            used_bytes={"blue/rpool/var": 8192},  # 8KB, well below 64MB
        )
        # The destroy call must be wired up to succeed.
        mock.on("zfs destroy -r blue/rpool/var").succeeds("")
        with patch_sh(mock):
            ok, too_big = repair.auto_repair_under_64mb(
                zfs,
                "rpool",
                "blue",
                make_log(),
            )
        assert ok is True
        assert too_big == []

    def test_auto_repair_aborts_when_dataset_exceeds_64mb(self):
        """Anything > 64MB → return (False, [those_datasets]) without
        destroying anything. Caller must surface this to the user."""
        big_size = repair.SIZE_LIMIT_BYTES + 1
        mock, zfs = self._setup_mock(
            target_pool="blue",
            target_datasets=[("blue", "filesystem"), ("blue/rpool/home", "filesystem")],
            target_snaps={"blue/rpool/home": ["X"]},
            source_snaps={"rpool/home": ["Y"]},
            used_bytes={"blue/rpool/home": big_size},
        )
        # Note: NO mock for `zfs destroy`. If auto-repair tried to destroy
        # this dataset, MockShell would raise — this is the test invariant.
        with patch_sh(mock):
            ok, too_big = repair.auto_repair_under_64mb(
                zfs,
                "rpool",
                "blue",
                make_log(),
            )
        assert ok is False
        assert len(too_big) == 1
        assert too_big[0].target == "blue/rpool/home"


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


class TestBackupStalenessReporting:  # pylint: disable=missing-function-docstring
    """``_report_staleness_at_end`` is purely informative — never
    fatals. Tests check that:
      - the WARN about the drive being expired at start fires only when
        ``age_at_start > retention_days``
      - the INFO list of other drives in danger zone excludes the
        just-backed-up drive
      - both messages are skipped when retention is None (sanoid.conf
        unavailable)
    """

    @staticmethod
    def _cfg(*, drives: dict[str, DriveInfo]) -> Config:
        cfg = make_config()
        cfg.known_drives.update(drives)
        return cfg

    def test_silent_when_retention_unknown(self):
        """``retention=None`` is the "no sanoid.conf" path — no
        output at all."""
        cfg = self._cfg(drives={})
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            _report_staleness_at_end(cfg, "black", 100, None, log)
        assert buf.getvalue() == "", "Expected no output when retention is None"

    def test_warn_when_drive_expired_at_start(self):
        """Selected drive was past retention when run started → WARN +
        purge+prepare hint + 'repair-divergent does not fix
        staleness' note."""
        cfg = self._cfg(drives={})
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            _report_staleness_at_end(cfg, "black", age_at_start=100, retention=90, log=log)
        out = buf.getvalue()
        assert "100 day(s) old" in out
        assert "90-day retention" in out
        assert "purge" in out and "prepare" in out
        assert "repair-divergent" in out and "does NOT fix staleness" in out

    def test_no_warn_when_drive_was_fresh(self):
        cfg = self._cfg(drives={})
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            _report_staleness_at_end(cfg, "black", age_at_start=10, retention=90, log=log)
        out = buf.getvalue()
        assert "past the" not in out

    def test_lists_other_drives_in_danger_zone(self):
        """Drives other than the backed-up one whose age ≥ (retention -
        30) appear in the INFO list."""
        five_days_ago = (datetime.now(UTC) - timedelta(days=70)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        recent = (datetime.now(UTC) - timedelta(days=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        cfg = self._cfg(
            drives={
                "blue": DriveInfo("blue", "1", "d1", last_backup_at=recent),
                "green": DriveInfo("green", "2", "d2", last_backup_at=five_days_ago),
            },
        )
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            _report_staleness_at_end(cfg, "blue", age_at_start=0, retention=90, log=log)
        out = buf.getvalue()
        assert "green" in out  # green is at 70 days (> 90 - 30)
        assert "Other drives approaching" in out

    def test_excludes_just_backed_up_drive_from_danger_list(self):
        """Drive we just finished backing up has age 0 now; if we
        included it the user would be confused. Test that ``exclude``
        works."""
        old = (datetime.now(UTC) - timedelta(days=80)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        cfg = self._cfg(
            drives={"black": DriveInfo("black", "1", "d", last_backup_at=old)},
        )
        log = make_log()
        buf = StringIO()
        with redirect_stdout(buf):
            # age_at_start=0 simulates "just-backed-up" semantics.
            _report_staleness_at_end(cfg, "black", age_at_start=0, retention=90, log=log)
        out = buf.getvalue()
        assert "Other drives approaching" not in out
        assert "black" not in out


# ═════════════════════════════════════════════════════════════════════════
#  lib/sanoid_retention.py — config parsing
# ═════════════════════════════════════════════════════════════════════════


class TestSanoidRetention:  # pylint: disable=missing-function-docstring
    """``worst_case_retention_days`` parses sanoid.conf and returns
    the largest retention horizon among templates actually used by
    rpool/bpool sections. Boundary cases:
      - file missing: WARN + None
      - no managed sections: None silent
      - mixed templates: returns the largest
      - only the buckets that matter for the horizon contribute
    """

    @staticmethod
    def _conf(text: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "sanoid.conf"
        p.write_text(text, encoding="utf-8")
        return p

    def test_per_template_horizon_is_max_of_buckets(self):
        # ``daily=14 weekly=8 monthly=3`` → max(14, 56, 90) = 90

        assert (
            _retention_days_of_template(
                {"daily": "14", "weekly": "8", "monthly": "3"},
            )
            == 90
        )
        # Old template: daily=2, no weekly, no monthly → max(2, 0, 0) = 2
        assert (
            _retention_days_of_template(
                {"daily": "2", "weekly": "0", "monthly": "0"},
            )
            == 2
        )
        # Production: max(7, 28, 90) = 90
        assert (
            _retention_days_of_template(
                {"daily": "7", "weekly": "4", "monthly": "3"},
            )
            == 90
        )

    def test_returns_largest_used_template(self):
        """Two templates, one with retention 90 used by rpool, one
        with retention 2 used by bpool — pick the larger (90)."""
        text = (
            "[rpool]\nuse_template = production\nrecursive = no\n"
            "[bpool]\nuse_template = minimal\nrecursive = no\n"
            "[template_production]\ndaily = 7\nweekly = 4\nmonthly = 3\n"
            "[template_minimal]\ndaily = 2\nweekly = 0\nmonthly = 0\n"
        )
        p = self._conf(text)
        log = make_log()

        assert worst_case_retention_days(log, conf_path=p) == 90

    def test_returns_smaller_when_only_minimal_used(self):
        """If only the smaller-retention template is referenced, that
        one defines the horizon."""
        text = (
            "[rpool]\nuse_template = minimal\n"
            "[template_minimal]\ndaily = 2\nweekly = 0\nmonthly = 0\n"
            "[template_production]\ndaily = 7\nweekly = 4\nmonthly = 3\n"
        )
        p = self._conf(text)
        log = make_log()

        assert worst_case_retention_days(log, conf_path=p) == 2

    def test_returns_none_when_file_missing(self):
        """Missing sanoid.conf: WARN + None (silent under tests, but
        the call must not raise)."""
        log = make_log()

        with redirect_stdout(StringIO()):
            result = worst_case_retention_days(
                log,
                conf_path=Path("/tmp/zark-test-nonexistent.conf"),
            )
            assert result is None

    def test_returns_none_when_no_managed_sections(self):
        """File present but only template definitions, no [rpool*]/
        [bpool*] sections referencing them."""
        text = "[template_production]\ndaily = 7\nweekly = 4\nmonthly = 3\n"
        p = self._conf(text)
        log = make_log()

        assert worst_case_retention_days(log, conf_path=p) is None

    def test_ignores_zvol_sections_without_use_template(self):
        """``autosnap=no`` zvol sections never define use_template
        and must not contribute to the horizon."""
        text = (
            "[rpool/keystore]\nautosnap = no\nautoprune = no\n"
            "[rpool]\nuse_template = production\n"
            "[template_production]\ndaily = 7\nweekly = 4\nmonthly = 3\n"
        )
        p = self._conf(text)
        log = make_log()

        # Only rpool contributes (daily=7, weekly=4, monthly=3) → 90.
        assert worst_case_retention_days(log, conf_path=p) == 90


# ═════════════════════════════════════════════════════════════════════════
#  lib/drives.py — drives_in_danger_zone
# ═════════════════════════════════════════════════════════════════════════


class TestDrivesInDangerZone:  # pylint: disable=missing-function-docstring
    """``drives_in_danger_zone`` returns drives whose age is at or
    above ``retention - margin``, sorted age-desc, with the named
    drive optionally excluded."""

    @staticmethod
    def _drive(name: str, last: str | None) -> DriveInfo:
        return DriveInfo(name=name, guid="g", drive_id="d", last_backup_at=last)

    def test_empty_when_no_drives(self):
        result = drives_in_danger_zone({}, retention_days=90, margin_days=30)
        assert not result

    def test_skips_drives_without_last_backup_at(self):
        drives = {"black": self._drive("black", None)}
        assert not drives_in_danger_zone(drives, retention_days=90, margin_days=30)

    def test_includes_drives_at_or_beyond_threshold(self):
        old = (datetime.now(UTC) - timedelta(days=70)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        drives = {"black": self._drive("black", old)}
        result = drives_in_danger_zone(drives, retention_days=90, margin_days=30)
        assert len(result) == 1
        assert result[0][0] == "black"
        assert result[0][1] >= 60  # 90 - 30 = 60 threshold; 70 days qualifies

    def test_excludes_drives_below_threshold(self):
        recent = (datetime.now(UTC) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        drives = {"black": self._drive("black", recent)}
        result = drives_in_danger_zone(drives, retention_days=90, margin_days=30)
        assert not result

    def test_excludes_named_drive(self):
        old = (datetime.now(UTC) - timedelta(days=70)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        drives = {
            "black": self._drive("black", old),
            "blue": self._drive("blue", old),
        }
        result = drives_in_danger_zone(
            drives,
            retention_days=90,
            margin_days=30,
            exclude="black",
        )
        assert len(result) == 1
        assert result[0][0] == "blue"

    def test_sorts_age_desc(self):
        d70 = (datetime.now(UTC) - timedelta(days=70)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        d80 = (datetime.now(UTC) - timedelta(days=80)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        drives = {
            "younger": self._drive("younger", d70),
            "older": self._drive("older", d80),
        }
        result = drives_in_danger_zone(drives, retention_days=90, margin_days=30)
        assert [name for name, _ in result] == ["older", "younger"]


# ═════════════════════════════════════════════════════════════════════════
#  commands/repair_divergent.py — interactive flow
# ═════════════════════════════════════════════════════════════════════════


def _make_div(target: str, used_bytes: int, used_human: str = "") -> DivergentDataset:
    """Tiny factory for DivergentDataset fixtures."""
    return DivergentDataset(
        source=target.split("/", 1)[1] if "/" in target else target,
        target=target,
        used_bytes=used_bytes,
        used_human=used_human or f"{used_bytes}B",
    )


class TestRepairDivergentHints:  # pylint: disable=missing-function-docstring
    """``_hint_for`` classification — covers the three main cases the
    operator sees in the per-dataset prompt block."""

    def test_orphan_when_source_missing(self):
        h = _hint_for("blue/rpool/old", source_exists=False, children=0)
        assert "orphan" in h

    def test_container_when_has_children(self):
        h = _hint_for("blue/rpool", source_exists=True, children=5)
        assert "container" in h

    def test_leaf_when_no_children(self):
        h = _hint_for("blue/rpool/var/log", source_exists=True, children=0)
        assert "leaf" in h


class TestRepairDivergentDoubleConfirm:  # pylint: disable=missing-function-docstring
    """``_prompt_double_confirm`` — accepts only the literal string
    ``DESTROY``. Anything else (yes, y, the dataset name, empty)
    cancels."""

    def test_accepts_literal_destroy(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="DESTROY"):
            assert _prompt_double_confirm(log, "blue/rpool", "100G") is True

    def test_rejects_lowercase(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="destroy"):
            assert _prompt_double_confirm(log, "blue/rpool", "100G") is False

    def test_rejects_yes(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="yes"):
            assert _prompt_double_confirm(log, "blue/rpool", "100G") is False

    def test_rejects_y(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="y"):
            assert _prompt_double_confirm(log, "blue/rpool", "100G") is False

    def test_rejects_empty(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value=""):
            assert _prompt_double_confirm(log, "blue/rpool", "100G") is False

    def test_accepts_destroy_with_whitespace(self):
        """``.strip()`` on input means surrounding whitespace is OK —
        the operator who hits space before pressing Enter shouldn't be
        punished."""
        log = make_log()
        with (
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                return_value="  DESTROY  ",
            ),
        ):
            assert _prompt_double_confirm(log, "blue/rpool", "100G") is True


class TestRepairDivergentActionPrompt:  # pylint: disable=missing-function-docstring
    """``_prompt_action`` and ``_prompt_failure_policy`` — verify the
    selected index maps correctly to the documented sentinel
    string. ``ask_choice`` reads via ``input()`` so we patch that."""

    def test_action_destroy_first_choice(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="1"):
            assert _prompt_action(log) == "destroy"

    def test_action_skip_second_choice(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="2"):
            assert _prompt_action(log) == "skip"

    def test_action_abort_third_choice(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="3"):
            assert _prompt_action(log) == "abort"

    def test_action_default_skip_on_empty(self):
        """Default is index=1 (skip) — the only fully reversible
        choice. Empty input falls back to that."""
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value=""):
            assert _prompt_action(log) == "skip"

    def test_failure_policy_continue(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="1"):
            assert _prompt_failure_policy(log) == "continue"

    def test_failure_policy_abort(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="2"):
            assert _prompt_failure_policy(log) == "abort"

    def test_failure_policy_keep_state_abort(self):
        log = make_log()
        with redirect_stdout(StringIO()), patch("builtins.input", return_value="3"):
            assert _prompt_failure_policy(log) == "keep_state_abort"


class TestRepairDivergentLoop:  # pylint: disable=missing-function-docstring
    """``_destroy_loop`` end-to-end with mocked sh.run + input.

    The loop has three branches under the prompt:
      - small (≤ 64 MB) auto-destroyed
      - big (> 64 MB) goes through prompt → destroy / skip / abort
      - big (> 1 GiB) requires extra DESTROY confirmation
      - any failed destroy triggers the once-per-session policy prompt
    """

    @staticmethod
    def _make_zfs() -> ZFS:
        return ZFS(make_log())

    def test_auto_destroys_small_datasets(self):
        """Datasets ≤ 64 MB are destroyed silently, no prompt."""
        small = _make_div("blue/rpool/var", used_bytes=10 * 1024 * 1024)  # 10 MiB
        mock = MockShell()
        mock.on(f"zfs destroy -r {small.target}").succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            destroyed, skipped, aborted = _destroy_loop(
                make_log(),
                self._make_zfs(),
                [small],
            )
        assert destroyed == [small.target]
        assert not skipped
        assert aborted is False

    def test_big_destroy_with_user_confirm(self):
        """A > 64 MB but ≤ 1 GiB dataset goes through the action
        prompt. User picks ``destroy`` (option 1) and the destroy
        succeeds — no double confirm because under 1 GiB."""
        big = _make_div("blue/rpool", used_bytes=200 * 1024 * 1024)  # 200 MiB
        mock = MockShell()
        mock.on(f"zfs destroy -r {big.target}").succeeds()
        # Children query for the dataset block — return empty.
        mock.on_prefix("zfs list").succeeds()
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                return_value="1",  # action: destroy
            ),
        ):
            destroyed, skipped, aborted = _destroy_loop(
                make_log(),
                self._make_zfs(),
                [big],
            )
        assert destroyed == [big.target]
        assert not skipped
        assert aborted is False

    def test_big_skip(self):
        """User skips — no destroy attempted."""
        big = _make_div("blue/rpool", used_bytes=200 * 1024 * 1024)
        mock = MockShell()
        mock.on_prefix("zfs list").succeeds()
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                return_value="2",  # action: skip
            ),
        ):
            destroyed, skipped, aborted = _destroy_loop(
                make_log(),
                self._make_zfs(),
                [big],
            )
        assert not destroyed
        assert skipped == [big.target]
        assert aborted is False
        # No destroy command should have been invoked.
        assert mock.was_not_called(f"zfs destroy -r {big.target}")

    def test_big_abort_skips_remaining(self):
        """User aborts on first big dataset — second one not even
        prompted, both end up in ``skipped``."""
        d1 = _make_div("blue/rpool", used_bytes=200 * 1024 * 1024)
        d2 = _make_div("blue/rpool/x", used_bytes=200 * 1024 * 1024)
        mock = MockShell()
        mock.on_prefix("zfs list").succeeds()
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                return_value="3",  # action: abort
            ),
        ):
            destroyed, skipped, aborted = _destroy_loop(
                make_log(),
                self._make_zfs(),
                [d1, d2],
            )
        assert not destroyed
        assert sorted(skipped) == sorted([d1.target, d2.target])
        assert aborted is True

    def test_double_confirm_required_above_1gib(self):
        """A > 1 GiB destroy is gated by the typed-DESTROY prompt.
        Two ``input()`` calls happen: action choice (1=destroy), then
        the literal ``DESTROY``. We use a side_effect list to feed
        them in order."""
        huge = _make_div("blue/rpool", used_bytes=2 * 1024**3)  # 2 GiB
        assert huge.used_bytes > DOUBLE_CONFIRM_BYTES
        mock = MockShell()
        mock.on(f"zfs destroy -r {huge.target}").succeeds()
        mock.on_prefix("zfs list").succeeds()
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                side_effect=["1", "DESTROY"],
            ),
        ):
            destroyed, _skipped, _aborted = _destroy_loop(
                make_log(),
                self._make_zfs(),
                [huge],
            )
        assert destroyed == [huge.target]

    def test_double_confirm_cancel_skips_dataset(self):
        """User picks destroy on a > 1 GiB dataset but types ``yes``
        (anything other than DESTROY) at the second prompt. Result:
        nothing is destroyed."""
        huge = _make_div("blue/rpool", used_bytes=2 * 1024**3)
        mock = MockShell()
        mock.on_prefix("zfs list").succeeds()
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                side_effect=["1", "yes"],  # action: destroy, then non-DESTROY answer
            ),
        ):
            destroyed, skipped, _aborted = _destroy_loop(
                make_log(),
                self._make_zfs(),
                [huge],
            )
        assert not destroyed
        assert skipped == [huge.target]
        assert mock.was_not_called(f"zfs destroy -r {huge.target}")

    def test_failure_policy_continue(self):
        """Two big datasets, the destroy of the first FAILS, the
        operator picks ``continue`` (option 1). The second dataset
        is still attempted."""
        d1 = _make_div("blue/rpool/a", used_bytes=200 * 1024 * 1024)
        d2 = _make_div("blue/rpool/b", used_bytes=200 * 1024 * 1024)
        mock = MockShell()
        mock.on_prefix("zfs list").succeeds()
        mock.on(f"zfs destroy -r {d1.target}").fails(stderr="busy")
        mock.on(f"zfs destroy -r {d2.target}").succeeds()
        # Inputs in order: action=destroy (1), action=destroy (1),
        # failure_policy=continue (1).
        # NOTE: failure prompt fires AFTER the failed destroy of d1,
        # then we advance to d2's action prompt.
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                side_effect=["1", "1", "1"],
            ),
        ):
            destroyed, skipped, aborted = _destroy_loop(
                make_log(),
                self._make_zfs(),
                [d1, d2],
            )
        assert destroyed == [d2.target]
        assert d1.target in skipped
        assert aborted is False

    def test_failure_policy_abort(self):
        """First destroy fails, operator picks ``abort`` (option 2).
        Second dataset is not touched."""
        d1 = _make_div("blue/rpool/a", used_bytes=200 * 1024 * 1024)
        d2 = _make_div("blue/rpool/b", used_bytes=200 * 1024 * 1024)
        mock = MockShell()
        mock.on_prefix("zfs list").succeeds()
        mock.on(f"zfs destroy -r {d1.target}").fails(stderr="busy")
        # Inputs: action=destroy (1), failure_policy=abort (2).
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch(
                "builtins.input",
                side_effect=["1", "2"],
            ),
        ):
            destroyed, skipped, aborted = _destroy_loop(
                make_log(),
                self._make_zfs(),
                [d1, d2],
            )
        assert not destroyed
        assert sorted(skipped) == sorted([d1.target, d2.target])
        assert aborted is True
        assert mock.was_not_called(f"zfs destroy -r {d2.target}")


class TestRepairDivergentSnapshotHelpers:  # pylint: disable=missing-function-docstring
    """``_snapshot_creation_dates`` and ``_shared_snapshot_with_source``
    — small zfs-list parsers that feed the per-dataset block."""

    def test_creation_dates_parses_tab_output(self):
        mock = MockShell()
        # The query that ``_snapshot_creation_dates`` issues — match the
        # exact prefix to avoid colliding with other zfs list calls.
        mock.on(
            "zfs list -H -p -o name,creation -t snapshot -s creation blue/rpool",
        ).succeeds(
            "blue/rpool@autosnap_2026-04-01\t1743465600\n"
            "blue/rpool@autosnap_2026-05-01\t1746057600\n",
        )
        with patch_sh(mock):
            out = _snapshot_creation_dates("blue/rpool")
        assert len(out) == 2
        assert out[0][0] == "blue/rpool@autosnap_2026-04-01"

    def test_creation_dates_empty_on_failure(self):
        mock = MockShell()
        mock.on_prefix(
            "zfs list -H -p -o name,creation -t snapshot",
        ).fails(stderr="no datasets")
        with patch_sh(mock):
            assert not _snapshot_creation_dates("blue/rpool")

    def test_shared_snapshot_returns_most_recent_match(self):
        """If both sides share two snapshots, the most recent
        (lex-last) wins."""
        mock = MockShell()
        # _snapshot_set in lib.repair calls ``zfs list -H -o name -t
        # snapshot <ds>`` — replicate that for both target and source.
        mock.on("zfs list -H -o name -t snapshot blue/rpool").succeeds(
            "blue/rpool@autosnap_2026-04-01\nblue/rpool@autosnap_2026-05-01\n",
        )
        mock.on("zfs list -H -o name -t snapshot rpool").succeeds(
            "rpool@autosnap_2026-04-01\nrpool@autosnap_2026-05-01\n",
        )
        with patch_sh(mock):
            shared = _shared_snapshot_with_source("blue/rpool", "rpool")
        assert shared == "autosnap_2026-05-01"

    def test_shared_snapshot_none_when_no_overlap(self):
        mock = MockShell()
        mock.on("zfs list -H -o name -t snapshot blue/rpool").succeeds(
            "blue/rpool@autosnap_2025-01-01\n",
        )
        mock.on("zfs list -H -o name -t snapshot rpool").succeeds(
            "rpool@autosnap_2026-05-01\n",
        )
        with patch_sh(mock):
            assert _shared_snapshot_with_source("blue/rpool", "rpool") is None


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
            assert zfs.verify_exported_pool_readback("black", device=self.DEV)
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
            assert not zfs.verify_exported_pool_readback("black", device=self.DEV)

    def test_fails_when_health_not_online(self):
        mock = MockShell()
        mock.on("sync").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on(self.IMPORT).succeeds()
        mock.on("zpool list -H -o health black").succeeds("DEGRADED")
        mock.on("zpool export black").succeeds()
        with patch_sh(mock):
            zfs = ZFS(make_log())
            assert not zfs.verify_exported_pool_readback("black", device=self.DEV)
        # Even on failure, the pool is re-exported to restore on-entry state.
        assert mock.was_called("zpool export black")


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

    def test_prepare_by_id_replaces_stale_entry_and_uses_by_id_vdev(self):
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(
            cfg_dir / "known_drives.json",
            {
                "blue": {"guid": "1", "drive_id": _KINGSTON_ID},
                "black": {"guid": "2", "drive_id": "usb-Micron-0:0"},
            },
        )
        ident = _purge_ident(None)
        mock = MockShell()
        mock.on("lsblk -no NAME /dev/sdb | tail -n +2").succeeds("")
        mock.on("blkid /dev/sdb").fails()
        mock.on("zpool list blue").fails()
        mock.on_prefix("zpool create").succeeds()
        mock.on_prefix("syncoid").succeeds()
        mock.on("zpool list bpool").succeeds("bpool")
        mock.on("zpool get -H -o value guid blue").succeeds("777")
        report = type("R", (), {"has_risk": False, "findings": [], "device": "/dev/sdb"})()
        with (
            patch_sh(mock),
            patch.object(Config, "default_config_dir", return_value=cfg_dir),
            patch.object(prepare_mod, "validate_external_block_device", return_value=ident),
            patch.object(prepare_mod, "check_device", return_value=report),
            patch.object(prepare_mod.Path, "exists", return_value=True),
            patch.object(ZFS, "verify_exported_pool_readback", return_value=True),
            patch.object(ZFS, "pool_export", return_value=True),
            patch("lib.log.Log.ask", return_value=True),
            patch("lib.log.Log.ask_input", return_value="blue"),
            patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
            patch.object(prepare_mod, "prompt_eject_or_attach"),
            redirect_stdout(StringIO()),
        ):
            prepare_mod.run([f"/dev/disk/by-id/{_KINGSTON_ID}"])
        create = next(c for c in mock.calls if c.startswith("zpool create"))
        assert "-R /run/zark/altroot/blue" in create
        assert create.endswith(f"blue /dev/disk/by-id/{_KINGSTON_ID}")
        syncoids = [c for c in mock.calls if c.startswith("syncoid --recursive")]
        assert len(syncoids) == 2
        assert all("--recvoptions=u" in c for c in syncoids)
        assert mock.was_called("zfs receive -u -F blue/keystore")
        data = json.loads((cfg_dir / "known_drives.json").read_text())
        assert data["blue"] == {
            "guid": "777",
            "drive_id": _KINGSTON_ID,
            "last_backup_at": None,
            "autoeject": True,
        }
        assert data["black"]["guid"] == "2"

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

    def test_forget_removes_only_that_entry(self):
        d = self._cfg_dir(
            {"blue": {"guid": "1", "drive_id": "a"}, "black": {"guid": "2", "drive_id": "b"}},
        )
        with (
            patch.object(Config, "default_config_dir", return_value=d),
            patch("lib.log.Log.ask", return_value=True),
            redirect_stdout(StringIO()),
        ):
            registry_mod.run(["forget", "blue"])
        assert list(json.loads((d / "known_drives.json").read_text())) == ["black"]

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
    ) -> MockShell:
        """``answer`` None simulates a closed terminal (EOF) at the YES prompt.

        The system.key copy goes below ``work``; Cleanup's atexit run is
        simulated after the command returns.
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
            patch.object(recover_mod, "_list_snapshots", return_value=snaps),
            patch.object(recover_mod, "_choose_point", return_value=point),
            patch.object(recover_mod, "_plan", return_value=plan),
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

    def test_unreadable_size_is_never_auto_destroyed(self):
        d = DivergentDataset("rpool/var", "blue/rpool/var", -1, "?")
        with (
            patch.object(repair, "find_divergent", return_value=[d]),
            patch("lib.repair.sh.run") as run_mock,
            redirect_stdout(StringIO()),
        ):
            ok, too_big = repair.auto_repair_under_64mb(
                ZFS(make_log()), "rpool", "blue", make_log()
            )
        assert not ok and too_big == [d]
        run_mock.assert_not_called()

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
        for name in ("recover", "purge", "repair_boot", "fix_rpool_mountpoint"):
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
    def _umount_backup(export_ok: bool) -> MockShell:
        mock = MockShell()
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
                redirect_stdout(StringIO()),
            ):
                try:
                    umount_mod.run([])
                except SystemExit:
                    pass
        return mock

    def test_umount_cleans_directories_only_after_the_export(self):
        # Review §8: with the pool still imported, find would delete inside the backup.
        assert self._umount_backup(export_ok=False).was_not_called("find /")
        ok = self._umount_backup(export_ok=True)
        assert any(c.startswith("find ") and " -xdev " in c for c in ok.calls)

    def test_umount_lets_the_zfs_unmount_propagate(self):
        # eli K6: a private backup tree left its copies mounted in the service
        # and snap namespaces, and the export failed "busy".
        calls = self._umount_backup(export_ok=True).calls
        assert not any(c.startswith("mount --make-rprivate") for c in calls)
        assert [c for c in calls if c.startswith("umount -R ")][-1].endswith("/backup")


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


def main() -> int:
    """Discover and run every Test* class in this module.

    Returns the number of failed tests (0 on full success), suitable as a
    process exit status.
    """
    passed = failed = 0
    real_logs = _real_log_state()
    test_classes = [
        obj
        for name, obj in sorted(globals().items())
        if isinstance(obj, type) and name.startswith("Test")
    ]

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
            try:
                method()
                print(f"    \033[0;32m✓\033[0m {name}")
                passed += 1
            except Exception as e:  # pylint: disable=broad-except
                print(f"    \033[0;31m✗\033[0m {name}: {e}")
                failed += 1

    changed = [p for p, st in _real_log_state().items() if st != real_logs[p]]
    if changed:
        print(f"\n    \033[0;31m✗\033[0m the run wrote a real log file: {', '.join(changed)}")
        failed += 1

    print(f"\n  {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
