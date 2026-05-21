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

import inspect
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

from unittest.mock import patch  # pylint: disable=wrong-import-position # noqa: E402

import commands.clean as clean_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.sh as _sh  # pylint: disable=wrong-import-position # noqa: E402
from commands.backup import (  # pylint: disable=wrong-import-position # noqa: E402
    _check_target_space,
    _detect_live_usb,
)
from commands.backup import (  # pylint: disable=wrong-import-position # noqa: E402
    _parse_args as _backup_parse_args,
)
from commands.backup import (  # pylint: disable=wrong-import-position # noqa: E402
    _report_staleness_at_end,
)
from commands.monitor import _draw_bar  # pylint: disable=wrong-import-position # noqa: E402
from commands.recover import (  # pylint: disable=wrong-import-position # noqa: E402
    _UBUNTU_ROOT_CHILDREN_ALL,
    _abort_missing_keystore,
    _apply_root_children_canmount,
    _check_target_disk_size,
    _detect_root_children,
    _disk_size_bytes,
    _emit_dataset_layout_warnings,
    _force_latest_signed_alternative,
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
from lib import repair  # pylint: disable=wrong-import-position # noqa: E402
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
from lib.keystore import Keystore  # pylint: disable=wrong-import-position # noqa: E402
from lib.log import Log  # pylint: disable=wrong-import-position # noqa: E402
from lib.repair import (  # pylint: disable=wrong-import-position # noqa: E402
    SIZE_LIMIT_BYTES,
    DivergentDataset,
    find_divergent,
    is_divergence_error,
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
                assert (
                    result == fake_etc_zark
                ), f"Expected fallback to /etc/zark (= {fake_etc_zark}), got {result}"
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
            assert (
                result == portable_root / "etc"
            ), f"portable run with no config should default to <root>/etc, got {result}"
        finally:
            if env_backup is not None:
                os.environ["ZARK_CONFIG_DIR"] = env_backup


# ═════════════════════════════════════════════════════════════════════════
#  lib/log.py
# ═════════════════════════════════════════════════════════════════════════


class TestLog:
    """Tests for Log ANSI stripping and fatal error handling."""

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
                            assert False, "Should have called fatal"
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
                    assert False, "Should have called fatal"
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
        assert (
            "/dev/nvme0n1" not in names
        ), "In-use disk leaked into candidate list — safety layer 2 broken"

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
            try:
                _sh.run("unknown_command")
                assert False, "Should have raised"
            except AssertionError as e:
                assert "unexpected command" in str(e)

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
#  commands/recover.py — dataset layout detection
# ═════════════════════════════════════════════════════════════════════════


class TestRecoverDatasetLayout:
    """
    Tests for _apply_root_children_canmount and the missing/extra detection.

    Covers three real scenarios:
      - Full layout: every expected dataset exists (real Ubuntu installer).
      - Reduced layout: only a subset exists (the QEMU integration test).
      - Extended layout: source has datasets unknown to zark.
    """

    UBUNTU = "ubuntu_test01"

    def _zfs_list_output(self, relative_children: list[str]) -> str:
        """
        Build the multi-line response zfs list returns. Includes the parent
        itself (which _detect_root_children must filter out) plus each child
        with the full prefix.
        """
        lines = [f"rpool/ROOT/{self.UBUNTU}"]
        for ds in relative_children:
            lines.append(f"rpool/ROOT/{self.UBUNTU}/{ds}")
        return "\n".join(lines)

    def _make_mock(self, children: list[str]) -> MockShell:
        mock = MockShell()
        # _detect_root_children's listing
        mock.on(f"zfs list -H -o name -r rpool/ROOT/{self.UBUNTU}").succeeds(
            self._zfs_list_output(children),
        )
        # Catch every `zfs set ... rpool/ROOT/<ubuntu>/<ds>` succeeding
        mock.on_prefix("zfs set").succeeds()
        return mock

    def test_full_ubuntu_layout_no_drift(self):
        """Real Ubuntu hardware: all 15 expected children present, no drift."""
        all_children = sorted(_UBUNTU_ROOT_CHILDREN_ALL)
        mock = self._make_mock(all_children)
        with patch_sh(mock):
            log = make_log()
            zfs = ZFS(log)
            missing, extra = _apply_root_children_canmount(self.UBUNTU, zfs, log)
        assert missing == set()
        assert extra == set()

    def test_reduced_layout_reports_missing(self):
        """QEMU test fixture: only a subset of the expected children exist."""
        present = [
            "usr",
            "usr/local",
            "var",
            "var/lib",
            "var/lib/dpkg",
            "var/log",
            "var/spool",
            "srv",
        ]
        mock = self._make_mock(present)
        with patch_sh(mock):
            log = make_log()
            zfs = ZFS(log)
            missing, extra = _apply_root_children_canmount(self.UBUNTU, zfs, log)
        # Things zark expected but the test fixture doesn't create:
        assert "var/mail" in missing
        assert "var/snap" in missing
        assert "var/www" in missing
        assert "var/games" in missing
        assert "var/lib/apt" in missing
        assert "var/lib/AccountsService" in missing
        assert "var/lib/NetworkManager" in missing
        # Things that ARE present must not be flagged missing:
        for ds in present:
            assert ds not in missing
        assert extra == set()

    def test_extended_layout_reports_extra(self):
        """Custom system: source has datasets zark doesn't know about."""
        actual = sorted(_UBUNTU_ROOT_CHILDREN_ALL) + ["opt", "srv/data"]
        mock = self._make_mock(actual)
        with patch_sh(mock):
            log = make_log()
            zfs = ZFS(log)
            missing, extra = _apply_root_children_canmount(self.UBUNTU, zfs, log)
        assert missing == set()
        assert extra == {"opt", "srv/data"}

    def test_only_intersection_receives_set(self):
        """
        zark must NOT issue `zfs set` against datasets it doesn't recognize,
        even if they're present. Extras are reported but not modified.
        """
        actual = ["usr", "var", "var/lib", "opt", "srv/data"]
        mock = self._make_mock(actual)
        with patch_sh(mock):
            log = make_log()
            zfs = ZFS(log)
            _apply_root_children_canmount(self.UBUNTU, zfs, log)
        # The known intersection got `zfs set`:
        assert mock.was_called(f"zfs set canmount=off rpool/ROOT/{self.UBUNTU}/usr")
        assert mock.was_called(f"zfs set canmount=off rpool/ROOT/{self.UBUNTU}/var")
        assert mock.was_called(f"zfs set canmount=on rpool/ROOT/{self.UBUNTU}/var/lib")
        # The unknown extras did NOT:
        assert mock.was_not_called(f"rpool/ROOT/{self.UBUNTU}/opt")
        assert mock.was_not_called(f"rpool/ROOT/{self.UBUNTU}/srv/data")

    def test_missing_datasets_not_set(self):
        """A dataset zark expects but isn't present must not trigger `zfs set`."""
        # Present: only usr and usr/local. Everything else missing.
        actual = ["usr", "usr/local"]
        mock = self._make_mock(actual)
        with patch_sh(mock):
            log = make_log()
            zfs = ZFS(log)
            _apply_root_children_canmount(self.UBUNTU, zfs, log)
        # Should set on the two that exist:
        assert mock.was_called(f"canmount=off rpool/ROOT/{self.UBUNTU}/usr")
        assert mock.was_called(f"canmount=on rpool/ROOT/{self.UBUNTU}/usr/local")
        # Should NOT have tried any of the absent ones:
        for absent in ("var", "var/lib", "var/log", "srv"):
            assert mock.was_not_called(f"rpool/ROOT/{self.UBUNTU}/{absent}")

    def test_emit_warnings_skipped_when_no_drift(self):
        """Empty missing + empty extra → no warnings emitted (capture stdout)."""
        buf = StringIO()
        with redirect_stdout(buf):
            log = Log()  # writes to stdout via print
            _emit_dataset_layout_warnings(self.UBUNTU, set(), set(), log)
        assert "Missing" not in buf.getvalue()
        assert "Extra" not in buf.getvalue()

    def test_emit_warnings_lists_both(self):
        """When both sets are non-empty, both sections are printed."""
        buf = StringIO()
        with redirect_stdout(buf):
            log = Log()
            _emit_dataset_layout_warnings(
                self.UBUNTU,
                {"var/mail", "var/snap"},
                {"opt"},
                log,
            )
        out = buf.getvalue()
        assert "Missing" in out
        assert "var/mail" in out
        assert "var/snap" in out
        assert "Extra" in out
        assert "opt" in out

    def test_detect_root_children_strips_prefix(self):
        """_detect_root_children returns relative names, not full paths."""
        mock = self._make_mock(["usr", "var/lib", "var/log"])
        with patch_sh(mock):
            children = _detect_root_children(self.UBUNTU)
        # Relative names only
        assert children == {"usr", "var/lib", "var/log"}
        # Parent itself is excluded
        assert f"rpool/ROOT/{self.UBUNTU}" not in children


# ═════════════════════════════════════════════════════════════════════════
#  commands/recover.py — preventive disk-size guard
# ═════════════════════════════════════════════════════════════════════════


class TestRecoverDiskSize:  # pylint: disable=missing-function-docstring
    """Tests for _disk_size_bytes() and _check_target_disk_size()."""

    def test_disk_size_bytes_ok(self):
        mock = MockShell()
        # 2 TB NVMe in raw bytes
        mock.on("lsblk -bdn -o SIZE /dev/nvme0n1").succeeds("2000398934016")
        with patch_sh(mock):
            assert _disk_size_bytes("/dev/nvme0n1") == 2000398934016

    def test_disk_size_bytes_failure(self):
        mock = MockShell()
        mock.on("lsblk -bdn -o SIZE /dev/missing").fails()
        with patch_sh(mock):
            assert _disk_size_bytes("/dev/missing") == 0

    def test_disk_size_bytes_unparseable(self):
        mock = MockShell()
        mock.on("lsblk -bdn -o SIZE /dev/weird").succeeds("not-a-number")
        with patch_sh(mock):
            assert _disk_size_bytes("/dev/weird") == 0

    def test_check_target_disk_size_passes_when_large_enough(self):
        # Source uses 100 GiB; 5% overhead → threshold 105 GiB.
        # Target is 200 GiB → fits.
        mock, zfs = make_mock_zfs()
        mock.on("lsblk -bdn -o SIZE /dev/nvme0n1").succeeds(str(200 * 1024**3))
        mock.on("zfs list -H -p -o used backup/rpool").succeeds(str(100 * 1024**3))
        log = make_log()
        with patch_sh(mock):
            # No raise expected
            _check_target_disk_size("/dev/nvme0n1", "backup", log, zfs)

    def test_check_target_disk_size_fatal_when_too_small(self):
        # Source uses 200 GiB; 5% overhead → threshold 210 GiB.
        # Target is 100 GiB → fatal.
        mock, zfs = make_mock_zfs()
        mock.on("lsblk -bdn -o SIZE /dev/nvme0n1").succeeds(str(100 * 1024**3))
        mock.on("zfs list -H -p -o used backup/rpool").succeeds(str(200 * 1024**3))
        log = make_log()
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch("builtins.input", return_value=""),
        ):
            try:
                _check_target_disk_size("/dev/nvme0n1", "backup", log, zfs)
            except SystemExit:
                return
        raise AssertionError("Expected SystemExit from log.fatal()")

    def test_check_target_disk_size_silent_when_lsblk_fails(self):
        # Cannot measure target → defer silently to reactive handler
        mock, zfs = make_mock_zfs()
        mock.on("lsblk -bdn -o SIZE /dev/nvme0n1").fails()
        log = make_log()
        with patch_sh(mock):
            # No raise even though the rpool used would dominate
            _check_target_disk_size("/dev/nvme0n1", "backup", log, zfs)

    def test_check_target_disk_size_silent_when_used_unknown(self):
        # Cannot measure backup/rpool → defer silently
        mock, zfs = make_mock_zfs()
        mock.on("lsblk -bdn -o SIZE /dev/nvme0n1").succeeds(str(100 * 1024**3))
        mock.on("zfs list -H -p -o used backup/rpool").fails()
        log = make_log()
        with patch_sh(mock):
            _check_target_disk_size("/dev/nvme0n1", "backup", log, zfs)

    def test_check_target_disk_size_boundary_just_above(self):
        # Exactly 5% above source.used → just passes
        used = 100 * 1024**3
        threshold = used * 105 // 100  # 105 GiB
        mock, zfs = make_mock_zfs()
        mock.on("lsblk -bdn -o SIZE /dev/nvme0n1").succeeds(str(threshold))
        mock.on("zfs list -H -p -o used backup/rpool").succeeds(str(used))
        log = make_log()
        with patch_sh(mock):
            _check_target_disk_size("/dev/nvme0n1", "backup", log, zfs)

    def test_check_target_disk_size_boundary_just_below(self):
        # One byte below threshold → fatal
        used = 100 * 1024**3
        threshold = used * 105 // 100
        mock, zfs = make_mock_zfs()
        mock.on("lsblk -bdn -o SIZE /dev/nvme0n1").succeeds(str(threshold - 1))
        mock.on("zfs list -H -p -o used backup/rpool").succeeds(str(used))
        log = make_log()
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch("builtins.input", return_value=""),
        ):
            try:
                _check_target_disk_size("/dev/nvme0n1", "backup", log, zfs)
            except SystemExit:
                return
        raise AssertionError("Expected SystemExit from log.fatal()")


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
        rule = _classify(self._ds("rpool"), self.UBUNTU)
        assert rule["template"] == "minimal"
        assert rule["recursive"] is False

    def test_bpool_root_is_minimal_non_recursive(self):
        rule = _classify(self._ds("bpool"), self.UBUNTU)
        assert rule["template"] == "minimal"
        assert rule["recursive"] is False

    def test_live_system_is_production_recursive(self):
        rule = _classify(self._ds(f"rpool/ROOT/{self.UBUNTU}"), self.UBUNTU)
        assert rule["template"] == "production"
        assert rule["recursive"] is True

    def test_userdata_is_production_recursive(self):
        rule = _classify(self._ds("rpool/USERDATA"), self.UBUNTU)
        assert rule["template"] == "production"
        assert rule["recursive"] is True

    def test_bpool_boot_is_production_recursive(self):
        """bpool/BOOT holds kernels and grub config — same retention horizon
        as user data so the user can roll back ~3 months after a bad update."""
        rule = _classify(self._ds("bpool/BOOT"), self.UBUNTU)
        assert rule["template"] == "production"
        assert rule["recursive"] is True

    def test_bpool_boot_children_are_skipped(self):
        """Children of bpool/BOOT (the named boot environment) must be
        skipped because the recursive rule on bpool/BOOT covers them."""
        rule = _classify(self._ds(f"bpool/BOOT/{self.UBUNTU}"), self.UBUNTU)
        assert not rule

    def test_root_container_is_minimal_non_recursive(self):
        rule = _classify(self._ds("rpool/ROOT"), self.UBUNTU)
        assert rule["template"] == "minimal"
        assert rule["recursive"] is False

    def test_zvol_is_excluded_from_autosnap(self):
        """Critical: keystore (and any future zvol) must NOT be autosnapped.

        The keystore zvol is replicated by prepare/recover, not by sanoid.
        Snapshotting it accumulates useless snapshots that nobody consults.
        """
        rule = _classify(self._ds("rpool/keystore", type_="volume"), self.UBUNTU)
        assert rule["template"] is None  # signals autosnap=no

    def test_discovered_filesystem_is_minimal_recursive(self):
        """rpool/var, rpool/libvirt, anything else discovered → minimal+recursive.

        This is the agnostic default that protects users from forgetting
        to configure sanoid for new datasets they create.
        """
        for name in ("rpool/var", "rpool/libvirt", "rpool/data"):
            rule = _classify(self._ds(name), self.UBUNTU)
            assert rule["template"] == "minimal", f"failed for {name}"
            assert rule["recursive"] is True, f"failed for {name}"

    def test_userdata_children_are_skipped(self):
        """Children of recursively-covered parents must NOT get their own rule.

        Two overlapping rules confuse sanoid (upstream issue 627). Children
        inherit the parent's recursive policy automatically.
        """
        for name in ("rpool/USERDATA/home_xyz", "rpool/USERDATA/root_xyz"):
            rule = _classify(self._ds(name), self.UBUNTU)
            assert not rule, f"{name} should be skipped, got {rule}"

    def test_live_system_children_are_skipped(self):
        for name in (
            f"rpool/ROOT/{self.UBUNTU}/var",
            f"rpool/ROOT/{self.UBUNTU}/var/lib/docker",
        ):
            rule = _classify(self._ds(name), self.UBUNTU)
            assert not rule

    def test_other_boot_environment_is_skipped(self):
        """If the user has an alternate BE (e.g. rpool/ROOT/ubuntu_old),
        we should NOT generate a rule for it — the named ubuntu_* rule
        only covers the live one."""
        rule = _classify(self._ds("rpool/ROOT/ubuntu_old"), self.UBUNTU)
        assert not rule

    def test_format_zvol_rule_emits_autosnap_no(self):
        rule = _classify(self._ds("rpool/keystore", type_="volume"), self.UBUNTU)
        lines = _format_rule("rpool/keystore", rule)
        assert lines[0] == "[rpool/keystore]"
        assert "autosnap = no" in lines
        assert "autoprune = no" in lines

    def test_format_filesystem_rule_emits_template(self):
        rule = _classify(self._ds(f"rpool/ROOT/{self.UBUNTU}"), self.UBUNTU)
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
        assert (
            "rpool/data/swap" in names
        ), "zvol must keep its explicit rule even under a recursive parent"
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
            if rule:
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
            if rule:
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
            ("rpool/var", _classify(DatasetInfo(name="rpool/var", type="filesystem"), self.UBUNTU)),
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

    def test_save_omits_field_when_none(self):
        """A drive that has never been backed up successfully must not
        carry an empty ``last_backup_at`` on disk — keeps the JSON
        minimal and avoids implying a never-populated field is set."""
        cfg = make_config()
        cfg.known_drives["black"] = DriveInfo(
            name="black",
            guid="111",
            drive_id="drv-A",
            last_backup_at=None,
        )
        cfg.save_drives()
        data = json.loads((cfg.config_dir / "known_drives.json").read_text())
        assert "last_backup_at" not in data["black"]
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

        now = datetime(2026, 5, 8, 15, 0, 0, tzinfo=timezone.utc)
        info = self._info("2026-05-08T15:00:00Z")
        assert drive_staleness_days(info, now=now) == 0

    def test_staleness_counts_days(self):

        now = datetime(2026, 5, 8, 15, 0, 0, tzinfo=timezone.utc)
        info = self._info("2026-04-08T15:00:00Z")  # 30 days earlier
        assert drive_staleness_days(info, now=now) == 30

    def test_is_stale_at_threshold_boundary_not_stale(self):
        """At exactly threshold_days, the drive is NOT yet stale —
        is_drive_stale uses strict ``>`` so the boundary is fresh."""

        now = datetime(2026, 5, 8, 15, 0, 0, tzinfo=timezone.utc)
        info = self._info("2026-03-09T15:00:00Z")  # 60 days earlier
        assert drive_staleness_days(info, now=now) == 60
        assert not is_drive_stale(info, 60, now=now)

    def test_is_stale_beyond_threshold(self):

        now = datetime(2026, 5, 8, 15, 0, 0, tzinfo=timezone.utc)
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
        diff = {
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
        five_days_ago = (datetime.now(timezone.utc) - timedelta(days=70)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
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
        old = (datetime.now(timezone.utc) - timedelta(days=80)).strftime(
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
        old = (datetime.now(timezone.utc) - timedelta(days=70)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        drives = {"black": self._drive("black", old)}
        result = drives_in_danger_zone(drives, retention_days=90, margin_days=30)
        assert len(result) == 1
        assert result[0][0] == "black"
        assert result[0][1] >= 60  # 90 - 30 = 60 threshold; 70 days qualifies

    def test_excludes_drives_below_threshold(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        drives = {"black": self._drive("black", recent)}
        result = drives_in_danger_zone(drives, retention_days=90, margin_days=30)
        assert not result

    def test_excludes_named_drive(self):
        old = (datetime.now(timezone.utc) - timedelta(days=70)).strftime(
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
        d70 = (datetime.now(timezone.utc) - timedelta(days=70)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        d80 = (datetime.now(timezone.utc) - timedelta(days=80)).strftime(
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


def main() -> int:
    """Discover and run every Test* class in this module.

    Returns the number of failed tests (0 on full success), suitable as a
    process exit status.
    """
    passed = failed = 0
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

    print(f"\n  {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
