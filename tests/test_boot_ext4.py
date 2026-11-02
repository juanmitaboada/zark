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
Tests of the boot layouts (M12, M13): which layout origin has, and the
refusals for the ones zark does not back up.

``tests/test_unit.py`` imports every Test* class here by name, so its runner
finds them; run that file, not this one.
"""

import json
import tempfile
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import commands.backup as backup_mod
import commands.prepare as prepare_mod
import commands.recover as recover_mod
from lib import boot_layout, pool_record, survey, zfs_features
from lib.log import Log
from tests.mock_sh import MockShell, patch_sh

ORIGIN = ("rpool", "bpool")
_RPOOL_PART = "/dev/disk/by-id/ata-KINGSTON_X_S1-part4"
_BOOT_PART = "/dev/sda2"


def _log() -> Log:
    return Log(log_file="/dev/null")


def _shell(findmnt: str | None, bpool: bool = False, boot_disk: str = "sda") -> MockShell:
    """No bpool importable (unless imported); rpool on sda; ``findmnt`` of /boot."""
    mock = MockShell()
    if bpool:
        mock.on("zpool list -H -o name bpool").succeeds("bpool")
    else:
        mock.on("zpool list -H -o name bpool").fails("no such pool")
    mock.on("env LC_ALL=C ZPOOL_VDEV_NAME_PATH=1 zpool import").succeeds("")
    mock.on("zpool list -vHP rpool").succeeds(f"rpool\t1T\n\t{_RPOOL_PART}\t1T")
    mock.on_prefix(f"lsblk -nrs -o NAME,TYPE {_RPOOL_PART}").succeeds("sda4 part\nsda disk")
    mock.on_prefix(f"lsblk -nrs -o NAME,TYPE {_BOOT_PART}").succeeds(
        f"{boot_disk}2 part\n{boot_disk} disk"
    )
    if findmnt is None:
        mock.on("findmnt -n -o FSTYPE,SOURCE,UUID /boot").returns(1)
        mock.on("findmnt -n -o FSTYPE,SOURCE /boot").returns(1)
    else:
        mock.on("findmnt -n -o FSTYPE,SOURCE,UUID /boot").succeeds(findmnt)
        mock.on("findmnt -n -o FSTYPE,SOURCE /boot").succeeds(" ".join(findmnt.split()[:2]))
    return mock


def _layout(mock: MockShell, kernels: list[str] | None = None) -> tuple[str, str]:
    """(layout or "", what was printed)."""
    out = StringIO()
    got = ""
    with (
        patch_sh(mock),
        patch("lib.boot_layout.installed_kernels", return_value=kernels or []),
        patch("builtins.input", return_value=""),
        redirect_stdout(out),
        redirect_stderr(StringIO()),
    ):
        try:
            got = survey.require_boot_layout(_log())
        except SystemExit:
            pass
    return got, out.getvalue()


_EXT4 = f"ext4 {_BOOT_PART} 8788631c-0000-4000-8000-000000000001\n"


class TestBootLayoutOrigin:  # pylint: disable=missing-function-docstring
    """M12-1 1a, M12R-7: the layout of origin, read at every run."""

    def test_the_26_10_installer_layout_is_ext4(self):
        got, _ = _layout(_shell(_EXT4), kernels=["7.3.0-10-generic"])
        assert got == "ext4"

    def test_an_imported_bpool_is_bpool(self):
        got, _ = _layout(_shell("zfs bpool/BOOT/ubuntu_x\n", bpool=True))
        assert got == "bpool"

    def test_an_imported_bpool_with_boot_not_mounted_is_bpool(self):
        """findmnt fails (nothing at /boot): today's answer, bpool."""
        got, _ = _layout(_shell(None, bpool=True))
        assert got == "bpool"

    def test_a_bpool_and_an_ext4_boot_are_refused(self):
        got, out = _layout(_shell(_EXT4, bpool=True), kernels=["7.3.0-10-generic"])
        assert not got
        assert "This system has a bpool and /boot on ext4" in out

    def test_boot_on_another_disk_is_refused(self):
        got, out = _layout(_shell(_EXT4, boot_disk="sdb"), kernels=["7.3.0-10-generic"])
        assert not got
        assert "/boot on ext4 is not as Ubuntu's installer lays it out" in out
        assert f"/boot ({_BOOT_PART}) is not on rpool's disk" in out

    def test_boot_without_a_kernel_of_the_root_is_refused(self):
        got, out = _layout(_shell(_EXT4), kernels=[])
        assert not got
        assert "/boot holds no kernel of this root" in out

    def test_boot_in_the_root_is_refused(self):
        got, out = _layout(_shell(None))
        assert not got
        assert "This system has no bpool" in out
        assert "/boot is not a mount of its own" in out

    def test_boot_on_another_file_system_is_refused(self):
        got, out = _layout(_shell(f"vfat {_BOOT_PART} 5FBB-7AE5\n"))
        assert not got
        assert "This system has no bpool" in out
        assert "/boot is on vfat" in out
        assert "Secure Boot is on" not in out
        assert survey.EXT4_ISSUE in out


