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

import tempfile
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import commands.backup as backup_mod
import commands.prepare as prepare_mod
from lib import boot_layout, survey
from lib.log import Log
from tests.mock_sh import MockShell, patch_sh

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
