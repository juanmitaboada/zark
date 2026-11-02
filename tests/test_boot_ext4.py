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

# pylint: disable=too-many-lines
# Rationale: the boot layouts' suite, one module by design, as tests/test_unit.py.

import json
import tempfile
from collections.abc import Callable
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import commands.backup as backup_mod
import commands.prepare as prepare_mod
import commands.recover as recover_mod
import commands.setup as setup_mod
from lib import (
    boot_chain,
    boot_layout,
    boot_rewrite,
    engine,
    exclusions,
    mount_props,
    origin_prune,
    pool_record,
    recover_ext4,
    replication as repl,
    retention,
    survey,
    zfs,
    zfs_features,
)
from lib.log import Log
from lib.restore_points import Snap
from lib.zfs import DatasetInfo
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
    """backup and prepare refuse an ext4 system without its zark-boot
    before anything is scanned or written (M12 plan 5.2)."""

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
        assert mock.was_called("findmnt -n -o SOURCE /")

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


def _refusal(rec: pool_record.PoolRecord | None, layout: str = "") -> str:
    out = StringIO()
    with redirect_stdout(out), patch("builtins.input", return_value=""):
        try:
            recover_mod.refuse_unrecoverable(
                rec,
                layout or pool_record.layout_of(rec),  # type: ignore[arg-type]
                _log(),
            )
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

    def test_an_ext4_point_without_a_record_is_refused(self):
        out = _refusal(None, "ext4")
        assert "This point's /boot is on ext4, and it has no record of origin's disk" in out
        assert "Nothing was erased" in out


_BE = "ubuntu_abc123"
_POINT = "zark_20261102T100000Z"


def _snap(dataset: str, name: str = _POINT) -> Snap:
    return Snap(dataset, name, "1", 10, 1)


_SNAPS = [_snap(f"rpool/ROOT/{_BE}"), _snap("rpool/USERDATA/home_x")]


def _from_record(
    read: object, snaps: list[Snap] | None = None, meta: dict[str, str] | None = None
) -> tuple[str, MockShell, dict[str, object] | None]:
    """recover's _pools_from_record with ``pool_record.read`` patched
    (a value, or an exception to raise): its output, the shell, and what
    it returned (None when it refused)."""
    mock = MockShell()
    mock.on("zpool upgrade -v").succeeds("")
    mock.on("zfs version").succeeds("zfs-2.4.4-1ubuntu4\n")
    out = StringIO()
    got: dict[str, object] | None = None
    reader = (
        MagicMock(side_effect=read) if isinstance(read, Exception) else MagicMock(return_value=read)
    )
    with (
        patch.object(pool_record, "read", reader),
        patch_sh(mock),
        redirect_stdout(out),
        patch("builtins.input", return_value=""),
    ):
        try:
            specs, _, _ = recover_mod._pools_from_record(  # pylint: disable=protected-access
                "backup", _POINT, _SNAPS if snaps is None else snaps, _BE, meta or {}, _log()
            )
            got = dict(specs)
        except SystemExit:
            got = None
    return out.getvalue(), mock, got


class TestRecoverPointLayout:  # pylint: disable=missing-function-docstring
    """M13R-1, M13R-2, M13F-6 6a: a point's layout from its record, else
    from the drive; a record that cannot be read is a refusal."""

    def test_the_record_decides_when_there_is_one(self):
        meta = {"origin-layout": "ext4"}
        snaps = [*_SNAPS, _snap(f"rpool/ROOT/{_BE}/zark-boot")]
        assert pool_record.point_layout(_record(), snaps, _BE, meta) == "bpool"
        rec = _record(boot=_boot_record())
        assert pool_record.point_layout(rec, _SNAPS, _BE, {}) == "ext4"

    def test_without_a_record_the_drive_decides(self):
        copy = _snap(f"rpool/ROOT/{_BE}/zark-boot", "autosnap_2026-11-01_00:00:00_daily")
        assert pool_record.point_layout(None, [*_SNAPS, copy], _BE, {}) == "ext4"
        assert pool_record.point_layout(None, _SNAPS, _BE, {"origin-layout": "ext4"}) == "ext4"
        assert pool_record.point_layout(None, _SNAPS, _BE, {}) == "bpool"
        other = _snap("rpool/ROOT/ubuntu_other/zark-boot")
        assert pool_record.point_layout(None, [*_SNAPS, other], _BE, {}) == "bpool"

    def test_only_an_ext4_system_marks_the_drive(self):
        assert pool_record.origin_layout_fields("ext4") == {"origin-layout": "ext4"}
        assert not pool_record.origin_layout_fields("bpool")

    def test_a_record_less_point_with_a_copy_is_refused_before_anything(self):
        out, mock, got = _from_record(None, [*_SNAPS, _snap(f"rpool/ROOT/{_BE}/zark-boot")])
        assert got is None and "has no record of origin's disk" in out
        assert "Nothing was erased" in out and mock.was_not_called("zpool upgrade -v")

    def test_a_record_less_point_of_a_marked_drive_is_refused(self):
        out, _, got = _from_record(None, meta={"origin-layout": "ext4"})
        assert got is None and "has no record of origin's disk" in out

    def test_a_record_less_point_of_a_2_1_drive_goes_on_with_the_warning(self):
        out, mock, got = _from_record(None)
        assert got == {} and mock.was_called("zpool upgrade -v")
        assert "does not record origin's pool features" in out

    def test_a_later_format_naming_its_zark_is_refused_by_name(self):
        value = json.dumps({"format": 2, "written_by": "3.0.0", "min_recover": "3.0.0"})
        assert pool_record.parse(value) is None
        assert pool_record.versions(value) == ("3.0.0", "3.0.0")
        e = pool_record.RecordError("unreadable", *pool_record.versions(value))
        out, mock, got = _from_record(e)
        assert got is None and "This point needs zark 3.0.0 or later to recover" in out
        assert "Nothing was erased" in out and mock.was_not_called("zpool upgrade -v")

    def test_an_unreadable_record_is_refused(self):
        out, _, got = _from_record(pool_record.RecordError("they disagree", "2.2.0", ""))
        assert got is None and "cannot be read: they" in out and "disagree" in out
        assert "Nothing was erased" in out

    def test_read_names_the_version_that_blocks(self):
        good = pool_record.dump(_record(written_by="2.2.0"))
        later = json.dumps({"format": 2, "written_by": "3.1.0", "min_recover": "3.0.0"})
        snaps = [_snap(f"rpool/ROOT/{_BE}"), _snap("rpool/USERDATA/home_x")]
        marks = {
            f"backup/rpool/ROOT/{_BE}@{_POINT}": good,
            f"backup/rpool/USERDATA/home_x@{_POINT}": later,
        }
        with patch.object(pool_record, "snapshot_marks", return_value=marks):
            try:
                pool_record.read("backup", _POINT, snaps)
            except pool_record.RecordError as e:
                assert (e.written_by, e.min_recover) == ("3.1.0", "3.0.0")
            else:
                raise AssertionError("no RecordError")

    def test_an_ext4_record_is_refused_before_the_pools_are_planned(self):
        """M13R-3 (M08): _pools_from_record itself calls the refusal."""
        for rec, said in (
            (_record(boot=_boot_record(), min_recover="2.2.0"), "which this zark does not"),
            (_record(min_recover="9.0.0"), "This point needs zark 9.0.0 or later"),
        ):
            out, mock, got = _from_record(rec)
            assert got is None and said in out, out
            assert "Nothing was erased" in out and mock.was_not_called("zpool upgrade -v")

    def test_the_drive_is_marked_at_create(self):
        cmd = zfs.create_command("b", "/d", "/r", "", {"origin-layout": "ext4"})
        assert "-O org.zark:origin-layout=ext4 -m none" in cmd
        assert "org.zark" not in zfs.create_command("b", "/d", "/r", "")

    def test_a_drive_that_will_not_take_the_mark_is_refused(self):
        mock = MockShell()
        mock.on("zfs set org.zark:origin-layout=ext4 blue").fails("read-only")
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out), patch("builtins.input", return_value=""):
            try:
                pool_record.mark_origin_layout("blue", "ext4", _log())
            except SystemExit:
                pass
        assert "Cannot record on blue that this system has /boot on ext4" in out.getvalue()
        quiet = MockShell()
        with patch_sh(quiet):
            pool_record.mark_origin_layout("blue", "bpool", _log())
        assert not quiet.calls