class TestBootLayoutKernels:  # pylint: disable=missing-function-docstring
    """M12R-7: a kernel of this root in /boot, read from the files."""

    def test_a_vmlinuz_without_its_modules_is_a_problem(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "boot").mkdir()
            Path(root, "boot", "vmlinuz-7.3.0-10-generic").write_text("k", encoding="ascii")
            mount = boot_layout.BootMount("ext4", _BOOT_PART, "u")
            with patch_sh(_shell(_EXT4)):
                problems = boot_layout.ext4_problems(mount, {"/dev/sda"}, root)
        assert problems == [
            "/boot holds no kernel of this root (a vmlinuz-<version> with its "
            + "/lib/modules/<version>): it may not have been mounted at boot"
        ]

    def test_a_vmlinuz_with_its_modules_is_none(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "boot").mkdir()
            Path(root, "boot", "vmlinuz-7.3.0-10-generic").write_text("k", encoding="ascii")
            mods = Path(root, "lib", "modules", "7.3.0-10-generic")
            mods.mkdir(parents=True)
            (mods / "modules.dep").write_text("", encoding="ascii")
            mount = boot_layout.BootMount("ext4", _BOOT_PART, "u")
            with patch_sh(_shell(_EXT4)):
                assert not boot_layout.ext4_problems(mount, {"/dev/sda"}, root)

    def test_the_copy_is_a_child_of_the_boot_environment(self):
        assert boot_layout.boot_copy("ubuntu_abc") == "rpool/ROOT/ubuntu_abc/zark-boot"


def _refused_before(run: object, patches: list[object], forbidden: str) -> MockShell:
    """``run()`` with the layout read as ext4: it must end before ``forbidden`` runs."""
    mock = _shell(_EXT4)
    with ExitStack() as stack:
        for cm in (
            patch_sh(mock),
            patch.object(survey, "require_bookmark_v2"),
            patch.object(survey, "require_boot_layout", return_value="ext4"),
            patch("builtins.input", return_value=""),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
            *patches,
        ):
            _ = stack.enter_context(cm)  # type: ignore[arg-type]
        try:
            run()  # type: ignore[operator]
        except SystemExit:
            pass
        else:
            raise AssertionError("expected a refusal")
    assert mock.was_not_called(forbidden)
    return mock


class TestBootLayoutCallers:  # pylint: disable=missing-function-docstring
    """backup and prepare refuse an ext4 system until its /boot is kept."""

    def test_backup_refuses_before_scanning(self):
        scan = MagicMock()
        mock = _refused_before(
            lambda: backup_mod.run([]),
            [
                patch.object(backup_mod, "Config"),
                patch.object(backup_mod, "_detect_live_usb", return_value=False),
                patch.object(backup_mod, "warn_rpool_mountpoint_lost"),
                patch.object(backup_mod, "warn_kernel_named_vdevs"),
                patch.object(backup_mod.certificates, "show_warnings"),
                patch.object(backup_mod, "scan_connected_drives", scan),
                patch.object(backup_mod.Cleanup, "register", lambda _self: None),
            ],
            "zpool import -d",
        )
        scan.assert_not_called()
        assert mock.was_called("findmnt -n -o FSTYPE,SOURCE /boot")

    def test_prepare_refuses_before_touching_the_disk(self):
        _ = _refused_before(
            lambda: prepare_mod.run(["/dev/sdb"]),
            [
                patch.object(prepare_mod, "Config"),
                patch.object(prepare_mod, "validate_external_block_device"),
                patch.object(prepare_mod, "warn_rpool_mountpoint_lost"),
            ],
            "zpool create",
        )


_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "boot-ext4"
_UUID = "0b0e7e57-0000-4000-8000-000000000001"
_SWAP_PARTUUID = "00000000-0000-4000-8000-0000000000a3"
_LINUX_FS = "0fc63daf-8483-4772-8e79-3d69d8477de4"


