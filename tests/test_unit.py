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
from io import StringIO
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

from unittest.mock import patch  # pylint: disable=wrong-import-position # noqa: E402

import commands.clean as clean_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.sh as _sh  # pylint: disable=wrong-import-position # noqa: E402
from commands.backup import _detect_live_usb  # pylint: disable=wrong-import-position # noqa: E402
from commands.monitor import _draw_bar  # pylint: disable=wrong-import-position # noqa: E402
from commands.recover import (  # pylint: disable=wrong-import-position # noqa: E402
    _UBUNTU_ROOT_CHILDREN_ALL,
    _abort_missing_keystore,
    _apply_root_children_canmount,
    _detect_root_children,
    _emit_dataset_layout_warnings,
)
from commands.repair_divergent import (  # pylint: disable=wrong-import-position # noqa: E402
    SIZE_LIMIT_BYTES,
    _find_divergent,
)
from commands.setup import (  # pylint: disable=wrong-import-position # noqa: E402
    _classify,
    _diff_rules,
    _discover_rules,
    _format_rule,
    _generate_sanoid_conf,
    _parse_sanoid_conf,
)
from commands.simulate import (  # pylint: disable=wrong-import-position # noqa: E402
    OVMF_CODE_CANDIDATES,
)
from lib.cleanup import Cleanup  # pylint: disable=wrong-import-position # noqa: E402
from lib.config import Config, DriveInfo  # pylint: disable=wrong-import-position # noqa: E402
from lib.drives import (  # pylint: disable=wrong-import-position # noqa: E402
    ConnectedDrive,
    validate_external_block_device,
)
from lib.keystore import Keystore  # pylint: disable=wrong-import-position # noqa: E402
from lib.log import Log  # pylint: disable=wrong-import-position # noqa: E402
from lib.sh import RunResult, part, run  # pylint: disable=wrong-import-position # noqa: E402
from lib.zfs import (  # pylint: disable=wrong-import-position # noqa: E402
    ZFS,
    DatasetInfo,
    fix_grub_bpool_uuid,
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
            assert result == Path("/etc/zark"), (
                f"system install with no config should default to /etc/zark, " f"got {result}"
            )
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
                f"portable run with no config should default to <root>/etc, " f"got {result}"
            )
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

    def test_release_all_holds_no_holds_returns_zero(self):
        """When no held snapshots exist, the helper does nothing and reports 0."""
        mock, zfs = make_mock_zfs()
        mock.on("zfs holds -r -H blue/bpool").succeeds("")
        with patch_sh(mock):
            assert zfs.release_all_holds("blue/bpool") == 0

    def test_release_all_holds_releases_each_hold(self):
        """Each (snapshot, tag) pair from `zfs holds` produces one zfs release call."""
        mock, zfs = make_mock_zfs()
        mock.on("zfs holds -r -H blue/bpool").succeeds(
            "blue/bpool@syncoid_carmen_2026-05-05:14:25:19-GMT02:00\tsyncoid\t-\n"
            "blue/bpool/BOOT@syncoid_carmen_2026-05-05:14:25:19-GMT02:00\tsyncoid\t-\n",
        )
        # Match exact snapshot names — verifies the parser pulls col 1 + col 2 correctly
        mock.on(
            "zfs release syncoid blue/bpool@syncoid_carmen_2026-05-05:14:25:19-GMT02:00",
        ).succeeds("")
        mock.on(
            "zfs release syncoid blue/bpool/BOOT@syncoid_carmen_2026-05-05:14:25:19-GMT02:00",
        ).succeeds("")
        with patch_sh(mock):
            assert zfs.release_all_holds("blue/bpool") == 2

    def test_release_all_holds_continues_on_partial_failure(self):
        """If one release fails, the others still run. Caller relies on
        the destroy that follows to detect any real problem."""
        mock, zfs = make_mock_zfs()
        mock.on("zfs holds -r -H blue/bpool").succeeds(
            "blue/bpool@snap1\ttagA\t-\n" + "blue/bpool@snap2\ttagB\t-\n",
        )
        mock.on("zfs release tagA blue/bpool@snap1").fails("permission denied")
        mock.on("zfs release tagB blue/bpool@snap2").succeeds("")
        with patch_sh(mock):
            # Reports only the successful one
            assert zfs.release_all_holds("blue/bpool") == 1

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


class TestRepairDivergent:  # pylint: disable=missing-function-docstring
    """Tests for repair-divergent's detection logic.

    The command's safety hinges on _find_divergent correctly identifying
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
        """Wire up a MockShell that answers all the queries _find_divergent makes."""
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
            divergent = _find_divergent(zfs, "rpool", "blue", make_log())
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
            divergent = _find_divergent(zfs, "rpool", "blue", make_log())
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
            divergent = _find_divergent(zfs, "rpool", "blue", make_log())
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
            divergent = _find_divergent(zfs, "rpool", "blue", make_log())
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
            divergent = _find_divergent(zfs, "rpool", "blue", make_log())
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
            divergent = _find_divergent(zfs, "rpool", "blue", make_log())
        assert len(divergent) == 1
        assert divergent[0].used_bytes == 8192


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