_COPY = "rpool/ROOT/ubuntu_abc123/zark-boot"


def _setup_shell(exists: bool = False, props: str = "posix\nsa\n") -> MockShell:
    mock = MockShell()
    mock.on("findmnt -n -o SOURCE /").succeeds("rpool/ROOT/ubuntu_abc123\n")
    listing = "rpool/ROOT\nrpool/ROOT/ubuntu_abc123\n" + (f"{_COPY}\n" if exists else "")
    mock.on("zfs list -H -o name -r rpool/ROOT").succeeds(listing)
    mock.on(f"zfs create -o canmount=noauto -o mountpoint=legacy {_COPY}").succeeds("")
    mock.on(f"zfs get -H -o value acltype,xattr {_COPY}").succeeds(props)
    return mock


def _keep(mock: MockShell, answer: bool = True) -> tuple[bool, str]:
    """(refused, what was printed) of setup's /boot step."""
    out = StringIO()
    with (
        patch_sh(mock),
        patch("lib.log.Log.ask", return_value=answer),
        patch("builtins.input", return_value=""),
        redirect_stdout(out),
    ):
        try:
            setup_mod.keep_boot(_log())
        except SystemExit:
            return True, out.getvalue()
    return False, out.getvalue()


def _eof(_prompt: str) -> str:
    raise EOFError


class TestSetupBootCopy:  # pylint: disable=missing-function-docstring
    """M12-2 2e, M13F-1 1a: setup creates zark-boot on an ext4 system."""

    def test_a_closed_terminal_declines_the_copy(self):
        """M13R-5 (M25): EOF at the question creates nothing, though yes is
        the default for a person who presses Enter."""
        mock = _setup_shell()
        out = StringIO()
        with patch_sh(mock), patch("builtins.input", side_effect=_eof), redirect_stdout(out):
            try:
                setup_mod.keep_boot(_log())
            except SystemExit:
                pass
        assert mock.was_not_called("zfs create")

    def test_it_is_created_in_one_zfs_create_with_its_properties(self):
        mock = _setup_shell()
        refused, out = _keep(mock)
        assert not refused
        assert mock.was_called(f"zfs create -o canmount=noauto -o mountpoint=legacy {_COPY}")
        assert mock.was_not_called("zfs set")
        assert f"{_COPY} created" in out
        assert "canmount=noauto, mountpoint=legacy" in out and "space: about" in out

    def test_declined_it_is_not_created_and_backup_will_refuse(self):
        mock = _setup_shell()
        refused, out = _keep(mock, answer=False)
        assert not refused and mock.was_not_called("zfs create")
        assert "backup and prepare refuse this system until it exists" in out

    def test_an_existing_copy_is_left_as_it_is(self):
        mock = _setup_shell(exists=True)
        refused, out = _keep(mock)
        assert not refused and mock.was_not_called("zfs create")
        assert f"{_COPY} exists" in out

    def test_without_acls_and_xattrs_from_rpool_it_is_refused(self):
        refused, out = _keep(_setup_shell(props="off\non\n"))
        assert refused
        assert "zfs get acltype,xattr: off, on" in out

    def test_a_leftover_copy_on_a_bpool_system_is_named_not_removed(self):
        mock = _setup_shell(exists=True)
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            setup_mod._unused_boot_copies(_log())  # pylint: disable=protected-access
        assert f"{_COPY}: no longer used by zark" in out.getvalue()
        assert mock.was_not_called("zfs destroy")

    def test_a_bpool_system_without_one_prints_nothing_new(self):
        mock = _setup_shell()
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            setup_mod._unused_boot_copies(_log())  # pylint: disable=protected-access
        assert not out.getvalue()

    def test_the_layout_setup_reads(self):
        mock = MockShell()
        mock.on("zpool list -H -o name bpool").succeeds("bpool")
        with patch_sh(mock):
            assert boot_layout.system_layout() == "bpool"
        mock = MockShell()
        mock.on("zpool list -H -o name bpool").fails("no such pool")
        mock.on("findmnt -n -o FSTYPE,SOURCE,UUID /boot").succeeds(f"ext4 /dev/sda2 {_UUID}\n")
        with patch_sh(mock):
            assert boot_layout.system_layout() == "ext4"
        mock = MockShell()
        mock.on("zpool list -H -o name bpool").fails("no such pool")
        with patch_sh(mock):
            assert boot_layout.system_layout() is None

    def test_sanoid_covers_it_through_the_boot_environment(self):
        ds = DatasetInfo(name=_COPY, type="filesystem")
        assert setup_mod._classify(ds, "ubuntu_abc123") is None  # pylint: disable=protected-access
        be = DatasetInfo(name="rpool/ROOT/ubuntu_abc123", type="filesystem")
        rule = setup_mod._classify(be, "ubuntu_abc123")  # pylint: disable=protected-access
        assert rule is not None and rule["template"] == "production" and rule["recursive"]

    def test_it_is_never_excluded(self):
        assert exclusions.never_excluded(_COPY) == "a boot environment is never excluded"

    def test_recover_never_mounts_it_with_the_tree(self):
        props = mount_props.choose(
            _COPY, "ubuntu_abc123", zark={}, cache={}, empty_with_children=False
        )
        assert (props.canmount, props.mountpoint, props.source) == ("noauto", "legacy", "ubuntu")