def _boot_record(**over: object) -> pool_record.BootRecord:
    base: dict[str, object] = {
        "dataset": "rpool/ROOT/ubuntu_abc123/zark-boot",
        "fstype": "ext4",
        "uuid": _UUID,
        "label": "",
        "size_bytes": 524288 * 4096,
        "features": pool_record.EXT4_FEATURES,
        "mount_options": "defaults",
        "partition": pool_record.Partition(2 * 1024**3, _LINUX_FS),
        "esp": pool_record.Esp(2201600 * 512, "1A2B-3C4D"),
        "swap": pool_record.Swap(
            4 * 1024**3, (_FIXTURES / "crypttab").read_text(encoding="ascii").strip()
        ),
    }
    base.update(over)
    return pool_record.BootRecord(**base)  # type: ignore[arg-type]


def _record(**over: object) -> pool_record.PoolRecord:
    base: dict[str, object] = {
        "ubuntu": "26.10",
        "zfs": "zfs-2.4.4-1ubuntu4",
        "kmod": "zfs-kmod-2.4.4-1ubuntu4",
        "pools": {"rpool": pool_record.PoolState("off", {"encryption": "active"})},
    }
    base.update(over)
    return pool_record.PoolRecord(**base)  # type: ignore[arg-type]


def _parse_as_2_1_1(value: str) -> tuple[object, ...] | None:
    """zark 2.1.1's parse (lib/pool_record.py:129-143 at v2.1.1), reduced to
    what it returns: the five keys it reads."""
    data = json.loads(value)
    if not isinstance(data, dict) or data.get("format") != 1:
        return None
    raw = data.get("pools")
    if not isinstance(raw, dict):
        return None
    return (data.get("ubuntu", ""), data.get("zfs", ""), data.get("kmod", ""), raw)


class TestPoolRecordBoot:  # pylint: disable=missing-function-docstring
    """M12-8, M12R-8, M12 plan 5.3: the record's boot entry and versions."""

    def test_a_2_1_record_is_a_bpool_point_any_zark_recovers(self):
        rec = pool_record.parse(pool_record.dump(_record(pools={})))
        assert rec is not None and rec.boot is None and not rec.min_recover
        assert pool_record.layout_of(rec) == "bpool"
        assert pool_record.recovers(rec.min_recover, "2.1.1")
        assert pool_record.layout_of(None) == "bpool"

    def test_an_ext4_record_reads_back_whole(self):
        rec = _record(boot=_boot_record(), written_by="2.2.0-rc2", min_recover="2.2.0")
        back = pool_record.parse(pool_record.dump(rec))
        assert back == rec
        assert pool_record.layout_of(back) == "ext4"

    def test_an_ext4_record_without_esp_or_swap_reads_back(self):
        rec = _record(boot=_boot_record(esp=None, swap=None), min_recover="2.2.0")
        assert pool_record.parse(pool_record.dump(rec)) == rec

    def test_a_bpool_record_carries_written_by_only(self):
        data = json.loads(pool_record.dump(_record(written_by="2.2.0-rc2")))
        assert data["written_by"] == "2.2.0-rc2"
        assert "min_recover" not in data and "boot" not in data and data["format"] == 1

    def test_a_bpool_boot_entry_is_a_bpool_point(self):
        value = json.dumps({"format": 1, "pools": {}, "boot": {"layout": "bpool"}})
        rec = pool_record.parse(value)
        assert rec is not None and pool_record.layout_of(rec) == "bpool"

    def test_an_unreadable_boot_entry_is_not_taken_for_bpool(self):
        value = json.dumps({"format": 1, "pools": {}, "boot": {"layout": "ext4", "fstype": "ext4"}})
        rec = pool_record.parse(value)
        assert rec is not None and rec.boot is None and rec.boot_problem
        assert pool_record.layout_of(rec) == "ext4"

    def test_the_full_record_fits_a_user_property(self):
        every = {f.name: "active" for f in zfs_features.FEATURES}
        pools = {p: pool_record.PoolState("openzfs-2.2,grub2", dict(every)) for p in ORIGIN}
        value = pool_record.dump(
            _record(pools=pools, boot=_boot_record(), written_by="2.2.0-rc2", min_recover="2.2.0")
        )
        assert len(value.encode()) < 8191

    def test_zark_2_1_1_reads_the_same_record_with_or_without_the_new_keys(self):
        plain = pool_record.dump(_record())
        extended = pool_record.dump(
            _record(boot=_boot_record(), written_by="2.2.0-rc2", min_recover="2.2.0")
        )
        assert _parse_as_2_1_1(plain) is not None
        assert _parse_as_2_1_1(extended) == _parse_as_2_1_1(plain)

    def test_version_comparison(self):
        assert pool_record.recovers("2.2.0", "2.2.0-rc3")
        assert not pool_record.recovers("2.2.0", "2.1.1")
        assert pool_record.recovers("2.2.0", "2.3.0")
        assert pool_record.recovers("2.2.0", "2.2.0")
        assert not pool_record.recovers("garbage", "2.3.0")
        assert not pool_record.recovers("2.2", "2.3.0")
        assert not pool_record.recovers("2.2.0", "")
        assert pool_record.recovers("", "anything")
        assert pool_record.zark_version_tuple("2.2.0-rc12") == (2, 2, 0)
        assert pool_record.zark_version_tuple("2.2.0~rc1") is None

    def test_a_non_text_min_recover_is_refused(self):
        value = json.dumps({"format": 1, "pools": {}, "min_recover": [2, 2, 0]})
        rec = pool_record.parse(value)
        assert rec is not None and not pool_record.recovers(rec.min_recover, "9.9.9")


