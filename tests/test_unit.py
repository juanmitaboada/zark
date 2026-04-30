"""
Unit tests for zark — all modules and command logic.

Run:  python3 -m pytest tests/test_unit.py -v
  or: python3 tests/test_unit.py

Tests run WITHOUT root, ZFS, or real disks. All shell commands are mocked.
"""

# pylint: disable=import-outside-toplevel


import json
import os
import sys
import tempfile

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

from unittest.mock import patch  # pylint: disable=wrong-import-position # noqa: E402

from lib.cleanup import Cleanup  # pylint: disable=wrong-import-position # noqa: E402
from lib.config import Config, DriveInfo  # pylint: disable=wrong-import-position # noqa: E402
from lib.drives import ConnectedDrive  # pylint: disable=wrong-import-position # noqa: E402
from lib.keystore import Keystore  # pylint: disable=wrong-import-position # noqa: E402
from lib.log import Log  # pylint: disable=wrong-import-position # noqa: E402
from lib.sh import RunResult, run  # pylint: disable=wrong-import-position # noqa: E402
from lib.zfs import (  # pylint: disable=wrong-import-position # noqa: E402
    ZFS,
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
        from lib.sh import part

        assert part("/dev/nvme0n1", 1) == "/dev/nvme0n1p1"
        assert part("/dev/nvme0n1", 4) == "/dev/nvme0n1p4"

    def test_sata(self):
        from lib.sh import part

        assert part("/dev/sda", 1) == "/dev/sda1"
        assert part("/dev/sda", 3) == "/dev/sda3"

    def test_virtio(self):
        from lib.sh import part

        assert part("/dev/vda", 2) == "/dev/vda2"

    def test_loop(self):
        from lib.sh import part

        assert part("/dev/loop0", 1) == "/dev/loop0p1"

    def test_mmcblk(self):
        from lib.sh import part

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
        mock.on("zfs list -H -o name,mountpoint,used,refer,canmount -r rpool").succeeds(
            "rpool\tnone\t500G\t192K\toff\n"
            "rpool/ROOT\tnone\t50G\t192K\toff\n"
            "rpool/ROOT/ubuntu_8bt2zy\t/\t50G\t10G\tnoauto\n"
            "rpool/ROOT/ubuntu_8bt2zy/home\t/home\t30G\t30G\ton\n",
        )
        with patch_sh(mock):
            datasets = zfs.list_datasets("rpool")
            assert len(datasets) == 4
            assert datasets[2].name == "rpool/ROOT/ubuntu_8bt2zy"
            assert datasets[2].mountpoint == "/"

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
        from commands.backup import (
            _detect_live_usb,
        )

        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("BOOT_IMAGE=/casper/vmlinuz boot=casper quiet splash")
        with patch_sh(mock):
            assert _detect_live_usb()

    def test_detect_live_usb_rofs(self):
        """Detects live USB via /rofs directory."""
        from commands.backup import (
            _detect_live_usb,
        )

        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("BOOT_IMAGE=/vmlinuz root=/dev/mapper/root")
        mock.on("test -d /rofs").succeeds()
        with patch_sh(mock):
            assert _detect_live_usb()

    def test_detect_live_usb_no_rpool(self):
        """Detects live USB when rpool is not imported."""
        from commands.backup import (
            _detect_live_usb,
        )

        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds("normal boot")
        mock.on("test -d /rofs").fails()
        mock.on("test -d /cow").fails()
        mock.on("zpool list rpool").fails()
        with patch_sh(mock):
            assert _detect_live_usb()

    def test_detect_normal_system(self):
        """Normal installed system is NOT live USB."""
        from commands.backup import (
            _detect_live_usb,
        )

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
        from lib.drives import validate_external_block_device

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
        from lib.drives import validate_external_block_device

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
        from commands.simulate import (
            OVMF_CODE_CANDIDATES,
        )

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
        import inspect  # pylint: disable=redefined-outer-name

        import commands.clean as clean_mod

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
        from commands.monitor import (
            _draw_bar,
        )

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
            import lib.sh as _sh

            r = _sh.run("echo hello")
            assert r.ok
            assert r.output == "hello"

    def test_failure_mock(self):
        mock = MockShell()
        mock.on("bad_cmd").fails("nope", rc=42)
        with patch_sh(mock):
            import lib.sh as _sh

            r = _sh.run("bad_cmd")
            assert not r.ok
            assert r.returncode == 42

    def test_unregistered_returns_127(self):
        mock = MockShell()
        with patch_sh(mock):
            import lib.sh as _sh

            r = _sh.run("unknown_command")
            assert r.returncode == 127

    def test_strict_mode(self):
        mock = MockShell(strict=True)
        with patch_sh(mock):
            import lib.sh as _sh

            try:
                _sh.run("unknown_command")
                assert False, "Should have raised"
            except AssertionError as e:
                assert "unexpected command" in str(e)

    def test_was_called(self):
        mock = MockShell()
        mock.on("zpool export backup").succeeds()
        with patch_sh(mock):
            import lib.sh as _sh

            _sh.run("zpool export backup")
        assert mock.was_called("zpool export backup")
        assert mock.was_not_called("zpool import")

    def test_call_count(self):
        mock = MockShell()
        mock.on("sync").succeeds()
        with patch_sh(mock):
            import lib.sh as _sh

            _sh.run("sync")
            _sh.run("sync")
            _sh.run("sync")
        assert mock.call_count("sync") == 3

    def test_regex_match(self):
        mock = MockShell()
        mock.on(r"zpool list \w+", regex=True).succeeds("pool_data")
        with patch_sh(mock):
            import lib.sh as _sh

            r = _sh.run("zpool list anything")
            assert r.ok

    def test_on_prefix(self):
        mock = MockShell()
        mock.on_prefix("syncoid").succeeds("syncing...")
        with patch_sh(mock):
            import lib.sh as _sh

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
        from pathlib import Path

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
        from pathlib import Path

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
        from commands.recover import (
            _UBUNTU_ROOT_CHILDREN_ALL,
            _apply_root_children_canmount,
        )

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
        from commands.recover import (
            _apply_root_children_canmount,
        )

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
        from commands.recover import (
            _UBUNTU_ROOT_CHILDREN_ALL,
            _apply_root_children_canmount,
        )

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
        from commands.recover import (
            _apply_root_children_canmount,
        )

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
        from commands.recover import (
            _apply_root_children_canmount,
        )

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
        from contextlib import (
            redirect_stdout,
        )
        from io import StringIO

        from commands.recover import (
            _emit_dataset_layout_warnings,
        )

        buf = StringIO()
        with redirect_stdout(buf):
            log = Log()  # writes to stdout via print
            _emit_dataset_layout_warnings(self.UBUNTU, set(), set(), log)
        assert "Missing" not in buf.getvalue()
        assert "Extra" not in buf.getvalue()

    def test_emit_warnings_lists_both(self):
        """When both sets are non-empty, both sections are printed."""
        from contextlib import (
            redirect_stdout,
        )
        from io import StringIO

        from commands.recover import (
            _emit_dataset_layout_warnings,
        )

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
        from commands.recover import (
            _detect_root_children,
        )

        mock = self._make_mock(["usr", "var/lib", "var/log"])
        with patch_sh(mock):
            children = _detect_root_children(self.UBUNTU)
        # Relative names only
        assert children == {"usr", "var/lib", "var/log"}
        # Parent itself is excluded
        assert f"rpool/ROOT/{self.UBUNTU}" not in children


# ═════════════════════════════════════════════════════════════════════════
#  Runner
# ═════════════════════════════════════════════════════════════════════════


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