def _setup_steps(layout: str | None) -> list[tuple[int, int, str]]:
    """The steps ``zark setup`` announces on a system of ``layout``."""
    steps: list[tuple[int, int, str]] = []
    with (
        patch.dict(setup_mod.DEPS, clear=True),
        patch.object(setup_mod, "system_layout", return_value=layout),
        patch.object(setup_mod, "keep_boot"),
        patch.object(setup_mod, "_unused_boot_copies"),
        patch.object(setup_mod, "configure_sanoid", return_value=True),
        patch.object(setup_mod, "_initial_snapshots"),
        patch.object(setup_mod, "ensure_bookmark_v2"),
        patch.object(setup_mod, "_check_secure_boot_alternatives"),
        patch.object(setup_mod, "_install_guards"),
        patch.object(setup_mod.alerts, "install"),
        patch.object(setup_mod.certificates, "show_warnings"),
        patch("lib.log.Log.step", lambda _s, n, t, text: steps.append((n, t, text))),
        patch_sh(MockShell()),
        redirect_stdout(StringIO()),
    ):
        try:
            setup_mod.run([])
        except SystemExit:
            pass
    return steps


class TestSetupSteps:  # pylint: disable=missing-function-docstring
    """A bpool system's setup is as before; an ext4 one has one more step."""

    def test_a_bpool_system_has_the_six_steps_of_before(self):
        steps = _setup_steps("bpool")
        assert [(n, t) for n, t, _ in steps] == [(n, 6) for n in range(1, 7)]
        assert "Keeping /boot for backups..." not in [s for _, _, s in steps]

    def test_an_ext4_system_keeps_boot_before_sanoid(self):
        steps = _setup_steps("ext4")
        assert [(n, t) for n, t, _ in steps] == [(n, 7) for n in range(1, 8)]
        assert [s for _, _, s in steps][1:3] == [
            "Keeping /boot for backups...",
            "Configuring sanoid...",
        ]


def _refresh_shell(rsync_rc: int = 0, mount_ok: bool = True) -> MockShell:
    mock = MockShell()
    if mount_ok:
        mock.on_prefix(f"mount -t zfs {_COPY} /").succeeds("")
    else:
        mock.on_prefix(f"mount -t zfs {_COPY} /").returns(1, "", "no such dataset")
    mock.on_prefix("rsync -aHAX").returns(rsync_rc, "", "" if not rsync_rc else "vanished")
    mock.on_prefix("umount /").succeeds("")
    return mock


class TestBootRefresh:  # pylint: disable=missing-function-docstring
    """M12-2 2e, M12R-7: /boot copied into zark-boot before the point."""

    def test_the_copy_runs_rsync_into_the_mounted_dataset_and_unmounts_it(self):
        mock = _refresh_shell()
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert boot_layout.refresh_boot_copy("ubuntu_abc123", _log())
        calls = [c for c in mock._calls if c.startswith(("mount", "rsync", "umount"))]  # pylint: disable=protected-access
        assert len(calls) == 3
        tmp = calls[0].split()[-1]
        assert calls[0] == f"mount -t zfs {_COPY} {tmp}"
        assert calls[1] == (
            "rsync -aHAX --numeric-ids --delete --inplace --one-file-system "
            + f"--exclude=/efi/ /boot/ {tmp}/"
        )
        assert calls[2] == f"umount {tmp}"
        assert not Path(tmp).exists()

    def test_any_rsync_status_but_0_fails_and_still_unmounts(self):
        for rc in (23, 24, 1):
            mock = _refresh_shell(rsync_rc=rc)
            out = StringIO()
            with patch_sh(mock), redirect_stdout(out):
                assert not boot_layout.refresh_boot_copy("ubuntu_abc123", _log())
            assert mock.was_called("umount /")
            assert f"rsync status {rc}" in out.getvalue()

    def test_a_copy_that_cannot_be_mounted_fails_without_rsync(self):
        mock = _refresh_shell(mount_ok=False)
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert not boot_layout.refresh_boot_copy("ubuntu_abc123", _log())
        assert mock.was_not_called("rsync")

    def test_the_manifest_leaves_the_esp_out_and_sees_a_new_kernel(self):
        with tempfile.TemporaryDirectory() as boot:
            Path(boot, "efi", "EFI").mkdir(parents=True)
            Path(boot, "efi", "EFI", "shim").write_text("s", encoding="ascii")
            Path(boot, "vmlinuz-1").write_text("k", encoding="ascii")
            before = boot_layout.manifest(boot)
            assert [e[0] for e in before] == ["vmlinuz-1"]
            Path(boot, "efi", "EFI", "grub").write_text("g", encoding="ascii")
            assert not boot_layout.manifest_diff(before, boot_layout.manifest(boot))
            Path(boot, "vmlinuz-2").write_text("k", encoding="ascii")
            assert boot_layout.manifest_diff(before, boot_layout.manifest(boot)) == ["vmlinuz-2"]

    def test_a_boot_that_changed_after_the_copy_makes_the_point_stale(self):
        refresh = boot_layout.BootRefresh("ubuntu_abc123", _log())
        manifests = iter(
            [(("vmlinuz-1", 1, 1, 1),), (("vmlinuz-1", 1, 1, 1), ("vmlinuz-2", 1, 2, 2))]
        )
        with (
            patch.object(boot_layout, "manifest", side_effect=lambda *_a: next(manifests)),
            patch.object(boot_layout, "refresh_boot_copy", return_value=True),
        ):
            assert refresh.refresh()
            assert refresh.check() == (
                "/boot changed during the run (vmlinuz-2): the point is not marked complete"
            )

    def test_an_unchanged_boot_leaves_the_point_whole(self):
        refresh = boot_layout.BootRefresh("ubuntu_abc123", _log())
        with (
            patch.object(boot_layout, "manifest", return_value=(("vmlinuz-1", 1, 1, 1),)),
            patch.object(boot_layout, "refresh_boot_copy", return_value=True),
        ):
            assert refresh.refresh()
            assert not refresh.check()

    def test_a_stale_point_is_not_ok(self):
        res = engine.Result(plans=[repl.DatasetPlan("rpool", repl.State.AT_POINT)])
        assert res.ok
        res.stale = "/boot changed during the run (vmlinuz-2): the point is not marked complete"
        assert not res.ok
        lines, missing = backup_mod._incomplete_lines(res, "zark_p")  # pylint: disable=protected-access
        assert missing == 0 and lines[0] == res.stale

    def test_a_missing_copy_is_refused_before_anything_is_written(self):
        out = StringIO()
        with (
            patch_sh(_setup_shell()),
            patch("builtins.input", return_value=""),
            redirect_stdout(out),
        ):
            try:
                boot_layout.require_boot_copy(_log())
            except SystemExit:
                pass
            else:
                raise AssertionError("expected a refusal")
        assert f"This system has no {_COPY}" in out.getvalue()
        assert "Nothing was written" in out.getvalue()

    def test_an_existing_copy_gives_the_boot_environment(self):
        with patch_sh(_setup_shell(exists=True)):
            assert boot_layout.require_boot_copy(_log()) == "ubuntu_abc123"