def _capture_shell(tune2fs_ok: bool = True) -> MockShell:
    mock = MockShell()
    mock.on("zpool get -H -p -o property,value all rpool").succeeds(
        "compatibility\toff\nfeature@encryption\tactive\n"
    )
    mock.on("zfs version").succeeds("zfs-2.4.4-1ubuntu4\nzfs-kmod-2.4.4-1ubuntu4")
    mock.on("findmnt -n -o FSTYPE,SOURCE,UUID /boot").succeeds(f"ext4 /dev/sda2 {_UUID}\n")
    mock.on("findmnt -n -o SOURCE /").succeeds("rpool/ROOT/ubuntu_abc123\n")
    if tune2fs_ok:
        mock.on("tune2fs -l /dev/sda2").succeeds(
            (_FIXTURES / "tune2fs-l.txt").read_text(encoding="ascii")
        )
    else:
        mock.on("tune2fs -l /dev/sda2").returns(1, "", "tune2fs: Bad magic number")
    mock.on("lsblk -b -n -d -o SIZE,PARTTYPE,PARTUUID /dev/sda2").succeeds(
        f"2147483648 {_LINUX_FS} 607282f7-0000-4000-8000-000000000002\n"
    )
    mock.on("blkid -p -s LABEL -o value /dev/sda2").succeeds("")
    mock.on("findmnt -n -o SOURCE,UUID /boot/efi").succeeds("/dev/sda1 1A2B-3C4D\n")
    mock.on("lsblk -b -n -d -o SIZE,PARTTYPE,PARTUUID /dev/sda1").succeeds(
        "1127219200 c12a7328-f81f-11d2-ba4b-00a0c93ec93b b7d5c279-0000-4000-8000-000000000003\n"
    )
    mock.on(
        f"lsblk -b -n -d -o SIZE,PARTTYPE,PARTUUID /dev/disk/by-partuuid/{_SWAP_PARTUUID}"
    ).succeeds(f"4294967296 {_LINUX_FS} {_SWAP_PARTUUID}\n")
    return mock


def _etc() -> tempfile.TemporaryDirectory[str]:
    # The caller enters it: `with _etc() as root:`.
    d = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
    Path(d.name, "etc").mkdir()
    for name in ("fstab", "crypttab"):
        Path(d.name, "etc", name).write_text(
            (_FIXTURES / name).read_text(encoding="ascii"), encoding="ascii"
        )
    return d