def _execute(before: bool | None) -> tuple[list[str], engine.Result]:
    """engine.execute with the point, sanoid and the copy recorded in order."""
    order: list[str] = []

    def step(name: str, result: bool) -> Callable[..., bool]:
        def record(*_a: object) -> bool:
            order.append(name)
            return result

        return record

    run = engine.Run(
        "backup",
        "111",
        _log(),
        "zark_p",
        pools=["rpool"],
        before_point=None if before is None else step("copy", before),
    )
    with (
        patch.object(engine.Run, "pause_sanoid", step("pause", True)),
        patch.object(engine.Run, "resume_sanoid", step("resume", True)),
        patch.object(engine.Run, "take_point", step("point", False)),
        patch.object(engine.Run, "undo_point", lambda _s: None),
    ):
        res = engine.execute(run, MagicMock(), lambda _p, _r: True)
    return order, res


class TestEngineBeforePoint:  # pylint: disable=missing-function-docstring
    """The copy of /boot runs after sanoid is paused and before the point."""

    def test_the_copy_comes_between_the_pause_and_the_point(self):
        order, _ = _execute(True)
        assert order == ["pause", "copy", "point", "resume"]

    def test_a_failed_copy_takes_no_point(self):
        order, res = _execute(False)
        assert order == ["pause", "copy", "resume"]
        assert res.aborted and res.error == "could not copy /boot before the point"

    def test_without_a_copy_the_run_is_as_before(self):
        order, _ = _execute(None)
        assert order == ["pause", "point", "resume"]


def _survey(layout: str) -> str:
    """ "" or the ReadError of a survey that finds a bpool tree on the drive."""
    lay: boot_layout.Layout = "ext4" if layout == "ext4" else "bpool"
    run = engine.Run("backup", "111", _log(), "zark_p", pools=["rpool"], layout=lay)
    dest = {"bpool": MagicMock(), "bpool/BOOT/ubuntu_x": MagicMock()}
    with (
        patch.object(engine, "read_origin", return_value={}),
        patch.object(engine, "read_dest", return_value=dest),
        patch.object(engine, "plan", return_value=[]),
    ):
        try:
            _ = run.survey()
        except engine.ReadError as e:
            return str(e)
    return ""


class TestEngineLayoutChange:  # pylint: disable=missing-function-docstring
    """M12-4 4a: a drive with bpool's backup, given an ext4 system."""

    def test_an_ext4_system_keeps_the_bpool_tree(self):
        assert not _survey("ext4")

    def test_a_bpool_system_without_bpool_is_refused_as_before(self):
        assert "bpool is not imported, and backup holds its backup" in _survey("bpool")

    def test_backup_keeps_it_without_asking(self):
        eng = MagicMock()
        eng.layout = "ext4"
        plans = [repl.DatasetPlan("bpool/BOOT/ubuntu_x", repl.State.ORPHAN, used=5)]
        ask = MagicMock(side_effect=AssertionError("asked"))
        with patch.object(Log, "ask_choice", ask), redirect_stdout(StringIO()):
            assert backup_mod._decide(plans, eng, "backup", None, _log())  # pylint: disable=protected-access
        eng.keep_orphan.assert_called_once()
        assert eng.keep_orphan.call_args.args[0] is plans[0]

    def test_a_bpool_system_still_asks(self):
        eng = MagicMock()
        eng.layout = "bpool"
        eng.dest = {}
        plans = [repl.DatasetPlan("bpool/BOOT/ubuntu_x", repl.State.ORPHAN, used=5)]
        with (
            patch.object(Log, "ask_choice", return_value=0) as ask,
            patch.object(backup_mod, "destroy_plan", return_value=MagicMock(ok=False)),
            patch.object(backup_mod, "_warn_blockers"),
            redirect_stdout(StringIO()),
        ):
            assert backup_mod._decide(plans, eng, "backup", None, _log())  # pylint: disable=protected-access
        ask.assert_called_once()
        eng.keep_orphan.assert_not_called()


class TestBootCopyDiverged:  # pylint: disable=missing-function-docstring
    """M12R-16: a zark-boot recreated on origin; archive is the default."""

    def test_the_question_says_what_destroy_costs_and_defaults_to_archive(self):
        eng = MagicMock()
        eng.layout = "ext4"
        eng.estimate.return_value = 1
        plans = [repl.DatasetPlan(_COPY, repl.State.DIVERGED, used=5, note="no common snapshot")]
        defaults: list[int] = []

        def choice(_q: str, _opts: list[str], default: int = 0) -> int:
            defaults.append(default)
            return 0

        out = StringIO()
        with (
            patch.object(Log, "ask_choice", side_effect=choice),
            patch.object(backup_mod, "destroy_plan", return_value=MagicMock(ok=False)),
            patch.object(backup_mod, "_warn_blockers"),
            patch.object(backup_mod, "_on_drive", return_value=[_COPY]),
            patch.object(backup_mod, "_make_room", return_value=[]),
            redirect_stdout(out),
        ):
            dst = MagicMock(avail_bytes=10**12)
            _ = backup_mod._decide(plans, eng, "backup", dst, _log())  # pylint: disable=protected-access
        assert defaults == [1]
        assert "destroy removes it from every older point" in out.getvalue()

    @staticmethod
    def _defaults(rels: list[str], avail: int) -> list[int]:
        """The default of each DIVERGED question _decide asks for ``rels``."""
        eng = MagicMock()
        eng.layout = "ext4"
        eng.estimate.return_value = 1
        plans = [repl.DatasetPlan(r, repl.State.DIVERGED, used=5, note="x") for r in rels]
        defaults: list[int] = []

        def choice(_q: str, _opts: list[str], default: int = 0) -> int:
            defaults.append(default)
            return 0

        with (
            patch.object(Log, "ask_choice", side_effect=choice),
            patch.object(backup_mod, "destroy_plan", return_value=MagicMock(ok=False)),
            patch.object(backup_mod, "_warn_blockers"),
            patch.object(backup_mod, "_on_drive", side_effect=lambda _e, rel: [rel]),
            patch.object(backup_mod, "_make_room", return_value=[]),
            redirect_stdout(StringIO()),
        ):
            dst = MagicMock(avail_bytes=avail)
            _ = backup_mod._decide(plans, eng, "backup", dst, _log())  # pylint: disable=protected-access
        return defaults

    def test_archive_is_not_the_default_when_it_does_not_fit(self):
        """M13R-5 (M22b): without room the archive option is not offered,
        so the default stays on skip."""
        assert self._defaults([_COPY], 0) == [0]

    def test_the_grouped_question_defaults_to_archive_with_the_copy_in_it(self):
        """M13R-5 (M22c): zark-boot among several lineages: archive for all,
        when it fits; skip otherwise."""
        assert self._defaults([_COPY, "rpool/var"], 10**12) == [1]
        assert self._defaults([_COPY, "rpool/var"], 0) == [0]
        assert self._defaults(["rpool/srv", "rpool/var"], 10**12) == [0]

    def test_another_dataset_still_defaults_to_skip(self):
        eng = MagicMock()
        eng.estimate.return_value = 1
        plans = [repl.DatasetPlan("rpool/var", repl.State.DIVERGED, used=5, note="x")]
        defaults: list[int] = []

        def choice(_q: str, _opts: list[str], default: int = 0) -> int:
            defaults.append(default)
            return 0

        with (
            patch.object(Log, "ask_choice", side_effect=choice),
            patch.object(backup_mod, "destroy_plan", return_value=MagicMock(ok=False)),
            patch.object(backup_mod, "_warn_blockers"),
            patch.object(backup_mod, "_on_drive", return_value=["rpool/var"]),
            patch.object(backup_mod, "_make_room", return_value=[]),
            redirect_stdout(StringIO()),
        ):
            _ = backup_mod._decide(plans, eng, "backup", MagicMock(avail_bytes=10**12), _log())  # pylint: disable=protected-access
        assert defaults == [0]


_DAY = 86400
_NOW = 1793664000  # 2026-11-03T00:00:00Z


def _pt(days_ago: float) -> str:
    return repl.point_name(datetime.fromtimestamp(_NOW - days_ago * _DAY, UTC))


def _dest(rel: str, ages: list[float], orphan: str = "") -> repl.Dest:
    snaps = [
        repl.Ref(_pt(a), f"{rel}-{a}", i + 1, int(_NOW - a * _DAY))
        for i, a in enumerate(sorted(ages, reverse=True))
    ]
    return repl.Dest(rel, orphan=orphan, snaps=snaps)


class TestBootCopyRetention:  # pylint: disable=missing-function-docstring
    """M12-2 2e, M12R-5: zark-boot and a kept bpool tree prune with their
    points; nothing new in the code."""

    def test_zark_boot_goes_and_stays_with_its_point(self):
        be = "rpool/ROOT/ubuntu_abc123"
        ages = [45.0, 44.0, 1.0]  # two in the same month outside the window
        dest = {be: _dest(be, ages), _COPY: _dest(_COPY, ages)}
        complete = {_pt(a) for a in ages}
        out = retention.plan_prune(dest, complete, retention.Policy(window_days=30), _NOW)
        gone = {r.target for r in out.snapshots}
        older, monthly = _pt(45.0), _pt(44.0)
        assert f"{be}@{older}" in gone and f"{_COPY}@{older}" in gone
        assert f"{_COPY}@{monthly}" not in gone and f"{_COPY}@{_pt(1.0)}" not in gone
        assert not out.datasets

    def test_a_kept_bpool_tree_with_every_point_too_old_is_destroyed_whole(self):
        be = "rpool/ROOT/ubuntu_abc123"
        tree = "bpool/BOOT/ubuntu_old"
        dest = {be: _dest(be, [1.0]), tree: _dest(tree, [95.0, 94.0], orphan="kept@2026-11-02")}
        complete = {_pt(1.0), _pt(95.0), _pt(94.0)}
        policy = retention.Policy(window_days=30, max_age_days=60)
        out = retention.plan_prune(dest, complete, policy, _NOW)
        assert {r.target for r in out.snapshots} == {f"{tree}@{_pt(95.0)}", f"{tree}@{_pt(94.0)}"}
        assert [top for top, _ in out.datasets] == [tree]

    def test_a_kept_bpool_tree_with_a_point_in_the_window_stays(self):
        be = "rpool/ROOT/ubuntu_abc123"
        tree = "bpool/BOOT/ubuntu_old"
        dest = {be: _dest(be, [1.0]), tree: _dest(tree, [95.0, 5.0], orphan="kept@2026-11-02")}
        complete = {_pt(1.0), _pt(95.0), _pt(5.0)}
        policy = retention.Policy(window_days=30, max_age_days=60)
        out = retention.plan_prune(dest, complete, policy, _NOW)
        assert {r.target for r in out.snapshots} == {f"{tree}@{_pt(95.0)}"}
        assert not out.datasets

    def test_origin_prune_offers_zark_boots_old_sanoid_snapshots(self):
        old = int(_NOW - 40 * _DAY)
        snap = repl.Ref("autosnap_2026-09-24_00:00:01_daily", "g", 1, old)
        origin = {_COPY: repl.Origin(_COPY, "filesystem", "noauto", "legacy", snaps=[snap])}
        inv = origin_prune.inventory(origin, {})
        targets = [i.target for i in origin_prune.older_than(inv, _NOW - 30 * _DAY)]
        assert targets == [f"{_COPY}@autosnap_2026-09-24_00:00:01_daily"]