class TestPoolRecordCapture:  # pylint: disable=missing-function-docstring
    """M12 plan 5.3, M13C-5: what an ext4 system's record holds, and the
    refusal when it cannot be read."""

    def test_an_ext4_system_records_its_boot_as_the_installer_made_it(self):
        with _etc() as root, patch_sh(_capture_shell()):
            boot, why = pool_record.capture_boot(root)
        assert boot is not None, why
        assert boot == _boot_record(
            esp=pool_record.Esp(1127219200, "1A2B-3C4D"),
            partition=pool_record.Partition(2147483648, _LINUX_FS),
        )
        assert "needs_recovery" not in boot.features and "orphan_present" not in boot.features

    def test_capture_names_rpool_alone_and_the_versions(self):
        with (
            _etc() as root,
            patch_sh(_capture_shell()),
            patch.object(
                pool_record, "_read", lambda p: Path(root + p).read_text(encoding="ascii")
            ),
        ):
            rec = pool_record.parse(pool_record.capture(_log(), "ext4"))
        assert rec is not None and set(rec.pools) == {"rpool"}
        assert rec.boot is not None and rec.min_recover == "2.2.0"
        assert rec.written_by == pool_record.VERSION

    def test_a_bpool_capture_records_written_by(self):
        mock = MockShell()
        mock.on("zpool get -H -p -o property,value all").succeeds("compatibility\toff\n")
        mock.on("zfs version").succeeds("zfs-2.4.1\nzfs-kmod-2.4.1")
        with patch_sh(mock):
            rec = pool_record.parse(pool_record.capture(_log()))
        assert rec is not None and set(rec.pools) == {"rpool", "bpool"}
        assert rec.written_by == pool_record.VERSION and not rec.min_recover

    def test_an_unreadable_boot_gives_no_record_and_says_why(self):
        out = StringIO()
        with _etc() as _root, patch_sh(_capture_shell(tune2fs_ok=False)), redirect_stdout(out):
            assert pool_record.capture(_log(), "ext4") == ""
        assert (
            "Cannot record /boot: tune2fs -l /dev/sda2: tune2fs: Bad magic number" in out.getvalue()
        )

    def test_no_record_on_an_ext4_system_is_refused_before_anything_is_written(self):
        out = StringIO()
        with redirect_stdout(out), patch("builtins.input", return_value=""):
            try:
                pool_record.require("", "ext4", _log())
            except SystemExit:
                pass
            else:
                raise AssertionError("expected a refusal")
        assert "Cannot record how this system's pools and /boot are" in out.getvalue()
        assert "Nothing was written" in out.getvalue()

    def test_no_record_on_a_bpool_system_goes_on(self):
        pool_record.require("", "bpool", _log())
        pool_record.require("{}", "ext4", _log())

    def test_backup_records_origin_before_scanning_drives(self):
        order: list[str] = []

        def capture(*_a: object) -> str:
            order.append("capture")
            return "{}"

        def scan(*_a: object) -> list[object]:
            order.append("scan")
            return []

        with (
            patch.object(backup_mod, "Config"),
            patch.object(backup_mod, "_detect_live_usb", return_value=False),
            patch.object(backup_mod, "warn_rpool_mountpoint_lost"),
            patch.object(backup_mod, "warn_kernel_named_vdevs"),
            patch.object(backup_mod.certificates, "show_warnings"),
            patch.object(survey, "require_bookmark_v2"),
            patch.object(survey, "require_boot_layout", return_value="bpool"),
            patch.object(pool_record, "capture", side_effect=capture),
            patch.object(backup_mod, "scan_connected_drives", side_effect=scan),
            patch.object(backup_mod.Cleanup, "register", lambda _self: None),
            patch_sh(MockShell()),
            patch("builtins.input", return_value=""),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            try:
                backup_mod.run([])
            except SystemExit:
                pass
        assert order == ["capture", "scan"]


def _refusal(rec: pool_record.PoolRecord | None) -> str:
    out = StringIO()
    with redirect_stdout(out), patch("builtins.input", return_value=""):
        try:
            recover_mod.refuse_unrecoverable(rec, _log())
        except SystemExit:
            return out.getvalue()
    return ""


class TestRecoverRefusesBeforeErasing:  # pylint: disable=missing-function-docstring
    """M12-8, M13F-3: a point this zark must not recover is refused before
    anything is erased."""

    def test_a_point_needing_a_newer_zark_is_refused(self):
        out = _refusal(_record(min_recover="9.0.0", written_by="9.0.0"))
        assert "This point needs zark 9.0.0 or later to recover" in out
        assert "written by zark 9.0.0" in out and "Nothing was erased" in out

    def test_an_unreadable_min_recover_is_refused(self):
        out = _refusal(_record(min_recover="soon"))
        assert "names an oldest zark this one cannot read: soon" in out
        assert "Nothing was erased" in out

    def test_an_ext4_point_is_refused(self):
        out = _refusal(_record(boot=_boot_record(), min_recover="2.2.0"))
        assert "This point's /boot is on ext4, which this zark does not recover" in out
        assert "Nothing was erased" in out

    def test_an_unreadable_boot_entry_is_refused(self):
        value = json.dumps({"format": 1, "pools": {}, "boot": {"layout": "zfs"}})
        out = _refusal(pool_record.parse(value))
        assert "Its /boot record is unreadable" in out

    def test_a_bpool_point_and_no_record_go_on(self):
        assert not _refusal(_record(written_by="2.2.0-rc2"))
        assert not _refusal(_record())
        assert not _refusal(None)