def _plan(rows: dict[str, bool]) -> MagicMock:
    """A restore plan whose rows are ``rel -> restored``."""
    plan = MagicMock()
    plan.be = "ubuntu_abc123"
    plan.pool = "backup"
    plan.keystore_snap = "backup/keystore@p"
    plan.row.side_effect = lambda rel: MagicMock(restored=rows[rel]) if rel in rows else None
    return plan


def _preflight(
    plan: MagicMock, boot: pool_record.BootRecord | None, missing: str = "", layout: str = ""
) -> str:
    mock = MockShell()
    mock.on("findmnt -rn -o TARGET").succeeds("/\n")
    mock.on("zpool list rpool").fails("no such pool")
    mock.on("zpool list bpool").fails("no such pool")
    for tool in [*recover_mod.RECOVER_TOOLS, *recover_ext4.EXT4_RECOVER_TOOLS]:
        if tool == missing:
            mock.on(f"which {tool}").fails("")
        else:
            mock.on(f"which {tool}").succeeds(f"/usr/bin/{tool}")
    mock.on("zfs list backup/keystore").succeeds("backup/keystore")
    out = StringIO()
    with (
        patch_sh(mock),
        patch.object(recover_mod, "_check_sizes"),
        patch("builtins.input", return_value=""),
        redirect_stdout(out),
    ):
        try:
            recover_mod._preflight(  # pylint: disable=protected-access
                plan,
                "/dev/sda",
                _log(),
                boot,
                layout or ("ext4" if boot is not None else "bpool"),  # type: ignore[arg-type]
            )
        except SystemExit:
            return "REFUSED\n" + out.getvalue()
    return out.getvalue()


_BE_ROOT = "rpool/ROOT/ubuntu_abc123"


class TestRecoverExt4Layout:  # pylint: disable=missing-function-docstring
    """M12-5 5a, M12R-6, M12R-8, M13F-5: an ext4 point's preflight and disk."""

    def test_the_disk_is_laid_out_as_origin_had_it(self):
        parts = recover_ext4.ext4_partitions(_boot_record())
        assert [(p.number, p.size, p.type_code, p.role) for p in parts] == [
            (1, 2201600 * 512, "EF00", "esp"),
            (2, 2 * 1024**3, "8300", "boot"),
            (3, 4 * 1024**3, "8300", "swap"),
            (4, 0, "BF00", "rpool"),
        ]
        assert recover_ext4.sgdisk_commands(parts, "/dev/sda") == [
            "sgdisk -n1:1M:+1100800K -t1:EF00 /dev/sda",
            "sgdisk -n2:0:+2097152K -t2:8300 /dev/sda",
            "sgdisk -n3:0:+4194304K -t3:8300 /dev/sda",
            "sgdisk -n4:0:0 -t4:BF00 /dev/sda",
        ]

    def test_without_swap_or_esp_record_rpool_is_the_third(self):
        small = _boot_record(
            esp=None, swap=None, partition=pool_record.Partition(512 * 1024**2, _LINUX_FS)
        )
        parts = recover_ext4.ext4_partitions(small)
        assert [(p.number, p.size, p.role) for p in parts] == [
            (1, 1024**3, "esp"),
            (2, 2 * 1024**3, "boot"),
            (3, 0, "rpool"),
        ]
        before = recover_mod._before_rpool_bytes(small)  # pylint: disable=protected-access
        assert before == 2 * 1024**2 + 3 * 1024**3

    def test_the_esp_keeps_its_recorded_size_within_bounds(self):
        """N3: at least 1 GiB, at most 4 GiB, whatever /boot/efi was mounted from."""

        def esp(size: int) -> int:
            return recover_ext4.esp_bytes(_boot_record(esp=pool_record.Esp(size, "1A2B-3C4D")))

        assert esp(2201600 * 512) == 2201600 * 512
        assert esp(512 * 1024**2) == 1024**3
        assert esp(500 * 1024**3) == 4 * 1024**3
        assert recover_ext4.esp_bytes(_boot_record(esp=None)) == 1024**3

    def test_the_new_boot_has_exactly_the_recorded_features_and_a_new_uuid(self):
        cmd = recover_ext4.mkfs_boot_command(_boot_record(), "/dev/sda2")
        assert cmd == ("mkfs.ext4 -F -O none," + ",".join(pool_record.EXT4_FEATURES) + " /dev/sda2")
        assert " -U " not in cmd
        assert "-L boot " in recover_ext4.mkfs_boot_command(_boot_record(label="boot"), "/dev/x")

    def test_an_ext4_point_without_its_boot_copy_is_refused(self):
        out = _preflight(_plan({_BE_ROOT: True}), _boot_record())
        assert out.startswith("REFUSED")
        assert f"This point has no copy of /boot ({_BE_ROOT}/zark-boot)" in out
        assert "Nothing was erased" in out

    def test_an_ext4_point_whose_copy_is_not_restored_is_refused(self):
        out = _preflight(_plan({_BE_ROOT: True, _COPY: False}), _boot_record())
        assert out.startswith("REFUSED")

    def test_an_ext4_point_with_its_copy_passes_without_the_bpool_warning(self):
        out = _preflight(_plan({_BE_ROOT: True, _COPY: True}), _boot_record())
        assert not out.startswith("REFUSED")
        assert "No bpool snapshot" not in out

    def test_an_ext4_point_needs_rsync_and_e2fsprogs(self):
        out = _preflight(_plan({_BE_ROOT: True, _COPY: True}), _boot_record(), missing="rsync")
        assert out.startswith("REFUSED") and "Missing tools in the live session: rsync" in out

    def test_an_ext4_point_without_its_record_is_refused_here_too(self):
        """M13R-1: the preflight takes the layout from the drive too."""
        out = _preflight(_plan({_BE_ROOT: True, _COPY: True}), None, layout="ext4")
        assert out.startswith("REFUSED") and "has no record of origin's disk" in out

    def test_a_bpool_point_needs_none_of_them(self):
        out = _preflight(_plan({_BE_ROOT: True}), None, missing="rsync")
        assert not out.startswith("REFUSED")
        assert "No bpool snapshot at this point" in out

    def test_an_ext4_points_pools_are_rpool_alone(self):
        rec = _record(
            pools={"rpool": pool_record.PoolState("off", {"encryption": "active"})},
            boot=_boot_record(),
            min_recover="2.2.0",
        )
        mock = MockShell()
        mock.on("zpool upgrade -v").succeeds("")
        with (
            patch_sh(mock),
            patch.object(pool_record, "read", return_value=rec),
            patch.object(recover_mod, "refuse_unrecoverable"),
            patch.object(recover_mod.zfs_features, "parse_upgrade_v", return_value=["encryption"]),
            redirect_stdout(StringIO()),
        ):
            specs, layout, got = recover_mod._pools_from_record(  # pylint: disable=protected-access
                "backup", "p", [], "ubuntu_abc123", {}, _log()
            )
        assert set(specs) == {"rpool"} and layout == "ext4" and got is rec

    def test_an_ext4_point_on_a_live_lacking_an_active_feature_is_refused(self):
        rec = _record(
            pools={
                "rpool": pool_record.PoolState(
                    "off", {"encryption": "active", "block_cloning_endian": "active"}
                )
            },
            boot=_boot_record(),
            min_recover="2.2.0",
        )
        out = StringIO()
        with (
            patch_sh(MockShell()),
            patch.object(pool_record, "read", return_value=rec),
            patch.object(recover_mod, "refuse_unrecoverable"),
            patch.object(recover_mod.zfs_features, "parse_upgrade_v", return_value=["encryption"]),
            patch("builtins.input", return_value=""),
            redirect_stdout(out),
        ):
            try:
                recover_mod._pools_from_record("backup", "p", [], "ubuntu_abc123", {}, _log())  # pylint: disable=protected-access
            except SystemExit:
                pass
            else:
                raise AssertionError("expected a refusal")
        assert "This live's ZFS lacks features origin's pools had active" in out.getvalue()
        assert "Nothing was erased" in out.getvalue()


_NEW_UUID = "11111111-2222-4333-8444-555555555555"
_NEW_PARTUUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    return [
        (a, b)
        for a, b in zip(before.splitlines(True), after.splitlines(True), strict=True)
        if a != b
    ]


class TestBootRewrite:  # pylint: disable=missing-function-docstring
    """M12-3 3a, BOOT-EXT4-8: fstab and crypttab moved to the new /boot and swap."""

    def test_fstab_changes_only_the_boot_line(self):
        text = (_FIXTURES / "fstab").read_text(encoding="ascii")
        new, n = boot_rewrite.rewrite_fstab(text, _UUID, _NEW_UUID)
        assert n == 1 and len(new.splitlines()) == len(text.splitlines())
        assert _changed_lines(text, new) == [
            (
                f"/dev/disk/by-uuid/{_UUID} /boot ext4 defaults 0 1\n",
                f"/dev/disk/by-uuid/{_NEW_UUID} /boot ext4 defaults 0 1\n",
            )
        ]

    def test_fstab_uuid_form_and_case(self):
        text = f"UUID={_UUID.upper()}\t/boot\text4\tdefaults\t0\t1\n"
        new, n = boot_rewrite.rewrite_fstab(text, _UUID, _NEW_UUID)
        assert (n, new) == (1, f"UUID={_NEW_UUID}\t/boot\text4\tdefaults\t0\t1\n")

    def test_fstab_without_the_old_uuid_changes_nothing(self):
        text = (_FIXTURES / "fstab").read_text(encoding="ascii")
        assert boot_rewrite.rewrite_fstab(text, _NEW_UUID, _UUID) == (text, 0)

    def test_crypttab_changes_only_the_swap_partuuid(self):
        text = (_FIXTURES / "crypttab").read_text(encoding="ascii") + "# a comment\n"
        new, n = boot_rewrite.rewrite_crypttab(text, _NEW_PARTUUID)
        assert n == 1
        assert new == text.replace(_SWAP_PARTUUID, _NEW_PARTUUID)

    def test_crypttab_without_a_swap_changes_nothing(self):
        text = "keystore-rpool /dev/zvol/rpool/keystore none luks\n"
        assert boot_rewrite.rewrite_crypttab(text, _NEW_PARTUUID) == (text, 0)


def _copy_shell(rsync_rc: int = 0, mount_ok: bool = True) -> MockShell:
    mock = MockShell()
    if mount_ok:
        mock.on_prefix(f"mount -t zfs -o ro {_COPY} /").succeeds("")
    else:
        mock.on_prefix(f"mount -t zfs -o ro {_COPY} /").returns(1, "", "no key")
    mock.on_prefix("rsync -aHAX --numeric-ids").returns(
        rsync_rc, "", "vanished" if rsync_rc else ""
    )
    mock.on_prefix("umount /").succeeds("")
    return mock


class TestRecoverBootCopyBack:  # pylint: disable=missing-function-docstring
    """M12R-6, M12R-12: step 13 for an ext4 point."""

    def test_the_copy_is_mounted_read_only_and_rsynced_into_the_new_boot(self):
        mock = _copy_shell()
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert not boot_chain.restore_boot_copy(_COPY, _log())
        calls = [c for c in mock._calls if c.startswith(("mount", "rsync", "umount"))]  # pylint: disable=protected-access
        tmp = calls[0].split()[-1]
        assert calls == [
            f"mount -t zfs -o ro {_COPY} {tmp}",
            f"rsync -aHAX --numeric-ids {tmp}/ {boot_chain.RECOVER_MNT}/boot/",
            f"umount {tmp}",
        ]
        assert not Path(tmp).exists()

    def test_a_failed_copy_is_an_error_of_the_recover(self):
        mock = _copy_shell(rsync_rc=23)
        with patch_sh(mock), redirect_stdout(StringIO()):
            errors = boot_chain.restore_boot_copy(_COPY, _log())
        assert errors == [f"/boot: copying {_COPY} failed (rsync status 23): vanished"]
        assert mock.was_called("umount /")

    def test_a_copy_that_cannot_be_mounted_is_an_error(self):
        mock = _copy_shell(mount_ok=False)
        with patch_sh(mock), redirect_stdout(StringIO()):
            errors = boot_chain.restore_boot_copy(_COPY, _log())
        assert errors and "cannot mount" in errors[0] and mock.was_not_called("rsync")

    def test_the_new_boot_is_mounted_after_the_root_and_before_the_rest(self):
        mock = MockShell()
        mock.on_prefix("zfs mount").succeeds("")
        mock.on_prefix("mount -t ext4").succeeds("")
        mock.on_prefix("mkdir -p").succeeds("")
        with (
            patch_sh(mock),
            patch.object(Path, "exists", return_value=True),
            redirect_stdout(StringIO()),
        ):
            boot_chain.mount_restored_system("ubuntu_abc123", False, _log(), "/dev/sda2")
        calls = [c for c in mock._calls if "mount" in c and not c.startswith("zfs mount |")]  # pylint: disable=protected-access
        assert calls[:3] == [
            "zfs mount rpool/ROOT/ubuntu_abc123",
            f"mount -t ext4 /dev/sda2 {boot_chain.RECOVER_MNT}/boot",
            "zfs mount -a",
        ]

    def test_kernel_gaps_name_the_copy_and_its_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "boot").mkdir()
            mods = Path(root, "lib", "modules", "7.3.0-11-generic")
            mods.mkdir(parents=True)
            (mods / "modules.dep").write_text("", encoding="ascii")
            gaps = recover_mod.kernel_gaps(root, "zark_2026-11-01_10:00:00Z", "zark-boot")
        assert gaps[0].startswith(
            "kernel 7.3.0-11-generic: in the restored root but not in /boot "
            + "(zark-boot at @zark_2026-11-01_10:00:00Z)"
        )

    def test_fstab_and_crypttab_of_the_restored_system(self):
        with _etc() as root:
            etc = Path(root, "etc")
            mock = MockShell()
            mock.on("blkid -s PARTUUID -o value /dev/sda3").succeeds(_NEW_PARTUUID + "\n")
            with patch_sh(mock), redirect_stdout(StringIO()):
                assert not recover_ext4.ext4_fstab(etc / "fstab", _UUID, _NEW_UUID, _log())  # pylint: disable=protected-access
                assert not recover_ext4.ext4_crypttab(
                    etc / "crypttab", _boot_record(), "/dev/sda", _log()
                )  # pylint: disable=protected-access
            assert f"/dev/disk/by-uuid/{_NEW_UUID} /boot" in (etc / "fstab").read_text(
                encoding="ascii"
            )
            assert f"PARTUUID={_NEW_PARTUUID} " in (etc / "crypttab").read_text(encoding="ascii")

    def test_no_swap_recorded_leaves_crypttab_alone(self):
        with _etc() as root:
            path = Path(root, "etc", "crypttab")
            before = path.read_text(encoding="ascii")
            with patch_sh(MockShell()):
                assert not recover_ext4.ext4_crypttab(
                    path, _boot_record(swap=None), "/dev/sda", _log()
                )  # pylint: disable=protected-access
            assert path.read_text(encoding="ascii") == before

    def test_an_fstab_without_the_old_uuid_is_an_error(self):
        with _etc() as root:
            fstab = Path(root, "etc", "fstab")
            with redirect_stdout(StringIO()):
                errors = recover_ext4.ext4_fstab(fstab, _NEW_UUID, _UUID, _log())  # pylint: disable=protected-access
        assert errors and "no /boot line names" in errors[0]


class TestRecoverEsp:  # pylint: disable=missing-function-docstring
    """N4: the ESP's mount point is made, and checked, before the mount."""

    @staticmethod
    def _esp(mkdir_ok: bool = True, mount_ok: bool = True) -> tuple[MockShell, str, bool]:
        mock = MockShell()
        mock.on("mkfs.vfat -F32 /dev/sda1").succeeds("")
        mkdir = mock.on(f"mkdir -p {recover_mod.RECOVER_MNT}/boot/efi")
        _ = mkdir.succeeds("") if mkdir_ok else mkdir.fails("read-only")
        mount = mock.on(f"mount /dev/sda1 {recover_mod.RECOVER_MNT}/boot/efi")
        _ = mount.succeeds("") if mount_ok else mount.fails("busy")
        out = StringIO()
        cleanup = MagicMock()
        with patch_sh(mock), redirect_stdout(out), patch("builtins.input", return_value=""):
            try:
                recover_mod._mount_esp("/dev/sda", cleanup, _log())  # pylint: disable=protected-access
            except SystemExit:
                return mock, out.getvalue(), False
        return mock, out.getvalue(), cleanup.track_mount.called

    def test_the_directory_comes_before_the_mount(self):
        mock, _, tracked = self._esp()
        calls = mock.calls
        mkdir = calls.index(f"mkdir -p {recover_mod.RECOVER_MNT}/boot/efi")
        assert mkdir < calls.index(f"mount /dev/sda1 {recover_mod.RECOVER_MNT}/boot/efi")
        assert tracked

    def test_a_failed_mkdir_or_mount_stops_the_recover(self):
        mock, out, _ = self._esp(mkdir_ok=False)
        assert "Cannot create" in out and mock.was_not_called("mount /dev/sda1")
        _, out, _ = self._esp(mount_ok=False)
        assert "Cannot mount the new ESP" in out
