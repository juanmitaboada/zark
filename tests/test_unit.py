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
Unit tests for zark - all modules and command logic.

Run:  python3 -m pytest tests/test_unit.py -v
  or: python3 tests/test_unit.py

Tests run WITHOUT root, ZFS, or real disks. All shell commands are mocked.
"""

# pylint: disable=too-many-lines
# Rationale: this is the single-file unit suite by design. A `main()` runner
# at the bottom of this file lets developers run tests without pytest, and
# splitting per-class would scatter that contract. The agreement is that
# only test code lives here - production modules stay under the standard
# line limit.

import ast
import atexit
import fcntl
import importlib.machinery
import importlib.util
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import (
    AbstractContextManager,
    ExitStack,
    contextmanager,
    nullcontext,
    redirect_stderr,
    redirect_stdout,
)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

from unittest import SkipTest  # pylint: disable=wrong-import-position # noqa: E402
from unittest.mock import (  # pylint: disable=wrong-import-position # noqa: E402
    ANY,
    MagicMock,
    patch,
)

import commands.backup as backup_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.chroot as chroot_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.clean as clean_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.finish as finish_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.fix_rpool_mountpoint as fix_rpool_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.health as health_cmd  # pylint: disable=wrong-import-position # noqa: E402
import commands.monitor as monitor_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.mount as mount_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.prepare as prepare_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.prune as prune_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.purge as purge_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.recover as recover_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.registry as registry_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.repair_boot as repair_boot_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.selftest as selftest_cmd  # pylint: disable=wrong-import-position # noqa: E402
import commands.setup as setup_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.status as status_mod  # pylint: disable=wrong-import-position # noqa: E402
import commands.umount as umount_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.alerts as alerts_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.certificates as certs  # pylint: disable=wrong-import-position # noqa: E402
import lib.drives as drives_lib  # pylint: disable=wrong-import-position # noqa: E402
import lib.grub_cfg as grub_cfg_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.health as health_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.initrd as initrd_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.live as live_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.lock as lock_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.mount as lib_mount  # pylint: disable=wrong-import-position # noqa: E402
import lib.notify as notify_mod  # pylint: disable=wrong-import-position # noqa: E402
import lib.origin_prune as oprune  # pylint: disable=wrong-import-position # noqa: E402
import lib.platform_facts as pfacts  # pylint: disable=wrong-import-position # noqa: E402
import lib.replication as repl  # pylint: disable=wrong-import-position # noqa: E402
import lib.retention as ret  # pylint: disable=wrong-import-position # noqa: E402
import lib.selftest as stest  # pylint: disable=wrong-import-position # noqa: E402
import lib.selftest_l2 as st_l2  # pylint: disable=wrong-import-position # noqa: E402
import lib.sh as _sh  # pylint: disable=wrong-import-position # noqa: E402
import lib.signals as _signals  # pylint: disable=wrong-import-position # noqa: E402
import lib.status as status_lib  # pylint: disable=wrong-import-position # noqa: E402
import lib.zfs_features as zfeat  # pylint: disable=wrong-import-position # noqa: E402
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
    _abort_missing_keystore,
    _check_sizes,
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
    anchors,
    apt_guard,
    backup_layout,
    boot_chain,
    drive_marks,
    engine,
    forensics,
    grub_guard,
    reclaim,
    restore_plan,
    survey,
)
from lib.boot_chain import (  # pylint: disable=wrong-import-position # noqa: E402
    _force_latest_signed_alternative,
    mount_restored_system,
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
    zark_command,
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
    TestResult as HealthResult,
    _check_fua,
    _check_transport,
    _transport_errors_since,
    check_device,
    estimate_seconds,
    generate_report,
    pool_findings,
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
    SYSTEM_KEY_PATH,
    Keystore,
    open_keystore,
)
from lib.log import Log, frame, text_width, wrap  # pylint: disable=wrong-import-position # noqa: E402
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
from lib.restore_plan import (  # pylint: disable=wrong-import-position # noqa: E402
    RestorePlan,
    RestoreRow,
    _absent_note,
    build_plan,
    choose_point,
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
from lib.selftest import (  # pylint: disable=wrong-import-position # noqa: E402
    all_checks as all_selftest_checks,
)
from lib.sh import RunResult, part, run  # pylint: disable=wrong-import-position # noqa: E402
from lib.zfs import (  # pylint: disable=wrong-import-position # noqa: E402
    ZFS,
    DatasetInfo,
    KeyStatusError,
    PoolInfo,
    Readback,
    create_command as zfs_create_command,
    drive_compatibility as zfs_drive_compatibility,
    fix_grub_bpool_uuid,
    parse_damage,
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


# The frames' width follows the terminal (M4d-21): the same on any terminal here.
os.environ["COLUMNS"] = "80"


def exists_only(*present: str, absent: Sequence[str] = ()) -> Any:
    """Patch ``Path.exists`` per path (M4d-31): True for ``present``, False for
    ``absent`` (an entry ending in "/" is a prefix), the real answer for any
    other path. Any patch of ``<module>.Path.exists`` replaces the one class
    attribute, for every Path in the process (CPython 3.14.4
    Lib/pathlib/__init__.py:663-672), the registry's included."""
    real = Path.exists

    def match(path: str, entries: Sequence[str]) -> bool:
        return any(path == e or (e.endswith("/") and path.startswith(e)) for e in entries)

    def exists(self: Path, *args: Any, **kwargs: Any) -> bool:
        if match(str(self), present):
            return True
        if match(str(self), absent):
            return False
        return real(self, *args, **kwargs)

    return patch.object(Path, "exists", autospec=True, side_effect=exists)


def make_log() -> Log:
    """Create a Log that writes to /dev/null."""
    return Log(log_file="/dev/null")


_EDGE = r"(?:\x1b\[[0-9;]*m)*[║│](?:\x1b\[[0-9;]*m)*"
_FRAME_ROW = re.compile(rf"^ *{_EDGE} ?(.*?)(?:\x1b\[0m)? *(?:{_EDGE})?$")


def unframed(text: str) -> str:
    """``text`` with each frame's rows joined into one line, borders gone, so
    a phrase the frame wrapped reads whole (M4d-21)."""
    out: list[str] = []
    rows: list[str] = []
    for line in [*text.splitlines(), ""]:
        m = _FRAME_ROW.match(line)
        if m:
            rows.append(m[1].strip())
            continue
        if rows:
            out.append(" ".join(r for r in rows if r))
            rows = []
        out.append(line)
    return "\n".join(out)


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
# Disk sizes come from sysfs; the machine running the tests must not decide
# whether a mocked /dev/sdb has a medium. The empty directory reads as "unknown".
_SYSFS_ROOT = patch("lib.identity.SYS_BLOCK", _TEST_LOG_DIR, create=True).start()
# Nor its machine-id whether a drive is this system's (M4a-7): the drives
# _fresh_fake prepares name this one as their owner, as prepare does.
TEST_MACHINE_ID = "7e57000000000000000000000000beef"
Path(_TEST_LOG_DIR, "machine-id").write_text(TEST_MACHINE_ID + "\n", encoding="ascii")
_MACHINE_ID = patch(
    "lib.drive_marks.MACHINE_ID_PATH", os.path.join(_TEST_LOG_DIR, "machine-id")
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
                exists_only(
                    absent=("/etc/zark/known_drives.json", "/usr/share/zark/etc/known_drives.json")
                ),
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
                exists_only(
                    absent=("/etc/zark/known_drives.json", "/home/user/zark/etc/known_drives.json")
                ),
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


class TestFrame:  # pylint: disable=missing-function-docstring
    """M4d-21 (I18, M3P.7): one frame for the banners and the question boxes."""

    @staticmethod
    def _plain(lines: list[str]) -> list[str]:
        return [Log._STRIP_RE.sub("", ln) for ln in lines]  # pylint: disable=protected-access

    def test_text_width_counts_columns(self):
        assert text_width(f"{Log.G}✅ done{Log.N}") == 7  # the emoji is 2 columns
        assert text_width("✓ ✗ ⚠ • →") == 9
        assert text_width("e\u0301") == 1  # a combining accent takes none

    def test_a_banner_is_at_least_as_wide_as_before_and_its_edges_line_up(self):
        lines = self._plain(
            frame(["short"], heavy=True, min_inner=58, color=Log.G, head=["✅ T"], columns=200)
        )
        assert lines[0] == "╔" + "═" * 58 + "╗" and lines[2] == "╠" + "═" * 58 + "╣"
        assert {text_width(ln) for ln in lines} == {60}, lines

    def test_it_grows_to_the_terminal_then_wraps_with_the_bullets_indent(self):
        body = ["  • " + "word " * 30 + "end"]
        lines = self._plain(frame(body, heavy=True, min_inner=58, color="", columns=80))
        assert {text_width(ln) for ln in lines} == {80}, lines
        assert lines[1].startswith("║    • word") and lines[2].startswith("║      word"), lines
        assert lines[-2].rstrip(" ║").endswith("end"), lines
        wide = frame(["x" * 70], heavy=True, min_inner=58, color="", columns=200)
        assert text_width(wide[0]) == 75  # the widest line and its margins

    def test_a_token_wider_than_the_frame_stays_whole(self):
        path = "/dev/disk/by-id/usb-" + "X" * 90 + "-0:0-part1"
        lines = self._plain(
            frame([f"Drive: {path}"], heavy=True, min_inner=58, color="", columns=80)
        )
        assert lines[1].startswith("║  Drive:") and lines[2] == f"║  {path}", lines

    def test_a_colour_goes_on_after_a_break(self):
        got = wrap(f"{Log.Y}" + "warn " * 20 + f"{Log.N}", 40)
        assert len(got) > 1 and got[0].endswith(Log.N) and got[1].startswith(Log.Y), got

    def test_the_question_box_has_its_right_edge(self):
        out = StringIO()
        with redirect_stdout(out), patch("builtins.input", return_value="y"):
            assert make_log().ask("Go on?")
        box = [ln for ln in self._plain(out.getvalue().splitlines()) if ln.strip()][:3]
        assert box[0] == "  ┌" + "─" * 53 + "┐" and box[1].endswith("│"), box
        assert {text_width(ln) for ln in box} == {57}, box

    def test_a_box_with_an_indent_fits_a_narrow_terminal(self):
        # M4dR-9: the room is the terminal's columns less the indent and the
        # two edges, so the whole row (indent included) is within them.
        body = ["word " * 40]
        light = self._plain(frame(body, heavy=False, min_inner=53, color="", columns=60))
        assert {text_width(ln) for ln in light} == {60}, light
        heavy = self._plain(frame(body, heavy=True, min_inner=58, color="", columns=70))
        assert {text_width(ln) for ln in heavy} == {70}, heavy

    def test_a_full_width_line_does_not_touch_the_border(self):
        # M5a-24 (eli B2, K1): a banner's line of exactly the inner width had
        # no space before the right edge, as a question box always has one.
        for width in (53, 54, 55, 56):
            lines = self._plain(
                frame(["x" * width], heavy=True, min_inner=58, color="", columns=80)
            )
            assert lines[1].endswith(" ║"), lines
            assert len({text_width(ln) for ln in lines}) == 1, lines

    def test_a_line_with_a_newline_keeps_the_right_edge(self):
        # M4dR-2: chroot, mount and umount refuse with a body line that holds
        # a "\n"; it printed as one row and the edge landed after the last piece.
        body = ["rpool is already imported.\n  If this is your running system, do not chroot it."]
        for heavy in (True, False):
            lines = self._plain(frame(body, heavy=heavy, min_inner=58, color="", columns=80))
            assert len(lines) == 4, lines  # top, two rows, bottom
            assert len({text_width(ln) for ln in lines}) == 1, lines
            assert lines[1].rstrip().endswith(("║", "│")) and lines[2].rstrip().endswith(("║", "│"))
            assert lines[2].lstrip(" ║│").startswith("If this is your running system"), lines
        head_lines = self._plain(
            frame(["x"], heavy=True, min_inner=58, color="", head=["T\nU"], columns=80)
        )
        assert len({text_width(ln) for ln in head_lines}) == 1, head_lines

    def test_below_the_minimum_width_the_minimum_wins(self):
        # M4dR-9: a terminal narrower than the box's minimum gets the minimum
        # (the terminal wraps the row); recorded, not changed.
        lines = self._plain(frame(["short"], heavy=False, min_inner=53, color="", columns=40))
        assert {text_width(ln) for ln in lines} == {57}, lines


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

        streams: list[object] = []

        def prompt(_text: str, stream: object = None) -> str:
            seen.append(out.shown)
            streams.append(stream)
            return "secret"

        with patch("lib.log.getpass.getpass", side_effect=prompt), redirect_stdout(out):
            assert make_log().ask_password("Passphrase for backup") == "secret"
        assert "Passphrase for backup" in seen[0]
        assert streams == [out]  # M2V-6: the prompt follows the box through stdout

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
        # Distinct from the safe-to-unplug banner - operators must not
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

    def test_the_child_is_named_by_executable_and_subcommand(self):
        # M4bF-7 7a: what the interruption line says it is stopping.
        name = _signals.child_name
        assert name("zfs send -w -I 'rpool/x@a' 'rpool/x@b'") == "zfs send"
        assert name("zpool export backup") == "zpool export"
        assert name("sh -c 'echo $$ > p; exec sleep 60'") == "sh"
        assert name("dd if=/dev/zero of=/dev/sdx bs=1M") == "dd"
        assert name("chroot /mnt/zark/chroot /bin/bash --login") == "chroot"
        assert name("") == ""

    def test_a_child_reads_dev_null_unless_input_is_given(self):
        # M4b-1 (mutation H3): a child in its own process group that read the
        # terminal would stop; stdin is /dev/null, a pipe only with `input`.
        r_pipe, w_pipe = os.pipe()
        saved = os.dup(0)
        try:
            os.dup2(r_pipe, 0)  # a stdin a child could inherit, not /dev/null
            inherited = run("readlink /proc/self/fd/0")
            fed = run("cat", input="from zark")
        finally:
            os.dup2(saved, 0)
            for fd in (saved, r_pipe, w_pipe):
                os.close(fd)
        assert inherited.output == "/dev/null", inherited
        assert fed.output == "from zark", fed


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
        # the caller usually cares about - it's the more direct symptom.
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
        # 10 GiB - typical "1% of 1 TB" backup margin from our preventive
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

    @staticmethod
    def _zvols(mock: MockShell, names: dict[str, str]) -> None:
        mock.on("ls -1 /dev/zd*").succeeds("\n".join(names))
        for dev, name in names.items():
            mock.on(f"/usr/lib/udev/zvol_id {dev}", regex=False).succeeds(name)
        mock.on_prefix("readlink -f /dev/zvol/").fails()

    def test_find_zvol_by_name_not_by_order(self):
        """H11: zd112 sorts before zd16 as text; the backup's keystore is zd16."""
        mock = MockShell()
        self._zvols(
            mock,
            {
                "/dev/zd0": "rpool/keystore",
                "/dev/zd112": "rpool/vm",
                "/dev/zd16": "backup/keystore",
            },
        )
        mock.on("zpool list rpool").succeeds()
        with patch_sh(mock):
            assert Keystore(make_log()).find_zvol_for_pool("backup") == "/dev/zd16"

    def test_find_zvol_skips_partitions(self):
        """H11: a partition node of another zvol shifted the old index."""
        mock = MockShell()
        mock.on("ls -1 /dev/zd*").succeeds("/dev/zd0\n/dev/zd0p1\n/dev/zd16")
        mock.on("/usr/lib/udev/zvol_id /dev/zd0").succeeds("rpool/vm")
        mock.on("/usr/lib/udev/zvol_id /dev/zd16").succeeds("rpool/keystore")
        mock.on("zpool list rpool").fails()
        with patch_sh(mock):
            assert Keystore(make_log()).find_zvol_for_pool("rpool") == "/dev/zd16"
        assert mock.was_not_called("zvol_id /dev/zd0p1")

    def test_find_zvol_single_device_of_another_pool_is_not_used(self):
        """Phase 0: one zvol and rpool imported used to be tried 'anyway'."""
        mock = MockShell()
        self._zvols(mock, {"/dev/zd0": "rpool/keystore"})
        mock.on("zpool list rpool").succeeds()
        with patch_sh(mock):
            assert Keystore(make_log()).find_zvol_for_pool("backup") is None

    def test_find_zvol_falls_back_to_the_udev_link(self):
        """Without zvol_id, the udev link /dev/zvol/<pool>/keystore decides."""
        mock = MockShell()
        mock.on("ls -1 /dev/zd*").succeeds("/dev/zd0\n/dev/zd16")
        mock.on_prefix("/usr/lib/udev/zvol_id").fails("not found", rc=127)
        mock.on("readlink -f /dev/zvol/backup/keystore").succeeds("/dev/zd16")
        with patch_sh(mock):
            assert Keystore(make_log()).find_zvol_for_pool("backup") == "/dev/zd16"

    def test_find_zvol_none(self):
        """No zvol devices found."""
        mock = MockShell()
        mock.on("ls -1 /dev/zd*").fails("No such file")
        mock.on_prefix("readlink -f /dev/zvol/").fails()
        with patch_sh(mock):
            assert Keystore(make_log()).find_zvol_for_pool("backup") is None

    def test_mount_wrong_passphrase(self):
        """Mount fails with wrong passphrase."""
        mock = MockShell()
        log = make_log()
        ks = Keystore(log)

        mock.on("ls -1 /dev/zd*").succeeds("/dev/zd0")
        mock.on("/usr/lib/udev/zvol_id /dev/zd0").succeeds("backup/keystore")
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

    def test_a_disabled_cleanup_has_nothing_pending(self):
        """M4dR-9: after disable() a later track is not run by it (the
        resource is the user's), and the stop line must not say otherwise."""
        cleanup = Cleanup(make_log())
        cleanup.track_dir("/tmp/x")
        assert cleanup.pending()
        cleanup.disable()
        assert not cleanup.pending()
        cleanup.track_dir("/tmp/y")  # tracked after the disable
        assert not cleanup.pending()

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
        Forced export was historically unprotected - the regression that
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
        """Cleanup is intentionally device-agnostic - it never issues
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
        """If both export attempts fail, no flush is issued - the pool
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
        - used by callers to know which pools were flushed."""
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
        """flush_device_cache() never ejects - that's eject_device's job.
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

    def test_the_eject_line_names_the_drive_the_prompt_named(self):
        """I18 (M4d-22): the by-id path goes to the debug line only."""
        path = f"/dev/disk/by-id/{_KINGSTON_ID}-part1"
        mock = MockShell()
        mock.on(f"eject {path}").succeeds()
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out), patch("builtins.input", return_value="y"):
            prompt_eject_or_attach(path, "blue", make_log(), default_eject=True)
        text = out.getvalue()
        assert "Drive 'blue' ejected - caches flushed" in text
        assert f"Device {path}" not in text


class TestZarkCommand:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """M4d-22: a tip names zark as it is run here, always with sudo."""

    def test_installed_or_from_a_checkout(self):
        with patch.object(Config, "zark_root", return_value=Path("/usr/share/zark")):
            assert zark_command() == "sudo zark"
        with patch.object(Config, "zark_root", return_value=Path("/media/stick/zark")):
            assert zark_command() == "sudo ./zark"

    def test_the_status_and_certificate_lines_name_the_command_as_run(self):
        # M4dP.1 (M5a-9): `sudo zark` was written out in the status lines and
        # the certificate warning, wrong from a checkout.
        st = status_lib.Status(caps={"rpool": 86}, residues=3)
        facts = _facts()
        checks = all_selftest_checks()
        for root, cmd in (("/usr/share/zark", "sudo zark"), ("/media/stick/zark", "sudo ./zark")):
            with patch.object(Config, "zark_root", return_value=Path(root)):
                assert f"{cmd} prune" in status_lib.motd_text(st)
                assert [t for _lvl, t in status_lib.cap_lines(st)] == [
                    f"rpool is 86% full (warning from 80%): {cmd} prune"
                ]
                assert status_lib.residue_line(3).endswith(f"{cmd} prune removes them")
                lines = certs.warning_lines(facts, checks, [], [])
                assert lines[0].endswith(f"run {cmd} selftest"), lines

    def test_no_tip_hard_codes_the_command(self):
        # The MOTD's lines (read at login) and the installed scripts say `zark`.
        allowed = {"lib/alerts.py", "lib/apt_guard.py", "lib/grub_guard.py", "lib/config.py"}
        # Sentences that name a command without telling to run it (M4dF-5).
        named = (
            "Mounted with 'zark mount",
            "A backup mounted with 'zark mount'",
            "browsed with 'zark mount'",
            "`zark prepare` always creates",
        )
        root = Path(__file__).resolve().parent.parent
        found: list[str] = []
        for path in sorted([*root.glob("lib/*.py"), *root.glob("commands/*.py"), root / "zark"]):
            rel = str(path.relative_to(root))
            if rel in allowed:
                continue
            tree = ast.parse(path.read_text("utf-8"))
            docs = {
                id(n.body[0].value)
                for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))
                and n.body
                and isinstance(n.body[0], ast.Expr)
            }
            found += [
                f"{rel}:{n.lineno}"
                for n in ast.walk(tree)
                if isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and id(n) not in docs
                and not any(x in n.value for x in named)
                # M4dF-5: also a command quoted in a sentence, 'zark setup'.
                and re.search(r"sudo (\./)?zark\b|['`](\./)?zark [a-z]", n.value)
            ]
        assert not found, found


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
#  commands/backup.py - live USB detection
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
        """Target smaller than source warns (not fatal) - operator override."""
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
        """If we cannot measure source, defer to the reactive handler - no fatal."""
        dst = self._make_pool_info("backup", used_bytes=10 * 1024**3, avail_bytes=1024**2)  # 1 MiB
        log = make_log()
        # Even with a near-empty target, no fatal: src_info is None
        _check_target_space(None, dst, "backup", "rpool", log)

    def test_silent_when_dst_info_none(self):
        """If we cannot measure target, defer to the reactive handler - no fatal."""
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
#  commands/backup.py - argument parsing
# ═════════════════════════════════════════════════════════════════════════


class TestBackupParseArgs:  # pylint: disable=missing-function-docstring
    """Tests for backup's lightweight argument parser.

    Default behaviour: take_snapshots=True (sanoid runs before syncoid).
    --no-snapshot is the only way to opt out - for cron/automation that
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
#  lib/drives.py - validate_external_block_device (shared by prepare/purge)
# ═════════════════════════════════════════════════════════════════════════


class TestValidateExternalBlockDevice:  # pylint: disable=missing-function-docstring
    """Tests for the external-drive safety validator (used by prepare and purge)."""

    @staticmethod
    def _run(mock: MockShell, dev: str, realpath: str) -> DiskIdentity:
        with (
            patch_sh(mock),
            exists_only("/dev/"),
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
            with exists_only("/dev/nvme0n1"):
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
                            pass  # Expected - fatal raises SystemExit

    def test_refuses_nonexistent(self):
        """Refuses non-existent devices."""
        log = make_log()

        with exists_only(absent=("/dev/sdz",)):
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
#  commands/simulate.py - OVMF detection
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
        # Common typo - the parser should accept either x or X
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
        # 16K is sci-fi territory - reject as a probable typo
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


class TestSimulatePrompts:  # pylint: disable=missing-function-docstring
    """R2-14: simulate's bare input() prompts, at EOF and with an answer."""

    def test_the_rw_confirmation_needs_the_path_typed(self):
        import commands.simulate as sim  # pylint: disable=import-outside-toplevel

        for given in (EOFError, "/dev/sda"):
            out = StringIO()
            with (
                patch("builtins.input", side_effect=EOFError if given is EOFError else [given, ""]),
                redirect_stdout(out),
            ):
                try:
                    sim._confirm_rw_or_abort("/dev/sdb", make_log())  # pylint: disable=protected-access
                except SystemExit:
                    pass
                else:
                    raise AssertionError(f"no abort for {given!r}")
            assert "typed path did not match the target disk" in out.getvalue()
        assert "You typed: '/dev/sda'" in out.getvalue()
        with patch("builtins.input", return_value="/dev/sdb"), redirect_stdout(StringIO()):
            sim._confirm_rw_or_abort("/dev/sdb", make_log())  # pylint: disable=protected-access

    def test_eof_at_the_qemu_prompt_does_not_install(self):
        # M5a-13: EOF used to answer yes and install packages with nobody there.
        import commands.simulate as sim  # pylint: disable=import-outside-toplevel

        mock = MockShell()
        mock.on("which qemu-system-x86_64").fails()
        out = StringIO()
        with (
            patch_sh(mock),
            patch("builtins.input", side_effect=EOFError),
            patch.object(sim, "_install_deps") as deps,
            redirect_stdout(out),
        ):
            try:
                sim.run([])
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("simulate went on without QEMU")
        assert not deps.called
        assert "QEMU is required for simulate" in out.getvalue()

    def test_enter_at_the_qemu_prompt_installs(self):
        import commands.simulate as sim  # pylint: disable=import-outside-toplevel

        class Stop(Exception):
            """Ends the run where the prompt's answer was used."""

        mock = MockShell()
        mock.on("which qemu-system-x86_64").fails()
        with (
            patch_sh(mock),
            patch("builtins.input", return_value=""),
            patch.object(sim, "_install_deps", side_effect=Stop) as deps,
            redirect_stdout(StringIO()),
        ):
            try:
                sim.run([])
            except Stop:
                pass
        assert deps.called

    def test_eof_at_the_disk_selection_aborts_before_qemu(self):
        import commands.simulate as sim  # pylint: disable=import-outside-toplevel

        mock = MockShell()
        mock.on("which qemu-system-x86_64").succeeds("/usr/bin/qemu-system-x86_64")
        with tempfile.NamedTemporaryFile() as fd:
            with (
                patch_sh(mock),
                patch.object(sim, "_detect_gl", return_value=(False, "test")),
                patch.object(sim, "_find_file", return_value=fd.name),
                patch.object(sim.shutil, "copy2"),
                patch.object(sim.os, "chmod"),
                patch.object(sim, "_list_candidate_disks", return_value=[("/dev/sdz", "sdz")]),
                patch("builtins.input", side_effect=EOFError),
                redirect_stdout(StringIO()) as out,
            ):
                try:
                    sim.run([])
                except SystemExit:
                    pass
                else:
                    raise AssertionError("no abort at EOF")
        assert "invalid selection" in out.getvalue()
        assert not [c for c in mock.calls if c.startswith("qemu-system")], mock.calls


class TestSimulateInUseDetection:  # pylint: disable=missing-function-docstring
    """Tests for _disk_in_use_reasons() - the safety-layer-1 detector."""

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
    """Tests for _list_candidate_disks() - only eligible disks must be listed."""

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
            "In-use disk leaked into candidate list - safety layer 2 broken"
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
    """Tests for _detect_gl() - host capability probe for virtio-vga-gl."""

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
        # qemu probe should not even be reached - dri is checked first
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
#  commands/clean.py - patterns
# ═════════════════════════════════════════════════════════════════════════


class TestClean:  # pylint: disable=missing-function-docstring
    """H6: clean acts only on what zark mounted, imported or opened."""

    @staticmethod
    def _env(td: str) -> tuple[str, str, ExitStack]:
        root = os.path.join(td, "mnt", "zark")
        altroot = os.path.join(td, "run", "zark", "altroot")
        stack = ExitStack()
        _ = stack.enter_context(patch.object(clean_mod, "ROOTS", (root, altroot), create=True))
        _ = stack.enter_context(
            patch.object(clean_mod, "MAPPER_DIR", os.path.join(td, "mapper"), create=True)
        )
        _ = stack.enter_context(
            patch.object(
                clean_mod, "ZVOL_INHIBIT", os.path.join(td, "zvol_inhibit_dev"), create=True
            )
        )
        return root, altroot, stack

    @staticmethod
    def _run(
        mock: MockShell,
        stack: ExitStack,
        answer: str = "y",
        marker: bool = False,
        out: StringIO | None = None,
    ) -> int:
        with (
            stack,
            patch_sh(mock),
            patch("builtins.input", return_value=answer),
            redirect_stdout(out if out is not None else StringIO()),
        ):
            if marker:  # where patch_sh put the engine's marker
                Path(clean_mod.engine.SANOID_MARKER).write_text("x\n", encoding="ascii")
            try:
                clean_mod.run([])
            except SystemExit as e:
                return int(e.code) if isinstance(e.code, int) else 1
        return 0

    def test_running_system_pools_are_never_touched(self):
        with tempfile.TemporaryDirectory() as td:
            _, altroot, stack = self._env(td)
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds(
                f"rpool\t-\nbpool\t-\nbackup\t{altroot}/backup"
            )
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds(
                f"/ rpool/ROOT/be\n/boot bpool/BOOT/be\n{altroot}/backup/var backup/rpool/var"
            )
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on_prefix("zpool export").succeeds()
            rc = self._run(mock, stack)
        assert mock.was_not_called("zfs unmount -a"), mock.calls
        assert mock.was_not_called("zpool export rpool"), mock.calls
        assert mock.was_not_called("zpool export bpool"), mock.calls
        assert mock.was_not_called("export -f"), mock.calls
        assert mock.was_called("zpool export backup"), mock.calls
        assert mock.was_not_called("rm "), mock.calls
        assert rc == 1  # the mocked table still shows the mount: not complete

    def test_a_selftest_mapper_is_listed_and_closed_before_the_export(self):
        # M4cR-1: inventory() wires _selftest_mappers in; the mapper on the
        # keystore zvol of a pool under zark's altroot is closed, then the
        # pool exported.
        with tempfile.TemporaryDirectory() as td:
            _, altroot, stack = self._env(td)
            os.makedirs(os.path.join(td, "mapper"))
            _ = Path(td, "mapper", "zst0123abcdc-ks").write_text("", encoding="ascii")
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds(
                f"rpool\t-\nzst0123abcdc\t{altroot}/zst0123abcdc"
            )
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ rpool/ROOT/be")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on("cryptsetup status zst0123abcdc-ks").succeeds(
                "/dev/mapper/zst0123abcdc-ks is active.\n  type:    LUKS2\n  device:  /dev/zd16\n"
            )
            mock.on_prefix("cryptsetup close").succeeds()
            mock.on_prefix("zpool export").succeeds()
            real = {"/dev/zvol/zst0123abcdc/keystore": "/dev/zd16"}
            out = StringIO()
            with patch.object(clean_mod.os.path, "realpath", side_effect=lambda p: real.get(p, p)):
                with patch_sh(mock):
                    assert clean_mod.inventory().keystores == ["zst0123abcdc-ks"]
                _ = self._run(mock, stack, out=out)
        assert "keystore: /dev/mapper/zst0123abcdc-ks (close)" in out.getvalue()
        close = mock.calls.index("cryptsetup close zst0123abcdc-ks")
        assert close < mock.calls.index("zpool export zst0123abcdc"), mock.calls

    def test_a_pool_imported_by_hand_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, stack = self._env(td)
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds("rpool\t-\ntank\t-\nother\t/srv/x")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ rpool/ROOT/be")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            rc = self._run(mock, stack)
        assert mock.was_not_called("zpool export"), mock.calls
        assert rc == 0

    def test_the_systems_keystore_mount_is_left_alone(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, stack = self._env(td)
            os.makedirs(os.path.join(td, "mapper", "zark_ks_backup"))
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds("rpool\t-")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds(
                "/run/keystore/rpool /dev/mapper/keystore-rpool"
            )
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on_prefix("cryptsetup close").succeeds()
            _ = self._run(mock, stack)
        assert mock.was_not_called("umount /run/keystore/rpool"), mock.calls
        assert mock.was_called("cryptsetup close zark_ks_backup"), mock.calls

    def test_zarks_keystore_on_top_is_unmounted(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, stack = self._env(td)
            os.makedirs(os.path.join(td, "mapper", "zark_ks_backup"))
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds("rpool\t-")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds(
                "/run/keystore/rpool /dev/mapper/keystore-rpool\n"
                "/run/keystore/rpool /dev/mapper/zark_ks_backup"
            )
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on_prefix("umount").succeeds()
            mock.on_prefix("cryptsetup close").succeeds()
            _ = self._run(mock, stack)
        assert mock.call_count("umount /run/keystore/rpool") == 1, mock.calls

    def test_nothing_is_removed_below_a_mount_that_stays(self):
        with tempfile.TemporaryDirectory() as td:
            root, _, stack = self._env(td)
            home = os.path.join(root, "backup", "home")
            os.makedirs(home)
            Path(home, "file").write_text("precious\n", encoding="ascii")
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds("rpool\t-")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds(f"{home} /srv/data")
            mock.on("findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(f"{home} ext4 /dev/x")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on_prefix("umount").fails("target is busy")
            mock.on_prefix("mount --make-rprivate").succeeds()
            rc = self._run(mock, stack)
            assert Path(home, "file").exists()
        assert rc == 1
        assert mock.was_not_called("rm "), mock.calls

    def test_only_empty_directories_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root, _, stack = self._env(td)
            os.makedirs(os.path.join(root, "backup", "rpool", "var"))
            os.makedirs(os.path.join(root, "chroot"))
            Path(root, "chroot", "note").write_text("x\n", encoding="ascii")
            Path(root, "chroot", "sub").mkdir()
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds("rpool\t-")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ rpool/ROOT/be")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            rc = self._run(mock, stack)
            assert not Path(root, "backup").exists()
            assert not Path(root, "chroot", "sub").exists()
            assert Path(root, "chroot", "note").exists()
        assert rc == 0

    def test_a_timer_left_stopped_is_started(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, stack = self._env(td)
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds("rpool\t-")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ rpool/ROOT/be")
            state = mock.on("systemctl is-active sanoid.timer").succeeds("inactive")
            start = mock.on("systemctl start sanoid.timer").succeeds()
            real = mock.mock_run

            def spy(cmd: str, **kw: Any) -> RunResult:
                r = real(cmd, **kw)
                if r.command == "systemctl start sanoid.timer":
                    state.succeeds("active")
                return r

            with patch.object(mock, "mock_run", side_effect=spy):
                rc = self._run(mock, stack, marker=True)
        assert start.call_count == 1, mock.calls
        assert rc == 0

    def test_declining_changes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            _, altroot, stack = self._env(td)
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds(f"backup\t{altroot}/backup")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ rpool/ROOT/be")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            rc = self._run(mock, stack, answer="n")
        assert mock.was_not_called("zpool export"), mock.calls
        assert mock.was_not_called("umount"), mock.calls
        assert rc == 0

    def test_zvol_inhibit_restored_after_the_export(self):
        with tempfile.TemporaryDirectory() as td:
            _, altroot, stack = self._env(td)
            param = Path(td, "zvol_inhibit_dev")
            param.write_text("1\n", encoding="ascii")
            mock = MockShell()
            listing = mock.on("zpool list -H -o name,altroot").succeeds(f"rpool\t{altroot}/rpool")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ overlay")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")

            mock.on("zpool export rpool").succeeds()
            real = mock.mock_run

            def spy(cmd: str, **kw: Any) -> RunResult:
                r = real(cmd, **kw)
                if cmd == "zpool export rpool":
                    _ = listing.returns(0, "")
                return r

            with patch.object(mock, "mock_run", side_effect=spy):
                rc = self._run(mock, stack)
            value = param.read_text(encoding="ascii").strip()
        assert value == "0", mock.calls
        assert rc == 0

    def test_no_directory_is_removed_while_a_mount_stays(self):
        # M4a review mutation C4: rmdir cannot go through a mount, so only
        # the call itself shows it.
        with tempfile.TemporaryDirectory() as td:
            root, _, stack = self._env(td)
            home = os.path.join(root, "backup", "home")
            os.makedirs(home)
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds("rpool\t-")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds(f"{home} /srv/data")
            mock.on("findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(f"{home} ext4 /dev/x")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on_prefix("umount").fails("target is busy")
            mock.on_prefix("mount --make-rprivate").succeeds()
            with patch.object(clean_mod, "_remove_empty_dirs", return_value=[]) as removal:
                rc = self._run(mock, stack)
        assert rc == 1
        removal.assert_not_called()

    def test_zvol_inhibit_stays_while_the_pool_stays_imported(self):
        # Mutation C6: the parameter is restored only once the pool is gone.
        with tempfile.TemporaryDirectory() as td:
            _, altroot, stack = self._env(td)
            param = Path(td, "zvol_inhibit_dev")
            param.write_text("1\n", encoding="ascii")
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds(f"rpool\t{altroot}/rpool")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ overlay")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on("zpool export rpool").fails("pool is busy")
            rc = self._run(mock, stack)
            value = param.read_text(encoding="ascii").strip()
        assert value == "1", mock.calls
        assert rc == 1

    def test_bpool_is_exported_before_rpool(self):
        # Mutation C7: bpool's /boot sits in rpool's tree; rpool first fails busy.
        with tempfile.TemporaryDirectory() as td:
            _, altroot, stack = self._env(td)
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds(
                f"rpool\t{altroot}/system\nbpool\t{altroot}/system"
            )
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ overlay")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on_prefix("zpool export").succeeds()
            _ = self._run(mock, stack)
        calls = mock.calls
        assert calls.index("zpool export bpool") < calls.index("zpool export rpool"), calls

    def test_a_failed_export_is_incomplete(self):
        # Mutation C11: a pool still imported is named, and the run fails.
        with tempfile.TemporaryDirectory() as td:
            _, altroot, stack = self._env(td)
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds(f"backup\t{altroot}/backup")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ rpool/ROOT/be")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on("zpool export backup").fails("pool is busy")
            out = StringIO()
            rc = self._run(mock, stack, out=out)
        assert rc == 1
        assert "CLEANUP INCOMPLETE" in out.getvalue()
        assert "✗ still imported: backup" in out.getvalue()

    def test_a_busy_pool_is_exported_once_the_copies_elsewhere_are_released(self):
        # W-1 (M4b-3 3b; mutation K1): clean, like umount, releases what other
        # namespaces keep below the trees it unmounted, then exports again.
        ns = "nsenter --mount=/proc/42/ns/mnt"
        mock = MockShell()
        listing = mock.on(f"{ns} findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
            "/ overlay overlay\n/mnt/zark/backup/home zfs backup/rpool/USERDATA/home"
        )
        export = mock.on("zpool export backup").fails("pool is busy")
        mock.on(f"{ns} umount -R /mnt/zark/backup/home").succeeds()
        real = mock.mock_run

        def run_hook(cmd: Any, *a: Any, **k: Any) -> RunResult:
            r = real(cmd, *a, **k)
            if str(cmd).startswith(f"{ns} umount -R"):
                _ = listing.succeeds("/ overlay overlay")
                _ = export.succeeds()
            return r

        mock.mock_run = run_hook  # type: ignore[method-assign]
        with (
            patch_sh(mock),
            patch.object(umount_mod, "_namespaces", return_value=["/proc/42/ns/mnt"]),
            redirect_stdout(StringIO()),
        ):
            clean_mod._export_pools(  # pylint: disable=protected-access
                ["backup"], {"/mnt/zark/backup": frozenset()}, make_log()
            )
        assert mock.call_count(f"{ns} umount -R /mnt/zark/backup/home") == 1, mock.calls
        assert mock.call_count("zpool export backup") == 2, mock.calls

    def test_no_directory_is_removed_while_a_zark_pool_stays(self):
        # M4aR-6 (M4aF-7): rmdir would detach copies held in other namespaces,
        # the ones that keep the export busy.
        with tempfile.TemporaryDirectory() as td:
            _, altroot, stack = self._env(td)
            os.makedirs(os.path.join(altroot, "backup", "home"))
            mock = MockShell()
            mock.on("zpool list -H -o name,altroot").succeeds(f"backup\t{altroot}/backup")
            mock.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ rpool/ROOT/be")
            mock.on("systemctl is-active sanoid.timer").succeeds("active")
            mock.on("zpool export backup").fails("pool is busy")
            out = StringIO()
            rc = self._run(mock, stack, out=out)
            kept = Path(altroot, "backup", "home").is_dir()
        assert rc == 1 and kept
        assert "No directory was removed while a zark pool is still imported." in out.getvalue()


class TestLock:  # pylint: disable=missing-function-docstring,protected-access
    """M2R-16 / M3P.3 (M4a-5): one kernel lock for every command that changes state."""

    _HOLD = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.ftruncate(fd, 0); os.write(fd, b'zark backup (pid 1, since then)')\n"
        "print('held', flush=True); time.sleep(60)\n"
    )

    def test_read_only_commands_run_without_the_lock(self):
        assert not lock_mod.needs_lock("status", ["--refresh"])
        assert not lock_mod.needs_lock("explore", [])
        assert not lock_mod.needs_lock("monitor", [])
        assert not lock_mod.needs_lock("registry", [])
        assert not lock_mod.needs_lock("registry", ["list"])
        for cmd in ("backup", "prune", "prepare", "purge", "recover", "clean", "health"):
            assert lock_mod.needs_lock(cmd, []), cmd
        assert lock_mod.needs_lock("registry", ["forget", "x"])
        # M3P.2: the package's prerm runs it; it touches no pool.
        assert not lock_mod.needs_lock("setup", ["--remove"])
        assert lock_mod.needs_lock("setup", [])

    def test_a_second_run_is_refused_while_the_first_holds_it(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zark.lock")
            holder = subprocess.Popen(  # pylint: disable=consider-using-with
                [sys.executable, "-c", self._HOLD, path],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
                out = StringIO()
                with (
                    patch.object(lock_mod, "LOCK_PATH", path),
                    patch.dict(lock_mod._STATE, clear=True),
                    redirect_stdout(out),
                ):
                    try:
                        lock_mod.guard("prune", [])
                    except SystemExit as e:
                        assert e.code == lock_mod.EX_TEMPFAIL
                    else:
                        raise AssertionError("a second run was not refused")
                assert "zark backup (pid 1, since then)" in out.getvalue()
            finally:
                holder.kill()
                _ = holder.wait()
            with patch.object(lock_mod, "LOCK_PATH", path), patch.dict(lock_mod._STATE, clear=True):
                lock_mod.guard("prune", [])
                os.close(lock_mod._STATE["fd"])
            assert Path(path).read_text(encoding="utf-8").startswith("zark prune (pid ")

    def test_two_zark_runs_exclude_each_other(self):
        # Mutation L2: the holder takes the lock through lib.lock itself, so a
        # shared lock there would let both runs in.
        hold = (
            "import sys, time\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "import lib.lock as lock\n"
            "lock.LOCK_PATH = sys.argv[1]\n"
            "lock.acquire('backup')\n"
            "print('held', flush=True); time.sleep(60)\n"
        )
        repo = str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zark.lock")
            holder = subprocess.Popen(  # pylint: disable=consider-using-with
                [sys.executable, "-c", hold, path, repo],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
                with (
                    patch.object(lock_mod, "LOCK_PATH", path),
                    patch.dict(lock_mod._STATE, clear=True),
                    redirect_stdout(StringIO()),
                ):
                    try:
                        lock_mod.guard("prune", [])
                    except SystemExit as e:
                        assert e.code == lock_mod.EX_TEMPFAIL
                    else:
                        os.close(lock_mod._STATE["fd"])
                        raise AssertionError("a second run was not refused")
            finally:
                holder.kill()
                _ = holder.wait()

    def test_a_lock_file_that_cannot_be_opened_says_so(self):
        # M4aR-9: not "another zark run"; the exit status stays 75 (try later).
        out = StringIO()
        with (
            patch.object(lock_mod.os, "open", side_effect=PermissionError(13, "Permission denied")),
            patch.dict(lock_mod._STATE, clear=True),
            redirect_stdout(out),
        ):
            try:
                lock_mod.guard("backup", [])
            except SystemExit as e:
                assert e.code == lock_mod.EX_TEMPFAIL
            else:
                raise AssertionError("expected a refusal")
        text = out.getvalue()
        assert f"Cannot take the lock {lock_mod.LOCK_PATH}: [Errno 13] Permission denied" in text
        assert "Another zark run" not in text

    def test_the_lock_file_is_private(self):
        # Mutation L6: mode 0600 whatever the umask.
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zark.lock")
            old = os.umask(0)
            try:
                with (
                    patch.object(lock_mod, "LOCK_PATH", path),
                    patch.dict(lock_mod._STATE, clear=True),
                ):
                    lock_mod.acquire("backup")
                    os.close(lock_mod._STATE["fd"])
            finally:
                _ = os.umask(old)
            assert os.stat(path).st_mode & 0o777 == 0o600

    def test_a_killed_holder_leaves_no_stale_lock(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zark.lock")
            holder = subprocess.Popen(  # pylint: disable=consider-using-with
                [sys.executable, "-c", self._HOLD, path],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
            holder.send_signal(signal.SIGKILL)
            _ = holder.wait()
            with patch.object(lock_mod, "LOCK_PATH", path), patch.dict(lock_mod._STATE, clear=True):
                lock_mod.guard("backup", [])
                os.close(lock_mod._STATE["fd"])

    def test_the_entry_point_takes_it_before_running_a_command(self):
        path = str(Path(__file__).resolve().parent.parent / "zark")
        loader = importlib.machinery.SourceFileLoader("zark_entry", path)
        spec = importlib.util.spec_from_file_location("zark_entry", path, loader=loader)
        assert spec is not None
        entry = importlib.util.module_from_spec(spec)
        loader.exec_module(entry)
        ran: list[str] = []
        with (
            patch.object(sys, "argv", ["zark", "prune"]),
            patch("os.geteuid", return_value=0),
            patch("lib.signals.guard_output"),
            patch.object(lock_mod, "guard", side_effect=SystemExit(lock_mod.EX_TEMPFAIL)) as g,
            patch.object(prune_mod, "run", side_effect=lambda _a: ran.append("prune")),
        ):
            try:
                entry.main()
            except SystemExit as e:
                assert e.code == lock_mod.EX_TEMPFAIL
        g.assert_called_once_with("prune", [])
        assert not ran


class TestCleanOnRealMounts:  # pylint: disable=missing-function-docstring
    """H6 on real mounts: tests/clean_harness.py runs the real clean in its own
    mount namespace. Needs root and mount namespaces; skipped otherwise."""

    def _run(self, case: str) -> dict[str, Any]:
        TestUmountPropagation._skip_unless_possible()  # pylint: disable=protected-access
        harness = Path(__file__).parent / "clean_harness.py"
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
        result: dict[str, Any] = json.loads(r.stdout.strip().splitlines()[-1])
        return result

    def test_a_busy_mount_keeps_its_files(self):
        # 1.0.12 ran rm -rf after a failed umount -R and deleted through the mount.
        r = self._run("busy")
        assert r == {"rc": 1, "file": True, "mounted": True, "root": True}, r

    def test_an_idle_tree_is_released_and_its_empty_directories_go(self):
        r = self._run("idle")
        assert r == {"rc": 0, "file": True, "mounted": False, "root": False}, r

    def test_empty_directories_on_another_filesystem_stay(self):
        # M4a review mutation C9: rmdir never walks into a filesystem mounted below a root.
        r = self._run("otherfs")
        assert r == {"inner": True}, r


# ═════════════════════════════════════════════════════════════════════════
#  commands/monitor.py - progress bar
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
        assert mock.was_not_called("zpool import -o readonly=on")  # the verification import

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
#  lib/zfs.py - fix_grub_bpool_uuid helper
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

    def test_a_title_that_is_not_utf8_is_written_back_as_read(self):
        """M4c-6: bytes that are not UTF-8 survive the rewrite unchanged."""
        fd, path = tempfile.mkstemp(suffix=".cfg")
        os.close(fd)
        p = Path(path)
        _ = p.write_bytes(
            b"menuentry 'Ubuntu \xff\xfe' {\n"
            + f"    search --no-floppy --fs-uuid --set=root {self.OLD_HEX}\n}}\n".encode(),
        )
        assert fix_grub_bpool_uuid(p, self.NEW_HEX, make_log()) is True
        assert p.read_bytes() == (
            b"menuentry 'Ubuntu \xff\xfe' {\n"
            + f"    search --no-floppy --fs-uuid --set=root {self.NEW_HEX}\n}}\n".encode()
        )
        os.unlink(p)

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
        # Sleep is unnecessary - we compare contents, not just mtime.
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
        must NOT be replaced - e.g., a comment, a UUID in a different context.
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
        - which broke boot whenever the recovered system's drive enumeration
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
        # must be rewritten - regex must accept the hints in between.
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
#  commands/recover.py - _abort_missing_keystore
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
        # Should never reach here - _abort_missing_keystore is NoReturn
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
        # must not crash with KeyError - the abort must still happen.
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
        continued silently. v1.0.5 must abort instead - verified here by
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
        """bpool/BOOT holds kernels and grub config - same retention horizon
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
        we should NOT generate a rule for it - the named ubuntu_* rule
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
        '# Preserved user sections' header - keep the file clean."""
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

    def test_sanoid_conf_gap(self):
        # M4c-10: finish's read-only view of setup's sanoid step.
        with tempfile.TemporaryDirectory() as td:
            conf = Path(td) / "sanoid.conf"
            real = Path

            def fake_path(p: object) -> Path:
                return conf if str(p) == "/etc/sanoid/sanoid.conf" else real(str(p))

            with (
                patch.object(setup_mod, "Path", fake_path),
                patch.object(setup_mod, "_detect_ubuntu_ds", return_value=self.UBUNTU),
                patch.object(setup_mod, "_discover_rules", return_value=self._planned()),
            ):
                zfs = ZFS(make_log())
                assert setup_mod.sanoid_conf_gap(zfs) == "write /etc/sanoid/sanoid.conf"
                _ = conf.write_text(_generate_sanoid_conf(self._planned()), encoding="utf-8")
                assert setup_mod.sanoid_conf_gap(zfs) == ""
                _ = conf.write_text(
                    _generate_sanoid_conf(self._planned()[:-1]),
                    encoding="utf-8",
                )
                gap = setup_mod.sanoid_conf_gap(zfs)
                assert gap == "update /etc/sanoid/sanoid.conf (1 section(s) differ)"

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
        # is present here too - preservation must work regardless of what
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
#  lib/boot_chain.py - Secure Boot .latest variant pinning
# ═════════════════════════════════════════════════════════════════════════


class TestForceLatestSignedAlternative:  # pylint: disable=missing-function-docstring
    """Tests for ``_force_latest_signed_alternative``.

    The function is the entry point used by ``recover`` to avoid copying
    the older shim/grub variant to the recovered system's ESP. Two
    behaviors must be guaranteed: (1) when the .latest variant exists
    in the chroot, run update-alternatives --set; (2) when it does not,
    do nothing - so older Ubuntu releases without the split keep working.
    """

    def test_no_op_when_latest_does_not_exist(self):
        """Older Ubuntu releases (or unusual chroots) without a .latest
        variant must not trigger update-alternatives - the function
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
        a warning but does not raise - the subsequent dpkg-reconfigure
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
#  commands/setup.py - Secure Boot alternatives status
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
        None - meaning this Ubuntu release doesn't ship the split."""
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
#  lib/config.py - UTC timestamps
# ═════════════════════════════════════════════════════════════════════════


class TestConfigTimestamps:  # pylint: disable=missing-function-docstring
    """``now_utc_iso`` / ``parse_utc_iso`` - the single source of truth
    for the timestamp written to ``last_backup_at``."""

    def test_now_utc_iso_shape(self):
        """Z-suffix, second precision, ISO-8601, no microseconds."""
        s = now_utc_iso()
        # YYYY-MM-DDTHH:MM:SSZ - exactly 20 characters.
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
        """``datetime.isoformat()`` emits ``+00:00`` rather than ``Z`` -
        the parser must accept both for robustness."""
        dt = parse_utc_iso("2026-05-08T15:05:57+00:00")
        assert dt is not None
        assert dt.hour == 15

    def test_parse_returns_none_on_garbage(self):
        assert parse_utc_iso("not a date") is None
        assert parse_utc_iso("") is None
        assert parse_utc_iso("2026-13-99T99:99:99Z") is None


# ═════════════════════════════════════════════════════════════════════════
#  lib/config.py - known_drives.json with last_backup_at
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
#  lib/drives.py - staleness helpers
# ═════════════════════════════════════════════════════════════════════════


class TestDrivesStaleness:  # pylint: disable=missing-function-docstring
    """``drive_staleness_days`` / ``is_drive_stale`` - the pre-flight
    arithmetic that gates ``zark backup``."""

    @staticmethod
    def _info(last: str | None) -> DriveInfo:
        return DriveInfo(name="x", guid="g", drive_id="d", last_backup_at=last)

    def test_staleness_none_when_field_absent(self):
        assert drive_staleness_days(self._info(None)) is None

    def test_staleness_none_when_field_malformed(self):
        """Malformed timestamps must not raise - the user shouldn't be
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
        """At exactly threshold_days, the drive is NOT yet stale -
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
        """Drives with no ``last_backup_at`` are not considered stale -
        they auto-populate on first successful backup."""
        assert not is_drive_stale(self._info(None), 60)


# ═════════════════════════════════════════════════════════════════════════
#  commands/setup.py - template_minimal migration diff
# ═════════════════════════════════════════════════════════════════════════


class TestSetupTemplateDiff:  # pylint: disable=missing-function-docstring
    """``_diff_rules`` must surface a diff for ``[template_minimal]``
    when the parsed values disagree with the expected constants.
    Saying yes to the migration regenerates the file with the new
    values - verified via a parse-of-generator round-trip."""

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
#  commands/backup.py - staleness reporting (informative only)
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
#  lib/sanoid_retention.py - config parsing
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


class TestSystemPoolImportOrder:  # pylint: disable=missing-function-docstring
    """R2-9: system pools are imported by /dev/disk/by-id first.

    A device hint (``zark chroot <device>``) is only the fallback, and any
    import that did not go through by-id is named in a warning: its vdevs,
    and the zpool.cache written from them, may carry kernel names.
    """

    HINT = "/dev/sda4"

    @staticmethod
    def _imports(mock: MockShell) -> list[str]:
        return [c for c in mock.calls if c.startswith("zpool import")]

    def test_by_id_first_even_with_a_hint(self):
        mock = MockShell()
        mock.on("zpool list rpool").fails()
        mock.on("zpool import -d /dev/disk/by-id rpool").succeeds()
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            assert ZFS(make_log()).pool_import("rpool", device=self.HINT)
        assert self._imports(mock) == ["zpool import -d /dev/disk/by-id rpool"]
        assert "not /dev/disk/by-id" not in out.getvalue()

    def test_the_hint_is_the_fallback_and_is_named(self):
        mock = MockShell()
        mock.on("zpool list rpool").fails()
        mock.on("zpool import -d /dev/disk/by-id rpool").fails("no such pool")
        mock.on("zpool import -f -d /dev/disk/by-id rpool").fails("no such pool")
        mock.on(f"zpool import -d {self.HINT} rpool").succeeds()
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            assert ZFS(make_log()).pool_import("rpool", device=self.HINT)
        assert self._imports(mock)[-1] == f"zpool import -d {self.HINT} rpool"
        assert f"rpool was imported by {self.HINT}, not /dev/disk/by-id" in out.getvalue()

    def test_the_auto_scan_is_last_and_named(self):
        mock = MockShell()
        mock.on("zpool list rpool").fails()
        mock.on("zpool import rpool").succeeds()
        mock.on_prefix("zpool import -d").fails("no such pool")
        mock.on_prefix("zpool import -f -d").fails("no such pool")
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            assert ZFS(make_log()).pool_import("rpool")
        assert self._imports(mock) == [
            "zpool import -d /dev/disk/by-id rpool",
            "zpool import -f -d /dev/disk/by-id rpool",
            "zpool import rpool",
        ]
        assert "a scan of every device" in out.getvalue()

    def test_by_id_is_never_tried_twice(self):
        mock = MockShell()
        mock.on("zpool list rpool").fails()
        mock.on_prefix("zpool import").fails("no such pool")
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert not ZFS(make_log()).pool_import("rpool", device="/dev/disk/by-id")
        assert sum("by-id" in c for c in self._imports(mock)) == 2  # with and without -f


class TestReadbackVerification:  # pylint: disable=missing-function-docstring
    """#1: verify_exported_pool_readback re-imports read-only and checks health.

    Guards against USB-SATA bridges that lie about FUA - an export that
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


class TestReadbackTracked:  # pylint: disable=missing-function-docstring
    """M4c-25 (M4bX.1): the read-back's import is the Cleanup's until exported."""

    DEV = "/dev/disk/by-id/usb-Micron_X_SERIAL-0:0-part1"
    IMPORT = f"zpool import -o readonly=on -N -R /run/zark/altroot/black -d {DEV} black"

    def _readback(self, export_ok: bool, import_ok: bool = True) -> tuple[Cleanup, list[str]]:
        mock = MockShell()
        mock.on("sync").succeeds()
        mock.on("drop_caches").succeeds()
        seen: list[str] = []
        cleanup = Cleanup(make_log())
        imp = mock.on(self.IMPORT)
        _ = imp.succeeds() if import_ok else imp.fails("x")

        def health(*_a: object) -> str:
            seen.extend(cleanup._pools)  # pylint: disable=protected-access
            return "ONLINE"

        _ = (
            mock.on("zpool export").succeeds()
            if export_ok
            else mock.on("zpool export").fails("busy")
        )
        with patch_sh(mock), patch.object(ZFS, "pool_health", side_effect=health):
            _ = ZFS(make_log()).verify_exported_pool_readback("black", self.DEV, cleanup)
        return cleanup, seen

    def test_tracked_while_imported_untracked_once_exported(self):
        cleanup, seen = self._readback(export_ok=True)
        assert seen == ["black"]
        assert not cleanup._pools  # pylint: disable=protected-access

    def test_still_tracked_when_the_export_fails(self):
        cleanup, _ = self._readback(export_ok=False)
        assert cleanup._pools == ["black"]  # pylint: disable=protected-access

    def test_untracked_when_the_import_fails(self):
        with patch.object(forensics, "capture"):
            cleanup, _ = self._readback(export_ok=True, import_ok=False)
        assert not cleanup._pools  # pylint: disable=protected-access


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
        assert finding.level == INFO  # M4a-12: most bridges say this
        assert finding.see_hardware_doc

    def test_fua_no_finding_when_clean(self):
        mock = MockShell()
        mock.on("dmesg").succeeds("[ 1.0] sd 0:0:0:0: [sda] Attached SCSI disk\n")
        with patch_sh(mock):
            assert _check_fua("sda") is None

    def test_transport_uas_is_info(self):
        mock = MockShell()
        mock.on("lsblk -dn -o TRAN /dev/sda").succeeds("usb")
        mock.on("scsi host").succeeds("[ 1.0] scsi host0: uas")
        with patch_sh(mock):
            finding = _check_transport("sda")
        assert finding is not None
        assert finding.level == INFO  # M4a-12: the known-bad list warns, not the driver

    def _report(self, vid_pid: tuple[str, str]) -> HealthReport:
        mock = MockShell()
        mock.on("lsblk -dn -o TRAN /dev/sda").succeeds("usb")
        mock.on("scsi host").succeeds("[ 1.0] scsi host0: uas")
        mock.on("dmesg").succeeds("[ 1.0] sd 0:0:0:0: [sda] doesn't support DPO or FUA\n")
        with (
            patch_sh(mock),
            patch("lib.health.whole_disk", return_value="/dev/sda"),
            patch("lib.health.usb_ids", return_value=vid_pid),
        ):
            return check_device("/dev/sda")

    def test_fua_and_uas_on_an_unknown_bridge_is_no_risk(self):
        # prepare asks, and health writes its report, only on has_risk (M2P.12).
        report = self._report(("152d", "0583"))
        assert {f.title for f in report.findings} >= {"Cache flush (FUA)", "USB transport"}
        assert not report.has_risk

    def test_known_bad_bridge_is_still_a_risk(self):
        assert self._report(("0634", "5604")).has_risk

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


class TestHealthPicker:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """I10: the health picker lists one physical disk once."""

    def test_registered_drive_and_bare_disk_are_one_entry(self):
        drive = ConnectedDrive(
            name="black",
            guid="1",
            drive_id="usb-Micron-0:0",
            dev_path="/dev/sda1",
            known=True,
            guid_changed=False,
            renamed=False,
            state="exported",
        )
        out = StringIO()
        with (
            patch.object(health_cmd, "scan_connected_drives", return_value=[drive]),
            patch.object(health_cmd, "_usb_disks", return_value=[("/dev/sda", "sda [USB disk]")]),
            patch.object(
                health_cmd, "whole_disk", side_effect=lambda d: d.rstrip("0123456789"), create=True
            ),
            patch.object(health_cmd.Config, "load", return_value=make_config()),
            patch("builtins.input", return_value=""),
            redirect_stdout(out),
        ):
            dev = health_cmd._select_device([], make_log())  # pylint: disable=protected-access
        assert dev == "/dev/sda1"
        assert "Which drive" not in out.getvalue()  # a single entry: nothing to choose


class TestHealthPoolFindings:  # pylint: disable=missing-function-docstring
    """I11 (M4a-11): read-only health reports the pool without importing it."""

    def _findings(self, mock: MockShell) -> list[Finding]:
        mock.on("lsblk -nrpo NAME /dev/sdb").succeeds("/dev/sdb\n/dev/sdb1")
        labels = {"/dev/sdb1": PoolLabel(name="black", guid="42", hostid="", hostname="")}
        with (
            patch_sh(mock),
            patch("lib.health.whole_disk", return_value="/dev/sdb"),
            patch("lib.health.read_pool_label", side_effect=labels.get),
        ):
            found = pool_findings("/dev/sdb")
        assert not any(c.startswith("zpool import -o") for c in mock.calls), mock.calls
        return found

    def test_exported_pool_shows_import_state_and_how_to_read_counters(self):
        mock = MockShell()
        mock.on("zpool list -H -o guid,name").succeeds("7\trpool")
        mock.on("zpool import -d /dev/sdb1").succeeds(
            "   pool: black\n     id: 42\n  state: DEGRADED\nstatus: One or more devices..."
        )
        found = self._findings(mock)
        assert len(found) == 1, found
        finding = found[0]
        assert finding.level == WARN and finding.title == "Pool black"
        assert "zpool import reports DEGRADED" in finding.detail
        assert "zpool import -o readonly=on -N -R /run/zark/altroot/black" in finding.detail

    def test_imported_pool_with_checksum_errors_warns(self):
        mock = MockShell()
        mock.on("zpool list -H -o guid,name").succeeds("42\tblack")
        mock.on("zpool list -H -o health black").succeeds("ONLINE")
        mock.on("zpool status -vp black").succeeds(_damaged_status("ONLINE", 4, 2))
        found = self._findings(mock)
        assert len(found) == 1, found
        finding = found[0]
        assert finding.level == WARN
        assert "ONLINE 0 0 4" in finding.detail
        assert "permanent errors in 2 object(s)" in finding.detail

    def test_read_only_run_reports_the_pool(self):
        out = StringIO()
        pool = Finding(WARN, "Pool black", "exported, zpool import reports DEGRADED")
        with (
            patch.object(health_cmd, "_select_device", return_value="/dev/sdb"),
            patch.object(health_cmd, "check_device", return_value=HealthReport("/dev/sdb")),
            patch.object(health_cmd, "pool_findings", return_value=[pool], create=True),
            patch.object(health_cmd, "generate_report", return_value=""),
            patch.object(health_cmd, "write_report_file", return_value="/tmp/r.txt"),
            patch("builtins.input", return_value=""),
            redirect_stdout(out),
        ):
            health_cmd.run([])
        assert "Pool black: exported, zpool import reports DEGRADED" in out.getvalue()

    @staticmethod
    def _destructive(test: HealthResult) -> tuple[str, int, bool]:
        """health's destructive run with ``test`` as its result: output, exit
        status, and whether the report was written."""
        out = StringIO()
        mock = MockShell()
        mock.on_prefix("lsblk -no NAME /dev/sdb").succeeds("")
        mock.on("blkid /dev/sdb").succeeds("")
        rc = 0
        with (
            patch_sh(mock),
            patch.object(health_cmd, "_select_device", return_value="/dev/sdb"),
            patch.object(health_cmd, "validate_external_block_device"),
            patch.object(health_cmd, "measure_write_speed", return_value=0.0),
            patch.object(health_cmd, "device_size_bytes", return_value=8 * 1024**3),
            patch.object(health_cmd, "check_device", return_value=HealthReport("/dev/sdb")),
            patch.object(health_cmd, "run_destructive_test", return_value=test),
            patch.object(health_cmd, "generate_report", return_value=""),
            patch.object(health_cmd, "write_report_file", return_value="/tmp/r.txt") as written,
            patch.object(Log, "ask_choice", side_effect=[1, 0]),  # destructive, fast
            patch.object(Log, "ask", side_effect=[False, True, False]),  # obfuscate, go, cold
            redirect_stdout(out),
        ):
            try:
                health_cmd.run([])
            except SystemExit as e:
                rc = int(e.code) if isinstance(e.code, int) else 1
        return out.getvalue(), rc, written.called

    def test_a_test_pool_left_behind_is_printed(self):
        # M4aV-3 V14: the destructive run's cleanup failure reaches the operator.
        test = HealthResult(
            "fast", False, 1, 0.0, True, "ONLINE", cleanup_error="zpool destroy failed: x"
        )
        out, rc, written = self._destructive(test)
        assert "Test pool was not destroyed: zpool destroy failed: x" in out
        assert rc == 1 and written  # M4aX.2, after the report

    def test_a_failed_test_exits_1_after_the_report(self):
        out, rc, written = self._destructive(
            HealthResult("fast", False, 1, 0.0, False, "re-import failed: x")
        )
        assert "DESTRUCTIVE TEST FAILED" in out
        assert rc == 1 and written

    def test_a_pass_exits_0_also_with_transport_errors(self):
        out, rc, _ = self._destructive(HealthResult("fast", False, 1, 0.0, True, "ONLINE"))
        assert rc == 0 and "Destructive test PASSED (ONLINE)" in out
        out, rc, written = self._destructive(
            HealthResult("fast", False, 1, 0.0, True, "ONLINE", ["[ 1.0] [sdb] DID_ERROR"])
        )
        assert rc == 0 and written and "PASSED WITH TRANSPORT ERRORS" in out

    def test_errors_at_the_unplug_are_shown_and_not_counted(self):
        line = "[ 2.0] sd 0:0:0:0: [sdb] Synchronize Cache(10) failed: hostbyte=DID_ERROR"
        out, rc, written = self._destructive(
            HealthResult("fast", True, 1, 0.0, True, "ONLINE", at_unplug=[line])
        )
        assert rc == 0 and not written, out
        assert "Destructive test PASSED (ONLINE)" in out
        assert "Kernel errors at the unplug, not counted (1): the pool was exported" in out
        assert f"  {line}" in out

    def test_eof_at_the_reconnect_pause_is_not_a_reconnect(self):
        # R2-14, M5a-13: the cold test's pause reads stdin; EOF there is
        # nobody having reconnected (the test used to go on as if they had).
        # The command raises nothing.
        mock = MockShell()
        mock.on_prefix("lsblk -no NAME /dev/sdb").succeeds("")
        mock.on("blkid /dev/sdb").succeeds("")
        answers: list[bool] = []

        def destructive(*_a: object, wait_for_reconnect: Callable[[], bool], **_k: object):
            answers.append(wait_for_reconnect())
            return HealthResult("fast", True, 1, 0.0, False, "the reconnect was not confirmed")

        out = StringIO()
        with (
            patch_sh(mock),
            patch.object(health_cmd, "_select_device", return_value="/dev/sdb"),
            patch.object(health_cmd, "validate_external_block_device"),
            patch.object(health_cmd, "measure_write_speed", return_value=0.0),
            patch.object(health_cmd, "device_size_bytes", return_value=8 * 1024**3),
            patch.object(health_cmd, "check_device", return_value=HealthReport("/dev/sdb")),
            patch.object(health_cmd, "run_destructive_test", side_effect=destructive),
            patch.object(health_cmd, "generate_report", return_value=""),
            patch.object(health_cmd, "write_report_file", return_value="/tmp/r.txt"),
            patch.object(Log, "ask_choice", side_effect=[1, 0]),  # destructive, fast
            patch.object(Log, "ask", side_effect=[False, True, True]),  # obfuscate, go, cold
            patch("builtins.input", side_effect=EOFError),
            redirect_stdout(out),
        ):
            try:
                health_cmd.run([])
            except SystemExit:
                pass
        assert answers == [False]

    def test_a_yes_no_question_can_decline_at_eof(self):
        # M5a-13: Enter takes the default, a closed terminal the on_eof answer.
        with patch("builtins.input", return_value=""), redirect_stdout(StringIO()):
            assert make_log().ask("Install?", default=True, on_eof=False)
        with patch("builtins.input", side_effect=EOFError), redirect_stdout(StringIO()):
            assert not make_log().ask("Install?", default=True, on_eof=False)
            assert make_log().ask("Install?", default=True)  # unchanged without on_eof

    def test_enter_at_the_reconnect_pause_goes_on(self):
        with patch("builtins.input", return_value=""), redirect_stdout(StringIO()):
            assert make_log().ask_enter("    Press Enter once reconnected... ")
        with patch("builtins.input", side_effect=EOFError), redirect_stdout(StringIO()):
            assert not make_log().ask_enter("    Press Enter once reconnected... ")

    def test_a_cold_test_whose_reconnect_is_not_confirmed_fails_before_the_import(self):
        mock = MockShell()
        mock.on_prefix("find /dev/disk/by-id/").succeeds("usb-Stick_SER-0:0\t../../sda\n")
        for cmd in (
            "zpool create",
            "dd",
            "sync",
            "zpool export",
            "eject",
            "zpool destroy",
            "rm -rf",
        ):
            mock.on(cmd).succeeds()
        with patch_sh(mock), exists_only("/dev/disk/by-id/"):
            result = run_destructive_test(
                "/dev/sda",
                PROFILE_FAST,
                True,
                make_log(),
                zfs=_FakeHealthZFS(),
                wait_for_reconnect=lambda: False,
            )
        assert not result.passed and "not confirmed" in result.detail, result
        assert mock.was_not_called("zpool import -o readonly=on")  # the verification import

    def test_imported_clean_pool_is_ok(self):
        mock = MockShell()
        mock.on("zpool list -H -o guid,name").succeeds("42\tblack")
        mock.on("zpool list -H -o health black").succeeds("ONLINE")
        mock.on("zpool status -vp black").succeeds(_damaged_status("ONLINE", 0, 0))
        found = self._findings(mock)
        assert len(found) == 1, found
        finding = found[0]
        assert finding.level == OK


class TestHealthDestructive:  # pylint: disable=missing-function-docstring,too-many-public-methods
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
        with patch_sh(mock), exists_only("/dev/sda"):
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
        mock.on("zpool import -o cachefile=none").succeeds()  # the cleanup's read-write import
        mock.on("zpool destroy").succeeds()
        mock.on("rm -rf").succeeds()
        with patch_sh(mock), exists_only("/dev/sda"):
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

        def _wait() -> bool:
            called["reconnect"] = True
            return True

        mock = MockShell()
        mock.on_prefix("find /dev/disk/by-id/").succeeds("usb-Stick_SER-0:0\t../../sda\n")
        mock.on("zpool create").succeeds()
        mock.on("dd").succeeds()
        mock.on("sync").succeeds()
        mock.on("zpool export").succeeds()
        mock.on("eject").succeeds()
        mock.on("drop_caches").succeeds()
        mock.on("zpool import").succeeds()
        mock.on("zpool destroy").succeeds()
        mock.on("rm -rf").succeeds()
        with patch_sh(mock), exists_only("/dev/disk/by-id/"):
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
        # First dmesg call (the marker, tail -1) - old timestamp.
        # Subsequent dmesg call (the scan) - contains a DID_ERROR after it.
        mock.on("dmesg | tail -1").succeeds("[ 100.0] sd 0:0:0:0: [sda] ready")
        mock.on("dmesg").succeeds(
            "[ 100.0] sd 0:0:0:0: [sda] ready\n"
            "[ 200.5] sd 0:0:0:0: [sda] Synchronize Cache(10) failed: "
            "Result: hostbyte=DID_ERROR driverbyte=DRIVER_OK\n",
        )
        with patch_sh(mock), exists_only("/dev/sda"):
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
        with patch_sh(mock), exists_only("/dev/sda"):
            run_destructive_test(
                "/dev/sda",
                PROFILE_FAST,
                False,
                make_log(),
                zfs=_FakeHealthZFS(),
            )
        assert mock.was_called("wipefs -a /dev/sda")
        assert mock.was_called("sgdisk --zap-all /dev/sda")

    @staticmethod
    def _cold_window(marks: list[float]) -> HealthResult:
        """A cold pass whose kernel log has a transport error before the eject
        (120), one at the unplug (200) and one after the reconnect (300);
        ``marks`` are the last kernel line's timestamps: at the start, right
        before the eject, once the links are back."""
        mock = MockShell()
        mock.on_prefix("find /dev/disk/by-id/").succeeds("usb-Stick_SER-0:0\t../../sda\n")
        for cmd in ("zpool create", "dd", "sync", "zpool export", "eject", "drop_caches"):
            mock.on(cmd).succeeds()
        for cmd in ("zpool import", "zpool destroy", "rm -rf", "wipefs", "sgdisk"):
            mock.on(cmd).succeeds()
        mock.on("dmesg").succeeds(
            "[ 120.0] sd 0:0:0:0: [sda] uas_eh_abort_handler 0 uas-tag 1\n"
            "[ 200.0] sd 0:0:0:0: [sda] Synchronize Cache(10) failed: "
            "Result: hostbyte=DID_ERROR driverbyte=DRIVER_OK\n"
            "[ 300.0] sd 0:0:0:0: [sda] uas_eh_device_reset_handler start\n"
        )
        with (
            patch_sh(mock),
            exists_only("/dev/disk/by-id/"),
            patch.object(health_mod, "_dmesg_now_ts", side_effect=marks),
        ):
            return run_destructive_test(
                "/dev/sda",
                PROFILE_FAST,
                True,
                make_log(),
                zfs=_FakeHealthZFS(),
                wait_for_reconnect=lambda: True,
            )

    def test_errors_at_the_unplug_are_listed_apart(self):
        # M4aP.9: eject, then the operator unplugs; the bridge's failed cache
        # flush at that moment is the unplug's, not the transport's.
        result = self._cold_window([100.0, 150.0, 250.0])
        assert result.passed
        assert [ln[:7] for ln in result.transport_errors] == ["[ 120.0", "[ 300.0"]
        assert [ln[:7] for ln in result.at_unplug] == ["[ 200.0"]
        with patch_sh(MockShell()):
            report = generate_report("/dev/sda", HealthReport("/dev/sda"), result, obfuscate=False)
        assert "At the unplug, not counted (1):\n  [ 200.0]" in report

    def test_without_the_unplug_marks_every_line_counts(self):
        result = self._cold_window([100.0, 0.0, 250.0])
        assert len(result.transport_errors) == 3 and not result.at_unplug

    def test_a_disk_name_that_only_starts_like_the_disks_is_not_the_disk(self):
        # M5a-17 (M4d plan 15 (6) b): "sda" was a substring of "sdaa".
        mock = MockShell()
        mock.on("dmesg").succeeds(
            "[ 300.0] sd 1:0:0:0: [sdaa] Synchronize Cache(10) failed: hostbyte=DID_ERROR\n"
            "[ 301.0] sd 0:0:0:0: [sda] Synchronize Cache(10) failed: hostbyte=DID_ERROR\n"
            "[ 302.0] sd 0:0:0:0: sda1: DID_ERROR\n"
        )
        with patch_sh(mock):
            errs = _transport_errors_since("sda", since_ts=100.0)
        assert len(errs) == 2 and not any("sdaa" in e for e in errs), errs

    def test_the_usb_reset_line_of_the_disks_own_bridge_counts(self):
        # M5a-18 (M4d plan 15 (6) a): the kernel's reset line has neither the
        # disk's name nor "scsi host" nor "uas"; it is found by the USB id.
        mock = MockShell()
        mock.on("dmesg").succeeds(
            "[ 300.0] usb 2-1.4: reset SuperSpeed USB device number 7 using xhci_hcd\n"
            "[ 301.0] usb 2-1: reset SuperSpeed USB device number 3 using xhci_hcd\n"  # the hub
            "[ 302.0] usb 2-2: reset SuperSpeed USB device number 5 using xhci_hcd\n"  # another
            "[ 303.0] usb 2-1.4: reset high-speed USB device number 7 using xhci_hcd\n"
            "[ 304.0] usb 2-1.4: new SuperSpeed USB device number 7 using xhci_hcd\n"
        )
        real = (
            "/sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1.4/2-1.4:1.0"
            + "/host6/target6:0:0/6:0:0:0"
        )
        with (
            patch_sh(mock),
            patch(
                "lib.health.os.path.realpath",
                side_effect=lambda p: real if p == "/sys/block/sdb/device" else p,
            ),
        ):
            errs = _transport_errors_since("sdb", since_ts=100.0)
        assert [e[:7] for e in errs] == ["[ 300.0", "[ 303.0"], errs

    def test_without_a_usb_id_the_reset_lines_are_not_the_disks(self):
        # M5a-18: a disk that is not USB has no bridge to reset.
        mock = MockShell()
        mock.on("dmesg").succeeds(
            "[ 300.0] usb 2-1: reset SuperSpeed USB device number 3 using xhci_hcd\n"
        )
        with patch_sh(mock):
            assert _transport_errors_since("sdb", since_ts=100.0) == []

    def test_after_the_reconnect_the_lines_are_about_the_new_kernel_name(self):
        # M5a-17 (M4d plan 15 (6) e): sdb came back as sdc; sdb is now another disk.
        mock = MockShell()
        mock.on_prefix("find /dev/disk/by-id/").succeeds("usb-Stick_SER-0:0\t../../sdb\n")
        for cmd in ("zpool create", "dd", "sync", "zpool export", "eject", "drop_caches"):
            mock.on(cmd).succeeds()
        for cmd in ("zpool import", "zpool destroy", "rm -rf", "wipefs", "sgdisk", "rmdir"):
            mock.on(cmd).succeeds()
        mock.on("dmesg").succeeds(
            "[ 120.0] sd 0:0:0:0: [sdb] Synchronize Cache(10) failed: hostbyte=DID_ERROR\n"
            "[ 300.0] sd 0:0:0:0: [sdb] Synchronize Cache(10) failed: hostbyte=DID_ERROR\n"
            "[ 310.0] sd 0:0:0:0: [sdc] Synchronize Cache(10) failed: hostbyte=DID_ERROR\n"
        )  # the one at 300 is about the disk that has the old name now
        by_id = "/dev/disk/by-id/usb-Stick_SER-0:0"
        with (
            patch_sh(mock),
            patch("lib.health.Path.exists", side_effect=lambda p=None: True),
            patch(
                "lib.health.os.path.realpath", side_effect=lambda p: "/dev/sdc" if p == by_id else p
            ),
            patch("lib.health.time.sleep"),
            patch.object(health_mod, "_dmesg_now_ts", side_effect=[100.0, 150.0, 250.0]),
            redirect_stdout(StringIO()),
        ):
            result = run_destructive_test(
                "/dev/sdb",
                PROFILE_FAST,
                True,
                make_log(),
                zfs=_FakeHealthZFS(),
                wait_for_reconnect=lambda: True,
            )
        assert len(result.transport_errors) == 2, result.transport_errors
        assert "[sdb]" in result.transport_errors[0] and "[ 120.0]" in result.transport_errors[0]
        assert "[sdc]" in result.transport_errors[1], result.transport_errors

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

    @staticmethod
    def _cold_mock(
        by_id: str = "usb-Stick_SER-0:0\t../../sdb\n",
        label: str = "",
    ) -> MockShell:
        mock = MockShell()
        mock.on_prefix("find /dev/disk/by-id/").succeeds(by_id)
        if label:
            mock.on("zdb -l /dev/sdc1").succeeds(label)
        for cmd in ("zpool create", "dd", "sync", "zpool export", "eject", "drop_caches"):
            mock.on(cmd).succeeds()
        mock.on("zpool import").succeeds()
        mock.on("zpool destroy").succeeds()
        mock.on("rmdir").succeeds()
        mock.on("wipefs").succeeds()
        mock.on("sgdisk").succeeds()
        mock.on("dmesg").succeeds("")
        mock.on_prefix("zdb -l").fails("failed to unpack label")
        return mock

    def _cold(self, mock: MockShell, back: bool, now: str) -> HealthResult:
        by_id = "/dev/disk/by-id/usb-Stick_SER-0:0"
        with (
            patch_sh(mock),
            patch("lib.health.Path.exists", side_effect=lambda p=None: back),
            patch("lib.health.os.path.realpath", side_effect=lambda p: now if p == by_id else p),
            patch("lib.health.time.sleep"),
            redirect_stdout(StringIO()),
        ):
            return run_destructive_test(
                "/dev/sdb",
                PROFILE_FAST,
                True,
                make_log(),
                zfs=_FakeHealthZFS(),
                wait_for_reconnect=lambda: True,
            )

    def test_cold_wipe_follows_the_disk_not_its_old_name(self):
        # H19: after the reconnect the stick is sdc and sdb is another disk.
        mock = self._cold_mock()
        result = self._cold(mock, back=True, now="/dev/sdc")
        assert result.passed
        assert mock.was_called("wipefs -a /dev/sdc")
        assert mock.was_not_called("/dev/sdb 2>"), mock.calls
        assert mock.was_called("-d /dev/disk/by-id/usb-Stick_SER-0:0-part1 zarktest_")

    def test_test_pool_never_enters_the_cachefile(self):
        mock = self._cold_mock()
        _ = self._cold(mock, back=True, now="/dev/sdc")
        for c in mock.calls:
            if c.startswith(("zpool create", "zpool import")):
                assert "cachefile=none" in c, c

    @staticmethod
    def _stateful_pool(mock: MockShell, rw_import_ok: bool = True, destroy: str = "ok") -> None:
        """``zpool list``/``readonly`` follow the imports, export and destroy;
        ``destroy``: "ok", "fails" (rc 1, still imported) or "noop" (rc 0,
        still imported)."""
        state = {"imported": False, "ro": False}
        real = mock.mock_run

        def spy(cmd: str, **kw: Any) -> RunResult:
            recorded = real(cmd, **kw)  # mock.calls keeps every command
            if cmd.startswith("zpool list -H -o name zarktest_"):
                return RunResult(0 if state["imported"] else 1, "", "", cmd)
            if cmd.startswith("zpool get -H -o value readonly zarktest_"):
                return RunResult(0, "on\n" if state["ro"] else "off\n", "", cmd)
            if cmd.startswith("zpool import") and "zarktest_" in cmd:
                ok = "readonly=on" in cmd or rw_import_ok
                if ok:
                    state.update(imported=True, ro="readonly=on" in cmd)
                return RunResult(0 if ok else 1, "", "" if ok else "I/O error", cmd)
            if cmd.startswith("zpool destroy -f zarktest_") and destroy != "ok":
                return RunResult(
                    *((1, "", "pool is busy") if destroy == "fails" else (0, "", "")), cmd
                )
            if cmd.startswith(("zpool export zarktest_", "zpool destroy -f zarktest_")):
                state["imported"] = False
            return recorded

        mock.mock_run = spy  # type: ignore[method-assign,assignment]

    def test_the_read_back_pool_is_destroyed_read_write(self):
        # M4aR-1: the read-back leaves the pool imported read-only, whose
        # destroy returns 0 and writes nothing to its labels.
        mock = self._cold_mock()
        self._stateful_pool(mock)
        result = self._cold(mock, back=True, now="/dev/sdc")
        assert result.passed and result.cleanup_error == "", result
        calls = mock.calls
        ro = next(i for i, c in enumerate(calls) if "readonly=on" in c and "import" in c)
        export = next(i for i, c in enumerate(calls) if c.startswith("zpool export") and i > ro)
        rw = next(
            i
            for i, c in enumerate(calls)
            if c.startswith("zpool import") and "readonly" not in c and i > export
        )
        destroy = next(i for i, c in enumerate(calls) if c.startswith("zpool destroy"))
        assert ro < export < rw < destroy, calls
        assert calls.index("wipefs -a /dev/sdc 2>/dev/null") > destroy

    def test_a_destroy_that_returns_0_but_leaves_the_pool_listed_is_a_failure(self):
        # M4aV-3 V12: the `zpool list` after a destroy that returned 0.
        mock = self._cold_mock()
        self._stateful_pool(mock, destroy="noop")
        result = self._cold(mock, back=True, now="/dev/sdc")
        assert "zpool destroy failed: the pool is still imported" in result.cleanup_error, result

    def test_a_failing_destroy_in_the_cleanup_is_a_failure(self):
        # M4aV-3 V24: `zpool destroy -f` fails: not reported as done.
        mock = self._cold_mock()
        self._stateful_pool(mock, destroy="fails")
        result = self._cold(mock, back=True, now="/dev/sdc")
        assert "zpool destroy failed: pool is busy" in result.cleanup_error, result

    def test_a_failed_export_stops_the_test_before_the_eject(self):
        # M5a-17 (M4d plan 15 (6) c): the export was not checked: the drive was
        # ejected, or read back, with the pool still imported.
        mock = self._cold_mock()
        self._stateful_pool(mock)
        real = mock.mock_run

        def busy(cmd: str, **kw: Any) -> RunResult:
            if cmd.startswith("zpool export zarktest_"):
                return RunResult(1, "", "pool is busy", cmd)
            return real(cmd, **kw)

        mock.mock_run = busy  # type: ignore[method-assign,assignment]
        result = self._cold(mock, back=True, now="/dev/sdc")
        assert not result.passed and "could not export the test pool: pool is busy" in result.detail
        assert mock.was_not_called("eject "), mock.calls
        assert mock.was_not_called("readonly=on"), mock.calls  # nor read back

    def test_the_altroot_stays_when_the_destroy_failed(self):
        # M5a-17 (M4d plan 15 (6) d): the directory is how `zark clean` finds
        # a test pool left imported.
        for destroy, removed in (("fails", False), ("ok", True)):
            mock = self._cold_mock()
            self._stateful_pool(mock, destroy=destroy)
            _ = self._cold(mock, back=True, now="/dev/sdc")
            assert any(c.startswith("rmdir ") for c in mock.calls) is removed, (destroy, mock.calls)

    def test_the_disk_link_after_the_reconnect_is_waited_for(self):
        # M4aV-3 V25: the disk's by-id link appears late (after the settle):
        # polled, not checked once; the run goes on to the read-back.
        mock = self._cold_mock()
        by_id = "/dev/disk/by-id/usb-Stick_SER-0:0"
        seen: list[str] = []

        def exists(p: object = None) -> bool:
            seen.append(str(p))
            return str(p) != by_id or seen.count(by_id) > 3

        with (
            patch_sh(mock),
            patch("lib.health.Path.exists", autospec=True, side_effect=exists),
            patch(
                "lib.health.os.path.realpath", side_effect=lambda p: "/dev/sdc" if p == by_id else p
            ),
            patch("lib.health.time.sleep"),
            redirect_stdout(StringIO()),
        ):
            result = run_destructive_test(
                "/dev/sdb",
                PROFILE_FAST,
                True,
                make_log(),
                zfs=_FakeHealthZFS(),
                wait_for_reconnect=lambda: True,
            )
        assert "did not reappear" not in result.detail, result
        assert any("readonly=on" in c for c in mock.calls), mock.calls

    def test_a_test_pool_that_was_not_destroyed_is_reported(self):
        mock = self._cold_mock()
        self._stateful_pool(mock, rw_import_ok=False)
        result = self._cold(mock, back=True, now="/dev/sdc")
        assert "could not be imported read-write to destroy it: I/O error" in result.cleanup_error
        assert "zpool import -d /dev/disk/by-id/usb-Stick_SER-0:0-part1" in result.cleanup_error
        assert mock.was_not_called("zpool destroy"), mock.calls
        with patch_sh(MockShell()):
            text = generate_report(
                "/dev/sdc", HealthReport(device="/dev/sdc"), result, obfuscate=False
            )
        assert "Test pool    : not destroyed: it could not be imported read-write" in text

    def test_the_links_are_waited_for_after_the_reconnect(self):
        # M4aR-8: udev makes <by-id>-part1 a moment after the disk's link.
        seen = iter([False, False, True])
        with (
            patch("lib.health.Path.exists", side_effect=lambda: next(seen)),
            patch("lib.health.time.sleep") as sleep,
        ):
            assert health_mod._wait_for_path("/dev/disk/by-id/x-part1")  # pylint: disable=protected-access
        assert sleep.call_count == 2

    def test_a_partition_link_that_never_appears_is_its_own_failure(self):
        mock = self._cold_mock()
        with patch("lib.health._wait_for_path", side_effect=lambda p: not p.endswith("-part1")):
            result = self._cold(mock, back=True, now="/dev/sdc")
        assert not result.passed
        part1 = "/dev/disk/by-id/usb-Stick_SER-0:0-part1"
        assert f"the partition link {part1} did not appear within 10 s" in result.detail
        assert mock.call_count("udevadm settle --timeout=10") == 1, mock.calls
        assert not [c for c in mock.calls if "readonly=on" in c], mock.calls

    def test_test_pool_lives_under_zarks_altroot(self):
        # M4aR-3: a run killed with the pool imported leaves it where clean
        # finds it; without -R it was "left alone, not imported by zark".
        mock = self._cold_mock()
        _ = self._cold(mock, back=True, now="/dev/sdc")
        cmds = [c for c in mock.calls if c.startswith(("zpool create", "zpool import"))]
        assert cmds, mock.calls
        for c in cmds:
            m = re.search(r" -R (/run/zark/altroot/zarktest_\d+) ", c)
            assert m, c
        assert " -m / " in cmds[0], cmds[0]
        listing = MockShell()
        listing.on("zpool list -H -o name,altroot").succeeds(
            "rpool\t-\nzarktest_1\t/run/zark/altroot/zarktest_1"
        )
        listing.on("findmnt -rn -o TARGET,SOURCE").succeeds("/ rpool/ROOT/be")
        listing.on("systemctl is-active sanoid.timer").succeeds("active")
        with patch_sh(listing):
            assert clean_mod.inventory().pools == ["zarktest_1"]

    def test_a_disk_that_does_not_come_back_is_not_wiped(self):
        mock = self._cold_mock()
        result = self._cold(mock, back=False, now="")
        assert not result.passed
        assert "did not reappear" in result.detail
        assert mock.was_not_called("wipefs"), mock.calls
        assert mock.was_not_called("sgdisk"), mock.calls

    def test_a_disk_with_another_pool_is_not_wiped(self):
        mock = self._cold_mock(label="LABEL 0\n    name: 'black'\n    pool_guid: 1\n")
        _ = self._cold(mock, back=True, now="/dev/sdc")
        assert mock.was_not_called("wipefs"), mock.calls

    def test_cold_without_a_by_id_name_stops_before_writing(self):
        mock = self._cold_mock(by_id="")
        result = self._cold(mock, back=True, now="/dev/sdb")
        assert not result.passed
        assert mock.was_not_called("zpool create"), mock.calls


class TestIsLiveUsb:  # pylint: disable=missing-function-docstring
    """lib.sh.is_live_usb - single source of truth for live-media detection."""

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
        mock.on("zpool import -N -R /mnt/repair -d /dev/disk/by-id bpool").fails(
            "pool was previously in use"
        )
        mock.on("zpool import -f -N -R /mnt/repair -d /dev/disk/by-id bpool").succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert zfs.pool_import("bpool", altroot="/mnt/repair", no_mount=True)
        import_calls = [c for c in mock.calls if c.startswith("zpool import")]
        # Clean attempt strictly precedes the forced one.
        assert import_calls == [
            "zpool import -N -R /mnt/repair -d /dev/disk/by-id bpool",
            "zpool import -f -N -R /mnt/repair -d /dev/disk/by-id bpool",
        ]


class _FakeKeystore:  # pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods # noqa: E501
    """Minimal stand-in for Keystore in system-mount tests (no real LUKS)."""

    bad_passphrase = False

    def __init__(self, loaded: int = 2):
        self._loaded = loaded
        self.readonly_calls: list[bool] = []

    def mount(self, pool, passphrase, *, readonly=False):  # pylint: disable=unused-argument
        self.readonly_calls.append(readonly)
        return True

    def load_pool_keys(self, pool_root):  # pylint: disable=unused-argument
        return self._loaded

    def umount(self):
        """Never reached in these tests, but Cleanup.track_keystore will
        call it if the cleanup handler ever runs - so the stub must have
        it, and the KeystoreLike protocol now says so."""


class TestSystemMountHelpers:  # pylint: disable=missing-function-docstring
    """lib.mount system-layout helpers used by `zark chroot` / `zark mount local`."""

    def test_find_system_root_dataset(self):
        mock = MockShell()
        mock.on("zfs list -H -o name -r rpool/ROOT").succeeds(
            "rpool/ROOT\nrpool/ROOT/ubuntu_8bt2zy\n",
        )
        with patch_sh(mock):
            assert find_system_root_dataset(make_log()) == "rpool/ROOT/ubuntu_8bt2zy"

    @staticmethod
    def _two_bes(mock: MockShell, root: str) -> None:
        mock.on(f"zfs list -H -o name -r {root}").succeeds(
            f"{root}\n{root}/ubuntu_x\n{root}/ubuntu_x/var\n{root}/zt5b\n"
            f"{root}/zt5b.archived-20261009\n",
        )
        snaps = f"zfs list -H -p -t snapshot -o name,creation -s creation -d 1 {root}"
        mock.on(f"{snaps}/ubuntu_x").succeeds(f"{root}/ubuntu_x@a\t1700000000")
        mock.on(f"{snaps}/zt5b").succeeds(f"{root}/zt5b@b\t1800000000")

    def test_running_root_is_taken_without_a_question(self):
        mock = MockShell()
        self._two_bes(mock, "rpool/ROOT")
        mock.on("findmnt -n -o SOURCE /").succeeds("rpool/ROOT/ubuntu_x")
        with patch_sh(mock), patch("builtins.input", side_effect=AssertionError("asked")):
            assert find_system_root_dataset(make_log()) == "rpool/ROOT/ubuntu_x"

    def test_live_session_asks_newest_first(self):
        # M2 review section 9: alphabetical order picked ubuntu_x.
        mock = MockShell()
        self._two_bes(mock, "rpool/ROOT")
        mock.on("findmnt -n -o SOURCE /").succeeds("/cow")
        with patch_sh(mock), patch("builtins.input", return_value=""), redirect_stdout(StringIO()):
            assert find_system_root_dataset(make_log()) == "rpool/ROOT/zt5b"

    def test_drive_with_two_boot_environments_asks(self):
        mock = MockShell()
        self._two_bes(mock, "backup/rpool/ROOT")
        out = StringIO()
        with patch_sh(mock), patch("builtins.input", return_value="2"), redirect_stdout(out):
            assert backup_layout.find_be("backup", make_log()) == "ubuntu_x"
        assert "archived" not in out.getvalue()
        with patch_sh(mock), patch("builtins.input", return_value=""), redirect_stdout(out):
            assert backup_layout.find_be("backup", make_log()) == "zt5b"

    def test_drive_with_one_boot_environment_does_not_ask(self):
        mock = MockShell()
        mock.on("zfs list -H -o name -r backup/rpool/ROOT").succeeds(
            "backup/rpool/ROOT\nbackup/rpool/ROOT/ubuntu_x\n",
        )
        with patch_sh(mock), patch("builtins.input", side_effect=AssertionError("asked")):
            assert backup_layout.find_be("backup", make_log()) == "ubuntu_x"

    def test_the_device_hint_is_rpools_import_fallback(self):
        # R2-14: zark chroot <device>: by-id first, then the device given.
        mock = MockShell()
        mock.on("zpool list rpool").fails()
        mock.on("zpool list bpool").succeeds("bpool")
        mock.on_prefix("zpool import -N -R /mnt/zark/chroot -d /dev/disk/by-id rpool").fails("no")
        mock.on_prefix("zpool import -f -N -R /mnt/zark/chroot -d /dev/disk/by-id rpool").fails(
            "no"
        )
        mock.on("zpool import -N -R /mnt/zark/chroot -d /dev/sda4 rpool").succeeds()
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            _ = mount_system_pools(
                "/mnt/zark/chroot",
                "pp",
                make_log(),
                ZFS(make_log()),
                _FakeKeystore(),  # type: ignore[arg-type]
                Cleanup(make_log()),
                device="/dev/sda4",
            )
        imports = [c for c in mock.calls if c.startswith("zpool import")]
        assert imports[:3] == [
            "zpool import -N -R /mnt/zark/chroot -d /dev/disk/by-id rpool",
            "zpool import -f -N -R /mnt/zark/chroot -d /dev/disk/by-id rpool",
            "zpool import -N -R /mnt/zark/chroot -d /dev/sda4 rpool",
        ], imports
        assert "rpool was imported by /dev/sda4, not /dev/disk/by-id" in out.getvalue()

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
        mock.on("zfs get -H -o value mountpoint rpool/ROOT/ubuntu_x").succeeds("/mnt/zark/chroot")
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
        mock.on("zfs get -H -o value mountpoint rpool/USERDATA").succeeds("/mnt/zark/chroot/home")
        # bpool boot env.
        mock.on("zfs list bpool/BOOT/ubuntu_x").succeeds("bpool/BOOT/ubuntu_x")
        mock.on("zfs mount bpool/BOOT/ubuntu_x").succeeds()
        mock.on("zfs get -H -o value mountpoint bpool/BOOT/ubuntu_x").succeeds(
            "/mnt/zark/chroot/boot"
        )

        cleanup = Cleanup(make_log())
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch.object(lib_mount, "_mount_in_tree", wraps=lib_mount._mount_in_tree) as each,  # pylint: disable=protected-access
        ):
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
        # No link in their paths: zfs mount, as before (M4d-18).
        assert mock.was_called("zfs mount rpool/USERDATA")
        assert mock.was_called("zfs mount bpool/BOOT/ubuntu_x")
        assert not mock.was_called("mount -t zfs")
        assert [c.args[0] for c in each.call_args_list] == ["rpool/USERDATA", "bpool/BOOT/ubuntu_x"]

    @staticmethod
    def _system_mock(listing: str) -> MockShell:
        """Both pools to import, by-id finding nothing, the hint working."""
        mock = MockShell()
        mock.on("zpool list rpool").fails()
        mock.on("zpool list bpool").fails()
        for pool in ("rpool", "bpool"):
            mock.on_prefix(f"zpool import -N -R /mnt/zark/chroot -d /dev/disk/by-id {pool}").fails()
            mock.on_prefix(
                f"zpool import -f -N -R /mnt/zark/chroot -d /dev/disk/by-id {pool}"
            ).fails()
            mock.on(rf"^zpool import -N .*-d /dev/sda4 {pool}$", regex=True).succeeds()
        mock.on(
            "zfs list -H -o name,mountpoint,used,refer,canmount,type -t filesystem,volume -r rpool",
        ).succeeds(listing)
        mock.on_prefix("zfs mount ").succeeds()
        mock.on("zfs get -H -o value mountpoint rpool/ROOT/ubuntu_x").succeeds("/mnt/zark/chroot")
        mock.on("zfs get -H -o value mountpoint rpool/ROOT/ubuntu_x/var").succeeds(
            "/mnt/zark/chroot/var"
        )
        mock.on("zfs get -H -o value mountpoint rpool/ROOT/zt5b").succeeds("/mnt/zark/chroot")
        mock.on("zfs get -H -o value mountpoint rpool/ROOT/zt5b/var").succeeds(
            "/mnt/zark/chroot/var"
        )
        mock.on("zfs list bpool/BOOT/ubuntu_x").succeeds("bpool/BOOT/ubuntu_x")
        mock.on("zfs get -H -o value mountpoint bpool/BOOT/ubuntu_x").succeeds(
            "/mnt/zark/chroot/boot"
        )
        return mock

    def _mount(self, mock: MockShell, readonly: bool = False) -> str:
        out = StringIO()
        with (
            patch_sh(mock),
            redirect_stdout(out),
            patch.object(lib_mount, "find_system_root_dataset", return_value="rpool/ROOT/ubuntu_x"),
        ):
            res = mount_system_pools(
                "/mnt/zark/chroot",
                "pp",
                make_log(),
                ZFS(make_log()),
                _FakeKeystore(),  # type: ignore[arg-type]
                Cleanup(make_log()),
                readonly=readonly,
                device="/dev/sda4",
            )
        assert res == ("/mnt/zark/chroot", "ubuntu_x")
        return out.getvalue()

    _TWO_BES = (
        "rpool/ROOT\tnone\t1G\t1G\toff\tfilesystem\n"
        "rpool/ROOT/ubuntu_x\t/\t1G\t1G\tnoauto\tfilesystem\n"
        "rpool/ROOT/ubuntu_x/var\t/var\t1G\t1G\ton\tfilesystem\n"
        "rpool/ROOT/zt5b\t/\t1G\t1G\tnoauto\tfilesystem\n"
        "rpool/ROOT/zt5b/var\t/var\t1G\t1G\ton\tfilesystem\n"
    )

    def test_the_device_hint_reaches_bpool_too(self):
        # M5a-12 (M4d plan 15 (1)): bpool went to the scan although the
        # operator named the disk.
        mock = self._system_mock(self._TWO_BES)
        _ = self._mount(mock)
        assert mock.was_called("zpool import -N -R /mnt/zark/chroot -d /dev/sda4 bpool"), mock.calls

    def test_other_boot_environments_are_skipped_and_said_once(self):
        # M4dR-5: their root has mountpoint=/, which resolves to this tree's
        # root and warned on every command.
        for readonly in (False, True):
            mock = self._system_mock(self._TWO_BES)
            out = self._mount(mock, readonly)
            assert "resolves to the root" not in out and "zt5b not mounted" not in out, out
            assert "Other boot environments are not mounted: zt5b" in out, out
            assert not mock.was_called("zfs mount rpool/ROOT/zt5b"), mock.calls
            assert mock.was_called("zfs mount rpool/ROOT/ubuntu_x/var")  # the root's own child
            assert mock.was_called("zfs mount bpool/BOOT/ubuntu_x")

    def test_a_link_in_the_installed_tree_is_resolved_inside_it(self):
        # M4c plan 14 (1): libzfs mounts at altroot + mountpoint, and the
        # kernel follows the tree's /var/run -> /run onto the live's /run.
        with tempfile.TemporaryDirectory() as td:
            alt = os.path.realpath(td)
            os.makedirs(f"{alt}/run")
            os.makedirs(f"{alt}/var")
            os.symlink("/run", f"{alt}/var/run")
            for readonly in (True, False):
                mock = MockShell()
                mock.on("zfs get -H -o value mountpoint rpool/x").succeeds(f"{alt}/var/run/x")
                mock.on_prefix("mount -t zfs").succeeds()
                mock.on_prefix("mkdir -p").succeeds()
                out = StringIO()
                with patch_sh(mock), redirect_stdout(out):
                    got = lib_mount._mount_in_tree(  # pylint: disable=protected-access
                        "rpool/x", alt, ZFS(make_log()), make_log(), readonly
                    )
                assert mock.was_not_called("zfs mount"), mock.calls
                if readonly:
                    assert got == f"{alt}/run/x"
                    assert mock.calls[-1] == f"mount -t zfs -o ro,zfsutil rpool/x {alt}/run/x"
                else:  # the host's realpath leaves the tree: refused
                    assert got == "" and mock.was_not_called("mount -t zfs"), mock.calls
                    assert (
                        f"rpool/x not mounted: {alt}/var/run/x resolves to /run/x" in out.getvalue()
                    )
            os.symlink("run", f"{alt}/lnk")  # a relative link stays in the tree
            mock = MockShell()
            mock.on("zfs get -H -o value mountpoint rpool/y").succeeds(f"{alt}/lnk/y")
            mock.on_prefix("mount -t zfs").succeeds()
            mock.on_prefix("mkdir -p").succeeds()
            with patch_sh(mock), redirect_stdout(StringIO()):
                got = lib_mount._mount_in_tree(  # pylint: disable=protected-access
                    "rpool/y", alt, ZFS(make_log()), make_log(), False
                )
        assert got == f"{alt}/run/y"
        assert mock.calls[-2:] == [
            f"mkdir -p {alt}/run/y",
            f"mount -t zfs -o zfsutil rpool/y {alt}/run/y",
        ]

    def test_read_only_imports_readonly_and_lists_what_cannot_mount(self):
        # M4c-21 (R2§8.6): nothing written to the installed pools, no remount pass.
        mock = MockShell()
        mock.on("zpool list rpool").fails()
        mock.on("zpool list bpool").fails()
        ro = "zpool import -N -o readonly=on -R /mnt/zark/system -d /dev/disk/by-id"
        mock.on(f"{ro} rpool").succeeds()
        mock.on(f"{ro} bpool").succeeds()
        mock.on("zfs list -H -o name -r rpool/ROOT").succeeds("rpool/ROOT\nrpool/ROOT/ubuntu_x\n")
        mock.on("zfs mount rpool/ROOT/ubuntu_x").succeeds()
        mock.on("zfs get -H -o value mountpoint rpool/ROOT/ubuntu_x").succeeds("/mnt/zark/system")
        mock.on(
            "zfs list -H -o name,mountpoint,used,refer,canmount,type -t filesystem,volume -r rpool",
        ).succeeds(
            "rpool/ROOT/ubuntu_x\t/\t1G\t1G\tnoauto\tfilesystem\n"
            "rpool/USERDATA\t/home\t1G\t1G\ton\tfilesystem\n"
            "rpool/srv\t/srv\t1G\t1G\ton\tfilesystem\n",
        )
        mock.on("zfs mount rpool/USERDATA").succeeds()
        mock.on("zfs get -H -o value mountpoint rpool/USERDATA").succeeds("/mnt/zark/system/home")
        mock.on("zfs mount rpool/srv").fails(
            "cannot mount '/mnt/zark/system/srv': Read-only file system"
        )
        mock.on("zfs get -H -o value mountpoint rpool/srv").succeeds("/mnt/zark/system/srv")
        mock.on("zfs list bpool/BOOT/ubuntu_x").succeeds("bpool/BOOT/ubuntu_x")
        mock.on("zfs mount bpool/BOOT/ubuntu_x").succeeds()
        mock.on("zfs get -H -o value mountpoint bpool/BOOT/ubuntu_x").succeeds(
            "/mnt/zark/system/boot"
        )
        ks = _FakeKeystore()
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            res = mount_system_pools(
                "/mnt/zark/system",
                "pp",
                make_log(),
                ZFS(make_log()),
                ks,
                Cleanup(make_log()),
                readonly=True,
            )
        assert res == ("/mnt/zark/system", "ubuntu_x")
        imports = [c for c in mock.calls if c.startswith("zpool import")]
        assert imports == [f"{ro} rpool", f"{ro} bpool"]
        assert not [c for c in mock.calls if "remount" in c]
        assert "Not mounted (no mountpoint directory" in out.getvalue()
        assert "rpool/srv" in out.getvalue()
        assert ks.readonly_calls == [True]


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
        mock.on("findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
            "/ overlay overlay\n"
            "/mnt/zark/system zfs rpool/ROOT/be\n"
            "/mnt/zark/system/boot zfs bpool/BOOT/be\n"
            "/mnt/zark/system/proc proc proc\n"
            "/mnt/zark/system/run tmpfs tmpfs\n"
            "/mnt/zark/system/run/user/1000 tmpfs tmpfs\n"
            "/mnt/zark/system/home zfs rpool/USERDATA/home\n"
            "/mnt/zark/system/srv zfs data/srv\n"
            "/mnt/zark/systemx tmpfs tmpfs\n"
            "/mnt/zark/backup zfs backup/rpool/ROOT/be\n"
            "/mnt/zark/backup/proc proc proc",
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
        # W-2: a host ZFS directory bound under the tree (data/srv, not one
        # of the pools being released) is a bind, made private like the rest.
        assert tree == [
            "mount --make-rprivate /mnt/zark/system/proc",
            "umount -R /mnt/zark/system/proc",
            "mount --make-rprivate /mnt/zark/system/run",
            "umount -R /mnt/zark/system/run",
            "mount --make-rprivate /mnt/zark/system/srv",
            "umount -R /mnt/zark/system/srv",
            "umount -R /mnt/zark/system",
        ]
        assert mock.was_not_called("zfs unmount")

    def test_a_failed_export_is_not_reported_as_unmounted(self):
        # K6-1 (eli): "SYSTEM UNMOUNTED - exported cleanly" printed under two
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
        assert "exported cleanly" not in out, out

    def test_copies_held_elsewhere_are_released_then_the_system_export_retried(self):
        # W-1 (M4b-3 3b; mutation U7): umount local, like umount of a backup,
        # releases the copies another namespace keeps and exports again.
        ns = "nsenter --mount=/proc/42/ns/mnt"
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool get -H -o value altroot rpool").succeeds("/mnt/zark/system")
        mock.on("zpool list bpool").succeeds("bpool")
        mock.on("findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
            "/ overlay overlay\n"
            "/mnt/zark/system zfs rpool/ROOT/be\n"
            "/mnt/zark/system/boot zfs bpool/BOOT/be"
        )
        listing = mock.on(f"{ns} findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
            "/ overlay overlay\n"
            "/mnt/zark/system zfs rpool/ROOT/be\n"
            "/mnt/zark/system/boot zfs bpool/BOOT/be"
        )
        export = mock.on_prefix("zpool export").fails("pool is busy")
        mock.on(f"{ns} umount -R /mnt/zark/system").succeeds()
        real = mock.mock_run

        def run_hook(cmd: Any, *a: Any, **k: Any) -> RunResult:
            r = real(cmd, *a, **k)
            if str(cmd) == f"{ns} umount -R /mnt/zark/system":
                _ = listing.succeeds("/ overlay overlay")
                _ = export.succeeds()
            return r

        mock.mock_run = run_hook  # type: ignore[method-assign]
        buf = StringIO()
        with (
            patch_sh(mock),
            patch.object(umount_mod, "_namespaces", return_value=["/proc/42/ns/mnt"]),
            patch.object(umount_mod, "flush_device_cache"),
            redirect_stdout(buf),
        ):
            _umount_local_system(make_log())
        assert mock.call_count(f"{ns} umount -R /mnt/zark/system") == 1, mock.calls
        assert mock.call_count("zpool export bpool") == 2, mock.calls
        assert mock.call_count("zpool export rpool") == 2, mock.calls
        assert "SYSTEM UNMOUNTED" in buf.getvalue() and "NOT UNMOUNTED" not in buf.getvalue()

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


class TestChrootClose:  # pylint: disable=missing-function-docstring
    """M4bR-8 (M4d-26): CHROOT CLOSED only when both pools were exported."""

    @staticmethod
    def _run(rpool_export_ok: bool) -> tuple[str, int]:
        mock = MockShell()
        mock.on_prefix("zpool import").succeeds("")
        mock.on("zpool export bpool").succeeds()
        export = mock.on("zpool export rpool")
        _ = export.succeeds() if rpool_export_ok else export.fails("pool is busy")
        mock.on("zpool export -f rpool").fails("pool is busy")
        state = {"shell": False}

        def mount(_alt, _pw, _log, _zfs, _ks, cleanup, **_k):
            cleanup.track_pool("rpool")
            cleanup.track_pool("bpool")
            return ("/mnt/zark/chroot", "ubuntu_x")

        def shell(_argv: list[str]) -> int:
            state["shell"] = True
            return 0

        def exists(pool: str) -> bool:  # after the shell, a busy rpool stays
            return state["shell"] and pool == "rpool" and not rpool_export_ok

        out = StringIO()
        rc = 0
        with (
            patch_sh(mock),
            patch.object(chroot_mod.Cleanup, "register"),
            patch.object(chroot_mod.sh, "is_live_usb", return_value=True),
            patch.object(chroot_mod, "mount_system_pools", side_effect=mount),
            patch.object(chroot_mod, "_setup_bind_mounts"),
            patch.object(chroot_mod.signals, "run_interactive", side_effect=shell),
            patch.object(ZFS, "pool_exists", side_effect=exists),
            patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
            redirect_stdout(out),
        ):
            try:
                chroot_mod.run([])
            except SystemExit as e:
                rc = int(e.code) if isinstance(e.code, int) else 1
        return unframed(out.getvalue()), rc

    def test_a_pool_left_imported_is_not_closed(self):
        out, rc = self._run(rpool_export_ok=False)
        assert rc == 1 and "CHROOT NOT CLOSED" in out, out
        assert "rpool is still imported - run: sudo zpool export rpool" in out, out
        assert "exported cleanly" not in out, out

    def test_both_exported_is_closed(self):
        out, rc = self._run(rpool_export_ok=True)
        assert rc == 0 and "CHROOT CLOSED" in out and "NOT CLOSED" not in out, out


class TestStopLine:  # pylint: disable=missing-function-docstring
    """M4bX.6 (M4d-26): the stop line says when nothing is left to clean up."""

    @staticmethod
    def _line(running: list[str], pending: list[Callable[[], bool]]) -> str:
        out = StringIO()
        with (
            patch.object(_signals, "_running", running),
            patch.object(_signals, "_pending", pending),
            patch.object(_signals, "_received", []),
            patch.object(_signals, "_log", [make_log()]),
            patch.object(_signals.signal, "signal"),
            redirect_stdout(out),
        ):
            try:
                _signals._on_signal(signal.SIGINT, None)  # pylint: disable=protected-access
            except KeyboardInterrupt:
                pass
        return out.getvalue()

    def test_the_line_by_what_is_left(self):
        idle = make_log()
        empty = Cleanup(idle)
        busy = Cleanup(idle)
        busy.track_pool("backup")
        line = self._line([], [empty.pending])
        assert "Interrupted by SIGINT: stopping, nothing left to clean up" in line, line
        assert "cleaning up (further" in self._line([], [empty.pending, busy.pending])
        assert "stopping zfs send (further" in self._line(["zfs send"], [empty.pending])
        assert "stopping zfs send, then cleaning up" in self._line(["zfs send"], [busy.pending])
        assert "SIGINT: cleaning up" in self._line([], [])  # no Cleanup known: as before

    def test_a_registered_cleanup_is_asked(self):
        cleanup = Cleanup(make_log())
        with patch.object(_signals, "_pending", []), patch("lib.cleanup.atexit.register"):
            cleanup.register()
            assert cleanup.pending in _signals._pending  # pylint: disable=protected-access


class TestAptGuard:  # pylint: disable=missing-function-docstring
    """The standalone apt/dpkg backup guard installed by setup/recover/finish.

    Covers the Python installer (writes three files, correct perms, overwrite
    semantics) and the cheap exit paths of the shell hook that can run without
    zpool/dpkg-deb present (the ZARK_INTERNAL escape and the no-sensitive-
    package short-circuit). The block path - external pool + boot-critical
    package - needs a real importable pool and is exercised by integration
    testing, not CI; here it is covered by content assertions on the script.
    """

    def test_remove_takes_its_files_the_config_first(self):
        """M3P.2: the three go, and /usr/local/lib/zark when it is left empty."""
        with tempfile.TemporaryDirectory() as td:
            apt_guard.install(target_root=td, log=make_log())
            hook = Path(td) / apt_guard.HOOK_RELATIVE_PATH
            assert not apt_guard.remove(td, make_log())
            for rel in (
                apt_guard.HOOK_RELATIVE_PATH,
                apt_guard.APT_CONF_RELATIVE_PATH,
                apt_guard.MOTD_RELATIVE_PATH,
            ):
                assert not (Path(td) / rel).exists(), rel
            assert not hook.parent.exists()

    def test_remove_takes_an_older_zarks_copy_and_leaves_the_rest(self):
        """An older zark's hook (its header and mark) goes; an edited APT
        config stays, and so does the hook it names (APT would fail on a
        missing hook); a MOTD script that is not zark's stays."""
        with tempfile.TemporaryDirectory() as td:
            apt_guard.install(target_root=td, log=make_log())
            hook = Path(td) / apt_guard.HOOK_RELATIVE_PATH
            conf = Path(td) / apt_guard.APT_CONF_RELATIVE_PATH
            motd = Path(td) / apt_guard.MOTD_RELATIVE_PATH
            _ = hook.write_text(
                "#!/bin/sh\n# apt-zfs-backup-guard - an older text\n# Installed by zark.\n",
                encoding="utf-8",
            )
            _ = conf.write_text(apt_guard.APT_CONF + 'APT::Other "1";\n', encoding="utf-8")
            _ = motd.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
            left = apt_guard.remove(td, make_log())
            assert left == [str(conf), str(hook), str(motd)], left
            _ = conf.write_text("// zark\n" + apt_guard.APT_CONF, encoding="utf-8")
            assert apt_guard.remove(td, make_log()) == [str(motd)]
            assert not hook.exists() and not conf.exists()

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


class TestSetupSnapshotsAndBootEnvironment:  # pylint: disable=missing-function-docstring
    """P0-1 and M1P.21 (M4d-23): both pools counted; no placeholder dataset."""

    @staticmethod
    def _mock(rpool: str, bpool: str) -> MockShell:
        mock = MockShell()
        mock.on("zpool list rpool").succeeds("rpool")
        mock.on("zpool list bpool").succeeds("bpool")
        mock.on("zfs list -H -o name -t snapshot -r rpool").succeeds(rpool)
        mock.on("zfs list -H -o name -t snapshot -r bpool").succeeds(bpool)
        return mock

    def test_both_pools_by_the_snapshots_name(self):
        rpool = "rpool/ROOT/u@autosnap_2026-10-19_00:00:01_daily\nrpool/autosnap_x@manual\n"
        bpool = "bpool/BOOT/u@autosnap_2026-10-19_00:00:01_daily\n"
        out = StringIO()
        with patch_sh(self._mock(rpool, bpool)), redirect_stdout(out):
            setup_mod._initial_snapshots(ZFS(make_log()), make_log())  # pylint: disable=protected-access
        assert "2 sanoid snapshots exist (rpool 1, bpool 1)" in out.getvalue()

    def test_the_first_ones_are_counted_on_both_pools(self):
        mock = self._mock("", "")
        taken = {"done": False}

        def spy(cmd: str, **kw: Any) -> RunResult:
            if cmd == "sanoid --take-snapshots":
                taken["done"] = True
                return RunResult(0, "", "", cmd)
            if taken["done"] and cmd.startswith("zfs list -H -o name -t snapshot -r "):
                pool = cmd.rsplit(" ", 1)[1]
                n = 91 if pool == "rpool" else 11
                return RunResult(
                    0, "".join(f"{pool}/d@autosnap_{i}_daily\n" for i in range(n)), "", cmd
                )
            return real(cmd, **kw)

        real = mock.mock_run
        out = StringIO()
        with (
            patch.object(mock, "mock_run", side_effect=spy),
            patch_sh(mock),
            patch("builtins.input", return_value="y"),
            redirect_stdout(out),
        ):
            setup_mod._initial_snapshots(ZFS(make_log()), make_log())  # pylint: disable=protected-access
        assert "Created 102 snapshots (rpool 91, bpool 11)" in out.getvalue()

    def test_no_boot_environment_is_fatal_not_a_prompt(self):
        # M1P.21: Enter took the placeholder ubuntu_XXXXXX, which names nothing.
        mock = MockShell()
        mock.on_prefix("which ").succeeds("/usr/bin/x")
        out = StringIO()
        with (
            patch_sh(mock),
            patch.object(setup_mod.certificates, "show_warnings"),
            patch.object(setup_mod, "_detect_ubuntu_ds", return_value=""),
            patch.object(setup_mod, "_discover_rules") as discover,
            patch("builtins.input", return_value="") as typed,
            redirect_stdout(out),
            redirect_stderr(StringIO()),
        ):
            try:
                setup_mod.run([])
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("setup went on")
        assert "No boot environment under rpool/ROOT" in unframed(out.getvalue())
        discover.assert_not_called()
        assert typed.call_count == 1  # only the FATAL's "Press Enter"


class TestOnlyThePackagesZarkUses:  # pylint: disable=missing-function-docstring
    """M2P.2, M2P.3 (M4d-30): no syncoid-era packages in setup or recover."""

    def test_setup_checks_only_what_zark_runs(self):
        assert set(setup_mod.DEPS) == {"zfs", "zpool", "sgdisk", "sanoid"}
        assert not {"syncoid", "bc", "pv", "mbuffer", "lzop"} & set(setup_mod.DEPS)

    def test_recover_installs_the_packages_of_the_missing_tools_only(self):
        mock = MockShell()
        for tool in ("zfs", "zgenhostid", "sgdisk", "cryptsetup"):
            mock.on(f"which {tool}").succeeds(f"/usr/sbin/{tool}")
        mock.on("which mkfs.vfat").fails()
        mock.on("which partprobe").fails()
        mock.on_prefix("apt-get install").succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            recover_mod._install_missing_tools(make_log())  # pylint: disable=protected-access
        assert [c for c in mock.calls if c.startswith("apt-get")] == [
            "apt-get install -y dosfstools parted"
        ]
        mock = MockShell()
        mock.on_prefix("which ").succeeds("/usr/bin/x")
        with patch_sh(mock), redirect_stdout(StringIO()):
            recover_mod._install_missing_tools(make_log())  # pylint: disable=protected-access
        assert mock.was_not_called("apt-get")


class TestSetupInstallsBothGuards:  # pylint: disable=missing-function-docstring
    """M4a-4: setup brings both guards up to date on the running system."""

    def test_setup_installs_both_guards_overwriting(self):
        with (
            patch.object(setup_mod.apt_guard, "install") as apt,
            patch.object(setup_mod.grub_guard, "install") as grub,
        ):
            setup_mod._install_guards(make_log())  # pylint: disable=protected-access
        apt.assert_called_once_with(target_root="", log=ANY)
        grub.assert_called_once_with(target_root="", log=ANY)

    def test_grub_guard_remove_takes_only_zarks_guard(self):
        """M3P.2: this zark's guard and an older zark's go; another script at
        the same path stays and is named."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / grub_guard.GUARD_RELATIVE_PATH
            grub_guard.install(td, make_log())
            assert not grub_guard.remove(td, make_log()) and not path.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(
                "#!/bin/sh\n# 09_zfs_backup_guard - older\n# Installed by zark.\n",
                encoding="utf-8",
            )
            assert not grub_guard.remove(td, make_log()) and not path.exists()
            _ = path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            assert grub_guard.remove(td, make_log()) == [str(path)] and path.exists()

    def test_grub_guard_rewrites_a_stale_copy_and_leaves_a_current_one(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td, grub_guard.GUARD_RELATIVE_PATH)
            path.parent.mkdir(parents=True)
            path.write_text("#!/bin/sh\nold guard\n", encoding="utf-8")
            grub_guard.install(target_root=td, log=make_log())
            assert path.read_text(encoding="utf-8") == grub_guard.GUARD_SCRIPT
            os.utime(path, (1, 1))
            grub_guard.install(target_root=td, log=make_log())
            assert path.stat().st_mtime == 1


class TestGuardsSeeImportedBackups:  # pylint: disable=missing-function-docstring
    """H18 / D3 (M4a-3): an imported backup pool blocks boot-critical updates too.

    The scripts run under sh with zpool, zfs and dpkg-deb stubbed; their PATH
    line is pointed at the stubs, so the machine's own pools are never read.
    """

    _DEB = "/var/cache/apt/archives/linux-image-7.0.0-99-generic_1_amd64.deb\n"

    @staticmethod
    def _stubs(td: str, importable: str, imported: str, zark_format: str) -> str:
        stub = Path(td, "stub")
        stub.mkdir()
        scripts = {
            "zpool": (
                "#!/bin/sh\n"
                'case "$1" in\n'
                f"  import) printf '{importable}' ;;\n"
                f"  list) printf '{imported}' ;;\n"
                "esac\n"
            ),
            "zfs": f"#!/bin/sh\nprintf '{zark_format}\\n'\n",
            "dpkg-deb": '#!/bin/sh\nbasename "$2" | cut -d_ -f1\n',
        }
        for name, body in scripts.items():
            Path(stub, name).write_text(body, encoding="ascii")
            Path(stub, name).chmod(0o755)
        return str(stub)

    @staticmethod
    def _script(td: str, text: str, stub: str) -> str:
        path = Path(td, "script")
        path.write_text(
            text.replace("PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH", f"PATH={stub}:/usr/bin:/bin"),
            encoding="utf-8",
        )
        return str(path)

    def _run(self, text: str, importable: str, imported: str, fmt: str = "-") -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as td:
            stub = self._stubs(td, importable, imported, fmt)
            env = {k: v for k, v in os.environ.items() if k != "ZARK_INTERNAL"}
            env["PATH"] = f"{stub}:/usr/bin:/bin"
            proc = subprocess.run(
                ["sh", self._script(td, text, stub)],
                input=self._DEB,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        return proc.returncode, proc.stdout + proc.stderr

    _SYSTEM = "rpool\\t-\\nbpool\\t-\\n"
    _BACKUP = "rpool\\t-\\nbpool\\t-\\nbackup\\t/run/zark/altroot/backup\\n"
    _TANK = "rpool\\t-\\nbpool\\t-\\ntank\\t-\\n"

    def test_hook_refuses_with_a_backup_imported_under_an_altroot(self):
        rc, out = self._run(apt_guard.HOOK_SCRIPT, "", self._BACKUP)
        assert rc == 1, out
        assert "backup" in out

    def test_hook_refuses_with_a_zark_drive_imported_without_an_altroot(self):
        rc, out = self._run(apt_guard.HOOK_SCRIPT, "", self._TANK, fmt="2")
        assert rc == 1, out

    def test_hook_allows_a_permanent_data_pool(self):
        rc, out = self._run(apt_guard.HOOK_SCRIPT, "", self._TANK)
        assert rc == 0, out

    def test_hook_allows_the_system_pools_alone(self):
        rc, out = self._run(apt_guard.HOOK_SCRIPT, "", self._SYSTEM)
        assert rc == 0, out

    def test_hook_still_refuses_an_importable_pool(self):
        rc, out = self._run(apt_guard.HOOK_SCRIPT, "   pool: black\\n", self._SYSTEM)
        assert rc == 1, out

    def test_motd_names_an_imported_backup(self):
        rc, out = self._run(apt_guard.MOTD_SCRIPT, "", self._BACKUP)
        assert rc == 0, out
        assert "backup" in out

    def test_grub_guard_refuses_with_a_backup_imported(self):
        rc, out = self._run(grub_guard.GUARD_SCRIPT, "", self._BACKUP)
        assert rc == 1, out

    def test_grub_guard_allows_the_system_pools_alone(self):
        rc, out = self._run(grub_guard.GUARD_SCRIPT, "", self._TANK)
        assert rc == 0, out

    def test_motd_names_a_zark_drive_imported_without_an_altroot(self):
        # Mutation G2: the MOTD's org.zark:format branch.
        rc, out = self._run(apt_guard.MOTD_SCRIPT, "", self._TANK, fmt="2")
        assert rc == 0, out
        assert "tank" in out, out

    def test_grub_guard_refuses_a_zark_drive_imported_without_an_altroot(self):
        # Mutation G6: the grub guard's org.zark:format branch.
        rc, out = self._run(grub_guard.GUARD_SCRIPT, "", self._TANK, fmt="2")
        assert rc == 1, out
        assert "tank" in out, out

    def test_the_advice_never_exports_a_pool(self):
        # Mutations G4 and G5: an export of a busy pool can leave /boot unmounted (P6).
        for script, want in ((apt_guard.MOTD_SCRIPT, 0), (grub_guard.GUARD_SCRIPT, 1)):
            rc, out = self._run(script, "", self._BACKUP)
            assert rc == want, out
            assert "backup" in out, out
            assert "zpool export" not in out, out


# ═════════════════════════════════════════════════════════════════════════
#  lib/identity.py and lib/registry.py (M1: I20-I25, hallazgo 7)
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
            exists_only("/dev/disk/by-id/"),
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
            exists_only("/dev/disk/by-id/"),
        ):
            ident = resolve_disk("/dev/sdb1", allow_partition=True)
        assert ident.disk == "/dev/sdb"

    def test_resolve_refuses_a_disk_with_no_medium(self):
        # M1P.12: a card reader with no card was accepted until zpool create.
        mock = _identity_mock()
        with tempfile.TemporaryDirectory() as sysfs:
            Path(sysfs, "sdb").mkdir()
            Path(sysfs, "sdb", "size").write_text("0\n", encoding="ascii")
            with (
                patch_sh(mock),
                patch("lib.identity.SYS_BLOCK", sysfs, create=True),
                patch("lib.identity.os.path.realpath", return_value="/dev/sdb"),
            ):
                try:
                    resolve_disk(f"/dev/disk/by-id/{_KINGSTON_ID}")
                except IdentityError as e:
                    assert "no medium" in str(e)
                    return
        raise AssertionError("expected IdentityError")

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
        assert mock.was_not_called("zpool import -o readonly=on")  # the verification import

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
        mock.on("/usr/lib/udev/zvol_id /dev/zd0").succeeds("backup/keystore")
        mock.on("cryptsetup open --readonly /dev/zd0 zark_ks_backup").succeeds()
        mock.on("mount -o ro,noload /dev/mapper/zark_ks_backup /run/keystore/rpool").succeeds()
        ks = Keystore(make_log())
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch("lib.keystore.Path.mkdir"),
            exists_only("/run/keystore/rpool/system.key"),
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


class TestForensics:  # pylint: disable=missing-function-docstring
    """I7 (M4a-9): a pool's state is kept before anything changes it."""

    def test_capture_writes_every_section_privately_next_to_the_log(self):
        mock = MockShell()
        mock.on("zpool status -vP black").succeeds("state: DEGRADED\nerrors: 3 data errors")
        mock.on("zpool events -v black").fails("no such pool")
        mock.on_prefix("zpool history -il black").succeeds("2026-09-25 receive")
        mock.on("command -v journalctl").succeeds("/usr/bin/journalctl")
        mock.on_prefix("journalctl -k").succeeds("kernel line")
        mock.on_prefix("lsblk").succeeds("sda 1.8T")
        with patch_sh(mock), redirect_stdout(StringIO()):
            path = forensics.capture("black", "pre-check: DEGRADED", make_log())
        try:
            assert os.path.dirname(path) == os.path.dirname(Log.default_file())
            assert os.path.basename(path).startswith("zark-forensics-black-")
            assert os.stat(path).st_mode & 0o777 == 0o600
            text = Path(path).read_text(encoding="utf-8")
        finally:
            os.unlink(path)
        assert "reason: pre-check: DEGRADED" in text
        assert "3 data errors" in text
        assert "$ zpool events -v black  (exit 1)" in text and "no such pool" in text
        assert "zpool history -il black | tail -n 200" in text
        assert "kernel line" in text and "sda 1.8T" in text

    def test_readback_captures_while_the_pool_is_still_imported(self):
        dev = "/dev/disk/by-id/usb-Micron_X_SERIAL-0:0-part1"
        mock = MockShell()
        mock.on(
            f"zpool import -o readonly=on -N -R /run/zark/altroot/black -d {dev} black"
        ).succeeds()
        mock.on("zpool list -H -o health black").succeeds("DEGRADED")
        mock.on("zpool status").succeeds("errors: 3 data errors")
        mock.on("zpool export black").succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            _ = ZFS(make_log()).verify_exported_pool_readback("black", device=dev)
        calls = mock.calls
        assert "zpool status -vP black" in calls, calls
        assert calls.index("zpool status -vP black") < calls.index("zpool export black")

    def test_readback_captures_when_it_cannot_re_import(self):
        # Mutation F3: the pool's state is kept when the re-import fails too.
        dev = "/dev/disk/by-id/usb-Micron_X_SERIAL-0:0-part1"
        mock = MockShell()
        mock.on(f"zpool import -o readonly=on -N -R /run/zark/altroot/black -d {dev} black").fails(
            "cannot import 'black': I/O error"
        )
        mock.on_prefix("zpool status -vP black").fails("no such pool")
        with patch_sh(mock), redirect_stdout(StringIO()):
            rb = ZFS(make_log()).verify_exported_pool_readback("black", device=dev)
        assert not rb.imported
        assert mock.was_called("zpool status -vP black"), mock.calls


_STATUS_HEAD = """  pool: black
 state: {state}
config:

\tNAME                                        STATE     READ WRITE CKSUM
\tblack                                       {state}     0     0     0
\t  /dev/disk/by-id/usb-Micron_X_SERIAL-0:0-part1  {state}     0     0    {cksum}

errors: {errors}
"""


def _damaged_status(state: str, cksum: int, objects: int) -> str:
    errors = "No known data errors"
    if objects:
        errors = "Permanent errors have been detected in the following files:\n\n"
        errors += "\n".join(f"        black/home@zark-1:/file{i}" for i in range(objects))
    return _STATUS_HEAD.format(state=state, cksum=cksum, errors=errors)


class TestBackupHealthCheck:  # pylint: disable=missing-function-docstring,protected-access
    """I8 (M4a-10): counters and objects are shown before the question."""

    def _check(self, health: str, status: str, answer: str = "") -> tuple[str, bool, object]:
        mock = MockShell()
        mock.on("zpool list -H -o health black").succeeds(health)
        mock.on("zpool status -vp black").succeeds(status)
        out = StringIO()
        exited = False
        with (
            patch_sh(mock),
            patch.object(backup_mod.forensics, "capture", return_value="/var/log/f.txt") as cap,
            patch("builtins.input", return_value=answer),
            redirect_stdout(out),
        ):
            try:
                backup_mod._check_health(ZFS(make_log()), "black", make_log())
            except SystemExit:
                exited = True
        return out.getvalue(), exited, cap

    def test_clean_online_passes_without_question_or_capture(self):
        out, exited, cap = self._check("ONLINE", _damaged_status("ONLINE", 0, 0))
        assert not exited and "Continue anyway" not in out
        cap.assert_not_called()  # type: ignore[attr-defined]

    def test_online_with_checksum_errors_asks_and_defaults_to_no(self):
        out, exited, cap = self._check("ONLINE", _damaged_status("ONLINE", 12, 0))
        assert "Continue anyway" in out  # empty answer below: the default is no
        assert "usb-Micron_X_SERIAL-0:0-part1 ONLINE 0 0 12" in out
        assert exited
        cap.assert_called_once()  # type: ignore[attr-defined]

    def test_degraded_shows_first_objects_and_where_the_rest_are(self):
        out, exited, _ = self._check("DEGRADED", _damaged_status("DEGRADED", 3, 25), "y")
        assert not exited
        assert "permanent errors in 25 object(s)" in out
        assert "black/home@zark-1:/file19" in out and "/file20" not in out
        assert "and 5 more in /var/log/f.txt" in out

    def test_faulted_is_captured_and_fatal(self):
        _, exited, cap = self._check("FAULTED", "")
        assert exited
        cap.assert_called_once()  # type: ignore[attr-defined]

    def test_parse_damage_ignores_header_and_clean_vdevs(self):
        damage = parse_damage(_damaged_status("ONLINE", 0, 0))
        assert not damage.any


def _wipe_succeeds(mock: MockShell, failing: str = "") -> None:
    """Every wipe step of purge succeeds and the disk probes blank, except ``failing``."""
    if failing:
        mock.on(failing).fails("injected")
    mock.on_prefix("dd if=/dev/urandom of=/dev/sdb").succeeds()
    mock.on("blockdev --getsize64 /dev/sdb").succeeds(str(64 * 1024**3))
    mock.on_prefix("blockdev --flushbufs").succeeds()
    mock.on("wipefs -a /dev/sdb").succeeds()
    mock.on("sgdisk --zap-all /dev/sdb").succeeds()
    mock.on("lsblk -nr -o NAME,TYPE /dev/sdb").succeeds("sdb disk")
    mock.on("blkid -p /dev/sdb").returns(2)


@contextmanager
def _held_taken(_disk: str, _log: Log) -> Iterator[bool]:
    """purge's disk lock, taken: the tests' disks are not there to lock (M4aV-1)."""
    yield True


_real_held_from_udev = purge_mod._held_from_udev  # pylint: disable=protected-access


class TestPurgeIdentity:  # pylint: disable=missing-function-docstring
    """I21: purge recognises the registered drive and removes its entries."""

    def _run(self, cfg_dir: Path, ident: DiskIdentity, mock: MockShell) -> None:
        with (
            patch_sh(mock),
            patch.object(Config, "default_config_dir", return_value=cfg_dir),
            patch.object(purge_mod, "validate_external_block_device", return_value=ident),
            patch.object(purge_mod, "_held_from_udev", _held_taken),
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
        _wipe_succeeds(mock)
        ident = _purge_ident(PoolLabel("backup", "3446866051930726346"))
        self._run(cfg_dir, ident, mock)
        assert mock.was_called(f"zpool import -N -R /run/zark/altroot/backup -d {vdev} backup")
        assert mock.was_called("zpool destroy backup")
        # I7: the pool's state is kept before it is destroyed.
        assert mock.calls.index("zpool status -vP backup") < mock.calls.index(
            "zpool destroy backup"
        )
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
            patch.object(
                purge_mod, "_wipe", return_value=purge_mod.Wiped([], checks_clean=True)
            ),  # FakeZfs has no disk
            patch("builtins.input", side_effect=["yes", "sdb", "n"]),
            patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
            redirect_stdout(out),
        ):
            purge_mod.run([f"/dev/disk/by-id/{_KINGSTON_ID}"])
        return unframed(out.getvalue())

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
        assert f"{zark_command()} registry forget 1111" in out

    def test_foreign_rpool_label_is_never_imported(self):
        cfg_dir = Path(tempfile.mkdtemp())
        mock = MockShell()
        _wipe_succeeds(mock)
        ident = _purge_ident(PoolLabel("rpool", "99"))
        with patch("lib.log.Log.ask", return_value=True):
            self._run(cfg_dir, ident, mock)
        assert mock.was_not_called("zpool import -o readonly=on")  # the verification import
        assert mock.was_not_called("zpool destroy")
        assert mock.was_called("wipefs -a /dev/sdb")


class TestPurgeIncomplete:  # pylint: disable=missing-function-docstring
    """M4a-13: a failed step or a disk that is not blank keeps the entry and anchors."""

    dropped: list[str]  # the GUIDs whose anchors the last _purge dropped

    def _purge(
        self,
        mock: MockShell,
        part1_exists: bool | Callable[[str], bool] = False,
        entries: dict[str, dict[str, str]] | None = None,
    ) -> tuple[str, int, dict[str, object]]:
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(
            cfg_dir / "known_drives.json",
            entries or {"backup": {"guid": "3446866051930726346", "drive_id": _KINGSTON_ID}},
        )
        mock.on("zpool list backup").fails()
        mock.on_prefix("zpool import -N -R /run/zark/altroot/backup").succeeds()
        ident = _purge_ident(PoolLabel("backup", "3446866051930726346"))
        out = StringIO()
        rc = 0
        # Taken, unless the test's own udev model (_udev) answers for it.
        hold: AbstractContextManager[object] = (
            patch.object(purge_mod, "_held_from_udev", _held_taken)
            if purge_mod._held_from_udev is _real_held_from_udev  # pylint: disable=protected-access
            else nullcontext()
        )
        with (
            patch_sh(mock),
            patch.object(Config, "default_config_dir", return_value=cfg_dir),
            patch.object(purge_mod, "validate_external_block_device", return_value=ident),
            patch.object(purge_mod.forensics, "capture", return_value=""),
            hold,
            patch.object(purge_mod.anchors, "drop_anchors_of", return_value="") as drop,
            # Only part1's presence: a global os.path.exists patch also hides the
            # registry from Path.exists on Python 3.14.
            patch.object(
                purge_mod,
                "_present",
                side_effect=part1_exists if callable(part1_exists) else lambda _p: part1_exists,
            ),
            patch("builtins.input", side_effect=["yes", "sdb", "n"]),
            patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
            redirect_stdout(out),
        ):
            try:
                purge_mod.run([f"/dev/disk/by-id/{_KINGSTON_ID}"])
            except SystemExit as e:
                rc = int(e.code) if isinstance(e.code, int) else 1
        if rc:
            drop.assert_not_called()
        self.dropped = [c.args[0] for c in drop.call_args_list]
        return unframed(out.getvalue()), rc, json.loads((cfg_dir / "known_drives.json").read_text())

    def test_all_steps_and_a_blank_disk_complete(self):
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        _wipe_succeeds(mock)
        out, rc, data = self._purge(mock)
        assert rc == 0 and "DRIVE PURGED" in out and not data
        # M4aV-1 9a: DRIVE PURGED lists every line too.
        purged = out.split("DRIVE PURGED", 1)[1]
        for line in (
            "✓ pool backup destroyed",
            "✓ first 10MB overwritten",
            "✓ last 10MB overwritten",
            "✓ signatures wiped",
            "✓ partition table destroyed",
            "✓ no partition left",
            "✓ no signature found (blkid -p)",
        ):
            assert line in purged, line
        # M4aX.4: the import's altroot goes with the pool.
        destroy = mock.calls.index("zpool destroy backup")
        assert mock.calls.index("rmdir /run/zark/altroot/backup") > destroy, mock.calls

    def test_the_anchors_of_every_entry_removed_go_too(self):
        # M2V-7 (a): an entry matched by drive ID names an older pool's GUID.
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        _wipe_succeeds(mock)
        entries = {
            "backup": {"guid": "3446866051930726346", "drive_id": _KINGSTON_ID},
            "old": {"guid": "222", "drive_id": _KINGSTON_ID},
        }
        _, rc, data = self._purge(mock, entries=entries)
        assert rc == 0 and not data
        assert self.dropped == ["3446866051930726346", "222"], self.dropped

    def test_failing_wipefs_is_incomplete(self):
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        _wipe_succeeds(mock, failing="wipefs -a /dev/sdb")
        out, rc, data = self._purge(mock)
        assert rc == 1 and "PURGE INCOMPLETE" in out
        # M4aF-4: every step is in the banner, done or not, not only the failure.
        for line in (
            "✓ pool backup destroyed",
            "✓ first 10MB overwritten",
            "✓ last 10MB overwritten",
            "✗ wipefs -a failed: injected",
            "✓ partition table destroyed",
            "✓ no partition left",
            "✓ no signature found (blkid -p)",
            "Kept: the registry entries backup and the pool's anchors.",
        ):
            assert line in out, line
        assert f"{Log.Y}⚠  IRREVERSIBLE OPERATION{Log.N}" in out  # M4aF-5
        assert "DRIVE PURGED" not in out
        assert "backup" in data
        assert mock.was_called("sgdisk --zap-all /dev/sdb")  # the rest still runs
        # M4aP.11: the pool was destroyed and every check is clean.
        assert "Purge again to run the failed step." in out
        assert "may still hold the pool" not in out

    def test_a_failed_step_after_a_pool_not_destroyed_may_still_hold_it(self):
        mock = MockShell()
        mock.on_prefix("zpool import -N -R /run/zark/altroot/backup").fails("I/O error")
        _wipe_succeeds(mock, failing="wipefs -a /dev/sdb")
        out, rc, _ = self._purge(mock)
        assert rc == 1 and "- pool backup could not be imported" in out
        assert "The disk may still hold the pool. Purge again, or look:" in out

    def test_a_failed_step_after_part1_went_away_may_still_hold_the_pool(self):
        # The label check did not run, so the checks are not all clean.
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        _wipe_succeeds(mock, failing="wipefs -a /dev/sdb")
        stack, state = self._udev(mock, reset=True)
        with stack:
            out, rc, _ = self._purge(mock, lambda _p: not state["gone"])
        assert rc == 1 and "The disk may still hold the pool. Purge again, or look:" in out

    def test_failing_destroy_writes_nothing(self):
        mock = MockShell()
        mock.on("zpool destroy backup").fails("pool is busy")
        _wipe_succeeds(mock)
        out, rc, data = self._purge(mock)
        assert rc == 1 and "zpool export backup" in out
        assert "✗ zpool destroy backup failed: pool is busy" in out
        assert mock.was_not_called("rmdir"), mock.calls
        assert "- nothing overwritten: the pool is still imported from this disk" in out
        assert mock.was_not_called("dd if=/dev/urandom")
        assert "backup" in data

    @staticmethod
    def _found_read_only(mock: MockShell, export_ok: bool = True) -> None:
        """The pool is already imported read-only when purge starts (a finished
        read-only ``zark mount``); exporting it removes it from ``zpool list``."""
        listed = mock.on("zpool list backup").succeeds()
        mock.on("zpool get -H -o value guid backup").succeeds("3446866051930726346")
        mock.on("zpool get -H -o value readonly backup").succeeds("on")
        export = mock.on("zpool export backup")
        _ = export.succeeds() if export_ok else export.fails("pool is busy")
        real = mock.mock_run

        def spy(cmd: str, **kw: Any) -> RunResult:
            r = real(cmd, **kw)
            if cmd == "zpool export backup" and r.ok:
                _ = listed.fails()
            return r

        mock.mock_run = spy  # type: ignore[method-assign,assignment]

    def test_a_read_only_pool_is_destroyed_read_write(self):
        # M4aR-1: a read-only pool's destroy returns 0 and leaves its labels.
        mock = MockShell()
        self._found_read_only(mock)
        mock.on("zpool destroy backup").succeeds()
        _wipe_succeeds(mock)
        out, rc, data = self._purge(mock)
        calls = mock.calls
        rw = "zpool import -N -R /run/zark/altroot/backup -d "
        imports = [c for c in calls if c.startswith(rw)]
        assert imports and "readonly" not in imports[0], calls
        assert (
            calls.index("zpool export backup")
            < calls.index(imports[0])
            < calls.index("zpool destroy backup")
        ), calls
        assert rc == 0 and "DRIVE PURGED" in out and not data
        assert "exported and imported read-write first" in out

    def test_a_read_only_pool_that_will_not_export_writes_nothing(self):
        mock = MockShell()
        self._found_read_only(mock, export_ok=False)
        _wipe_succeeds(mock)
        out, rc, data = self._purge(mock)
        assert rc == 1 and "could not be exported: pool is busy" in out
        assert "Export it (zpool export backup), then purge again." in out
        assert mock.was_not_called("zpool destroy"), mock.calls
        assert mock.was_not_called("dd if=/dev/urandom"), mock.calls
        assert "backup" in data

    def test_a_read_only_pool_that_will_not_re_import_writes_nothing(self):
        mock = MockShell()
        self._found_read_only(mock)
        mock.on_prefix("zpool import -N -R /run/zark/altroot/backup").fails("I/O error")
        _wipe_succeeds(mock)
        out, rc, data = self._purge(mock)
        assert rc == 1 and "could not be imported read-write to destroy it" in out
        assert "- nothing overwritten: the pool was not destroyed" in out
        assert "Purge again: it imports the pool read-write itself." in out
        assert mock.was_not_called("zpool destroy"), mock.calls
        assert mock.was_not_called("dd if=/dev/urandom"), mock.calls
        assert "backup" in data

    @staticmethod
    def _udev(
        mock: MockShell, locking: bool = True, reset: bool = False
    ) -> tuple[ExitStack, dict[str, int]]:
        """What udevd does on eli (checklist R4, M4aF-14): when a dd closes the
        disk, it re-reads the partition table, and the overwritten
        protective MBR takes part1 away, unless the disk is held with
        LOCK_EX. part1 is gone after wipefs in any case. ``reset``: the
        drive re-enumerates during the first dd, so part1 goes even held."""
        state = {"held": False, "gone": False, "released_at": -1}
        real = mock.mock_run

        def spy(cmd: str, **kw: Any) -> RunResult:
            r = real(cmd, **kw)
            if cmd.startswith("dd if=/dev/urandom of=/dev/sdb") and (reset or not state["held"]):
                state["gone"] = True
            if cmd.startswith("wipefs -a /dev/sdb"):
                state["gone"] = True
            return r

        @contextmanager
        def held(_disk: str, _log: Log) -> Iterator[bool]:
            state["held"] = locking
            try:
                yield locking
            finally:
                state["held"] = False
                state["released_at"] = len(mock.calls)  # calls made while held

        stack = ExitStack()
        _ = stack.enter_context(patch.object(mock, "mock_run", side_effect=spy))
        _ = stack.enter_context(patch.object(purge_mod, "_held_from_udev", held))
        return stack, state

    def test_part1_labels_are_read_after_the_overwrite_before_wipefs(self):
        # M4aR-2, M4aF-14: the read comes after both dd passes, while the disk
        # is held from udev, and before wipefs.
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        mock.on_prefix("zdb -l").fails("failed to unpack label")
        _wipe_succeeds(mock)
        part1 = f"/dev/disk/by-id/{_KINGSTON_ID}-part1"
        stack, state = self._udev(mock)
        with stack:
            out, rc, _ = self._purge(mock, lambda _p: not state["gone"])
        calls = mock.calls
        assert rc == 0, out
        assert calls.count(f"zdb -l {part1}") == 1, calls
        second_dd = max(i for i, c in enumerate(calls) if c.startswith("dd if=/dev/urandom"))
        flush = calls.index(f"blockdev --flushbufs {part1}")
        assert (
            second_dd < flush < calls.index(f"zdb -l {part1}") < calls.index("wipefs -a /dev/sdb")
        )
        # M4aV-3 V3: read while the disk is still held from udev.
        assert calls.index(f"zdb -l {part1}") < state["released_at"], (state, calls)
        assert "No ZFS label on the first partition after the overwrite" in out

    def test_an_unheld_disk_is_not_overwritten(self):
        # M4aV-1 9c: unheld, udev would take part1 at the first dd's close and
        # the label check could not run; nothing is written, and purge again
        # finds part1 and runs it.
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        _wipe_succeeds(mock)
        stack, state = self._udev(mock, locking=False)
        with stack:
            out, rc, data = self._purge(mock, lambda _p: not state["gone"])
        assert rc == 1 and "PURGE INCOMPLETE" in out
        assert "✗ nothing overwritten: /dev/sdb could not be held from udev" in out
        assert not [c for c in mock.calls if c.startswith(("dd ", "wipefs", "sgdisk"))], mock.calls
        assert "backup" in data
        # M4dF-4: the advice names the holder, not something left or a pool.
        assert "Nothing was overwritten: another program holds /dev/sdb" in out, out
        assert "sudo fuser -v /dev/sdb" in out and "Close it and purge again." in out
        assert "Something is left" not in out and "may still hold the pool" not in out

    def test_part1_gone_under_the_lock_is_warned_and_in_drive_purged(self):
        # M4aV-1 9a: a reset during the dd takes part1 even held; the label
        # check did not run, and the success banner says so.
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        _wipe_succeeds(mock)
        stack, state = self._udev(mock, reset=True)
        probed_held: list[int] = []

        def present(_p: str) -> bool:
            probed_held.append(state["held"])
            return not state["gone"]

        with stack:
            out, rc, _ = self._purge(mock, present)
        assert mock.was_not_called("zdb -l"), mock.calls
        line = "labels on the first partition not read: it went away during the overwrite"
        assert rc == 0 and f"- {line}" in out.split("DRIVE PURGED", 1)[1], out
        assert f"[WARN]{Log.N}    Labels on the first partition not read" in out, out
        # had_part1 is read under the lock, not before it.
        assert probed_held and probed_held[0], probed_held

    def test_the_disk_is_held_from_udev_while_the_body_runs(self):
        # udev's re-read takes LOCK_EX|LOCK_NB, its workers LOCK_SH|LOCK_NB.
        with tempfile.NamedTemporaryFile() as disk:
            probe = os.open(disk.name, os.O_RDONLY)
            try:
                with (
                    redirect_stdout(StringIO()),
                    purge_mod._held_from_udev(disk.name, make_log()) as held,  # pylint: disable=protected-access
                ):
                    assert held
                    try:
                        fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        raise AssertionError("udevd could take the lock")
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)  # released on exit
            finally:
                os.close(probe)

    def test_the_wipe_numbers_its_steps_only_for_purge(self):
        # M4cP.4: inside the self-test's [k/8] its steps are unnumbered.
        lines = (
            "Overwriting first 10MB...",
            "Overwriting last 10MB...",
            "Wiping filesystem signatures...",
            "Destroying partition table...",
        )
        for steps in (True, False):
            mock = MockShell()
            _wipe_succeeds(mock)
            out = StringIO()
            with (
                patch_sh(mock),
                patch.object(purge_mod, "_held_from_udev", _held_taken),
                patch.object(purge_mod, "_present", return_value=False),
                redirect_stdout(out),
            ):
                _ = purge_mod._wipe("/dev/sdb", "/dev/sdb1", make_log(), steps=steps)  # pylint: disable=protected-access
            text = out.getvalue()
            for n, line in enumerate(lines, 2):
                assert (f"[{n}/5]{Log.N} {line}" in text) is steps, (steps, line)
                assert (f"[INFO]{Log.N}      {line}" in text) is not steps, (steps, line)

    def test_a_disk_another_program_holds_is_not_held(self):
        # M4aV-3 V6: waited for 10 s, warned, and the caller is told.
        with tempfile.NamedTemporaryFile() as disk:
            other = os.open(disk.name, os.O_RDONLY)
            fcntl.flock(other, fcntl.LOCK_SH)  # another open file description
            out = StringIO()
            try:
                with (
                    patch.object(purge_mod.time, "sleep") as sleep,
                    redirect_stdout(out),
                    purge_mod._held_from_udev(disk.name, make_log()) as held,  # pylint: disable=protected-access
                ):
                    assert not held
            finally:
                os.close(other)
        assert sleep.call_count == 100
        assert f"Cannot lock {disk.name}: another program held it for 10 s" in out.getvalue()

    def test_a_disk_that_cannot_be_opened_is_not_held(self):
        out = StringIO()
        with (
            redirect_stdout(out),
            purge_mod._held_from_udev("/nonexistent/sdz", make_log()) as held,  # pylint: disable=protected-access
        ):
            assert not held
        assert "Cannot open /nonexistent/sdz to hold it from udev" in out.getvalue()

    def test_a_label_left_by_the_overwrite_is_incomplete(self):
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        mock.on_prefix("zdb -l").succeeds(
            "LABEL 0\n    name: 'backup'\n    pool_guid: 3446866051930726346"
        )
        _wipe_succeeds(mock)
        stack, state = self._udev(mock)
        with stack:
            out, rc, data = self._purge(mock, lambda _p: not state["gone"])
        assert rc == 1 and "PURGE INCOMPLETE" in out
        assert "after the overwrite: pool backup (3446866051930726346)" in out
        assert "backup" in data

    def test_label_left_on_part1_is_incomplete(self):
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        mock.on_prefix("zdb -l").succeeds(
            "LABEL 2\n    name: 'backup'\n    pool_guid: 3446866051930726346"
        )
        _wipe_succeeds(mock)
        out, rc, data = self._purge(mock, part1_exists=True)
        assert rc == 1 and "ZFS label still on" in out
        assert "backup" in data

    def test_signature_left_is_incomplete(self):
        mock = MockShell()
        mock.on("zpool destroy backup").succeeds()
        mock.on("blkid -p /dev/sdb").succeeds('/dev/sdb: PTTYPE="gpt"')
        _wipe_succeeds(mock)
        out, rc, data = self._purge(mock)
        assert rc == 1 and "signature still on /dev/sdb" in out
        assert "The disk may still hold the pool. Purge again, or look:" in out
        assert "blkid -p /dev/sdb; lsblk /dev/sdb" in out
        assert "backup" in data


class TestPurgeReportUnregistered:  # pylint: disable=missing-function-docstring
    """M4aF-4: an unregistered blank-label disk has nothing to keep; M4aF-5: the warning."""

    @staticmethod
    def _purge_blank_label(mock: MockShell) -> tuple[str, int]:
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        out = StringIO()
        rc = 0
        with (
            patch_sh(mock),
            patch.object(Config, "default_config_dir", return_value=cfg_dir),
            patch.object(
                purge_mod, "validate_external_block_device", return_value=_purge_ident(None)
            ),
            patch.object(purge_mod, "_present", return_value=False),
            patch.object(purge_mod, "_held_from_udev", _held_taken),
            patch("builtins.input", side_effect=["y", "yes", "sdb", "n"]),
            redirect_stdout(out),
        ):
            try:
                purge_mod.run([f"/dev/disk/by-id/{_KINGSTON_ID}"])
            except SystemExit as e:
                rc = int(e.code) if isinstance(e.code, int) else 1
        return out.getvalue(), rc

    def test_unregistered_disk_without_label_says_nothing_is_kept(self):
        mock = MockShell()
        _wipe_succeeds(mock, failing="wipefs -a /dev/sdb")
        text, rc = self._purge_blank_label(mock)
        assert rc == 1
        assert "- no ZFS label on this disk: no pool to destroy" in text
        assert "Not registered and no pool label: nothing to keep." in text
        assert "anchors" not in text.split("PURGE INCOMPLETE", 1)[1]
        # M4aP.11: no label, every check clean: only the step is to run again.
        assert "Purge again to run the failed step." in text
        assert "may still hold the pool" not in text

    def test_an_unregistered_blank_disk_is_named_by_its_model_and_name(self):
        # M4d-22: the eject prompt named the bare kernel path.
        mock = MockShell()
        _wipe_succeeds(mock)
        text, rc = self._purge_blank_label(mock)
        assert rc == 0 and "Eject drive 'DT microDuo 3C (/dev/sdb)' now?" in text, text

    def test_a_disk_without_label_and_a_signature_left_says_something_is_left(self):
        mock = MockShell()
        mock.on("blkid -p /dev/sdb").succeeds('/dev/sdb: PTTYPE="gpt"')
        _wipe_succeeds(mock)
        text, rc = self._purge_blank_label(mock)
        assert rc == 1
        assert "Something is left on the disk. Purge again, or look:" in text
        assert "may still hold the pool" not in text

    def test_irreversible_subtitle_is_yellow(self):
        out = StringIO()
        with redirect_stdout(out):
            make_log().banner("PURGE BACKUP DRIVE", "⚠  IRREVERSIBLE OPERATION", warn=True)
        assert f"{Log.Y}⚠  IRREVERSIBLE OPERATION{Log.N}" in out.getvalue()


class TestPrepareIdentity:  # pylint: disable=missing-function-docstring
    """I16/I22/I23 + I-G in prepare."""

    asked: list[str]  # the yes/no questions of the last _prepare

    def _prepare(
        self,
        fake: FakeZfs,
        cfg_dir: Path,
        readback: Readback | None = None,
        out: StringIO | None = None,
        **answers: str,
    ) -> int:
        """Run prepare; ``answers`` by the first word of an input question
        (Pool, Days); anything not given takes the default. ``readback`` is
        the drive's read-back (ONLINE when not given); ``out`` gets stdout;
        ``self.asked`` the yes/no questions, each answered yes."""
        answers = {"Pool": "blue", **answers}
        self.asked = []

        def ask(q: str, **_k: object) -> bool:
            self.asked.append(q)
            return True

        ident = _purge_ident(None)
        report = type("R", (), {"has_risk": False, "findings": [], "device": "/dev/sdb"})()
        if readback is None:
            readback = Readback(imported=True, health="ONLINE", reexported=True)
        rc = 0
        with ExitStack() as stack:
            for cm in (
                patch_sh(fake),
                patch.object(Config, "default_config_dir", return_value=cfg_dir),
                patch.object(prepare_mod, "validate_external_block_device", return_value=ident),
                patch.object(prepare_mod, "check_device", return_value=report),
                exists_only("/run/keystore/rpool/system.key"),
                patch.object(ZFS, "verify_exported_pool_readback", return_value=readback),
                patch.object(ZFS, "pool_guid", return_value="777"),
                patch("lib.log.Log.ask", side_effect=ask),
                patch(
                    "lib.log.Log.ask_input",
                    side_effect=lambda q, d="", **_k: answers.get(q.split()[0], d),
                ),
                patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
                patch("builtins.input", return_value=""),
                patch.object(prepare_mod, "prompt_eject_or_attach"),
                patch.object(Cleanup, "register", lambda _self: None),
                redirect_stdout(StringIO() if out is None else out),
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
        # M2V-7 (b): the question says the anchors go too.
        assert (
            "Remove blue from the registry (and their anchors in origin) when preparing?"
            in self.asked
        ), self.asked
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
        assert fake.ds["blue"].props["org.zark:first-point"] == home[0]  # M3P.25
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

    def test_not_verified_prepare_leaves_no_anchors(self):
        """M2V-5 N28: a prepare whose read-back is not clean ends DRIVE NOT
        VERIFIED, unregistered, and drops the anchors its datasets got."""
        fake = _fresh_fake(())
        cfg_dir = Path(tempfile.mkdtemp())
        registry_write_atomic(cfg_dir / "known_drives.json", {})
        bad = Readback(imported=True, health="DEGRADED", status="x", reexported=True)
        out = StringIO()
        assert self._prepare(fake, cfg_dir, readback=bad, out=out) == 1
        assert "DRIVE NOT VERIFIED" in out.getvalue()
        names = [
            n
            for ds, d in fake.ds.items()
            if ds.split("/")[0] in ("rpool", "bpool")
            for n in [b.name for b in d.bookmarks] + fake.names(ds)
        ]
        assert not [n for n in names if repl.anchor_disk(n) == "777"], names
        assert json.loads((cfg_dir / "known_drives.json").read_text()) == {}

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
        with patch.object(drive_marks, "MACHINE_ID_PATH", str(mid)):
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
            exists_only(),
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
        assert "drive GUID 1:" in out and f"{zark_command()} registry forget 1" in out
        assert "drive GUID 2:" not in out
        _ = self._registry(fake, d, ["forget", "1"])
        assert fake.bookmarks("rpool/ROOT/be") == [repl.anchor_name("2", P2)]
        assert list(json.loads((d / "known_drives.json").read_text())) == ["blue"]

    def test_list_tells_points_recover_restored_from_left_ones(self):
        """M2X.6 (M4d-24): a recovered point names no drive and has its
        drive's anchor bookmark, same guid."""
        fake = _fresh_fake(("blue",))
        assert _engine_run(fake, "blue", "2", P1).ok
        bm = next(b for b in fake.ds["rpool/ROOT/be"].bookmarks if b.name.startswith("zark_2_"))
        fake.ds["rpool/ROOT/be"].snaps.append(FSnap(P1, bm.guid, bm.txg, bm.creation))
        fake.ds["rpool/var"].snaps.append(FSnap(P2, "99999", 999, 1))  # a run left it
        d = self._cfg_dir({"blue": {"guid": "2", "drive_id": "b"}})
        out = " ".join(self._registry(fake, d, ["list"]).split())
        assert (
            "1 backup point(s) restored by recover (anchored for blue): the next complete "
            + "backup of a registered drive removes them"
        ) in out, out
        assert "1 backup point(s) left in origin (interrupted runs" in out, out

    def test_a_restored_point_needs_its_anchors_guid_and_no_owner(self):
        anchor = repl.anchor_name("2", P1)
        o = repl.Origin(
            "rpool/a",
            snaps=[repl.Ref(P1, "7", 1, 1)],
            bookmarks=[repl.Ref(anchor, "7", 1, 1)],
        )
        other = repl.Origin(
            "rpool/b", snaps=[repl.Ref(P1, "8", 1, 1)], bookmarks=[repl.Ref(anchor, "9", 1, 1)]
        )
        origin = {"rpool/a": o, "rpool/b": other}
        assert anchors.restored_points(origin) == {f"rpool/a@{P1}": "2"}
        assert not anchors.restored_points(origin, owned={f"rpool/a@{P1}": "2"})

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
        assert "could not remove" in out and f"{zark_command()} registry forget 1" in out
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
            assert choose_point(points, make_log()) is points[-1]
        text = out.getvalue()
        assert "backup point" in text and "carried: hourly" in text

    def test_four_points_ordered_by_creation(self):
        points = restore_points(_stick_snaps(), f"rpool/ROOT/{_BE}")
        assert [_utc(p) for p in points] == ["15:52:42", "16:00:01", "16:12:50", "17:00:01"]
        assert [p.family for p in points] == ["autosnap_", "autosnap_", "syncoid_eli_", "autosnap_"]

    def test_default_is_the_newest(self):
        points = restore_points(_stick_snaps(), f"rpool/ROOT/{_BE}")
        with patch("builtins.input", return_value=""), redirect_stdout(StringIO()):
            assert choose_point(points, make_log()) is points[-1]

    def test_invalid_selection_asks_again(self):
        points = restore_points(_stick_snaps(), f"rpool/ROOT/{_BE}")
        with (
            patch("builtins.input", side_effect=["x", "9", "2"]),
            redirect_stdout(StringIO()),
        ):
            assert choose_point(points, make_log()) is points[1]

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
        patch.object(restore_plan, "_probe_be", return_value=probe),
        patch.object(restore_plan, "_referenced", side_effect=fake_referenced),
        patch.object(
            restore_plan,
            "_empty_root",
            side_effect=lambda full: (empty or {}).get(full.split("@")[0], False),
        ),
    ):
        return build_plan("backup", "/dev/sdb1", _BE, point, snaps, make_log())


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
        label = restore_plan._snapshot_label  # pylint: disable=protected-access
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
            patch.object(restore_plan, "_probe_be", return_value=("", "", "")),
            patch.object(restore_plan, "_referenced", side_effect=referenced),
            patch.object(restore_plan, "_empty_root", side_effect=empty_root),
            redirect_stdout(StringIO()),
        ):
            plan = build_plan("backup", "/dev/sdb1", _BE, point, snaps, make_log())
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


class TestRecoverMonthlyBase:  # pylint: disable=missing-function-docstring
    """M3P.13 (M4b-6 6a): each dataset starts from its newest monthly before
    the point, so origin shares it with the drives in rotation."""

    P2 = "zark_2026-10-08_10:00:00Z"
    MONTHLY = "autosnap_2026-09-01_00:00:00_monthly"

    def _plan(self) -> tuple[RestorePlan, str]:
        be = f"rpool/ROOT/{_BE}"
        t0, t1, t2 = 1_756_684_800, 1_759_312_800, 1_759_917_600
        lines = [
            f"backup/{be}@autosnap_2026-08-01_00:00:00_monthly\t1\t1\t{t0 - 2_678_400}",
            f"backup/{be}@{self.MONTHLY}\t2\t2\t{t0}",
            f"backup/{be}@autosnap_2026-10-01_00:00:00_daily\t5\t5\t{t1}",
            f"backup/{be}@{self.P2}\t6\t6\t{t2}",
            f"backup/{be}/var@{self.P2}\t7\t7\t{t2}",
        ]
        snaps = parse_snapshots(lines, "backup")
        point = next(
            p for p in restore_points(snaps, be) if any(x.name == self.P2 for x in p.members[be])
        )
        mock = MockShell()
        mock.on("zfs list -Hp -o name,type -r backup/rpool").succeeds(
            "\n".join(f"backup/{n}\tfilesystem" for n in ("rpool", "rpool/ROOT", be, f"{be}/var"))
        )
        mock.on("zfs list -Hp -o name,type -r backup/bpool").fails()
        mock.on_prefix("zfs get -Hp -s local -o name,property,value org.zark").succeeds("")
        mock.on("zfs list -Hp -t snapshot -o name,createtxg -s createtxg backup/keystore").succeeds(
            "backup/keystore@prepare_20261001_100000\t9",
        )
        mock.on(f"zfs get -Hp -o value written@{self.MONTHLY} backup/{be}@{self.P2}").succeeds(
            "300\n"
        )
        out = StringIO()
        with (
            patch_sh(mock),
            patch.object(restore_plan, "_probe_be", return_value=("", "", "")),
            patch.object(
                restore_plan, "_referenced", side_effect=lambda names: dict.fromkeys(names, 1000)
            ),
            patch.object(restore_plan, "_empty_root", return_value=False),
            redirect_stdout(out),
        ):
            plan = build_plan("backup", "/dev/sdb1", _BE, point, snaps, Log())
        return plan, out.getvalue()

    def test_the_newest_monthly_before_the_point_is_the_base(self):
        plan, out = self._plan()
        be = plan.row(f"rpool/ROOT/{_BE}")
        var = plan.row(f"rpool/ROOT/{_BE}/var")
        assert be is not None and be.base is not None and be.base.name == self.MONTHLY
        assert be.base_bytes == 300  # monthly 1000 + written since 300 - point 1000
        assert var is not None and var.base is None
        assert "1 dataset(s) restored from the point only" in out
        assert plan.rpool_bytes == 1000 + 300 + 1000 + plan.keystore_bytes

    def test_the_base_is_sent_in_full_then_the_point_from_it(self):
        plan, _ = self._plan()
        row = plan.row(f"rpool/ROOT/{_BE}")
        assert row is not None
        pipes: list[str] = []
        mock = MockShell()

        def pipe(cmd1: str, _cmd2: str, _log: object = None) -> RunResult:
            pipes.append(cmd1)
            return RunResult(0, "", "", cmd1)

        mock.mock_run_pipe = pipe  # type: ignore[method-assign,assignment]
        with patch_sh(mock), redirect_stdout(StringIO()):
            got = recover_mod._receive_row("backup", row, raw=True, log=Log())  # pylint: disable=protected-access
        assert got == recover_mod.Received(row, "")
        be = f"backup/rpool/ROOT/{_BE}"
        assert pipes == [
            f"zfs send -w {be}@{self.MONTHLY}",
            f"zfs send -w -i {be}@{self.MONTHLY} {be}@{self.P2}",
        ]

    def test_a_point_that_fails_after_its_base_names_where_it_was_left(self):
        # M4bR-6, M4c-13: the dataset is at the monthly, not "receive failed".
        plan, _ = self._plan()
        row = plan.row(f"rpool/ROOT/{_BE}")
        assert row is not None
        results = iter([RunResult(0, "", "", "a"), RunResult(1, "", "boom", "b")])
        mock = MockShell()
        mock.mock_run_pipe = (  # type: ignore[method-assign,assignment]
            lambda *_a, **_k: next(results)
        )
        with patch_sh(mock), redirect_stdout(StringIO()):
            got = recover_mod._receive_row("backup", row, raw=True, log=Log())  # pylint: disable=protected-access
        assert got.error == (
            f"rpool/ROOT/{_BE}: left at @{self.MONTHLY}, older than the point "
            + "(the point's receive failed)"
        )
        # M4cP.2: the row as it was left, at the monthly.
        assert got.row is not None and got.row.snap == row.base and got.row.rel == row.rel
        results = iter([RunResult(1, "", "boom", "a")])
        with patch_sh(mock), redirect_stdout(StringIO()):
            got = recover_mod._receive_row("backup", row, raw=True, log=Log())  # pylint: disable=protected-access
        assert got == recover_mod.Received(None, f"rpool/ROOT/{_BE}: receive failed")

    def test_a_monthly_in_another_lineage_is_not_a_base(self):
        # An incremental needs both snapshots in one dataset: a monthly kept
        # in an archived lineage cannot be the base of the live point.
        rel = "rpool/ROOT/x"
        point = Snap(rel, self.P2, "2", 6, 6)
        archived = Snap(rel, self.MONTHLY, "1", 2, 2, source=f"{rel}.archived-20261001")
        row = RestoreRow(rel, point)
        monthly_base = restore_plan._monthly_base  # pylint: disable=protected-access
        assert monthly_base(row, [archived, point]) is None
        live = Snap(rel, self.MONTHLY, "1", 2, 2)
        assert monthly_base(row, [live, point]) == live

    def test_a_point_that_is_itself_a_monthly_gets_no_base(self):
        # 6a (M4b-6; mutation R6): a monthly chosen as the point is already
        # what every drive carried; an older monthly would only add blocks.
        rel = "rpool/ROOT/x"
        older = Snap(rel, "autosnap_2026-08-01_00:00:00_monthly", "1", 2, 2)
        chosen = Snap(rel, self.MONTHLY, "2", 6, 6)
        monthly_base = restore_plan._monthly_base  # pylint: disable=protected-access
        assert monthly_base(RestoreRow(rel, chosen), [older, chosen]) is None

    def test_bases_that_do_not_fit_are_dropped_before_refusing(self):
        snap = Snap("rpool/ROOT/x", "a", "1", 1, 1)
        base = Snap("rpool/ROOT/x", self.MONTHLY, "0", 0, 0)
        rows = [
            RestoreRow(
                "rpool/ROOT/x", snap, referenced=300 * 1024**3, base=base, base_bytes=200 * 1024**3
            )
        ]
        point = restore_points([snap], "rpool/ROOT/x")[0]
        plan = RestorePlan("backup", "/dev/sdb1", "x", point, rows, "backup/keystore@k", 1024**2)
        assert TestRecoverSizeCheck._check(plan, str(476 * 1024**3))  # pylint: disable=protected-access
        assert rows[0].base is None and rows[0].base_bytes == 0


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
            raise survey.ReadError("could not read: zfs get")
        return snaps

    return read


class TestRecoverNoWipeBeforeYes:  # pylint: disable=missing-function-docstring
    """Hallazgo 15: nothing touches the internal disk before pre-flight + YES."""

    def _run(self, answer: str | None, preflight_fails: bool = False) -> MockShell:
        with tempfile.TemporaryDirectory() as td:
            return self._run_in(td, answer, preflight_fails)

    def _run_in(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
        self,
        work: str,
        answer: str | None,
        preflight_fails: bool = False,
        calls: dict[str, list[tuple[object, ...]]] | None = None,
        snaps_fail: bool = False,
        meta_fail: bool = False,
        shell_setup: Callable[[MockShell], None] | None = None,
        extra: Sequence[AbstractContextManager[object]] = (),
        everything_succeeds: bool = False,
    ) -> MockShell:
        """``answer`` None simulates a closed terminal (EOF) at the YES prompt.

        The system.key copy goes below ``work``; Cleanup's atexit run is
        simulated after the command returns. ``calls``, when given, receives
        the positional arguments of every ``choose_point`` and ``build_plan`` call.
        ``snaps_fail`` leaves the real ``list_snapshots`` in place with the
        engine's listing of the drive failing; ``meta_fail`` fails the read
        of the drive's metadata. ``shell_setup`` adds responses to the shell
        before any default, and ``extra`` patches go in last.
        """
        source_key = os.path.join(work, "source.key")
        Path(source_key).write_bytes(b"k" * 32)
        cleanups: list[Cleanup] = []
        mock = MockShell()
        if shell_setup is not None:
            shell_setup(mock)
        meta = mock.on("zfs get -H -s local -o property,value all backup")
        _ = meta.fails() if meta_fail else meta.succeeds("org.zark:format\t2\n")
        if everything_succeeds:  # last: the first match wins
            _ = mock.on_prefix("").succeeds()
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
            patch.object(drive_marks, "drive_snapshots", side_effect=_marks_unreadable(snaps))
            if snaps_fail
            else patch.object(recover_mod, "list_snapshots", return_value=snaps),
            patch.object(recover_mod, "choose_point", side_effect=spy("choose_point", point)),
            patch.object(recover_mod, "build_plan", side_effect=spy("build_plan", plan)),
            patch.object(recover_mod, "_select_target", return_value="/dev/sda"),
            patch.object(recover_mod, "_preflight", side_effect=preflight),
            patch("builtins.input", side_effect=EOFError)
            if answer is None
            else patch("builtins.input", return_value=answer),
            patch("lib.cleanup.USB_FLUSH_DELAY_SEC", 0),
            redirect_stdout(StringIO()),
            *extra,
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

    def test_a_bpool_left_at_its_monthly_is_mounted_and_its_kernels_checked(self):
        """M4dR-6 (P8): ``run`` mounts bpool explicitly whenever the restore
        put something there, a bpool left at its monthly included, and names
        the kernels that monthly lacks."""
        monthly = "autosnap_2026-09-01_00:00:00_monthly"
        seen: dict[str, list[object]] = {"mount": [], "gaps": []}

        def mount(*args: object) -> None:
            seen["mount"].append(args[:2])

        def gaps(*args: object) -> list[str]:
            seen["gaps"].append(args[1])
            return []

        real_exists = Path.exists

        def exists(self: Path) -> bool:  # the target disk's partitions
            return str(self).startswith("/dev/sda") or real_exists(self)

        with tempfile.TemporaryDirectory() as td:
            extra: list[AbstractContextManager[object]] = [
                patch.object(recover_mod, "RECOVER_MNT", os.path.join(td, "mnt")),
                patch.object(Path, "exists", exists),
                patch.object(recover_mod, "restore_rpool_rows", return_value=[]),
                patch.object(recover_mod, "_restore_encryption_root", return_value=[]),
                patch.object(recover_mod, "restore_bpool", return_value=(monthly, [])),
                patch.object(recover_mod, "_export_verified"),
                patch.object(recover_mod, "_refuse_imported_system_pools"),
                patch.object(recover_mod, "_stable_vdev", return_value="/dev/x"),
                patch.object(recover_mod, "drop_prepare_snapshot"),
                patch.object(recover_mod, "_install_missing_tools"),
                patch.object(recover_mod, "mount_restored_system", side_effect=mount),
                patch.object(recover_mod, "kernel_gaps", side_effect=gaps),
                patch.object(recover_mod, "install_boot_chain", return_value=[]),
                patch.object(recover_mod, "regenerate_initrd", return_value=[]),
            ]
            _ = self._run_in(td, "YES", extra=extra, everything_succeeds=True)
        assert seen["mount"] == [(_BE, True)], seen
        assert seen["gaps"] == [monthly], seen

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

    def test_both_pools_are_created_on_the_targets_by_id_partitions(self):
        """R2-14: recover creates bpool and rpool on the target's
        /dev/disk/by-id partitions, not on its kernel names."""
        disk_id = "ata-KINGSTON_SKC600MS512G_50026B7784FC3319"
        links = f"{disk_id}\t../../sda\n" + "".join(
            f"{disk_id}-part{n}\t../../sda{n}\n" for n in range(1, 5)
        )

        def shell(mock: MockShell) -> None:
            _ = mock.on_prefix("find /dev/disk/by-id/").succeeds(links)
            _ = mock.on_prefix("zpool create").succeeds()

        real_exists = recover_mod.Path.exists

        def exists(path: Path, *a: Any, **k: Any) -> bool:
            p = str(path)
            if p.startswith(("/dev/sda", f"/dev/disk/by-id/{disk_id}-part")):
                return True
            return real_exists(path, *a, **k)

        with tempfile.TemporaryDirectory() as td:
            mock = self._run_in(
                td,
                "YES",
                shell_setup=shell,
                extra=[patch.object(recover_mod.Path, "exists", autospec=True, side_effect=exists)],
            )
        creates = [c for c in mock.calls if c.startswith("zpool create")]
        assert len(creates) == 2, mock.calls[-30:]
        assert creates[0].endswith(f"bpool /dev/disk/by-id/{disk_id}-part2"), creates
        assert creates[1].endswith(f"rpool /dev/disk/by-id/{disk_id}-part4"), creates

    def test_no_wipe_when_not_confirmed(self):
        mock = self._run("NO")
        assert mock.was_not_called("wipefs")
        assert not [c for c in mock.calls if c.startswith("sgdisk")]  # `which sgdisk` runs
        assert mock.was_not_called("zpool destroy")

    def test_no_wipe_when_preflight_fails(self):
        mock = self._run("YES", preflight_fails=True)
        assert mock.was_not_called("wipefs")

    def test_no_wipe_when_terminal_closes_at_yes(self):
        mock = self._run(None)
        assert mock.was_not_called("wipefs")
        assert not [c for c in mock.calls if c.startswith("sgdisk")]  # `which sgdisk` runs

    def test_a_failed_read_of_the_marks_stops_before_the_menu(self) -> None:
        """M3R-9: the drive's snapshots and marks are read strictly; a failed
        listing is fatal instead of turning the drive into a legacy one."""
        calls: dict[str, list[tuple[object, ...]]] = {}
        with tempfile.TemporaryDirectory() as td:
            mock = self._run_in(td, "YES", calls=calls, snaps_fail=True)
        assert "choose_point" not in calls and "build_plan" not in calls
        assert mock.was_not_called("wipefs")
        assert not [c for c in mock.calls if c.startswith("sgdisk")]

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
        (menu,) = calls["choose_point"]
        (plan,) = calls["build_plan"]
        assert menu[2] is complete
        assert plan[-1] is complete

    def test_a_failed_read_of_the_metadata_stops_before_the_menu(self) -> None:
        """M4c-18 (M3X.1): the metadata is read strictly, as the marks are; an
        unread last-point would turn the drive into a legacy one."""
        calls: dict[str, list[tuple[object, ...]]] = {}
        with tempfile.TemporaryDirectory() as td:
            mock = self._run_in(td, "YES", calls=calls, meta_fail=True)
        assert "choose_point" not in calls
        assert mock.was_not_called("wipefs")


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


class TestRecoverBootChain:  # pylint: disable=missing-function-docstring
    """M1P.8, M4c-9, M4c-7: kernel count, debconf's ESP, the EFI binaries."""

    LINE = "grub-efi-amd64 grub-efi/install_devices multiselect /dev/disk/by-id/nvme-X-part1"

    def _debconf(self, stderr: str = "", shown: str = "") -> tuple[bool, MockShell]:
        mock = MockShell()
        mock.on_prefix("chroot /r bash -c").returns(0, stderr=stderr)
        mock.on("chroot /r debconf-show grub-efi-amd64").succeeds(shown)
        with patch_sh(mock), redirect_stdout(StringIO()):
            ok = boot_chain.set_install_devices("/r", "/dev/disk/by-id/nvme-X-part1", make_log())
        return ok, mock

    def test_the_line_has_its_owner_and_type(self):
        ok, mock = self._debconf(shown="* grub-efi/install_devices: /dev/disk/by-id/nvme-X-part1\n")
        assert ok
        assert f"echo '{self.LINE}' | debconf-set-selections" in mock.calls[0]

    def test_a_warning_is_not_set(self):
        ok, _ = self._debconf(stderr="warning: Unknown type /dev/x, skipping line 1")
        assert not ok

    def test_the_value_is_read_back(self):
        ok, _ = self._debconf(shown="  grub-efi/install_devices: /dev/sda1\n")
        assert not ok
        ok, _ = self._debconf(shown="  grub-pc/install_devices: /dev/sda\n")
        assert not ok

    def test_kernel_count_ignores_the_links(self):
        with tempfile.TemporaryDirectory() as td:
            boot = Path(td) / "boot"
            boot.mkdir()
            (Path(td) / "usr/bin").mkdir(parents=True)
            _ = (Path(td) / "usr/bin/bash").write_text("")
            _ = (boot / "vmlinuz-7.0.0-38-generic").write_text("")
            (boot / "vmlinuz").symlink_to("vmlinuz-7.0.0-38-generic")
            (boot / "vmlinuz.old").symlink_to("vmlinuz-7.0.0-38-generic")
            mock = MockShell()
            mock.on("zfs mount rpool/ROOT/x").succeeds()
            mock.on("zfs mount bpool/BOOT/x").succeeds()
            mock.on(f"findmnt -n {td}/boot").succeeds()
            mock.on("zfs mount -a").succeeds()
            buf = StringIO()
            with patch_sh(mock), patch.object(boot_chain, "RECOVER_MNT", td), redirect_stdout(buf):
                mount_restored_system("x", True, make_log())
        assert "(1 kernel(s))" in buf.getvalue()

    def test_a_missing_efi_binary_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            efi = Path(td) / "boot/efi/EFI/ubuntu"
            efi.mkdir(parents=True)
            _ = (efi / "grubx64.efi").write_text("")
            mock = MockShell()
            mock.on_prefix(f"chroot {td} debconf-show").succeeds(
                "* grub-efi/install_devices: /dev/sda1\n",
            )
            mock.on_prefix(f"chroot {td} bash -c").succeeds()
            mock.on_prefix("find /dev/disk/by-id/").succeeds("")
            with (
                patch_sh(mock),
                patch.object(boot_chain, "RECOVER_MNT", td),
                patch.object(boot_chain.grub_guard, "install"),
                patch.object(boot_chain.apt_guard, "install"),
                patch.object(boot_chain, "_install_keystore_initramfs"),
                patch.object(ZFS, "write_zpool_cache"),
                redirect_stdout(StringIO()),
            ):
                errors = boot_chain.install_boot_chain(
                    "/dev/sda", "x", ZFS(make_log()), Cleanup(make_log()), make_log()
                )
        assert errors == [
            "EFI: shimx64.efi missing in /boot/efi/EFI/ubuntu after the signed packages' "
            + "reconfigure",
        ]

    def test_debconf_gets_the_esp_by_id(self):
        # R2-14: the ESP's /dev/disk/by-id name when it has one.
        by_id = "/dev/disk/by-id/nvme-X-part1"
        with tempfile.TemporaryDirectory() as td:
            efi = Path(td) / "boot/efi/EFI/ubuntu"
            efi.mkdir(parents=True)
            for name in ("grubx64.efi", "shimx64.efi"):
                _ = (efi / name).write_text("")
            mock = MockShell()
            mock.on_prefix(f"chroot {td} debconf-show").succeeds(
                f"* grub-efi/install_devices: {by_id}\n"
            )
            mock.on_prefix(f"chroot {td} bash -c").succeeds()
            mock.on_prefix("find /dev/disk/by-id/ -lname '*/nvme0n1p1'").succeeds(by_id + "\n")
            with (
                patch_sh(mock),
                patch.object(boot_chain, "RECOVER_MNT", td),
                patch.object(boot_chain.grub_guard, "install"),
                patch.object(boot_chain.apt_guard, "install"),
                patch.object(boot_chain, "_install_keystore_initramfs"),
                patch.object(ZFS, "write_zpool_cache"),
                redirect_stdout(StringIO()),
            ):
                errors = boot_chain.install_boot_chain(
                    "/dev/nvme0n1", "x", ZFS(make_log()), Cleanup(make_log()), make_log()
                )
        assert not errors, errors
        sets = [c for c in mock.calls if "debconf-set-selections" in c]
        assert len(sets) == 1 and f"install_devices multiselect {by_id}'" in sets[0], sets


class TestRecoverAnchors:  # pylint: disable=missing-function-docstring
    """M4c-12 (M4bF-2): recover anchors what it restored for the drive it read."""

    @staticmethod
    def _row(rel: str, name: str, creation: int) -> RestoreRow:
        return RestoreRow(rel, Snap(rel, name, "g", 1, creation))

    def test_rpool_rows_are_bookmarked_with_the_drive_anchor_names(self):
        stamp = int(datetime(2026, 10, 7, 10, 0, tzinfo=UTC).timestamp())
        rows = [
            self._row("rpool/ROOT/be", P1, stamp),
            self._row("rpool/srv", "autosnap_2026-10-07_08:00:00_hourly", stamp - 7200),
        ]
        mock = MockShell()
        mock.on_prefix("zfs bookmark rpool/ROOT/be@").succeeds()
        mock.on_prefix("zfs bookmark rpool/srv@").fails("dataset is busy")
        with patch_sh(mock), redirect_stdout(StringIO()):
            errors = recover_mod.anchor_restored(rows, "backup", "111", make_log())
        assert mock.calls == [
            f"zfs bookmark rpool/ROOT/be@{P1} rpool/ROOT/be#zark_111_2026-10-07_10:00:00Z",
            "zfs bookmark rpool/srv@autosnap_2026-10-07_08:00:00_hourly "
            + "rpool/srv#zark_111_2026-10-07_08:00:00Z",
        ]
        assert errors == [
            "rpool/srv: not anchored for backup: its next backup of it resends it in full",
        ]

    def test_bpool_is_renamed_never_bookmarked(self):
        stamp = int(datetime(2026, 10, 7, 10, 0, tzinfo=UTC).timestamp())
        mock = MockShell()
        mock.on_prefix("zfs rename").succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            got = recover_mod.anchor_bpool(
                self._row("bpool/BOOT/be", P1, stamp), "backup", "111", make_log()
            )
        assert not got
        assert mock.calls == [
            f"zfs rename bpool/BOOT/be@{P1} bpool/BOOT/be@zark_111_2026-10-07_10:00:00Z",
        ]
        assert mock.was_not_called("zfs bookmark")


class TestRecoverLeftAtTheMonthly:  # pylint: disable=missing-function-docstring
    """M4cP.2: a dataset whose point failed after its monthly arrived is
    named, mounted (bpool) and anchored at that monthly."""

    MONTHLY = "autosnap_2026-09-01_00:00:00_monthly"
    M_STAMP = int(datetime(2026, 9, 1, 0, 0, tzinfo=UTC).timestamp())
    P_STAMP = int(datetime(2026, 10, 7, 10, 0, tzinfo=UTC).timestamp())

    def _row(self, rel: str) -> RestoreRow:
        return RestoreRow(
            rel,
            Snap(rel, P1, "g", 2, self.P_STAMP),
            base=Snap(rel, self.MONTHLY, "m", 1, self.M_STAMP),
        )

    @staticmethod
    def _mock(*pipes: bool) -> MockShell:
        results = iter([RunResult(0 if ok else 1, "", "" if ok else "boom", "p") for ok in pipes])
        mock = MockShell()
        mock.mock_run_pipe = (  # type: ignore[method-assign,assignment]
            lambda *_a, **_k: next(results)
        )
        mock.on_prefix("zfs create").succeeds()
        mock.on_prefix("zfs rename").succeeds()
        mock.on_prefix("zfs bookmark").succeeds()
        return mock

    def _bpool(self, *pipes: bool) -> tuple[str | None, list[str], MockShell]:
        mock = self._mock(*pipes)
        with patch_sh(mock), redirect_stdout(StringIO()):
            left, errors = recover_mod.restore_bpool(
                "backup", self._row("bpool/BOOT/be"), "111", make_log()
            )
        return left, errors, mock

    def test_bpool_left_at_its_monthly_is_named_and_anchored_there(self):
        left, errors, mock = self._bpool(True, False)
        assert left == self.MONTHLY
        assert errors == [
            f"bpool/BOOT/be: left at @{self.MONTHLY}, older than the point "
            + "(the point's receive failed)"
        ]
        assert mock.was_called(
            f"zfs rename bpool/BOOT/be@{self.MONTHLY} bpool/BOOT/be@zark_111_2026-09-01_00:00:00Z"
        ), mock.calls

    def test_bpool_at_its_point_and_without_any(self):
        left, errors, mock = self._bpool(True, True)
        assert left == "" and not errors
        assert mock.was_called(f"zfs rename bpool/BOOT/be@{P1} "), mock.calls
        left, errors, mock = self._bpool(False)
        assert left is None
        assert errors == ["bpool: receive failed - kernels need reinstallation"]
        assert mock.was_not_called("zfs rename")
        with redirect_stdout(StringIO()):
            left, errors = recover_mod.restore_bpool("backup", None, "111", make_log())
        assert left is None and errors[0].startswith("bpool: not in backup at this point")

    def test_an_rpool_dataset_left_at_its_monthly_is_bookmarked_there(self):
        mock = self._mock(True, True, True, False)
        rows = [self._row("rpool/ROOT/be"), self._row("rpool/srv")]
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            errors = recover_mod.restore_rpool_rows(
                "backup", rows, "rpool/ROOT/be", "111", make_log()
            )
        # M4dR-6 (P6): one at its point, one at its monthly: one restored of two.
        assert "1 of 2 rpool dataset(s) restored" in out.getvalue(), out.getvalue()
        assert "2 of 2" not in out.getvalue()
        assert errors == [
            f"rpool/srv: left at @{self.MONTHLY}, older than the point (the point's receive failed)"
        ]
        assert mock.calls == [
            f"zfs bookmark rpool/ROOT/be@{P1} rpool/ROOT/be#zark_111_2026-10-07_10:00:00Z",
            f"zfs bookmark rpool/srv@{self.MONTHLY} rpool/srv#zark_111_2026-09-01_00:00:00Z",
        ]

    def test_kernels_the_monthly_boot_lacks_are_named(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "boot").mkdir()
            _ = (Path(td) / "boot/vmlinuz-7.0.0-30-generic").write_text("")
            for v in ("7.0.0-30-generic", "7.0.0-38-generic"):
                (Path(td) / f"lib/modules/{v}").mkdir(parents=True)
                _ = (Path(td) / f"lib/modules/{v}/modules.dep").write_text("")
            (Path(td) / "lib/modules/7.0.0-12-generic").mkdir()  # no modules.dep: removed
            assert recover_mod.kernel_gaps(td, self.MONTHLY) == [
                "kernel 7.0.0-38-generic: in the restored root but not in /boot "
                + f"(bpool at @{self.MONTHLY}): reinstall it from zark chroot: "
                + "apt install --reinstall linux-image-7.0.0-38-generic"
            ]
            (Path(td) / "boot/vmlinuz-7.0.0-30-generic").unlink()
            gaps = recover_mod.kernel_gaps(td, self.MONTHLY)
        assert len(gaps) == 3 and gaps[-1].startswith(
            "no kernel in /boot matches the restored root"
        )


class TestRecoverKeystoreSnapshot:  # pylint: disable=missing-function-docstring
    """M4c-14: prepare's keystore snapshot is not left in the recovered origin."""

    def test_prepare_snapshot_is_destroyed(self):
        mock = MockShell()
        mock.on("zfs destroy rpool/keystore@prepare_20261001_100000").succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            recover_mod.drop_prepare_snapshot("backup/keystore@prepare_20261001_100000", make_log())
        assert mock.calls == ["zfs destroy rpool/keystore@prepare_20261001_100000"]

    def test_failure_is_a_warning_and_other_names_are_left(self):
        mock = MockShell()
        mock.on_prefix("zfs destroy").fails("dataset is busy")
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            recover_mod.drop_prepare_snapshot("backup/keystore@prepare_1", make_log())
            recover_mod.drop_prepare_snapshot("backup/keystore@autosnap_x_daily", make_log())
        assert mock.calls == ["zfs destroy rpool/keystore@prepare_1"]
        assert "Could not remove rpool/keystore@prepare_1" in out.getvalue()


class TestKeyStatusRead:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """M4c-19: the keystatus read is strict when asked."""

    def test_strict_raises_lenient_is_empty(self):
        mock = MockShell()
        mock.on("zfs get -H keystatus -r backup/rpool").fails("I/O error")
        with patch_sh(mock):
            zfs = ZFS(make_log())
            assert not zfs.datasets_needing_key("backup/rpool")
            try:
                _ = zfs.datasets_needing_key("backup/rpool", strict=True)
            except KeyStatusError as e:
                assert "I/O error" in str(e)
            else:
                raise AssertionError("expected KeyStatusError")


class TestKeysForEncryptionRoots:  # pylint: disable=missing-function-docstring
    """P0-5 and its kin (M4d-19): only an encryption root takes a key."""

    GET = (
        "zfs get -H -o name,property,value -t filesystem,volume -r "
        + "encryptionroot,keystatus backup/rpool"
    )
    LISTING = "".join(
        f"{name}\tencryptionroot\t{root}\n{name}\tkeystatus\t{status}\n"
        for name, root, status in (
            ("backup/rpool", "backup/rpool", "unavailable"),
            ("backup/rpool/ROOT", "backup/rpool", "unavailable"),  # inherits
            ("backup/rpool/srv.archived-20261001", "backup/rpool", "unavailable"),  # a clone
            ("backup/rpool/USERDATA/home_x", "backup/rpool/USERDATA/home_x", "unavailable"),  # raw
            ("backup/rpool/data", "backup/rpool/data", "available"),
            ("backup/rpool/plain", "-", "-"),
        )
    )

    def test_the_roots_of_what_is_locked_in_order(self):
        mock = MockShell()
        mock.on(self.GET).succeeds(self.LISTING)
        with patch_sh(mock):
            got = ZFS(make_log()).encryption_roots_needing_key("backup/rpool")
        assert got == ["backup/rpool", "backup/rpool/USERDATA/home_x"]
        mock = MockShell()
        mock.on(self.GET).fails("I/O error")
        with patch_sh(mock):
            zfs = ZFS(make_log())
            assert not zfs.encryption_roots_needing_key("backup/rpool")
            try:
                _ = zfs.encryption_roots_needing_key("backup/rpool", strict=True)
            except KeyStatusError as e:
                assert "I/O error" in str(e)
            else:
                raise AssertionError("expected KeyStatusError")

    def test_both_loaders_load_only_the_roots(self):
        want = [
            f"zfs load-key -L file://{SYSTEM_KEY_PATH} backup/rpool",
            f"zfs load-key -L file://{SYSTEM_KEY_PATH} backup/rpool/USERDATA/home_x",
        ]
        mock = MockShell()
        mock.on(self.GET).succeeds(self.LISTING)
        mock.on_prefix("zfs load-key").succeeds()
        ks = Keystore(make_log())
        with patch_sh(mock), patch.object(Keystore, "is_mounted", True):
            assert ks.load_pool_keys("backup/rpool") == 2
        assert [c for c in mock.calls if c.startswith("zfs load-key")] == want
        mock.reset()
        with patch_sh(mock):
            n = recover_mod._load_keys_from_file(SYSTEM_KEY_PATH, "backup/rpool", make_log())  # pylint: disable=protected-access
        assert n == 2 and [c for c in mock.calls if c.startswith("zfs load-key")] == want


class TestSelftestLeftovers:  # pylint: disable=missing-function-docstring
    """M4c-26 (M4sR-5): a SIGKILLed self-test's directory and mapper."""

    def test_setup_removes_only_unused_leftovers(self):
        with tempfile.TemporaryDirectory() as td:
            names = ("zark-selftest-1-aaaa", "zark-selftest-2-bbbb", "zark-selftest-3-cccc")
            for n in names:
                os.makedirs(f"{td}/{n}")
            os.makedirs(f"{td}/other")
            bench = stest.Bench(make_log(), Cleanup(make_log()), root=td)
            mock = MockShell()
            mock.on("zpool list -vHP").succeeds(f"zst1b\t1G\n\t{td}/{names[1]}/zst1b.img\t1G\n")
            mock.on("findmnt -rn -o TARGET").succeeds(f"/\n{td}/{names[2]}/mnt\n")
            mock.on_prefix("rm -r --one-file-system").succeeds()
            out = StringIO()
            with patch_sh(mock), redirect_stdout(out):
                bench._remove_leftovers()  # pylint: disable=protected-access
            rms = [c for c in mock.calls if c.startswith("rm ")]
            assert rms == [f"rm -r --one-file-system {td}/{names[0]}"]
            assert f"Removed {td}/{names[0]}, left by an earlier self-test" in out.getvalue()

    def test_setup_removes_the_leftovers_first(self):
        # M4cR-3: Bench.setup calls _remove_leftovers before anything else,
        # even when it then refuses (here: no space in /dev/shm).
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(f"{td}/zark-selftest-1-aaaa")
            bench = stest.Bench(make_log(), Cleanup(make_log()), root=td)
            mock = MockShell()
            mock.on("zpool list -vHP").succeeds("")
            mock.on("findmnt -rn -o TARGET").succeeds("/\n")
            mock.on_prefix("rm -r --one-file-system").succeeds()
            small = os.statvfs_result((4096, 4096, 10, 10, 10, 10, 10, 10, 0, 255))
            with (
                patch_sh(mock),
                patch("lib.selftest.os.statvfs", return_value=small),
                redirect_stdout(StringIO()),
            ):
                try:
                    bench.setup()
                except stest.SetupError:
                    pass
                else:
                    raise AssertionError("no refusal")
        assert mock.calls == [
            "zpool list -vHP",
            "findmnt -rn -o TARGET",
            f"rm -r --one-file-system {td}/zark-selftest-1-aaaa",
        ], mock.calls

    def test_a_failed_listing_removes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(f"{td}/zark-selftest-1-aaaa")
            bench = stest.Bench(make_log(), Cleanup(make_log()), root=td)
            mock = MockShell()
            mock.on("zpool list -vHP").fails()
            mock.on("findmnt -rn -o TARGET").succeeds("/\n")
            with patch_sh(mock), redirect_stdout(StringIO()):
                bench._remove_leftovers()  # pylint: disable=protected-access
            assert mock.was_not_called("rm ")

    def test_clean_closes_the_selftest_mapper_of_its_pool(self):
        with tempfile.TemporaryDirectory() as td:
            mapper = Path(td)
            for n in ("zst0123abcdc-ks", "zst0123abcdz-ks", "zstnothexxc-ks", "other"):
                _ = (mapper / n).write_text("")
            mock = MockShell()
            mock.on("cryptsetup status zst0123abcdc-ks").succeeds(
                "/dev/mapper/zst0123abcdc-ks is active.\n  type:    LUKS2\n  device:  /dev/zd16\n"
            )
            real = {
                "/dev/zd16": "/dev/zd16",
                "/dev/zvol/zst0123abcdc/keystore": "/dev/zd16",
            }
            with (
                patch_sh(mock),
                patch.object(clean_mod.os.path, "realpath", side_effect=lambda p: real.get(p, p)),
            ):
                got = clean_mod._selftest_mappers(  # pylint: disable=protected-access
                    mapper, ["zst0123abcdc"]
                )
        assert got == ["zst0123abcdc-ks"]


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
        ok = "dracut --force /boot/initrd.img-7.0.0-31-generic 7.0.0-31-generic"
        bad = "dracut --force /boot/initrd.img-7.0.0-34-generic 7.0.0-34-generic"
        mock.on(f"chroot {root} {ok}").succeeds()
        mock.on(f"chroot {root} {bad}").fails(rc=1)
        with patch_sh(mock), redirect_stdout(StringIO()):
            failures = regenerate_initrd(root, make_log())
        assert failures == ["dracut failed for 7.0.0-34-generic (rc=1)"]
        assert mock.was_not_called("--regenerate-all")
        assert mock.was_not_called("7.0.0-14")

    def test_dracut_is_given_the_output_path_and_nothing_is_made_on_the_esp(self):
        # M4c-5: without the path, an ESP with loader/entries gets the initrd.
        root = self._root()
        mock = MockShell()
        with patch_sh(mock), redirect_stdout(StringIO()):
            _ = regenerate_initrd(root, make_log())
        assert mock.was_not_called("--kver")
        assert mock.was_not_called("mkdir")
        assert mock.was_not_called("machine-id")

    def test_the_kernel_links_are_not_kernels(self):
        # M1P.8: the postinst's vmlinuz and vmlinuz.old links, and a vmlinuz-<v> link.
        root = Path(self._root())
        (root / "boot/vmlinuz").symlink_to("vmlinuz-7.0.0-34-generic")
        (root / "boot/vmlinuz.old").symlink_to("vmlinuz-7.0.0-31-generic")
        (root / "lib/modules/7.0.0-99-generic").mkdir(parents=True)
        (root / "lib/modules/7.0.0-99-generic/modules.dep").write_text("")
        (root / "boot/vmlinuz-7.0.0-99-generic").symlink_to("vmlinuz-7.0.0-34-generic")
        assert installed_kernels(str(root)) == ["7.0.0-31-generic", "7.0.0-34-generic"]

    def test_the_running_system_is_not_chrooted(self):
        mock = MockShell()
        mock.on("dracut --force /boot/initrd.img-7.0.0-34-generic 7.0.0-34-generic").succeeds()
        with (
            patch_sh(mock),
            patch.object(initrd_mod, "installed_kernels", return_value=["7.0.0-34-generic"]),
            exists_only("/usr/bin/dracut"),
            redirect_stdout(StringIO()),
        ):
            assert not regenerate_initrd("", make_log())
        assert mock.calls == [
            "dracut --force /boot/initrd.img-7.0.0-34-generic 7.0.0-34-generic",
        ]


class TestRepairBootCleanup:  # pylint: disable=missing-function-docstring
    """P0-6: every mount is tracked, /boot goes before rpool, banner is truthful."""

    @staticmethod
    def _run(
        export_rpool_ok: bool,
        grub_failure: str = "",
        vdev: str = "/dev/disk/by-id/ata-DISK-part4",
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
        mock.on("zpool list -vHP rpool").succeeds(f"rpool\t1G\n\t{vdev}\t1G")
        mock.on(f"lsblk -nrs -o NAME,TYPE {vdev}").succeeds("sda4 part\nsda disk")
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

    def test_every_step_has_something_under_it(self):
        # M1P.10: [2/7] and [3/7] printed with nothing under them.
        _, out, _, _ = self._run(export_rpool_ok=True)
        lines = [ln for ln in Log._STRIP_RE.sub("", out).splitlines() if ln.strip()]  # pylint: disable=protected-access
        steps = [i for i, ln in enumerate(lines) if re.match(r"\[\d/5\] ", ln)]
        assert len(steps) == 5, lines
        assert steps[1] + 1 not in steps, lines  # the mount's lines are under its step

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

    def test_kernel_named_vdevs_make_the_repair_incomplete(self):
        # R2-9: the zpool.cache written here keeps whatever the import recorded.
        _, out, exited, _ = self._run(export_rpool_ok=True, vdev="/dev/sda4")
        assert exited
        assert "BOOT REPAIR INCOMPLETE" in out
        assert "vdevs recorded by kernel names: /dev/sda4" in out

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
        generated: str | bytes,
        update_grub_ok: bool = True,
        stderr: str = "boom",
        kernels: list[str] | None = None,
    ) -> str:
        grub_cfg = root / "grub.cfg"

        def fake_run(cmd: str, **_kw: object) -> RunResult:
            if "update-grub" in cmd:
                data = generated if isinstance(generated, bytes) else generated.encode()
                grub_cfg.write_bytes(data)
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
                kernels or [],
                make_log(),
            )

    def test_entries_found_is_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "grub.cfg").write_text("old vmlinuz-1", encoding="utf-8")
            with redirect_stdout(StringIO()):
                assert self._regenerate(root, "linux /vmlinuz-7") == ""

    def test_every_installed_kernel_needs_its_entry(self):
        # M4c-6: 10_linux_zfs exits 0 even when it stops half way.
        cfg = (
            'menuentry "Ubuntu" {\n'
            '\tlinux\t"/BOOT/ubuntu_x@/vmlinuz-7.0.0-38-generic" root=ZFS="rpool/ROOT/x"\n'
            "}\n"
            "function zsyshistorymenu {\n"
            '\tlinux "${kernel}" root=ZFS="${dataset}"\n'
            "}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "grub.cfg").write_text("OLD vmlinuz", encoding="utf-8")
            with redirect_stdout(StringIO()):
                both = ["7.0.0-31-generic", "7.0.0-38-generic"]
                reason = self._regenerate(root, cfg, kernels=both)
                assert reason == "update-grub wrote no entry for 7.0.0-31-generic"
                assert self._regenerate(root, cfg, kernels=["7.0.0-38-generic"]) == ""
                # A version that is a prefix of another is not its entry.
                assert self._regenerate(root, cfg, kernels=["7.0.0-3"]) != ""
                # The zsyshistorymenu line alone is no entry.
                hist = cfg.split("function")[1]
                reason = self._regenerate(root, hist)
                assert reason == "update-grub produced no kernel entries"

    def test_a_title_that_is_not_utf8_is_read(self):
        cfg = b'menuentry "Ubuntu \xff" {\n\tlinux /BOOT/x@/vmlinuz-7.0.0-38-generic ro\n}\n'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with redirect_stdout(StringIO()):
                assert self._regenerate(root, cfg, kernels=["7.0.0-38-generic"]) == ""

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
    """J, M4c-4/5/7/8: finish reports what it did, never touches the pools' import."""

    @staticmethod
    def _run(  # pylint: disable=too-many-locals
        grub_failure: str = "",
        initrd_failures: tuple[str, ...] = (),
        health: str = "ONLINE",
        efi_failure: str = "",
        gaps: tuple[str, ...] = (),
        unmounted: tuple[str, ...] = (),
        bpool_guid: str = "",
    ) -> tuple[str, bool, list[str]]:
        order: list[str] = []

        def fake_run(cmd: str, **_kw: object) -> RunResult:
            order.append(cmd)
            return RunResult(returncode=0, stdout="", stderr="", command=cmd)

        def fake_grub(*a: object) -> str:
            order.append(f"regenerate_grub_cfg {a[1]} {a[3]}")
            return grub_failure

        def fake_initrd(root: str, _log: Log) -> list[str]:
            order.append(f"regenerate_initrd {root!r}")
            return list(initrd_failures)

        def fake_efi(esp: str, _log: Log) -> str:
            order.append(f"check_esp_and_efi {esp}")
            return efi_failure

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
            patch.object(finish_mod, "installed_kernels", return_value=["7.0.0-38-generic"]),
            patch.object(finish_mod, "regenerate_initrd", side_effect=fake_initrd),
            patch.object(finish_mod, "regenerate_grub_cfg", side_effect=fake_grub),
            patch.object(
                finish_mod,
                "fix_grub_bpool_uuid",
                side_effect=lambda *a, **_k: order.append(f"fix_grub_bpool_uuid {a[0]}"),
            ),
            patch.object(finish_mod, "esp_of_rpool_disk", return_value="/dev/nvme1n1p1"),
            patch.object(finish_mod, "check_esp_and_efi", side_effect=fake_efi),
            patch.object(finish_mod, "mount_canmount_on", return_value=list(unmounted)),
            patch.object(finish_mod, "setup_gaps", return_value=list(gaps)),
            patch.object(ZFS, "pool_exists", return_value=True),
            patch.object(ZFS, "pool_guid", return_value=bpool_guid),
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
        return unframed(buf.getvalue()), exited, order

    def test_all_good_is_complete(self):
        out, exited, order = self._run()
        assert not exited
        assert "FINISH COMPLETE" in out and "GRUB config updated ✓" in out
        assert "initrds rebuilt ✓" in out and "EFI binaries present ✓" in out
        assert "regenerate_grub_cfg update-grub ['7.0.0-38-generic']" in order

    def test_no_pool_is_exported_or_imported(self):
        # M4c-4: an export of the running rpool unmounts what is not busy, then fails.
        _, _, order = self._run()
        assert not [c for c in order if c.startswith(("zpool export", "zpool import"))]
        assert "Pool hostid synced" not in self._run()[0]

    def test_cache_then_initrds_then_grub_then_esp(self):
        _, _, order = self._run()
        cache = order.index("zpool set cachefile=/etc/zfs/zpool.cache bpool")
        initrd = order.index("regenerate_initrd ''")
        grub = next(i for i, c in enumerate(order) if c.startswith("regenerate_grub_cfg"))
        assert cache < initrd < grub < order.index("check_esp_and_efi /dev/nvme1n1p1")
        fstab = order.index("blkid -s UUID -o value /dev/nvme1n1p1")
        assert grub < fstab < order.index("check_esp_and_efi /dev/nvme1n1p1")

    def test_the_bpool_uuid_is_fixed_after_update_grub(self):
        # R2-14: the order test with a bpool GUID, so the fix runs.
        _, _, order = self._run(bpool_guid="123")
        grub = next(i for i, c in enumerate(order) if c.startswith("regenerate_grub_cfg"))
        fix = next(i for i, c in enumerate(order) if c.startswith("fix_grub_bpool_uuid"))
        assert grub < fix, order

    def test_guard_refusal_is_incomplete(self):
        reason = "update-grub failed: ERROR: External ZFS pool(s) detected: backup"
        out, exited, _ = self._run(grub_failure=reason)
        assert exited
        assert "FINISH INCOMPLETE" in out
        assert "GRUB config updated ✓" not in out
        assert f"grub.cfg not regenerated: {reason}" in out
        assert "Disconnect the external drive(s)" in out

    def test_initrd_failure_names_its_kernel_and_sends_to_repair_boot(self):
        out, exited, _ = self._run(initrd_failures=("dracut failed for 7.0.0-38-generic (rc=1)",))
        assert exited
        assert "FINISH INCOMPLETE" in out and "initrds rebuilt ✓" not in out
        assert "✗ initrd: dracut failed for 7.0.0-38-generic (rc=1)" in out
        assert "sudo ./zark repair-boot" in out and "Disconnect" not in out

    def test_a_kernel_without_entry_sends_to_repair_boot(self):
        out, exited, _ = self._run(grub_failure="update-grub wrote no entry for 7.0.0-38-generic")
        assert exited and "sudo ./zark repair-boot" in out

    def test_efi_failure_is_incomplete(self):
        out, exited, _ = self._run(efi_failure="the ESP is not mounted on /boot/efi")
        assert exited
        assert "✗ EFI: the ESP is not mounted on /boot/efi" in out
        assert "Mount the ESP of rpool's disk on /boot/efi," in out

    def test_degraded_pool_is_incomplete(self):
        out, exited, _ = self._run(health="DEGRADED")
        assert exited
        assert "✗ pool rpool: DEGRADED" in out

    def test_no_setup_line_when_setup_has_nothing_to_do(self):
        # M4c-10: setup is recommended only for what is missing.
        out, exited, _ = self._run()
        assert not exited and "setup" not in out.split("FINISH COMPLETE")[1]

    def test_the_setup_line_names_what_is_missing(self):
        out, exited, _ = self._run(
            gaps=("install the alerts timer", "enable feature@bookmark_v2 on rpool")
        )
        assert not exited
        assert "setup would: install the alerts timer" in out
        assert "setup would: enable feature@bookmark_v2 on rpool" in out
        assert "sudo ./zark setup" in out

    def test_a_dataset_left_unmounted_is_a_warning(self):
        # M4c-11 11a: named, with the advice; the finish is still complete.
        out, exited, _ = self._run(unmounted=("rpool/USERDATA/home_x",))
        assert not exited and "FINISH COMPLETE" in out
        assert "⚠ not mounted: rpool/USERDATA/home_x: reboot and run finish again" in out


class TestFinishChecks:  # pylint: disable=missing-function-docstring
    """M4c-4 and M4c-7: the hostid line and the ESP/EFI verdict, read-only."""

    @staticmethod
    def _hostid(label: str, system: bytes | None) -> str:
        mock = MockShell()
        mock.on("zpool list -vHP rpool").succeeds("rpool\t1G\n\t/dev/disk/by-id/nvme-X-part4\t1G")
        mock.on("zdb -l /dev/disk/by-id/nvme-X-part4").succeeds(label)
        buf = StringIO()
        with tempfile.TemporaryDirectory() as td:
            hostid = Path(td) / "hostid"
            if system is not None:
                _ = hostid.write_bytes(system)
            real = Path

            def fake_path(p: object) -> Path:
                return hostid if str(p) == "/etc/hostid" else real(str(p))

            with patch_sh(mock), patch.object(finish_mod, "Path", fake_path), redirect_stdout(buf):
                finish_mod.check_label_hostid("rpool", make_log())
        return buf.getvalue()

    LABEL = (
        "------------------------------------\nLABEL 0\n------------------------------------\n"
        + "    version: 5000\n    name: 'rpool'\n    pool_guid: 42\n    hostid: 2864358476\n"
    )

    def test_label_hostid_matches(self):
        out = self._hostid(self.LABEL, (2864358476).to_bytes(4, "little"))
        assert "labels carry this system's hostid (aabaa44c) ✓" in out

    def test_label_hostid_differs_and_names_both(self):
        out = self._hostid(self.LABEL, (1).to_bytes(4, "little"))
        assert "labels carry hostid aabaa44c, this system's is 00000001" in out
        assert "the next boot rewrites them" in out

    def test_unreadable_label_is_a_warning(self):
        out = self._hostid("", b"\x01\x00\x00\x00")
        assert "cannot read its label" in out

    @staticmethod
    def _efi(
        findmnt: str, present: set[str], after: set[str] | None = None
    ) -> tuple[str, MockShell]:
        mock = MockShell()
        mock.on("findmnt -n -o SOURCE,FSTYPE /boot/efi").succeeds(findmnt)
        state = {"files": set(present)}

        def reconfigure() -> None:
            state["files"] = set(after if after is not None else present)

        mock.on("dpkg-reconfigure -f noninteractive grub-efi-amd64-signed").succeeds()
        mock.on("dpkg-reconfigure -f noninteractive shim-signed").fails(rc=1)

        class _P:  # pylint: disable=too-few-public-methods
            def __init__(self, p: object) -> None:
                self.p = str(p)

            def exists(self) -> bool:
                if any(c.startswith("dpkg-reconfigure") for c in mock.calls):
                    reconfigure()
                return Path(self.p).name in state["files"]

        with patch_sh(mock), patch.object(finish_mod, "Path", _P), redirect_stdout(StringIO()):
            return finish_mod.check_esp_and_efi("/dev/sda1", make_log()), mock

    def test_esp_not_mounted_looks_at_no_file(self):
        reason, mock = self._efi("", set())
        assert reason == "the ESP is not mounted on /boot/efi"
        assert mock.was_not_called("dpkg-reconfigure")

    def test_a_directory_of_bpool_is_not_the_esp(self):
        reason, _ = self._efi("bpool/BOOT/x zfs", {"shimx64.efi", "grubx64.efi"})
        assert reason == "the ESP is not mounted on /boot/efi"

    def test_another_disks_esp_is_named(self):
        reason, _ = self._efi("/dev/sdb1 vfat", {"shimx64.efi", "grubx64.efi"})
        assert reason == "/boot/efi is /dev/sdb1, not the ESP of rpool's disk (/dev/sda1)"

    def test_binaries_present(self):
        reason, mock = self._efi("/dev/sda1 vfat", {"shimx64.efi", "grubx64.efi"})
        assert reason == "" and mock.was_not_called("dpkg-reconfigure")

    def test_reconfigure_that_does_not_restore_them_is_a_failure(self):
        reason, _ = self._efi("/dev/sda1 vfat", {"grubx64.efi"})
        assert reason == (
            "shimx64.efi missing in /boot/efi/EFI/ubuntu "
            + "(grub-efi-amd64-signed rc=0, shim-signed rc=1)"
        )

    LISTING = (
        "rpool\toff\t/\t-\t-\tno\n"
        "rpool/ROOT/x\tnoauto\t/\t-\t-\tyes\n"
        "rpool/USERDATA/home_x\ton\t/home/x\tavailable\t-\tno\n"
        "rpool/srv\ton\t/srv\t-\t-\tno\n"
        "rpool/var/lib\ton\t/var/lib\t-\t-\tyes\n"
        "rpool/locked\ton\t/locked\tunavailable\t-\tno\n"
        "rpool/partial\ton\t/partial\t-\t1-abc\tno\n"
        "rpool/legacy\ton\tlegacy\t-\t-\tno\n"
    )

    def test_canmount_on_datasets_get_one_mount(self):
        mock = MockShell()
        mock.on_prefix("zfs list -H -o name,canmount,mountpoint").succeeds(self.LISTING)
        mock.on("zfs mount rpool/USERDATA/home_x").succeeds()
        mock.on("zfs mount rpool/srv").fails("cannot mount")
        with patch_sh(mock), redirect_stdout(StringIO()):
            left = finish_mod.mount_canmount_on(make_log())
        assert left == ["rpool/srv"]
        mounts = [c for c in mock.calls if c.startswith("zfs mount")]
        assert mounts == ["zfs mount rpool/USERDATA/home_x", "zfs mount rpool/srv"]

    def test_a_failed_listing_is_reported(self):
        mock = MockShell()
        mock.on_prefix("zfs list -H -o name,canmount,mountpoint").fails()
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert finish_mod.mount_canmount_on(make_log()) == ["(the dataset listing failed)"]

    @staticmethod
    def _gaps(good: bool) -> list[str]:
        mock = MockShell()
        mock.on("systemctl is-enabled sanoid.timer").succeeds("enabled" if good else "disabled")
        mock.on("systemctl is-active sanoid.timer").succeeds("active")
        mock.on("systemctl is-enabled zark-status.timer").succeeds("enabled")

        class _P:  # pylint: disable=too-few-public-methods
            def __init__(self, p: object) -> None:
                self.p = str(p)

            def exists(self) -> bool:
                return good

        with (
            patch_sh(mock),
            patch.object(finish_mod, "Path", _P),
            patch.object(finish_mod, "sanoid_conf_gap", return_value="" if good else "write x"),
            patch.object(finish_mod, "bookmark_feature", return_value="active" if good else ""),
            patch.object(finish_mod.apt_guard, "is_current", return_value=good),
            patch.object(finish_mod.grub_guard, "is_current", return_value=good),
            patch.object(finish_mod.alerts, "files_current", return_value=good),
        ):
            return finish_mod.setup_gaps(ZFS(make_log()))

    def test_setup_gaps_none(self):
        assert not self._gaps(True)

    def test_setup_gaps_each_named(self):
        assert self._gaps(False) == [
            "enable and start sanoid.timer",
            "write x",
            "enable feature@bookmark_v2 on rpool",
            "install the alerts timer",
            "update the APT guard to this zark's",
            "update the grub guard to this zark's",
        ]

    def test_installed_files_are_current_until_changed(self):
        with tempfile.TemporaryDirectory() as td, redirect_stdout(StringIO()):
            assert not apt_guard.is_current(td) and not grub_guard.is_current(td)
            apt_guard.install(target_root=td, log=make_log())
            grub_guard.install(target_root=td, log=make_log())
            assert apt_guard.is_current(td) and grub_guard.is_current(td)
            guard = Path(td) / grub_guard.GUARD_RELATIVE_PATH
            _ = guard.write_text(guard.read_text(encoding="utf-8") + "# older\n", encoding="utf-8")
            assert not grub_guard.is_current(td)
            hook = Path(td) / apt_guard.HOOK_RELATIVE_PATH
            hook.chmod(0o644)
            assert not apt_guard.is_current(td)

    def test_alerts_files_current(self):
        with tempfile.TemporaryDirectory() as td:
            assert not alerts_mod.files_current(td, "/opt/zark/zark")
            base = Path(td)
            for rel, text in (
                (alerts_mod.MOTD_SCRIPT_PATH, alerts_mod.MOTD_SCRIPT),
                (alerts_mod.SERVICE_PATH, alerts_mod.service_unit("/opt/zark/zark")),
                (alerts_mod.TIMER_PATH, alerts_mod.TIMER_UNIT),
            ):
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                _ = (base / rel).write_text(text, encoding="utf-8")
            assert alerts_mod.files_current(td, "/opt/zark/zark")
            assert not alerts_mod.files_current(td, "/usr/share/zark/zark")

    def test_reconfigure_that_restores_them_is_success(self):
        reason, _ = self._efi("/dev/sda1 vfat", {"grubx64.efi"}, {"grubx64.efi", "shimx64.efi"})
        assert reason == ""


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
            exists_only("/mnt/recover/usr/bin/bash"),
            patch.object(boot_chain.Path, "glob", return_value=[]),
            redirect_stdout(StringIO()),
        ):
            mount_restored_system("x", True, make_log())
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
                mount_restored_system("x", True, make_log())
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
            patch.object(restore_plan, "_probe_be", return_value=("", "", "")),
            patch.object(restore_plan, "_referenced", side_effect=lambda n: dict.fromkeys(n, 1)),
            patch.object(restore_plan, "_empty_root", return_value=False),
            redirect_stdout(StringIO()),
        ):
            plan = build_plan("backup", "/dev/sdb1", _BE, point, snaps + extra, make_log())
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


class TestCommandSignals:  # pylint: disable=missing-function-docstring
    """H12 (M4b-1): a signal stops the running child first, then the command,
    and its cleanup runs once, after the child is gone.

    Each case runs tests/signal_harness.py in its own process; the signals are
    sent from outside it, to it alone, so the children only stop if zark
    passes them on.
    """

    @staticmethod
    def _run(scenario: str) -> tuple[int, dict[str, Any], float]:
        harness = Path(__file__).parent / "signal_harness.py"
        with tempfile.TemporaryDirectory() as td:
            start = time.monotonic()
            r = subprocess.run(
                [sys.executable, str(harness), td, scenario],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=90,
                check=False,
            )
            took = time.monotonic() - start
            path = Path(td, "result.json")
            result: dict[str, Any] = (
                json.loads(path.read_text(encoding="ascii")) if path.exists() else {}
            )
            log = Path(td, "zark.log")
            lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            result["interrupted"] = [ln for ln in lines if "Interrupted by" in ln]
        return r.returncode, result, took

    def _assert_stopped(self, scenario: str, code: int) -> dict[str, Any]:
        rc, result, took = self._run(scenario)
        expected = {"cleanup": 1, "children_alive": [], "continued": False, "cleanup_done": True}
        got = {k: result.get(k) for k in expected}
        # The children would run for a minute: stopped, the run ends in seconds.
        assert (rc, got) == (code, expected) and took < 20, (scenario, rc, result, took)
        return result

    def test_a_signal_stops_the_pipeline_before_the_cleanup(self):
        for name in ("TERM", "HUP", "INT"):
            self._assert_stopped(f"pipe:{name}", 128 + signal.Signals[f"SIG{name}"])

    def test_a_signal_stops_a_plain_command_behind_a_shell(self):
        # dash keeps `sh -c` as the parent: only the process group reaches sleep.
        self._assert_stopped("run:TERM", 128 + signal.SIGTERM)
        self._assert_stopped("run:HUP", 128 + signal.SIGHUP)

    def test_a_ctrl_c_reaches_the_child_as_sigint(self):
        # Mutation S10: the child gets the signal the user sent, SIGINT for a
        # Ctrl-C and SIGTERM for anything else (its wait status says which).
        result = self._assert_stopped("run:INT", 128 + signal.SIGINT)
        assert result.get("child_status") == -signal.SIGINT, result
        result = self._assert_stopped("run:HUP", 128 + signal.SIGHUP)
        assert result.get("child_status") == -signal.SIGTERM, result

    def test_a_grandchild_that_ignores_the_stop_is_killed_too(self):
        # M4bF-10 10a: the shell between zark and its command dies with the
        # signal; the command behind it ignores it. The stop waits for the
        # whole process group and SIGKILLs what is left, so the cleanup never
        # runs under a child that is still working.
        result = self._assert_stopped("orphan:TERM", 128 + signal.SIGTERM)
        assert result.get("child_status") == -signal.SIGTERM, result

    def test_two_signals_pending_together_stop_once(self):
        # logind's SIGTERM and SIGHUP; CPython runs the lower number first.
        self._assert_stopped("pipe:TERM,HUP", 128 + signal.SIGHUP)
        self._assert_stopped("pipe:INT,TERM", 128 + signal.SIGINT)

    def test_a_signal_during_the_cleanup_is_ignored(self):
        self._assert_stopped("cleanup:TERM,HUP", 128 + signal.SIGTERM)
        self._assert_stopped("cleanup:INT,TERM", 128 + signal.SIGINT)

    def test_a_signal_during_the_normal_end_cleanup_stops_nothing(self):
        # M4bR-2 (M4bF-5 5a): the explicit cleanup.run() of a command that
        # ended normally ignores the three signals too; every step runs and
        # the command goes on to its ending.
        rc, result, took = self._run("normalend:TERM")
        expected = {"cleanup": 1, "children_alive": [], "continued": True, "cleanup_done": True}
        got = {k: result.get(k) for k in expected}
        assert (rc, got) == (0, expected) and took < 20, (rc, result, took)
        assert result["interrupted"] == [], result["interrupted"]

    def test_after_the_normal_end_cleanup_a_signal_stops_the_command_again(self):
        # M4bF-5 5a: the handler is back once the cleanup is over (a backup's
        # read-back and eject prompt still answer a Ctrl-C).
        rc, result, _ = self._run("normalend:TERM,INT")
        assert rc == 130 and result.get("continued") is True, (rc, result)

    def test_a_signal_after_the_signal_started_cleanup_is_ignored(self):
        # M4bV-2 F5b: a cleanup a signal started leaves the three signals
        # ignored (no resume): a second one during the rest of the exit
        # raises nothing inside atexit.
        rc, result, took = self._run("afterexit:TERM,HUP")
        assert rc == 128 + signal.SIGTERM and took < 20, (rc, result, took)
        assert result.get("cleanup_done") is True and result.get("after_cleanup") is True, result
        assert len(result["interrupted"]) == 1, result["interrupted"]

    def test_the_interruption_line_names_the_child_it_stops(self):
        # M4bR-8 (M4bF-7 7a): "stopping <child>", by executable and
        # subcommand, both sides of a pipeline; the full command is in the
        # log already.
        result = self._assert_stopped("pipe:TERM", 128 + signal.SIGTERM)
        assert any(
            "stopping echo | echo, then cleaning up" in ln for ln in result["interrupted"]
        ), result["interrupted"]
        result = self._assert_stopped("run:HUP", 128 + signal.SIGHUP)
        assert any("stopping sh, then cleaning up" in ln for ln in result["interrupted"]), result[
            "interrupted"
        ]

    def test_a_child_that_ignores_the_stop_is_killed_after_the_wait(self):
        # M4bR-7 (mutation S4): the child traps SIGTERM; the harness shortens
        # STOP_WAIT_SECONDS, which stop_children reads when it runs, and the
        # child ends by SIGKILL within seconds instead of running on.
        result = self._assert_stopped("stubborn:TERM", 128 + signal.SIGTERM)
        assert result.get("child_status") == -signal.SIGKILL, result

    @staticmethod
    def _without_reader(scenario: str) -> tuple[int, dict[str, Any]]:
        """Run a scenario whose stdout reader goes away after the first line."""
        harness = Path(__file__).parent / "signal_harness.py"
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.Popen(  # pylint: disable=consider-using-with
                [sys.executable, str(harness), td, scenario],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            assert p.stdout is not None
            _ = p.stdout.readline()
            p.stdout.close()  # the reader goes away
            rc = p.wait(timeout=60)
            path = Path(td, "result.json")
            result = json.loads(path.read_text(encoding="ascii")) if path.exists() else {}
        return rc, {k: result.get(k) for k in ("cleanup", "continued", "cleanup_done")}

    def test_a_reader_that_goes_away_stops_the_command_through_its_cleanup(self):
        # M4b-2: was killed by SIGPIPE (SIG_DFL) at the first print, no cleanup.
        got = self._without_reader("deadpipe:")
        want = {"cleanup": 1, "continued": False, "cleanup_done": True}
        assert got == (128 + signal.SIGPIPE, want), got

    def test_after_the_normal_end_cleanup_a_dead_stdout_stops_the_command_again(self):
        # M4bV-2 F4: resume() clears the teardown mark, so a reader that goes
        # away after the cleanup stops the command as before it.
        got = self._without_reader("resumed:")
        assert got == (
            128 + signal.SIGPIPE,
            {"cleanup": 1, "continued": False, "cleanup_done": True},
        ), got

    def test_a_cleanup_with_nobody_reading_still_runs_to_its_end(self):
        # The command ended normally; only the cleanup finds the reader gone.
        got = self._without_reader("quietend:")
        assert got == (0, {"cleanup": 1, "continued": True, "cleanup_done": True}), got

    def test_a_child_that_owns_the_terminal_keeps_its_ctrl_c(self):
        # chroot's shell, simulate's QEMU: a Ctrl-C to zark is let through,
        # a later SIGTERM stops the child (SIGHUP to it), then the cleanup.
        self._assert_stopped("interactive:INT,TERM", 128 + signal.SIGTERM)
        self._assert_stopped("interactive:HUP", 128 + signal.SIGHUP)

    def test_the_entry_point_installs_the_handler_before_a_command(self):
        path = str(Path(__file__).resolve().parent.parent / "zark")
        loader = importlib.machinery.SourceFileLoader("zark_entry", path)
        spec = importlib.util.spec_from_file_location("zark_entry", path, loader=loader)
        assert spec is not None
        entry = importlib.util.module_from_spec(spec)
        loader.exec_module(entry)
        order: list[str] = []
        with (
            patch.object(sys, "argv", ["zark", "prune"]),
            patch("os.geteuid", return_value=0),
            patch("lib.signals.guard_output", side_effect=lambda *_a: order.append("guard")),
            patch.object(lock_mod, "guard"),
            patch("lib.signals.install", side_effect=lambda *_a: order.append("install")),
            patch.object(prune_mod, "run", side_effect=lambda _a: order.append("prune")),
        ):
            entry.main()
        assert order == ["guard", "install", "prune"], order


class TestLiveInstallerProbe:  # pylint: disable=missing-function-docstring
    """M4aF-6 (M4b-14): on a live session the installer's probe service is
    stopped, for this session only, before a command that imports pools."""

    @staticmethod
    def _mock(live: bool, snap_ok: bool = True) -> MockShell:
        mock = MockShell()
        mock.on("cat /proc/cmdline").succeeds(
            "BOOT_IMAGE=/casper/vmlinuz boot=casper" if live else ""
        )
        mock.on("test -d /rofs").returns(0 if live else 1)
        mock.on("test -d /cow").returns(0 if live else 1)
        stop = mock.on(f"snap stop {live_mod.PROBE_SERVICE}")
        if snap_ok:
            stop.succeeds("")
        else:
            stop.fails('error: snap "ubuntu-desktop-bootstrap" not found')
        return mock

    def test_stopped_before_an_import_on_a_live_session(self) -> None:
        mock = self._mock(live=True)
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert live_mod.stop_installer_probe("recover", [], Log())
        assert mock.was_called("snap stop ubuntu-desktop-bootstrap.subiquity-server")
        assert not mock.was_called("--disable")  # runtime only
        for args in ([], ["run"], ["--stick", "/dev/sdz"], ["run", "--only", "POOL-01"]):
            mock = self._mock(live=True)
            with patch_sh(mock), redirect_stdout(StringIO()):
                assert live_mod.stop_installer_probe("selftest", args, Log()), args

    def test_left_alone_on_an_installed_system_or_without_an_import(self) -> None:
        cases: list[tuple[bool, str, list[str]]] = [
            (False, "mount", []),
            (True, "status", []),
            (True, "backup", []),
            # M4sP.7: selftest's other subcommands import nothing.
            (True, "selftest", ["status"]),
            (True, "selftest", ["report"]),
            (True, "selftest", ["certificate", "x.json"]),
            (True, "selftest", ["warnings", "on"]),
        ]
        for live, cmd, args in cases:
            mock = self._mock(live=live)
            with patch_sh(mock), redirect_stdout(StringIO()):
                assert not live_mod.stop_installer_probe(cmd, args, Log())
            assert not mock.was_called("snap stop"), (live, cmd, args)

    def test_a_missing_snap_is_not_an_error(self) -> None:
        mock = self._mock(live=True, snap_ok=False)
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            assert not live_mod.stop_installer_probe("clean", [], Log())
        assert "ERROR" not in out.getvalue() and "FATAL" not in out.getvalue()

    def test_the_entry_point_stops_it_before_the_command(self) -> None:
        path = str(Path(__file__).resolve().parent.parent / "zark")
        loader = importlib.machinery.SourceFileLoader("zark_entry", path)
        spec = importlib.util.spec_from_file_location("zark_entry", path, loader=loader)
        assert spec is not None
        entry = importlib.util.module_from_spec(spec)
        loader.exec_module(entry)
        order: list[str] = []
        with (
            patch.object(sys, "argv", ["zark", "prune", "--dry-run"]),
            patch("os.geteuid", return_value=0),
            patch("lib.signals.guard_output"),
            patch.object(lock_mod, "guard"),
            patch("lib.signals.install"),
            patch.object(
                live_mod,
                "stop_installer_probe",
                side_effect=lambda c, a, _l: order.append(f"stop {c} {a}"),
            ),
            patch.object(prune_mod, "run", side_effect=lambda _a: order.append("prune")),
        ):
            entry.main()
        assert order == ["stop prune ['--dry-run']", "prune"], order


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

    def test_links_resolve_inside_the_tree(self):
        # M4c-22 (M1P.19): an absolute link of the backup is the tree's, not the live's.
        in_tree = lib_mount.resolve_in_tree
        with tempfile.TemporaryDirectory() as td:
            root = os.path.realpath(td)
            os.makedirs(f"{root}/run/lock")
            os.makedirs(f"{root}/var")
            os.symlink("/run", f"{root}/var/run")
            os.symlink("../../../..", f"{root}/var/up")
            os.symlink("run/lock", f"{root}/lk")
            os.symlink("loop", f"{root}/loop")
            assert in_tree(root, f"{root}/var/run/lock") == f"{root}/run/lock"
            assert in_tree(root, f"{root}/var/up/etc") == f"{root}/etc"
            assert in_tree(root, f"{root}/lk/x") == f"{root}/run/lock/x"
            assert in_tree(root, f"{root}/../../etc") == f"{root}/etc"
            assert in_tree(root, f"{root}/loop/x") is None
            assert in_tree(root, root) == root

    def test_read_only_mounts_at_the_resolved_path_read_write_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            root = os.path.realpath(td)
            os.makedirs(f"{root}/run/user")
            os.makedirs(f"{root}/var")
            os.symlink("/run", f"{root}/var/run")
            plan = [mount_mod.LayoutMount("rpool/var/run/user", f"{root}/var/run/user")]
            for readonly in (True, False):
                mock = MockShell()
                mock.on("zfs list -H -o name -r backup/rpool/ROOT").succeeds(
                    f"backup/rpool/ROOT\nbackup/rpool/ROOT/{_BE}",
                )
                mock.on_prefix("mount -t zfs").succeeds()
                mock.on_prefix("mkdir -p").succeeds()
                with (
                    patch_sh(mock),
                    patch.object(mount_mod, "_plan_origin_layout", return_value=plan),
                    redirect_stdout(StringIO()),
                ):
                    _, skipped = mount_mod._mount_origin_layout(  # pylint: disable=protected-access
                        "backup", root, make_log(), readonly=readonly
                    )
                mounts = [c for c in mock.calls if "backup/rpool/var/run/user" in c]
                if readonly:
                    assert mounts == [
                        f"mount -t zfs -o ro,zfsutil backup/rpool/var/run/user {root}/run/user"
                    ]
                else:
                    assert not mounts
                    assert f"{root}/var/run/user resolves to /run/user" in skipped[0].reason

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
            # Read-only resolves inside the tree (M4c-22): the absolute link is
            # the tree's own path, which does not exist; .. stops at the root.
            reasons = {x.rel: x.reason for x in skipped}
            assert reasons["rpool/data"] == f"no directory {mnt}/data"
            assert reasons["rpool/up"] == f"{mnt}/etc/../.. resolves to the root"

    @staticmethod
    def _umount_backup(
        export_ok: bool,
        setup: Callable[[MockShell, str], None] | None = None,
        namespaces: list[str] | None = None,
    ) -> tuple[MockShell, str]:
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
            if setup is not None:
                setup(mock, td)
            with (
                patch_sh(mock),
                patch.object(
                    umount_mod, "_namespaces", return_value=namespaces or ["/proc/42/ns/mnt"]
                ),
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
        return mock, unframed(buf.getvalue())

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

    def test_copies_held_elsewhere_are_released_then_the_export_retried(self):
        # W-1 (M4b-3 3b): the propagated unmount skips a bind with a mount of
        # its own below it; umount releases the copies where they are, only
        # below its tree, and exports again.
        ns = "nsenter --mount=/proc/42/ns/mnt"
        seen: dict[str, str] = {}

        def setup(mock: MockShell, td: str) -> None:
            tree = seen["tree"] = f"{td}/backup"
            mock.on("findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
                f"/ overlay overlay\n{tree} zfs backup/rpool/ROOT/be\n{tree}/run tmpfs tmpfs"
            )
            listing = mock.on(f"{ns} findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
                f"/ overlay overlay\n{tree} zfs backup/rpool/ROOT/be\n{tree}/run tmpfs tmpfs\n"
                + f"{tree}/run/user tmpfs tmpfs\n/srv zfs data/srv"
            )
            export = mock.on("zpool export backup").fails("pool is busy")
            # Ahead of the helper's own answer: busy until the release.
            mock._responses.insert(0, mock._responses.pop())  # pylint: disable=protected-access
            mock.on(f"{ns} umount -R {tree}").succeeds()
            real = mock.mock_run

            def run_hook(cmd: Any, *a: Any, **k: Any) -> RunResult:
                r = real(cmd, *a, **k)
                if str(cmd) == f"{ns} umount -R {tree}":
                    _ = listing.succeeds("/ overlay overlay\n/srv zfs data/srv")
                    _ = export.succeeds()
                return r

            mock.mock_run = run_hook  # type: ignore[method-assign]

        mock, out = self._umount_backup(export_ok=True, setup=setup)
        umounts = [c for c in mock.calls if c.startswith(f"{ns} umount")]
        assert umounts == [f"{ns} umount -R {seen['tree']}"], mock.calls
        assert mock.call_count("zpool export backup") == 2, mock.calls
        assert "BACKUP NOT UNMOUNTED" not in out, out

    def test_a_bind_the_tree_had_is_released_where_only_it_stays(self):
        # M4bV-2 F7: the other namespace holds only a (tmpfs, tmpfs) bind
        # below the tree (no ZFS mount of the pool there): released because
        # the tree had that pair here, and the export retried.
        ns = "nsenter --mount=/proc/42/ns/mnt"
        seen: dict[str, str] = {}

        def setup(mock: MockShell, td: str) -> None:
            tree = seen["tree"] = f"{td}/backup"
            mock.on("findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
                f"/ overlay overlay\n{tree} zfs backup/rpool/ROOT/be\n{tree}/run tmpfs tmpfs"
            )
            listing = mock.on(f"{ns} findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
                f"/ overlay overlay\n{tree}/run tmpfs tmpfs"
            )
            export = mock.on("zpool export backup").fails("pool is busy")
            mock._responses.insert(0, mock._responses.pop())  # pylint: disable=protected-access
            mock.on(f"{ns} umount -R {tree}/run").succeeds()
            real = mock.mock_run

            def run_hook(cmd: Any, *a: Any, **k: Any) -> RunResult:
                r = real(cmd, *a, **k)
                if str(cmd) == f"{ns} umount -R {tree}/run":
                    _ = listing.succeeds("/ overlay overlay")
                    _ = export.succeeds()
                return r

            mock.mock_run = run_hook  # type: ignore[method-assign]

        mock, out = self._umount_backup(export_ok=True, setup=setup)
        umounts = [c for c in mock.calls if c.startswith(f"{ns} umount")]
        assert umounts == [f"{ns} umount -R {seen['tree']}/run"], mock.calls
        assert "BACKUP NOT UNMOUNTED" not in out, out

    def test_an_unrelated_mount_at_the_same_path_elsewhere_is_left_alone(self):
        # M4bR-3 (M4bF-6 6a, review E3): a namespace with a root of its own
        # can hold anything at zark's path; only a copy of the tree (a ZFS
        # mount of the pool, or the same fstype and source as a mount the
        # tree had here) is released, and only a copy counts as holding it.
        alien, copy = "nsenter --mount=/proc/42/ns/mnt", "nsenter --mount=/proc/43/ns/mnt"
        seen: dict[str, str] = {}

        def setup(mock: MockShell, td: str) -> None:
            tree = seen["tree"] = f"{td}/backup"
            mock.on("findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
                f"/ overlay overlay\n{tree} zfs backup/rpool/ROOT/be\n{tree}/run tmpfs tmpfs"
            )
            mock.on(f"{alien} findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
                f"/ tmpfs rootfs\n{tree} tmpfs e3-unrelated\n{tree}/run tmpfs othertmp"
            )
            listing = mock.on(f"{copy} findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
                f"/ overlay overlay\n{tree} zfs backup/rpool/ROOT/be\n{tree}/run tmpfs tmpfs"
            )
            export = mock.on("zpool export backup").fails("pool is busy")
            mock._responses.insert(0, mock._responses.pop())  # pylint: disable=protected-access
            mock.on(f"{copy} umount -R {tree}").succeeds()
            real = mock.mock_run

            def run_hook(cmd: Any, *a: Any, **k: Any) -> RunResult:
                r = real(cmd, *a, **k)
                if str(cmd) == f"{copy} umount -R {tree}":
                    _ = listing.succeeds("/ overlay overlay")
                    _ = export.succeeds()
                return r

            mock.mock_run = run_hook  # type: ignore[method-assign]

        mock, out = self._umount_backup(
            export_ok=True, setup=setup, namespaces=["/proc/42/ns/mnt", "/proc/43/ns/mnt"]
        )
        tree = seen["tree"]
        umounts = [c for c in mock.calls if "umount -R" in c and "nsenter" in c]
        assert umounts == [f"{copy} umount -R {tree}"], mock.calls
        assert "BACKUP NOT UNMOUNTED" not in out and "still held" not in out, out
        assert f"left alone in {alien}: {tree} (tmpfs e3-unrelated), not zark's" in out, out

    def test_what_another_namespace_still_holds_names_its_process(self):
        # M4bR-8 (M4bF-8 8a): the banner names the namespace's process (from
        # /proc/<pid>/comm) or the snap whose namespace snapd pins.
        me = f"/proc/{os.getpid()}/ns/mnt"
        comm = Path("/proc/self/comm").read_text(encoding="utf-8").strip()
        label = umount_mod._ns_label  # pylint: disable=protected-access
        assert label(me) == f"mount namespace {me}, process {comm}"
        assert (
            label("/run/snapd/ns/firefox.mnt") == "mount namespace pinned by snapd for snap firefox"
        )

        seen: dict[str, str] = {}

        def setup(mock: MockShell, td: str) -> None:
            tree = seen["tree"] = f"{td}/backup"
            mock.on("findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
                f"/ overlay overlay\n{tree} zfs backup/rpool/ROOT/be"
            )
            mock.on(f"nsenter --mount={me} findmnt -rn -o TARGET,FSTYPE,SOURCE").succeeds(
                f"/ overlay overlay\n{tree} zfs backup/rpool/ROOT/be"
            )
            mock.on(f"nsenter --mount={me} umount -R {tree}").fails("target is busy")

        _, out = self._umount_backup(export_ok=False, setup=setup, namespaces=[me])
        assert f"still held: {seen['tree']} (mount namespace {me}, process {comm})" in out, out

    def test_every_other_mount_namespace_is_listed_once(self):
        # Processes share namespaces; snapd pins some with no process in them.
        own = os.readlink("/proc/self/ns/mnt")
        with tempfile.TemporaryDirectory() as td:
            proc, snapd = Path(td, "proc"), Path(td, "snapd")
            # M4bV-2 F9: in pid order, numerically: 99 is older than 100.
            for pid, ns in (("100", "mnt:[1]"), ("99", "mnt:[1]"), ("102", own), ("self", "x")):
                Path(proc, pid, "ns").mkdir(parents=True)
                os.symlink(ns, Path(proc, pid, "ns", "mnt"))
            snapd.mkdir()
            Path(snapd, "lxd.mnt").write_text("", encoding="ascii")
            Path(snapd, "lxd.info").write_text("", encoding="ascii")
            got = umount_mod._namespaces(str(proc), str(snapd))  # pylint: disable=protected-access
        assert got == [f"{proc}/99/ns/mnt", f"{snapd}/lxd.mnt"], got

    def test_what_stays_mounted_here_is_named(self):
        # R2§11.4a: a process inside the tree keeps it here; the banner names
        # that mount instead of only blaming other namespaces.
        def setup(mock: MockShell, td: str) -> None:
            mock.on("findmnt -rn -o TARGET").succeeds(f"/\n{td}/backup\n{td}/backup/home")

        _, out = self._umount_backup(export_ok=False, setup=setup)
        assert "still mounted here:" in out and "/backup (in use? fuser -vm" in out, out

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
        # findmnt -r escapes spaces and non-ASCII bytes (\x20), so the tree's
        # path is kept plain, as zark's own trees are.
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
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
        assert r == {"propagated_left": [], "other_left": [], "tree_left": [], "host_lost": []}, r

    def test_an_empty_bind_leaves_nothing_in_another_namespace(self):
        r = self._run("bind")
        assert r == {"propagated_left": [], "other_left": [], "tree_left": [], "host_lost": []}, r

    def test_a_host_mount_below_a_bound_directory_survives(self):
        # V-3: a plain `umount -R` (commit 21) unmounted it through the shared bind.
        # W-1: the copies the propagated unmount leaves elsewhere are released.
        r = self._run("bind-hostmount")
        assert r["propagated_left"], r  # what the release is there for
        assert (r["other_left"], r["tree_left"], r["host_lost"]) == ([], [], []), r

    def test_nested_binds_as_chroot_makes_lose_no_host_mount(self):
        # /dev with /dev/pts inside it, as `zark chroot` binds them (X-1): no
        # host mount lost, nothing left here or in the other namespace (W-1).
        r = self._run("nested-bind")
        assert (r["other_left"], r["tree_left"], r["host_lost"]) == ([], [], []), r

    def test_the_harness_refuses_before_any_mount_without_its_own_namespace(self):
        # X-3: an unshare that changed nothing must stop the harness before its
        # first mount command, or `--make-rprivate /` would hit the host.
        import tests.umount_propagation_harness as harness  # pylint: disable=import-outside-toplevel

        calls: list[tuple[str, ...]] = []
        with (
            patch.object(harness.os, "unshare", lambda _flags: None),
            patch.object(harness, "_run", lambda *a: calls.append(a)),
            patch.object(harness.sys, "argv", ["harness", tempfile.gettempdir(), "datasets"]),
        ):
            try:
                harness.main()
            except SystemExit as e:
                assert "refusing" in str(e.code), e.code
            else:
                raise AssertionError("the harness ran without a namespace of its own")
        assert not calls, calls


class TestStableVdevs:  # pylint: disable=missing-function-docstring
    """eli 2026-09-30: kernel-named vdevs broke the boot with a USB stick plugged in."""

    @staticmethod
    def _vdev(links: str, exists: bool) -> tuple[str, str]:
        mock = MockShell()
        mock.on_prefix("find /dev/disk/by-id/").succeeds(links)
        buf = StringIO()
        with (
            patch_sh(mock),
            exists_only("/dev/disk/by-id/")
            if exists
            else exists_only(absent=("/dev/disk/by-id/",)),
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
    """Per-dataset states (redesign §4; M2 decisions 5-10, 18)."""

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


def _destroyed(cmd: str) -> list[str]:
    """``ds@name`` for each name of ``zfs destroy ds@a,b,c``."""
    ds, _, spec = cmd.split()[-1].partition("@")
    return [f"{ds}@{n}" for n in spec.split(",")]


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
        fake.ds[p].props["org.zark:origin-machine-id"] = TEST_MACHINE_ID  # as prepare writes
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
    """The engine end to end against FakeZfs (redesign §4, I-A...I-G)."""

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

    def test_fake_writes_a_bookmark_token_as_the_kernel_reads_it(self) -> None:
        """M4d plan 15 (9): a stream cut after being sent against a bookmark
        names it ``#name`` in its token; the resume check then finds it."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        _prune_all_autosnaps(fake)  # the next base is a bookmark
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        assert not _engine_run(fake, "backup", "111", P2).ok
        token = fake.ds["backup/rpool/USERDATA/home"].token
        assert token.split("|")[3].startswith("#"), token
        assert fake._resume_check(token) == ""  # pylint: disable=protected-access

    def test_fake_get_recursive_reads_the_options_as_options(self) -> None:
        """M4d plan 15 (9): ``zfs get -H keystatus -r <root>`` lists the root
        and what is below it; ``-r`` was read as the property."""
        fake = self._fresh()
        fake.ds["rpool/USERDATA/home"].props["keystatus"] = "unavailable"
        fake.ds["rpool/var"].props["keystatus"] = "available"
        with patch_sh(fake), redirect_stdout(StringIO()):
            zfs = ZFS(make_log())
            assert zfs.datasets_needing_key("rpool") == ["rpool/USERDATA/home"]
            assert not zfs.datasets_needing_key("rpool/var")
            assert zfs.datasets_needing_key("rpool/USERDATA") == ["rpool/USERDATA/home"]

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
        partial state cannot resume and goes; no snapshot is lost, and the
        hourly the drive holds, anchored when the run stopped (8b), is the
        base of the next one."""
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
        after = fake.names("backup/rpool/USERDATA/home")
        assert after[: len(before)] == before and after[-1] == P3

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
        # Nor the hourly's anchor (8b): no bookmark outlives its snapshot's recover.
        hourly = fake.ds["backup/rpool/USERDATA/home"].snaps[-1].guid
        home.bookmarks = [b for b in home.bookmarks if b.guid != hourly]
        asked: list[repl.State] = []

        def decide(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            p = next(q for q in plans if q.rel == "rpool/USERDATA/home")
            asked.append(p.state)
            return p.state is not repl.State.ROLLBACK or eng.rollback(p)

        res = _engine_run(fake, "backup", "111", P3, decide)
        assert asked == [repl.State.ROLLBACK]
        assert res.ok, res
        assert P3 in fake.names("backup/rpool/USERDATA/home")

    def test_token_stale_at_transfer_time_is_discarded_and_replanned(self) -> None:
        """M2V-5 T4b: the token's snapshot goes between the survey and the
        transfer (pruned by hand during the questions): the transfer's own
        dry run finds it gone, the partial state is discarded once, and the
        dataset is planned afresh in the same run."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        assert not _engine_run(fake, "backup", "111", P2).ok
        named = fake.ds["backup/rpool/USERDATA/home"].token.split("|")[2]
        before = fake.names("backup/rpool/USERDATA/home")
        seen: list[repl.State] = []

        def decide(plans: list[repl.DatasetPlan], _eng: engine.Run) -> bool:
            seen.append(next(q for q in plans if q.rel == "rpool/USERDATA/home").state)
            home = fake.ds["rpool/USERDATA/home"]
            home.snaps = [x for x in home.snaps if x.name != named]
            return True

        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P3, sleep=lambda _s: None)
            res = engine.execute(eng, Cleanup(Log()), decide)
        assert seen == [repl.State.RESUME]
        assert res.ok, res
        calls = fake.calls
        aborts = [
            i
            for i, c in enumerate(calls)
            if c.startswith("zfs receive -A backup/rpool/USERDATA/home")
        ]
        sends = [i for i, c in enumerate(calls) if c.startswith("zfs send -t ")]
        assert len(aborts) == 1 and sends and sends[0] < aborts[0], calls
        assert "partial receive discarded: cannot resume send:" in out.getvalue()
        assert not fake.ds["backup/rpool/USERDATA/home"].token
        after = fake.names("backup/rpool/USERDATA/home")
        assert after[: len(before)] == before and after[-1] == P3

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

    def test_an_origin_dataset_named_like_an_archive_is_excluded(self) -> None:
        """M2R-13 (M4b-7 7b): ``.archived-`` in an origin name is reserved:
        the dataset is EXCLUDED with a reason, never sent, and the run is
        complete without it."""
        fake = self._fresh()
        fake.dataset("rpool/USERDATA/old.archived-2024")
        fake.snap("rpool/USERDATA/old.archived-2024", "autosnap_x")
        res = _engine_run(fake, "backup", "111", P1)
        assert res.ok, res
        p = next(q for q in res.plans if q.rel == "rpool/USERDATA/old.archived-2024")
        assert p.state is repl.State.EXCLUDED and "reserves for archived lineages" in p.note
        assert "backup/rpool/USERDATA/old.archived-2024" not in fake.ds
        assert not any("old.archived-2024" in c for c in fake.calls if "send" in c)

    def test_an_incremental_child_of_a_failed_parent_is_sent(self) -> None:
        """M2X.1 (M4b-12): a child the drive already holds needs only its
        incremental; its parent's failure does not hold it back."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.inject["backup/rpool/var"] = ("interrupt", 0)
        res = _engine_run(fake, "backup", "111", P2)
        assert not res.ok and not res.outcomes["rpool/var"].ok
        assert res.outcomes["rpool/var/lib"].ok
        assert P2 in fake.names("backup/rpool/var/lib")

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

    def test_marker_stays_when_the_timer_cannot_be_started(self) -> None:
        """M2V-5 S9: when `systemctl start sanoid.timer` fails at the end of a
        run, the marker stays, and the next run starts the timer and removes
        it."""
        fake = self._fresh()
        fake.fail_cmds.add("systemctl start sanoid.timer")
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P1, sleep=lambda _s: None)
            assert engine.execute(eng, Cleanup(Log()), lambda _p, _r: True).ok
            assert Path(engine.SANOID_MARKER).exists()
            assert fake.units["sanoid.timer"] == "inactive"
            assert "Could not start sanoid.timer;" in out.getvalue()
            fake.fail_cmds.discard("systemctl start sanoid.timer")
            again = StringIO()
            with redirect_stdout(again):
                eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
                assert engine.execute(eng, Cleanup(Log()), lambda _p, _r: True).ok
            assert not Path(engine.SANOID_MARKER).exists()
        assert fake.units["sanoid.timer"] == "active"
        assert "left stopped by an earlier zark run" in again.getvalue()

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
        fake.fail_cmds.add("zfs snapshot -o org.zark:point-for=111 bpool")
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
        # M2V-7 (e): the texts matched are libzfs's in English.
        assert any(c.startswith("env LC_ALL=C zfs send -nvP -t ") for c in fake.calls), fake.calls
        assert res.ok, res
        home = fake.names("backup/rpool/USERDATA/home")
        assert P2 in home and P3 in home

    def test_a_failed_probe_of_a_root_is_not_a_missing_root(self) -> None:
        """M2V-7 (d): strict, a root is missing only when the pool's listing
        lacks it; its own probe failing (I/O) read as missing."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.fail_cmds.add("zfs list -H -o name backup/rpool")
        with patch_sh(fake):
            dest = survey.read_dest("backup", strict=True)
        assert "rpool/ROOT/be" in dest, sorted(dest)

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

    def test_a_point_the_drive_never_held_names_no_drive(self) -> None:
        """M4c-12: a recovered system's point is not "on the backup" of
        another drive: its line says what it is."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        recovered = "zark_2026-10-01_09:00:00Z"
        # Older than anything this drive holds: no stream of it carries it.
        made = fake.snap("rpool/ROOT/be", recovered)
        fake.ds["rpool/ROOT/be"].snaps.remove(made)
        made.txg = 0
        fake.ds["rpool/ROOT/be"].snaps.insert(0, made)
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            _ = engine.execute(eng, Cleanup(Log()), lambda _p, _r: True)
        text = out.getvalue()
        assert f"removed rpool/ROOT/be@{recovered}: not on backup; it names no drive" in text
        assert f"removed rpool/ROOT/be@{P2}: on the backup, anchored as" in text

    def test_drop_disk_anchors(self) -> None:
        """registry forget: one disk's anchors go, the other's stay."""
        fake = self._fresh(("black", "blue"))
        assert _engine_run(fake, "black", "1", P1).ok
        assert _engine_run(fake, "blue", "2", P2).ok
        with patch_sh(fake), redirect_stdout(StringIO()):
            n, failed = anchors.drop_disk_anchors("1", ["rpool", "bpool"], Log())
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
            assert anchors.drop_disk_anchors("2", ["rpool", "bpool"], Log())[0] > 0
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

    def test_a_point_never_taken_on_bpool_is_not_destroyed_there(self) -> None:
        """M4bV-2 F13c: rpool's point taken, bpool's snapshot failed: the undo
        removes rpool's and runs no zfs destroy on bpool, where the point does
        not exist (it would fail and warn of a point that was never there)."""
        fake = self._fresh()
        real_run = fake.mock_run

        def run_(cmd: str, *args: object, **kwargs: object) -> RunResult:
            if cmd.startswith("zfs snapshot") and " bpool" in cmd:
                fake._calls.append(cmd)  # pylint: disable=protected-access
                return RunResult(1, "", "cannot create snapshot: out of space\n", cmd)
            return real_run(cmd, *args, **kwargs)  # type: ignore[arg-type]

        fake.mock_run = run_  # type: ignore[method-assign]
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out), redirect_stderr(StringIO()):
            log = Log()
            eng = engine.Run("backup", "111", log, P1, sleep=lambda _s: None)
            res = engine.execute(eng, Cleanup(log), lambda _p, _r: True)
        assert res.aborted and f"could not take {P1}" in res.error
        assert any(c.startswith("zfs destroy rpool") for c in fake.calls), fake.calls
        assert not any(c.startswith("zfs destroy bpool") for c in fake.calls), fake.calls
        assert "could not remove" not in out.getvalue()
        assert _origin_points(fake) == []

    def test_interrupted_run_keeps_the_point_where_a_receive_began(self) -> None:
        """Decision 13, as 8b amends it (M4b-4): after a Ctrl-C the point
        stays where a partial receive needs it, and goes everywhere else; a
        dataset that reached it keeps it as this drive's anchor instead."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-08_08:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)  # the hourly lands
        real_pipe = fake.mock_run_pipe

        def pipe(cmd1: str, cmd2: str, log: object = None) -> RunResult:
            r = real_pipe(cmd1, cmd2, log)
            if "rpool/USERDATA/home" in cmd1 and not r.ok:
                raise KeyboardInterrupt  # the Ctrl-C that cut that receive
            return r

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
        assert fake.ds["backup/rpool/USERDATA/home"].token
        left = _origin_points(fake)
        assert left == [f"rpool/USERDATA/home@{P2}"]  # the partial resumes from it
        reached = [r for r in eng.started if P2 in fake.names(f"backup/{r}")]
        assert reached and all(
            f"zark_111_{P2[len('zark_') :]}" in fake.bookmarks(r)
            for r in reached
            if "bpool" not in r
        )
        # The hourly home holds is anchored: the next run continues from it.
        hourly = fake.ds["backup/rpool/USERDATA/home"].snaps[-1].guid
        assert any(b.guid == hourly for b in fake.ds["rpool/USERDATA/home"].bookmarks)

    def _interrupted(self, log_path: str, fail: str = "") -> tuple[FakeZfs, "engine.Run"]:
        """An interrupted run as above, logging to ``log_path``; ``fail``
        names a dataset whose point the fake refuses to destroy."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-08_08:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        real_pipe, real_run = fake.mock_run_pipe, fake.mock_run

        def pipe(cmd1: str, cmd2: str, log: object = None) -> RunResult:
            r = real_pipe(cmd1, cmd2, log)
            if "rpool/USERDATA/home" in cmd1 and not r.ok:
                raise KeyboardInterrupt
            return r

        def run_(cmd: str, *args: object, **kwargs: object) -> RunResult:
            if fail and cmd == f"zfs destroy {fail}@{P2}":
                return RunResult(1, "", "cannot destroy snapshot: dataset is busy\n", cmd)
            return real_run(cmd, *args, **kwargs)  # type: ignore[arg-type]

        fake.mock_run_pipe = pipe  # type: ignore[method-assign]
        fake.mock_run = run_  # type: ignore[method-assign]
        with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            log = Log(log_path)
            cleanup = Cleanup(log)
            eng = engine.Run("backup", "111", log, P2, sleep=lambda _s: None)
            try:
                _ = engine.execute(eng, cleanup, lambda _p, _r: True)
            except KeyboardInterrupt:
                pass
            cleanup.run()
        return fake, eng

    def test_an_interrupted_run_logs_every_point_it_removes_and_keeps(self) -> None:
        """M4bR-1 (M4bF-4 4a): the log names each destroy with its reason,
        what stays and why, and a destroy that failed; before, the destroys
        of the point left no line at all (review E4)."""
        log_path = os.path.join(tempfile.mkdtemp(), "zark.log")
        fake, eng = self._interrupted(log_path, fail="rpool/var/lib/docker")
        lines = Path(log_path).read_text(encoding="utf-8").splitlines()
        reached = [r for r in eng.started if P2 in fake.names(f"backup/{r}") and "bpool" not in r]
        assert reached
        for rel in reached:
            assert any(f"removed {rel}@{P2}: anchored for backup first" in ln for ln in lines), rel
        not_started = [
            r for r in engine.expected(eng.origin) if r not in eng.started and "bpool" not in r
        ]
        assert not_started
        for rel in not_started:
            if rel == "rpool/var/lib/docker":
                continue
            assert any(
                f"removed {rel}@{P2}: not on the backup, nothing to resume" in ln for ln in lines
            ), rel
        assert any(
            f"kept rpool/USERDATA/home@{P2}: partial receive to resume" in ln for ln in lines
        )
        assert any(
            f"could not remove rpool/var/lib/docker@{P2}: cannot destroy snapshot" in ln
            for ln in lines
        ), lines
        assert sorted(_origin_points(fake)) == sorted(
            [f"rpool/USERDATA/home@{P2}", f"rpool/var/lib/docker@{P2}"]
        )

    def test_every_point_names_its_drive(self) -> None:
        """8b (M4b-4): the point is taken with ``org.zark:point-for`` in the
        same ``zfs snapshot``, so one left in origin says whose it is."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 0)
        assert not _engine_run(fake, "backup", "111", P2).ok
        assert _origin_points(fake) == [f"rpool/USERDATA/home@{P2}"]
        with patch_sh(fake):
            assert anchors.point_owners(["rpool", "bpool"]) == {f"rpool/USERDATA/home@{P2}": "111"}
        takes = [c for c in fake.calls if c.startswith("zfs snapshot") and P2 in c]
        assert takes and all(c.startswith("zfs snapshot -o org.zark:point-for=111 ") for c in takes)

    def test_another_drives_point_is_anchored_for_it_before_it_goes(self) -> None:
        """8b (M4b-4): a point another drive's run left (its anchor failed)
        is bookmarked as that drive's anchor before this drive's run removes
        it; that drive then continues without a rollback."""
        fake = self._fresh(("backup", "other"))
        assert _engine_run(fake, "backup", "111", P1).ok
        assert _engine_run(fake, "other", "222", P1).ok
        fake.fail_cmds.add("rpool/USERDATA/home#zark_222_")  # every anchor of 222 on home
        assert _engine_run(fake, "other", "222", P2).ok
        fake.fail_cmds.clear()
        assert f"rpool/USERDATA/home@{P2}" in _origin_points(fake)
        guid = next(s.guid for s in fake.ds["rpool/USERDATA/home"].snaps if s.name == P2)
        assert _engine_run(fake, "backup", "111", P3).ok
        assert f"rpool/USERDATA/home@{P2}" not in _origin_points(fake)
        mark = next(
            b for b in fake.ds["rpool/USERDATA/home"].bookmarks if b.name == f"zark_222_{P2[5:]}"
        )
        assert mark.guid == guid
        asked: list[repl.State] = []

        def decide(plans: list[repl.DatasetPlan], _eng: engine.Run) -> bool:
            asked.extend(p.state for p in plans)
            return True

        assert _engine_run(fake, "other", "222", "zark_2026-10-10_10:00:00Z", decide).ok
        assert repl.State.ROLLBACK not in asked

    def test_an_anchor_that_fails_is_retried_once(self) -> None:
        """M3R-5 / M2P.7 (M4b-4): a failed ``zfs bookmark`` is tried again
        after a pause; when that works the point goes as usual."""
        fake = self._fresh()
        real = fake.mock_run
        failed: list[str] = []

        def failing_once(cmd: str, *args: object, **kwargs: object) -> RunResult:
            if cmd.startswith("zfs bookmark") and "rpool/USERDATA/home@" in cmd and not failed:
                failed.append(cmd)
                return RunResult(
                    1, "", "cannot create bookmark: pool I/O is currently suspended\n", cmd
                )
            return real(cmd, *args, **kwargs)  # type: ignore[arg-type]

        fake.mock_run = failing_once  # type: ignore[method-assign]
        slept: list[float] = []
        with patch_sh(fake), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            log = Log()
            eng = engine.Run("backup", "111", log, P1, sleep=slept.append)
            res = engine.execute(eng, Cleanup(log), lambda _p, _r: True)
        assert res.ok and failed
        assert engine.ANCHOR_RETRY_SECONDS in slept
        assert f"zark_111_{P1[5:]}" in fake.bookmarks("rpool/USERDATA/home")
        assert _origin_points(fake) == []

    def test_a_point_whose_anchor_exists_already_is_released(self) -> None:
        """A run stopped between the bookmark and the destroy left both: the
        existing anchor (same guid) counts, and the point goes."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.snap("rpool/USERDATA/home", P2)
        with patch_sh(fake), redirect_stdout(StringIO()):
            assert fake.mock_run(
                f"zfs bookmark rpool/USERDATA/home@{P2} rpool/USERDATA/home#zark_222_{P2[5:]}"
            ).ok
            assert anchors.release_point(f"rpool/USERDATA/home@{P2}", "222", Log())
        assert P2 not in fake.names("rpool/USERDATA/home")

    def test_a_point_whose_anchor_cannot_be_made_stays(self) -> None:
        """8b (M4b-4; mutation E9): a point that cannot be anchored for its
        drive is kept, since removing it would cost that drive a rollback."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.snap("rpool/USERDATA/home", P2)
        real = fake.mock_run

        def no_bookmarks(cmd: str, *args: object, **kwargs: object) -> RunResult:
            if cmd.startswith("zfs bookmark"):
                return RunResult(
                    1, "", "cannot create bookmark: pool I/O is currently suspended\n", cmd
                )
            return real(cmd, *args, **kwargs)  # type: ignore[arg-type]

        fake.mock_run = no_bookmarks  # type: ignore[method-assign]
        before = len(fake.calls)
        with patch_sh(fake), redirect_stdout(StringIO()):
            assert not anchors.release_point(f"rpool/USERDATA/home@{P2}", "222", Log())
        assert P2 in fake.names("rpool/USERDATA/home")
        assert not [c for c in fake.calls[before:] if c.startswith("zfs destroy")], fake.calls[
            before:
        ]

    def test_a_snapshot_already_anchored_is_not_bookmarked_again(self) -> None:
        """M4b-4 (mutation E6): the drive's newest snapshot already marked by
        a bookmark of this drive, under another name, gets no second one."""
        fake = self._fresh()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "h1")
        be = "rpool/ROOT/be"
        h1 = fake.ds[be].snaps[-1]
        # Carried to the drive after P1: its newest there, still in origin.
        fake.ds[f"backup/{be}"].snaps.append(FSnap(h1.name, h1.guid, fake._dtxg(), h1.creation))  # pylint: disable=protected-access
        with patch_sh(fake), redirect_stdout(StringIO()):
            assert fake.mock_run(
                f"zfs bookmark {be}@{h1.name} {be}#zark_111_2026-10-07_11:00:00Z"
            ).ok
            before = len(fake.calls)
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            assert be not in eng.anchor_held()
        marks = [c for c in fake.calls[before:] if c.startswith("zfs bookmark") and be in c]
        assert marks == [], marks


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


class TestBackupDecide:  # pylint: disable=too-many-public-methods
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
        assert "...and 3 more (full list in zark.log)" in out.getvalue()
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
        fake.ds["backup"].props["org.zark:format"] = drive_marks.FORMAT
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

    def test_keeping_the_newer_snapshots_is_offered_only_when_it_fits(self) -> None:
        """M4b-5: without room for the incrementals that follow, option 2 is
        the rollback; a full resend is never offered for ROLLBACK."""
        fake = self._with_rollback()
        offered: list[list[str]] = []

        def choice(_q: str, opts: list[str], _d: int = 0) -> int:
            offered.append(list(opts))
            return 1

        plans, eng = self._plans(fake)
        with (
            patch_sh(fake),
            patch.object(Log, "ask_choice", side_effect=choice),
            patch.object(Log, "ask_text", return_value="ROLLBACK"),
            patch("builtins.input", side_effect=EOFError),
            redirect_stdout(StringIO()),
        ):
            try:
                _ = _decide(plans, eng, "backup", PoolInfo("backup", avail_bytes=1), Log())
            except SystemExit:
                pass  # the transfer does not fit either
        assert len(offered[0]) == 2 and offered[0][1].startswith("roll back")
        assert not any("resend in full" in o for opts in offered for o in opts)

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

    def test_orphan_destroy_lists_what_goes_before_the_word(self) -> None:
        """M2V-5 N24: the ORPHAN destroy names every dataset it takes on the
        drive before DESTROY is asked, as the DIVERGED one does."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _ = fake.ds.pop("rpool/var/lib/docker")
        _ = fake.ds.pop("rpool/var/lib")
        plans, eng = self._plans(fake)
        out = StringIO()
        shown: list[str] = []

        def typed(*_a: object, **_k: object) -> str:
            shown.append(out.getvalue())
            return "DESTROY"

        with (
            patch_sh(fake),
            patch.object(Log, "ask_choice", side_effect=[2]),
            patch.object(Log, "ask_text", side_effect=typed),
            redirect_stdout(out),
        ):
            assert _decide(plans, eng, "backup", PoolInfo("backup", avail_bytes=10**12), Log())
        assert "DESTROY takes 2 dataset(s) on the backup:" in shown[0], shown[0]
        assert "backup/rpool/var/lib  (" in shown[0]
        assert "backup/rpool/var/lib/docker  (" in shown[0]
        assert "backup/rpool/var/lib" not in fake.ds

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

    def test_newer_snapshots_kept_and_each_lineage_continues(self) -> None:
        """M3R-1 (M4b-5): each rolled-ahead dataset keeps its newer snapshot
        in an archive, its children go back under it, and every one
        continues with an incremental from the common snapshot: nothing is
        destroyed and nothing is resent in full."""
        fake = self._var_tree_rolled_ahead()
        before = len(fake.calls)
        res = self._run_with_answers(fake, [1])
        assert res.ok, res
        calls = fake.calls[before:]
        for ds in ("rpool/var", "rpool/var/lib", "rpool/var/lib/docker"):
            kept = f"backup/{ds}.archived-{TODAY}"
            assert fake.names(kept) == [P2, "autosnap_gone"], ds  # newer than autosnap_h1
            assert fake.ds[kept].origin == f"backup/{ds}@autosnap_h1"
            names = fake.names(f"backup/{ds}")
            assert "autosnap_gone" not in names and names[-1] == P3 and P1 in names
        sends = [c for c in calls if c.startswith("zfs send ") and "rpool/var" in c]
        assert sends and all(" -i " in c or " -I " in c for c in sends if "-nvP" not in c)
        assert not any(
            c.startswith(("zfs destroy backup/rpool/var", "zfs rollback")) for c in calls
        )

    def test_the_clone_that_takes_the_lineages_place_carries_the_marks(self) -> None:
        """M4bR-4 (M4bF-9 9a, eli F1.6): a clone has a property set of its
        own, so the live dataset would sit with canmount=on and no org.zark
        marks until its next receive; the clone is made with them."""
        fake = self._var_tree_rolled_ahead()
        fake.inject["backup/rpool/var"] = ("interrupt", 0)  # the incremental never completes
        before = len(fake.calls)
        assert not self._run_with_answers(fake, [1]).ok
        clones = [c for c in fake.calls[before:] if c.startswith("zfs clone")]
        assert clones and all(
            " -o canmount=noauto -o org.zark:canmount=" in c and " -o org.zark:mountpoint=" in c
            for c in clones
        ), clones
        live = fake.ds["backup/rpool/var"].props
        assert live.get("canmount") == "noauto", live
        assert (
            live.get("org.zark:canmount") == "off" and live.get("org.zark:mountpoint") == "/var"
        ), live

    def test_the_kept_lineage_is_cloned_without_mounting(self) -> None:
        """I-G on the ROLLBACK keep path (mutation E15): the clone that takes
        the lineage's place is made with ``-u``; nothing mounts outside a
        command that asked for it."""
        fake = self._var_tree_rolled_ahead()
        before = len(fake.calls)
        assert self._run_with_answers(fake, [1]).ok
        clones = [c for c in fake.calls[before:] if c.startswith("zfs clone")]
        assert clones and all(c.startswith("zfs clone -u ") for c in clones), clones

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

    def _two_diverged(self) -> FakeZfs:
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        for ds in ("rpool/var", "rpool/var/lib", "rpool/var/lib/docker", "rpool/USERDATA/home"):
            fake.ds[ds].snaps.clear()
            fake.ds[ds].bookmarks.clear()
            fake.snap(ds, "autosnap_recreated")
        return fake

    def _joint(self, fake: FakeZfs, picks: list[int], typed: str = "") -> tuple[list[str], str]:
        asked: list[str] = []

        def choice(question: str, _opts: list[str], _default: int = 0) -> int:
            asked.append(question)
            return picks.pop(0)

        def decide(plans: list[repl.DatasetPlan], eng: engine.Run) -> bool:
            return _decide(plans, eng, "backup", PoolInfo("backup", avail_bytes=10**12), Log())

        out = StringIO()
        with (
            patch.object(Log, "ask_choice", side_effect=choice),
            patch.object(Log, "ask_text", return_value=typed),
            patch_sh(fake),
            redirect_stdout(out),
            redirect_stderr(StringIO()),
        ):
            eng = engine.Run("backup", "111", Log(), P3, sleep=lambda _s: None)
            _ = engine.execute(eng, Cleanup(Log()), decide)
        return asked, out.getvalue()

    def test_several_diverged_are_asked_once_and_skipped_by_default(self) -> None:
        """M3P.27 (M4b-11): one question for every diverged top; its default
        skips them all, children included, and changes nothing."""
        fake = self._two_diverged()
        tree = ("backup/rpool/var", "backup/rpool/USERDATA/home")
        before = {d: fake.names(d) for d in fake.ds if d.startswith(tree)}
        asked, _ = self._joint(fake, [0])
        assert asked == ["2 datasets have diverged:"]
        assert {d: fake.names(d) for d in fake.ds if d.startswith(tree)} == before

    def test_several_diverged_archived_together(self) -> None:
        """Archive all: one rename per top, each resent in full."""
        fake = self._two_diverged()
        asked, _ = self._joint(fake, [1])
        assert asked == ["2 datasets have diverged:"]
        renames = sorted(c for c in fake.calls if c.startswith("zfs rename backup/"))
        assert renames == [
            f"zfs rename backup/rpool/USERDATA/home backup/rpool/USERDATA/home.archived-{TODAY}",
            f"zfs rename backup/rpool/var backup/rpool/var.archived-{TODAY}",
        ]
        assert fake.names("backup/rpool/USERDATA/home") == [P3]

    def test_several_diverged_destroyed_after_listing_what_goes(self) -> None:
        """Destroy all: every dataset each destroy takes is listed before
        the typed DESTROY."""
        fake = self._two_diverged()
        asked, out = self._joint(fake, [2], typed="DESTROY")
        assert asked == ["2 datasets have diverged:"]
        assert "DESTROY takes 4 dataset(s) on the backup:" in out
        for ds in ("rpool/USERDATA/home", "rpool/var", "rpool/var/lib", "rpool/var/lib/docker"):
            assert f"backup/{ds}  (" in out
        assert not any(".archived-" in d for d in fake.ds)
        assert fake.names("backup/rpool/var/lib/docker") == [P3]

    def test_several_diverged_can_still_be_decided_one_by_one(self) -> None:
        """The last option asks today's question for each top."""
        fake = self._two_diverged()
        asked, _ = self._joint(fake, [3, 0, 1])
        assert asked == [
            "2 datasets have diverged:",
            "rpool/USERDATA/home has diverged:",
            "rpool/var has diverged:",
        ]
        assert [d for d in fake.ds if d.endswith(".archived-" + TODAY)] == [
            f"backup/rpool/var.archived-{TODAY}"
        ]

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
        """O1: "Rolled back rpool/ROOT/...@zark_...Z", not "rpool/root/...z"."""
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
        assert "    ...and 2 more (see the table above)" in text


# The real method, before any test patches it (M4cR-2).
_REAL_DATASETS_NEEDING_KEY = ZFS.datasets_needing_key


class _TickingClock:  # pylint: disable=too-few-public-methods
    """backup's ``datetime``, one second later at each call (M4aP.5): points
    are named to the second, so two runs of one test never share a name."""

    _ticks = itertools.count()

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        """The real time, plus one second more than at the previous call."""
        return datetime.now(tz) + timedelta(seconds=next(cls._ticks))


class TestBackupCommand:  # pylint: disable=too-many-public-methods
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
                patch.object(backup_mod, "datetime", _TickingClock),
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

    def test_the_completed_notification_comes_before_the_eject_question(self) -> None:
        """M4dF-3: the eject question waits for the user; the notification
        that the backup is done goes out before it, not after the answer."""
        seen: list[str] = []
        _, rc = self._run(
            _fresh_fake(),
            patch.object(backup_mod, "_notify", side_effect=lambda t, _m: seen.append(t)),
            patch.object(
                backup_mod,
                "prompt_eject_or_attach",
                side_effect=lambda *_a, **_k: seen.append("eject?"),
            ),
        )
        assert rc == 0, seen
        assert seen[-2:] == ["\u2705 Backup completed", "eject?"], seen

    def test_a_failed_read_back_says_the_drive_already_records_the_point(self) -> None:
        """M2X.2 (M4b-13): the drive's metadata is written before the export,
        so BACKUP NOT VERIFIED says the drive records the point while the
        registry's last backup stays as it was."""
        fake = _fresh_fake()
        out = StringIO()
        bad = Readback(imported=True, health="DEGRADED", reexported=True)
        cfg, rc = self._run(
            fake,
            patch.object(ZFS, "verify_exported_pool_readback", return_value=bad),
            redirect_stdout(out),
        )
        point = fake.ds["backup"].props["org.zark:last-point"]
        text = " ".join(out.getvalue().split())
        assert rc == 1 and "BACKUP NOT VERIFIED" in text
        assert f"backup already records {point} as its last backup point" in text
        assert not cfg.known_drives["backup"].last_backup_at

    def test_an_unread_key_status_stops_before_the_point(self) -> None:
        """M4c-19 (review §4.7): a failed keystatus read was "Key already
        loaded"; it is fatal now, before anything is written. The fake's
        `zfs get keystatus` fails and the real datasets_needing_key reads it,
        so the test fails if backup stops asking for a strict read (M4cR-2)."""
        fake = _fresh_fake()
        fake.fail_cmds.add("zfs get -H keystatus -r backup/rpool")
        out = StringIO()
        _, rc = self._run(
            fake,
            patch.object(ZFS, "datasets_needing_key", _REAL_DATASETS_NEEDING_KEY),
            redirect_stdout(out),
        )
        assert rc == 1
        assert "Cannot read the key status of backup: fake: injected failure" in out.getvalue()
        assert "Key already loaded" not in out.getvalue()
        assert not _origin_points(fake)

    def test_an_importable_bpool_elsewhere_does_not_stop_a_run_with_bpool_imported(self) -> None:
        """M4aV-3 Q4 (and the M2 review's N55): this system's bpool is
        imported and another disk's bpool is importable: require_boot_pool
        returns at once, and the run goes on."""
        fake = _fresh_fake()
        assert "bpool" in fake.pools
        fake.importable = {"bpool"}
        out = StringIO()
        _, rc = self._run(fake, redirect_stdout(out))
        assert rc == 0, out.getvalue()
        assert "bpool is not imported" not in out.getvalue()

    def test_a_drive_without_its_first_point_gets_it_at_the_next_backup(self) -> None:
        """M3P.25 (M4b-9): a drive whose first point was not recorded (prepared
        before rc5) records its oldest point at the start of the next backup."""
        fake = _fresh_fake()
        assert self._run(fake)[1] == 0
        first = next(n for n in fake.names("backup/rpool/USERDATA/home") if repl.POINT_RE.match(n))
        assert "org.zark:first-point" not in fake.ds["backup"].props  # no point before that run
        fake.clock += 3600
        assert self._run(fake)[1] == 0
        assert fake.ds["backup"].props["org.zark:first-point"] == first

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

    def test_the_banner_shows_the_pool_roots_available(self) -> None:
        """M3P.9: BACKUP COMPLETED shows the pool root's zfs used and
        available (as step 4 and the space plan do), not zpool's free."""
        out = StringIO()
        _, rc = self._run(_fresh_fake(), redirect_stdout(out))
        text = re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue())
        assert rc == 0
        assert f"Available:      {_sh.humanize_bytes(10**13)}" in text, text[-1500:]
        assert f"Used on target: {_sh.humanize_bytes(10**9)} (" in text

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
            replicated = repl.expected(survey.read_origin(["rpool", "bpool"]))
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

    def test_a_partial_whose_dataset_then_needs_a_decision_says_so(self) -> None:
        """M3R-15: without its partial state the dataset needs a decision this
        run no longer asks: the line says it is asked again next time."""
        fake = self._interrupted_then_written()
        home = fake.ds["rpool/USERDATA/home"]
        home.bookmarks.clear()  # every base the drive shares is gone from origin
        home.snaps = [x for x in home.snaps if "hourly" not in x.name]
        out = StringIO()
        with patch.object(Log, "ask_text", return_value="DISCARD"):
            _cfg, rc = self._run(
                fake, patch.object(Log, "ask_choice", return_value=1), redirect_stdout(out)
            )
        text = " ".join(out.getvalue().split())
        assert "(a partial transfer is discarded too, then asked again next time)" in text, text
        assert rc == 1, text

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
        assert status_lib.residue_line(1) in out.getvalue()
        assert "In origin: 1 snapshot(s) no registered drive needs" in out.getvalue()
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
        assert "no anchor" not in text and "no registered drive needs" not in text
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

    def test_two_runs_in_one_second_take_two_points(self) -> None:
        """M4aP.5: with the ticking clock the second run's point is its own."""
        fake = _fresh_fake()
        newest: list[str] = []
        with patch.object(_TickingClock, "_ticks", itertools.count()):
            for _ in range(2):
                assert self._run(fake)[1] == 0
                newest.append(fake.names("backup/rpool/ROOT/be")[-1])
        assert newest[0] != newest[1] and all(repl.POINT_RE.match(n) for n in newest), newest

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
        """A backup writes origin-machine-id: a drive whose owner was lost, and
        that holds this system's point, is accepted and owned again (M4a-7)."""
        fake = _fresh_fake()
        fake.ds["backup"].props["org.zark:origin-machine-id"] = "aaaa1111"
        with patch.object(drive_marks, "MACHINE_ID_PATH", self._machine_id("aaaa1111")):
            _, rc = self._run(fake)
            assert rc == 0
            del fake.ds["backup"].props["org.zark:origin-machine-id"]
            _, rc = self._run(fake)
        assert rc == 0
        assert fake.ds["backup"].props["org.zark:origin-machine-id"] == "aaaa1111"

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
            patch.object(drive_marks, "MACHINE_ID_PATH", self._machine_id("aaaa1111")),
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
        with patch.object(drive_marks, "MACHINE_ID_PATH", self._machine_id("aaaa1111")):
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
        with patch.object(drive_marks, "MACHINE_ID_PATH", missing):
            _, rc = self._run(fake)
        assert rc == 1
        assert "backup/rpool" not in fake.ds

    def test_unreadable_metadata_is_refused(self) -> None:
        """M2R-7 (E11b): `zfs get` fails on a drive that names another system:
        refused, not taken for a drive without an owner."""
        fake = _fresh_fake()
        fake.ds["backup"].props["org.zark:origin-machine-id"] = "bbbb2222"
        fake.fail_cmds.add("zfs get -H -s local -o property,value all")
        with patch.object(drive_marks, "MACHINE_ID_PATH", self._machine_id("aaaa1111")):
            _, rc = self._run(fake)
        assert rc == 1
        assert not any(c.startswith(("zfs snapshot", "systemctl stop")) for c in fake.calls)
        assert "backup/rpool" not in fake.ds

    def _untouched(self, fake: FakeZfs) -> None:
        assert not any(
            c.startswith(("zfs snapshot", "systemctl stop", "zfs set", "zfs destroy"))
            for c in fake.calls
        ), fake.calls
        assert "org.zark:origin-machine-id" not in fake.ds["backup"].props

    def test_blank_drive_without_owner_is_refused(self) -> None:
        """M2R-7 (M4a-7): no owner and nothing in common: refused, owner not written."""
        fake = _fresh_fake()
        del fake.ds["backup"].props["org.zark:origin-machine-id"]
        out = StringIO()
        _, rc = self._run(fake, patch("sys.stdout", out))
        assert rc == 1
        self._untouched(fake)
        assert "no owner recorded and nothing in common" in out.getvalue()
        # M4aR-7: a prepared drive shares its point with origin; what is
        # refused is a drive with nothing in common, not "never backed up".
        assert "No snapshot or bookmark in common with this system" in out.getvalue()
        assert "never backed up" not in out.getvalue()

    def test_unowned_drive_sharing_only_older_snapshots_is_accepted(self) -> None:
        """M4a review mutation O5: every dataset is ROLLBACK (origin lost the
        newest anchors), which still counts as something in common."""
        fake = _fresh_fake()
        _ = _engine_run(fake, "backup", "111", P1)
        _tick(fake, "2026-10-10_00:00:00_daily")
        _ = _engine_run(fake, "backup", "111", P2)
        for ds, d in fake.ds.items():  # origin loses the anchors of P2
            if ds.split("/")[0] in ("rpool", "bpool"):
                d.bookmarks = []
                d.snaps = [s for s in d.snaps if not s.name.startswith("zark_")]
        del fake.ds["backup"].props["org.zark:origin-machine-id"]
        with patch_sh(fake), redirect_stdout(StringIO()), patch("builtins.input", return_value=""):
            states = {
                p.state
                for p in engine.plan(
                    survey.read_origin(survey.source_pools(), strict=True),
                    survey.read_dest("backup", strict=True),
                    "",
                )
            }
            drive_marks.check_drive_owner("backup", Log())
        assert states & set(drive_marks.SHARED_STATES) == {engine.State.ROLLBACK}, states

    def test_other_systems_datasets_without_owner_are_refused(self) -> None:
        """M2R-7 (E11a): a registered drive holding another system's datasets
        and no owner: refused before any question, key or point."""
        fake = _fresh_fake()
        del fake.ds["backup"].props["org.zark:origin-machine-id"]
        for rel in ("backup/rpool", "backup/rpool/ROOT", "backup/rpool/ROOT/other"):
            fake.dataset(rel)
        _ = fake.snap("backup/rpool/ROOT/other", "autosnap_2026-10-01_00:00:00_daily")
        reached = AssertionError("reached past the owner check")
        _, rc = self._run(
            fake,
            patch.object(ZFS, "datasets_needing_key", side_effect=reached),
            patch("lib.log.Log.ask", side_effect=reached),
        )
        assert rc == 1
        self._untouched(fake)

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
            patch.object(drive_marks, "MACHINE_ID_PATH", self._machine_id("aaaa1111")),
            patch("builtins.input", return_value=""),
            redirect_stdout(out),
        ):
            try:
                drive_marks.check_drive_owner("backup", Log())
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


_NVME = "/dev/disk/by-id/nvme-Samsung_X_S1"
_USB = "/dev/disk/by-id/usb-Micron_X_SERIAL-0:0"


class TestM2R16Smalls:  # pylint: disable=missing-function-docstring,protected-access
    """M2R-16 (M4a-14): change-key failures in recover's verdict; prepare's
    keystore snapshot never left in origin."""

    def test_failed_change_key_is_an_error(self):
        mock = MockShell()
        mock.on("zfs list -H -o name,encryptionroot -r rpool").succeeds(
            "rpool\trpool\nrpool/ROOT\t-\nrpool/ROOT/be\trpool/ROOT/be\nrpool/home\trpool/home"
        )
        mock.on("zfs change-key -i rpool/ROOT/be").succeeds()
        mock.on("zfs change-key -i rpool/home").fails("key not loaded")
        with patch_sh(mock), redirect_stdout(StringIO()):
            errors = recover_mod._restore_encryption_root(make_log())
        assert errors == ["rpool/home: change-key -i failed (still its own encryptionroot)"]

    def test_keystore_snapshot_is_destroyed_when_the_send_dies(self):
        mock = MockShell()
        mock.on_prefix("zfs snapshot rpool/keystore@prepare_").succeeds()
        mock.on_prefix("zfs destroy rpool/keystore@prepare_").succeeds()
        log = make_log()
        cleanup = Cleanup(log)
        with (
            patch_sh(mock),
            patch.object(prepare_mod.sh, "run_pipe", side_effect=KeyboardInterrupt),
            redirect_stdout(StringIO()),
        ):
            try:
                _ = prepare_mod._send_keystore("blue", cleanup, log)
            except KeyboardInterrupt:
                pass
            assert mock.was_not_called("zfs destroy")
            cleanup.run()  # what the signal handler or atexit does
        assert mock.was_called("zfs destroy rpool/keystore@prepare_")


class TestBootPoolRefusal:  # pylint: disable=missing-function-docstring
    """M2V-2 (M4a-6): the bpool refusal tells this system's bpool from another disk's."""

    def _refusal(self, listing: str) -> str:
        mock = MockShell()
        mock.on("zpool list -H -o name bpool").fails("no such pool")
        mock.on("env LC_ALL=C ZPOOL_VDEV_NAME_PATH=1 zpool import").succeeds(listing)
        mock.on("zpool list -vHP rpool").succeeds(f"rpool\t1T\n\t{_NVME}-part4\t1T")
        mock.on_prefix(f"lsblk -nrs -o NAME,TYPE {_NVME}").succeeds("nvme0n1p4 part\nnvme0n1 disk")
        mock.on_prefix(f"lsblk -nrs -o NAME,TYPE {_USB}").succeeds("sdb1 part\nsdb disk")
        out = StringIO()
        with patch_sh(mock), patch("builtins.input", return_value=""), redirect_stdout(out):
            try:
                survey.require_boot_pool(make_log())
            except SystemExit:
                return unframed(out.getvalue())
        raise AssertionError("expected a refusal")

    @staticmethod
    def _bpool(pool_id: str, dev: str) -> str:
        return (
            f"   pool: bpool\n     id: {pool_id}\n  state: ONLINE\n config:\n\n"
            f"\tbpool  ONLINE\n\t  {dev}  ONLINE\n\n"
        )

    @staticmethod
    def _pool(name: str, pool_id: str, dev: str) -> str:
        return (
            f"   pool: {name}\n     id: {pool_id}\n  state: ONLINE\n config:\n\n"
            f"\t{name}  ONLINE\n\t  {dev}  ONLINE\n\n"
        )

    def test_only_a_pool_named_bpool_counts(self):
        """M2V-5 N54: bpool is not imported, and the importable pools are a
        drive and another disk's rpool: no bpool among them, no refusal."""
        mock = MockShell()
        mock.on("zpool list -H -o name bpool").fails("no such pool")
        mock.on("env LC_ALL=C ZPOOL_VDEV_NAME_PATH=1 zpool import").succeeds(
            self._pool("blue", "444", f"{_USB}-part1") + self._pool("rpool", "555", f"{_USB}-part4")
        )
        mock.on("zpool list -vHP rpool").succeeds(f"rpool\t1T\n\t{_NVME}-part4\t1T")
        out = StringIO()
        with patch_sh(mock), patch("builtins.input", return_value=""), redirect_stdout(out):
            survey.require_boot_pool(make_log())
        assert "bpool is not imported" not in out.getvalue()

    def test_own_bpool_is_imported_by_id(self):
        out = self._refusal(self._bpool("111", f"{_NVME}-part3"))
        assert "bpool id 111" in out and "on rpool's disk" in out
        assert "sudo zpool import 111" in out
        assert "zpool export" not in out

    def test_another_disks_bpool_is_never_advised_for_import(self):
        out = self._refusal(
            self._bpool("111", f"{_NVME}-part3") + self._bpool("222", f"{_USB}-part2")
        )
        assert "sudo zpool import 111" in out
        assert "bpool id 222" in out and "another disk's" in out
        assert "sudo zpool import 222" not in out and "sudo zpool import bpool" not in out
        assert "do not import it" in out

    def test_system_without_its_own_bpool(self):
        out = self._refusal(self._bpool("222", f"{_USB}-part2"))
        assert "this system has none of its own" in out
        assert "sudo zpool import" not in out

    def test_a_bpool_sharing_one_disk_with_rpool_is_this_systems(self):
        # M4aR-4 (review E2 case 5): a mirrored bpool, rpool on one of its disks.
        listing = (
            "   pool: bpool\n     id: 111\n  state: ONLINE\n config:\n\n"
            f"\tbpool  ONLINE\n\t  mirror-0  ONLINE\n\t    {_NVME}-part3  ONLINE\n"
            f"\t    {_USB}-part3  ONLINE\n\n"
        )
        out = self._refusal(listing)
        assert "on rpool's disk" in out and "sudo zpool import 111" in out
        assert "another disk's" not in out and "Disconnect" not in out
        assert "none of its own" not in out

    def test_a_bpool_without_a_readable_device_is_not_called_another_disks(self):
        # M4aR-4 (review E2 case 6): its only vdev is shown by guid.
        listing = (
            "   pool: bpool\n     id: 333\n  state: UNAVAIL\n config:\n\n"
            "\tbpool  UNAVAIL\n\t  1234567890123456789  UNAVAIL\n\n"
        )
        out = self._refusal(listing)
        assert "bpool id 333: no readable device; cannot tell whose it is" in out
        assert "sudo zpool import (it only lists)" in out
        assert "Disconnect" not in out and "another disk's" not in out
        assert "none of its own" not in out


class TestDriveMetadata:
    """Decision 17a: drive metadata written by backup, shown by recover and mount."""

    def test_round_trip_and_display(self):
        """What write_metadata stores, read_metadata and log_metadata show."""
        fake = _fresh_fake()
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out):
            assert drive_marks.write_metadata(
                "backup",
                {"format": "2", "version": "2.0.0-rc2", "origin-host": "eli", "last-point": P1},
                Log(),
            )
            meta = drive_marks.log_metadata("backup", Log())
        assert meta["format"] == "2" and meta["origin-host"] == "eli"
        assert f"written by zark 2.0.0-rc2 on eli, last point {P1}" in out.getvalue()

    def test_pre_v2_drive(self):
        """A drive without org.zark:* is named as written by an older zark."""
        fake = _fresh_fake()
        del fake.ds["backup"].props["org.zark:origin-machine-id"]
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out):
            assert not drive_marks.log_metadata("backup", Log())
        assert "zark <= 2.0.0-rc1" in out.getvalue()


def _marked(fake: FakeZfs, pool: str = "backup") -> set[str]:
    """Names of the backup points complete on a drive, as recover reads them."""
    with patch_sh(fake):
        return complete_points(drive_marks.drive_snapshots(pool), "")


class TestRecordCompleteWarning:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """M2V-7 (c): zfs set marks every target it can open, so only some are unmarked."""

    def test_the_warning_says_every_and_the_unmarked_ones(self):
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        fake.fail_cmds.add("zfs set org.zark:point-complete")
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out):
            rels = repl.expected(survey.read_origin(["rpool", "bpool"]))
            assert not drive_marks.record_complete("backup", P1, rels, Log())
        text = " ".join(out.getvalue().split())
        assert "Could not mark every complete point on backup:" in text, text
        assert "(recover lists the unmarked ones as incomplete)" in text, text


def _record(fake: FakeZfs, point: str, pool: str = "backup") -> None:
    """record_complete for every expected dataset, as backup does after a complete run."""
    with patch_sh(fake), redirect_stdout(StringIO()):
        rels = repl.expected(survey.read_origin(["rpool", "bpool"]))
        assert drive_marks.record_complete(pool, point, rels, Log())


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

    def test_the_newest_whole_point_is_kept_before_a_newer_partial_one(self) -> None:
        """M3V-1 (F5d): of the run-complete points, the one kept is the newest
        whole one; a newer one a rollback left partial does not replace it."""
        times = {_pt("2026-06-01"): _days_ago(30), _pt("2026-06-02"): _days_ago(29)}
        complete = set(times)
        assert ret.newest_complete(times, complete, {_pt("2026-06-01")}) == _pt("2026-06-01")
        assert ret.newest_complete(times, complete, set()) == _pt("2026-06-02")

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

    def test_the_recorded_first_point_keeps_the_history_boundary(self) -> None:
        """M3P.25 (M4b-9): once the policy removed the drive's first points,
        a sanoid snapshot after the recorded first point is still under the
        policy, not 1.x history; one before it stays history."""
        old = [
            ("autosnap_2026-03-01_00:00:00_monthly", _days_ago(320)),
            ("autosnap_2026-11-01_00:00:00_daily", _days_ago(75)),
        ]
        dest = _drive({self.BE: [*old, (_pt("2026-12-01"), _days_ago(45))]})
        complete = {_pt("2026-12-01")}
        assert ret.plan_prune(dest, complete, ret.Policy(), NOW).empty  # boundary moved
        first = repl.point_name(datetime.fromtimestamp(_days_ago(80), UTC))
        gone = _gone(ret.plan_prune(dest, complete, ret.Policy(), NOW, first=first))
        assert gone == {f"{self.BE}@autosnap_2026-11-01_00:00:00_daily"}

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
        assert not ret.whole_datasets(dest, p.snapshots)

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
            dest = survey.read_dest("backup", strict=True)
            complete = drive_marks.drive_complete("backup")
            return ret.plan_prune(
                dest,
                complete.run,
                policy or ret.Policy(),
                NOW,
                whole=complete.whole,
                pinned=drive_marks.drive_pins("backup"),
            )

    def test_prune_by_exact_name_keeps_bases_and_marks_true(self) -> None:
        """Every removal is an exact name, one `zfs destroy` per dataset
        (M3P.26); the next
        backup is still incremental; what is left of a pruned point is not
        complete (M2V-4)."""
        fake = self._drive_with_history()
        prune = self._plan(fake)
        gone = {r.target for r in prune.snapshots}
        assert f"rpool/ROOT/be@{P1}" in gone and f"rpool/ROOT/be@{P2}" not in gone
        before = len(fake.calls)
        with patch_sh(fake), redirect_stdout(StringIO()):
            count, failed = reclaim.apply_prune("backup", prune, Log())
        destroys = [c for c in fake.calls[before:] if c.startswith("zfs destroy")]
        assert count == len(prune.snapshots) and not failed
        assert all("%" not in c and " -r " not in c for c in destroys)
        named = [_destroyed(c) for c in destroys]
        assert sorted(t for ts in named for t in ts) == sorted(f"backup/{t}" for t in gone)
        assert len({ts[0].partition("@")[0] for ts in named}) == len(destroys)  # one per dataset
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

    def test_the_first_point_is_recorded_once_before_retention(self) -> None:
        """M3P.25 (M4b-9): a drive without org.zark:first-point gets its
        oldest backup point; a recorded one is never rewritten."""
        fake = self._drive_with_history()
        fake.ds["backup"].props.pop("org.zark:first-point", None)
        with patch_sh(fake), redirect_stdout(StringIO()):
            drive_marks.record_first_point("backup", Log())
            assert fake.ds["backup"].props["org.zark:first-point"] == P1
            assert drive_marks.drive_complete("backup").first == P1
            fake.ds["backup"].props["org.zark:first-point"] = P2
            drive_marks.record_first_point("backup", Log())
        assert fake.ds["backup"].props["org.zark:first-point"] == P2

    def test_destroys_are_grouped_per_dataset_at_most_100_names(self) -> None:
        """M3P.26 (M4b-10): 250 snapshots of one dataset and one of another:
        three commands for the first (100, 100, 50) and one plain one."""
        fake = FakeZfs()
        fake.pool("backup")
        fake.dataset("backup/a")
        fake.dataset("backup/b")
        names = [f"s{i:03}" for i in range(250)]
        for n in names:
            fake.snap("backup/a", n)
        fake.snap("backup/b", "only")
        targets = [(f"backup/a@{n}", "old") for n in names] + [("backup/b@only", "old")]
        with patch_sh(fake), redirect_stdout(StringIO()):
            count, failed = reclaim.destroy_snapshots(targets, Log())
        destroys = [c for c in fake.calls if c.startswith("zfs destroy")]
        assert (count, failed) == (251, [])
        assert [len(_destroyed(c)) for c in destroys] == [100, 100, 50, 1]
        assert destroys[-1] == "zfs destroy backup/b@only"
        assert not fake.names("backup/a") and not fake.names("backup/b")

    def test_a_group_that_fails_is_retried_one_by_one(self) -> None:
        """The kernel destroys none of a group when one name fails; the
        others then go one by one and only the held one is reported."""
        fake = FakeZfs()
        fake.pool("backup")
        fake.dataset("backup/a")
        for n in ("x", "y", "z"):
            fake.snap("backup/a", n)
        fake.ds["backup/a"].snaps[1].userrefs = 1  # zfs hold
        out = StringIO()
        with patch_sh(fake), redirect_stdout(out):
            count, failed = reclaim.destroy_snapshots(
                [(f"backup/a@{n}", "old") for n in ("x", "y", "z")], Log()
            )
        assert (count, failed) == (2, ["backup/a@y"])
        assert fake.names("backup/a") == ["y"]
        assert "removed backup/a@x: old" in out.getvalue()
        assert "could not remove backup/a@y" in out.getvalue()

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
            freed = reclaim.reclaim("backup", prune.snapshots)
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
                count, failed = reclaim.apply_prune("backup", prune, Log(logf))
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
            _count, failed = reclaim.apply_prune("backup", prune, Log())
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
            _ = reclaim.apply_prune("backup", prune, Log())
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
            dest = survey.read_dest("backup", strict=True)
            prune = ret.plan_prune(
                dest, drive_marks.drive_complete("backup").run, ret.Policy(), NOW
            )
            _ = reclaim.apply_prune("backup", prune, Log())
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
        assert "has no zark 2 anchor" not in out  # M4d-32: anchored everywhere
        # M4d-33: after the removal, the capacity waits for the pool to free it.
        assert "zpool get -Hp -o value freeing rpool" in fake.calls[before:]
        destroys = [
            c for c in fake.calls[before:] if c.startswith("zfs destroy ") and "-nvp" not in c
        ]
        assert destroys and all("%" not in c and " -r" not in c for c in destroys)
        assert len({_destroyed(c)[0].partition("@")[0] for c in destroys}) == len(destroys)
        assert not any(n.startswith("autosnap_") for n in fake.names("rpool/USERDATA/home"))
        assert fake.bookmarks("rpool/USERDATA/home")
        with patch_sh(fake), redirect_stdout(StringIO()):
            eng = engine.Run("backup", "111", Log(), P2, sleep=lambda _s: None)
            assert eng.take_point()
            plans = {p.rel: p.state for p in eng.survey()}
        assert plans["rpool/USERDATA/home"] is repl.State.VIA_BOOKMARK

    def test_a_drive_without_a_zark_2_anchor_is_warned_of(self) -> None:
        """M4d-32 (carmen, 2026-10-19): a 1.x drive's base on each dataset is a
        sanoid snapshot the age cut can remove; it is named before the count."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        cfg = make_config()
        cfg.known_drives["backup"] = DriveInfo("backup", "111", "d")
        cfg.known_drives["black"] = DriveInfo("black", "222", "e")  # never had a zark 2 backup
        now = fake.clock + 10 * ret.DAY
        out = self._origin(
            fake,
            cfg,
            *self._answers([1, 0], ["5"], "no", False),
            patch.object(prune_mod.time, "time", return_value=float(now)),
        )
        text = " ".join(Log._STRIP_RE.sub("", out).split())  # pylint: disable=protected-access
        assert (
            "black has no zark 2 anchor on bpool/BOOT/be, rpool/ROOT/be, rpool/USERDATA/home"
            + " and 3 more: its base there is a sanoid snapshot this can remove; "
            + "back up to it first"
        ) in text, text
        assert text.index("black has no zark 2 anchor") < text.index(
            "sanoid snapshot(s) older than 5 days"
        )
        assert "backup has no" not in text

    def test_the_warning_names_only_drives_unanchored_where_the_cut_touches(self) -> None:
        """M4dR-7: ``unanchored`` lists a drive for every dataset it lacks an
        anchor on; the cut warns of the ones whose snapshots it removes."""
        inv = oprune.Inventory(
            autosnaps={"daily": [oprune.Item("rpool/A@autosnap_2026-01-01_00:00:01_daily", 100)]},
            unanchored={"rpool/A": ["amber"], "rpool/B": ["black"]},
        )
        out = StringIO()
        with (
            patch_sh(MockShell()),
            redirect_stdout(out),
            patch.object(Log, "ask_input", return_value="1"),
            patch.object(prune_mod, "_typed", return_value=False),
            patch.object(prune_mod.reclaim, "reclaim_names", return_value=0),
            patch.object(prune_mod.time, "time", return_value=float(10 * ret.DAY)),
        ):
            prune_mod._by_age(inv, Log())  # pylint: disable=protected-access
        text = " ".join(Log._STRIP_RE.sub("", out.getvalue()).split())  # pylint: disable=protected-access
        assert "amber has no zark 2 anchor on rpool/A: its base there" in text, text
        assert "black" not in text, text

    def test_the_warning_is_one_line_per_drive_with_at_most_three_datasets(self) -> None:
        """M4dR-3: a drive that lacks an anchor on many datasets is one line."""
        rels = [f"rpool/d{i}" for i in range(5)]
        inv = oprune.Inventory(
            autosnaps={
                "daily": [oprune.Item(f"{r}@autosnap_2026-01-01_00:00:01_daily", 100) for r in rels]
            },
            unanchored={r: ["amber", "black"] if r != "rpool/d4" else ["black"] for r in rels},
        )
        out = StringIO()
        with (
            patch_sh(MockShell()),
            redirect_stdout(out),
            patch.object(Log, "ask_input", return_value="1"),
            patch.object(prune_mod, "_typed", return_value=False),
            patch.object(prune_mod.reclaim, "reclaim_names", return_value=0),
            patch.object(prune_mod.time, "time", return_value=float(10 * ret.DAY)),
        ):
            prune_mod._by_age(inv, Log())  # pylint: disable=protected-access
        text = " ".join(Log._STRIP_RE.sub("", out.getvalue()).split())  # pylint: disable=protected-access
        assert text.count("amber has no zark 2 anchor") == 1, text
        assert "amber has no zark 2 anchor on rpool/d0, rpool/d1, rpool/d2 and 1 more:" in text
        assert "black has no zark 2 anchor on rpool/d0, rpool/d1, rpool/d2 and 2 more:" in text

    @staticmethod
    def _capacity(freeing: list[int], settle: bool) -> tuple[str, list[str]]:
        """rpool's freeing figures in order (the last one repeats); output, calls."""
        mock = MockShell()
        mock.on("zpool list -Hp -o name,capacity,size,free rpool").succeeds(
            f"rpool\t60\t{100 << 30}\t{40 << 30}"
        )
        values = iter(freeing)
        last = {"v": freeing[-1]}
        real = mock.mock_run

        def spy(cmd: str, **kw: Any) -> RunResult:
            if cmd == "zpool get -Hp -o value freeing rpool":
                mock._calls.append(cmd)  # pylint: disable=protected-access
                last["v"] = next(values, last["v"])
                return RunResult(0, f"{last['v']}\n", "", cmd)
            return real(cmd, **kw)

        clock = itertools.count(0, 10)  # ten seconds per read of the clock
        out = StringIO()
        with (
            patch.object(mock, "mock_run", side_effect=spy),
            patch_sh(mock),
            patch.object(prune_mod.survey, "source_pools", return_value=["rpool"]),
            patch.object(prune_mod.time, "sleep"),
            patch.object(prune_mod.time, "monotonic", side_effect=lambda: next(clock)),
            redirect_stdout(out),
        ):
            prune_mod._capacity(Log(), settle=settle)  # pylint: disable=protected-access
        return Log._STRIP_RE.sub("", out.getvalue()), mock.calls  # pylint: disable=protected-access

    def test_the_capacity_after_a_removal_waits_for_the_pool_to_free_it(self) -> None:
        """M4d-33: `zpool list` right after the destroys lags the async free."""
        out, calls = self._capacity([5 << 30, 1 << 30, 0], settle=True)
        assert "Waiting for rpool to free what was removed..." in out
        assert "rpool: 60% full (40.0G free of 100.0G)" in out and "still freeing" not in out
        assert calls.count("zpool get -Hp -o value freeing rpool") == 3
        assert calls.index("zpool list -Hp -o name,capacity,size,free rpool") == len(calls) - 1

    def test_a_pool_still_freeing_at_the_cap_is_said_so(self) -> None:
        """M4d-33: at about 60 s the line says the figure will grow."""
        out, _ = self._capacity([5 << 30], settle=True)
        assert "still freeing in the background, the free space will grow" in out

    def test_no_wait_before_any_removal(self) -> None:
        """M4d-33: the menu's first figures are read at once."""
        out, calls = self._capacity([5 << 30], settle=False)
        assert not [c for c in calls if "freeing" in c] and "Waiting" not in out

    def test_mistyped_word_removes_nothing(self) -> None:
        """A typed word that is not PRUNE removes nothing."""
        fake = _fresh_fake()
        before = {ds: fake.names(ds) for ds in fake.ds}
        out = self._origin(fake, make_config(), *self._answers([1, 0], ["0"], "prune", True))
        assert before == {ds: fake.names(ds) for ds in fake.ds}
        assert "Not confirmed (PRUNE was not typed) - nothing removed" in out

    def test_interrupted_points_warn_of_both_effects(self) -> None:
        """M3 decision 8a (eli R8.7), shrunk by 8b (M4b-4): each point names
        its drive, and the warning says it is anchored for it first; the
        rollback remains for points without a named drive, the resend for all."""
        fake = _fresh_fake()
        assert _engine_run(fake, "backup", "111", P1).ok
        _tick(fake, "2026-10-07_12:00:00_hourly")
        fake.inject["backup/rpool/USERDATA/home"] = ("interrupt", 1)
        assert not _engine_run(fake, "backup", "111", P2).ok
        assert _origin_points(fake)

        picked: list[str] = []

        def pick(_q: str, options: list[str], _d: int = 0) -> int:
            wanted = (
                "remove backup points left in origin (interrupted runs, a run whose anchor "
                + "failed, or a recovery)"
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
        assert f"rpool/USERDATA/home@{P2} (for drive 111)" in text
        assert (
            "A point that names its drive is first anchored for it (on bpool it becomes that "
            + "drive's anchor), so that drive does not roll back; without a named drive, a "
            + "drive that received part of the point rolls those datasets back at its next "
            + "backup (it asks first). Either way, a drive whose transfer was interrupted "
            + "discards it and sends it again"
        ) in text
        assert _origin_points(fake)

    def test_a_point_that_names_its_drive_is_anchored_for_it_first(self) -> None:
        """8b (M4b-4): a confirmed removal bookmarks a named point as its
        drive's anchor before destroying it."""
        fake = _fresh_fake(("backup", "other"))
        assert _engine_run(fake, "other", "222", P1).ok
        fake.fail_cmds.add("rpool/USERDATA/home#zark_222_")  # every anchor of 222 on home
        assert _engine_run(fake, "other", "222", P2).ok
        fake.fail_cmds.clear()
        guid = next(s.guid for s in fake.ds["rpool/USERDATA/home"].snaps if s.name == P2)
        wanted = (
            "remove backup points left in origin (interrupted runs, a run whose anchor failed, "
            + "or a recovery)"
        )
        picked: list[str] = []

        def pick(_q: str, options: list[str], _d: int = 0) -> int:
            if wanted in options and not picked:
                picked.append(wanted)
                return options.index(wanted)
            return 0

        out = self._origin(
            fake,
            make_config(),
            patch.object(Log, "ask_choice", side_effect=pick),
            patch.object(Log, "ask_text", return_value=prune_mod.WORD),
            patch("builtins.input", side_effect=EOFError),
        )
        assert f"rpool/USERDATA/home@{P2}  (for drive 222)" in out
        assert _origin_points(fake) == []
        assert fake.calls.count(f"zfs destroy rpool/USERDATA/home@{P2}") == 1  # once, released
        mark = next(
            b for b in fake.ds["rpool/USERDATA/home"].bookmarks if b.name == f"zark_222_{P2[5:]}"
        )
        assert mark.guid == guid

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
            dest = survey.read_dest("backup", strict=True)
            prune = ret.plan_prune(
                dest, drive_marks.drive_complete("backup").run, ret.Policy(), NOW
            )
            _ = reclaim.apply_prune("backup", prune, Log())
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

    def _prune_drive_run(
        self, fake: FakeZfs, *answers: AbstractContextManager[object]
    ) -> tuple[str, bool]:
        """``zark prune`` on the drive ``backup``; its output and whether it
        stopped (fatal)."""
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
                *answers,
            ):
                stack.enter_context(cm)
            try:
                prune_mod._prune_drive(cfg, drive, Log())  # pylint: disable=protected-access
            except SystemExit:
                return out.getvalue(), True
        return out.getvalue(), False

    def test_what_the_policy_cannot_remove_is_named(self) -> None:
        """M3X.5: a held snapshot the policy would remove is reported as kept,
        with why, as backup's retention does."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        with patch_sh(fake), redirect_stdout(StringIO()):
            dest = survey.read_dest("backup", strict=True)
            prune = ret.plan_prune(
                dest, drive_marks.drive_complete("backup").run, ret.Policy(), NOW
            )
        held = prune.snapshots[0]
        rel, _, name = held.target.partition("@")
        next(x for x in fake.ds[f"backup/{rel}"].snaps if x.name == name).userrefs = 1
        out, stopped = self._prune_drive_run(fake, *self._answers([], ["0"], "", False))
        assert not stopped
        assert f"kept backup/{held.target}: held (zfs hold)" in out

    def test_prune_reads_the_recorded_first_point(self) -> None:
        """M3P.25 (M4b-9): with the first point gone from the drive, the sanoid
        snapshots after the recorded one stay under the policy: removed by
        it, never offered as 1.x history."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        first = repl.point_name(datetime.fromtimestamp(_days_ago(70), UTC))
        fake.ds["backup"].props["org.zark:first-point"] = first
        for ds, d in fake.ds.items():
            if ds.startswith("backup/"):
                d.snaps = [x for x in d.snaps if x.name != P1]
        out, stopped = self._prune_drive_run(fake, *self._answers([], ["0"], "", False))
        assert not stopped
        assert "retention removes nothing" not in out  # the policy takes them
        assert "1.x history" not in out

    def test_history_before_the_first_point_is_named_by_its_date_not_its_writer(self) -> None:
        """M4bF-12 (eli F2.4): a sanoid snapshot older than the drive's first
        backup point is offered as what it is, history from before that
        point, whoever wrote the drive; a 2.x drive gets one when a dataset
        arrives with older snapshots. "1.x" is said nowhere."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        for ds, d in fake.ds.items():
            if ds.startswith("backup/"):
                for snap in d.snaps:
                    if snap.name == "autosnap_2026-10-02_08:00:00_hourly":
                        snap.creation = _days_ago(80)  # before P1, 70 days ago
        typed: list[str] = []

        def ask_text(_self: Log, prompt: str, **_kw: object) -> str:
            typed.append(prompt)
            return "PRUNE"

        answers = self._answers([], ["0"], "", False)
        answers[2] = patch.object(Log, "ask_text", ask_text)
        out, stopped = self._prune_drive_run(fake, *answers)
        assert not stopped
        assert "from before its first backup point" in out
        assert "older than the drive's first backup point, removed by hand" in out
        assert typed == [
            "    Type PRUNE to remove the history from before backup's first backup point: "
        ]
        assert "1.x" not in out
        assert "autosnap_2026-10-02_08:00:00_hourly" not in fake.names("backup/rpool/ROOT/be")

    def test_a_failed_read_after_the_retention_is_fatal(self) -> None:
        """M3V-2 (F8c): the drive read again after the retention is applied
        must be read in full; a failed listing stops prune before the
        oldest points are offered."""
        fake = TestRetentionOnDrive()._drive_with_history()  # pylint: disable=protected-access
        real = reclaim.apply_prune

        def apply_then_fail(pool: str, prune: ret.Prune, log: Log) -> tuple[int, list[str]]:
            r = real(pool, prune, log)
            fake.fail_cmds.add("zfs list -Hp -t filesystem,volume")
            return r

        out, stopped = self._prune_drive_run(
            fake,
            patch.object(prune_mod.reclaim, "apply_prune", side_effect=apply_then_fail),
            *self._answers([], ["1"], "PRUNE", True),
        )
        assert stopped and "Cannot read backup" in out
        assert "Beyond the policy" not in out

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
            shared=["blue: no anchor in origin; 1 dataset(s) share no snapshot with it: ..."],
            residues=23,
        )
        text = status_lib.motd_text(st)
        assert f"rpool is 86% full (warning from 80%): {zark_command()} prune" in text
        assert "bpool" not in text
        assert "zark: last backups: black 3 day(s) ago, blue 34 day(s) ago" in text
        assert "blue: no anchor in origin" in text
        assert "zark: In origin: 23 snapshot(s) no registered drive needs (`syncoid_*`" in text

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
        assert not good.shared and not good.read_error
        assert not bad.shared and bad.read_error.startswith("could not read the source pools")
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
        mock.on("loginctl show-session 2 -p Type -p Class -p Active -p Name -p User").succeeds(
            "Type=wayland\nClass=user\nActive=yes\nName=br0th3r\nUser=1000\n"
        )
        mock.on("loginctl show-session 3 -p Type -p Class -p Active -p Name -p User").succeeds(
            "Type=tty\nClass=user\nActive=yes\nName=root\nUser=0\n"
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
        """A session that is not its seat's active one (Active=no) gets nothing
        (M3R-13, A5)."""
        mock = MockShell()
        mock.on("loginctl list-sessions --no-legend").succeeds(
            "2 1000 br0th3r seat0 tty2\n4 1001 guest seat0 tty3\n"
        )
        mock.on("loginctl show-session 2 -p Type -p Class -p Active -p Name -p User").succeeds(
            "Type=wayland\nClass=user\nActive=yes\nName=br0th3r\nUser=1000\n"
        )
        mock.on("loginctl show-session 4 -p Type -p Class -p Active -p Name -p User").succeeds(
            "Type=wayland\nClass=user\nActive=no\nName=guest\nUser=1001\n"
        )
        mock.on_prefix("runuser").succeeds("")
        with patch_sh(mock):
            assert notify_mod.notify("zark: disk space", "rpool is 92% full") == 1
        calls = [c for c in mock.calls if c.startswith("runuser")]
        assert len(calls) == 1 and "-u br0th3r " in calls[0]

    def test_a_greeter_on_an_idle_seat_is_not_notified(self) -> None:
        """M3P.1 (M4d-6 6b): with nobody logged in, the login screen is the
        seat's active session (Class=greeter): it gets nothing."""
        mock = MockShell()
        mock.on("loginctl list-sessions --no-legend").succeeds("c1 120 gdm seat0 tty1\n")
        mock.on("loginctl show-session c1 -p Type -p Class -p Active -p Name -p User").succeeds(
            "Type=wayland\nClass=greeter\nActive=yes\nName=gdm\nUser=120\n"
        )
        mock.on_prefix("runuser").succeeds("")
        with patch_sh(mock):
            assert notify_mod.notify("zark: disk space", "rpool is 92% full") == 0
        assert mock.was_not_called("runuser")

    def test_a_hung_session_bus_does_not_hold_the_notification(self) -> None:
        """M4d-6 6b: notify-send runs with a timeout."""
        seen: list[int | None] = []
        mock = MockShell()
        mock.on("loginctl list-sessions --no-legend").succeeds("2 1000 br0th3r seat0 tty2\n")
        mock.on("loginctl show-session 2 -p Type -p Class -p Active -p Name -p User").succeeds(
            "Type=wayland\nClass=user\nActive=yes\nName=br0th3r\nUser=1000\n"
        )
        real = mock.mock_run

        def spy(cmd: Any, *a: Any, **k: Any) -> RunResult:
            if str(cmd).startswith("runuser"):
                seen.append(k.get("timeout"))
            return real(cmd, *a, **k)

        with patch.object(mock, "mock_run", side_effect=spy), patch_sh(mock):
            _ = notify_mod.notify("t", "b")
        assert seen == [notify_mod.NOTIFY_TIMEOUT_SEC], seen

    def test_backup_notifies_through_loginctl(self) -> None:
        """M3P.1: backup's notifications go through lib.notify, which finds
        Wayland sessions too (backup used `who`, which lists none)."""
        with patch.object(notify_mod, "notify", return_value=1) as sent:
            backup_mod._notify("zark: backup completed", "Duration: 1m 2s")  # pylint: disable=protected-access
        sent.assert_called_once_with("zark: backup completed", "Duration: 1m 2s")

    def test_the_report_shows_the_critical_level_as_an_error(self) -> None:
        """M3P.23: `zark status` prints the critical CAP level as [ERROR] (red,
        as the MOTD shows it) and the warning level as [WARN]."""
        out = StringIO()
        with redirect_stdout(out):
            status_mod._report(status_lib.Status(caps={"rpool": 92, "bpool": 85}), Log())  # pylint: disable=protected-access
        lines = out.getvalue().splitlines()
        rpool = next(ln for ln in lines if "rpool: 92% full (critical)" in ln)
        bpool = next(ln for ln in lines if "bpool: 85% full (warning)" in ln)
        assert "[ERROR]" in rpool and "[WARN]" in bpool, lines

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
        assert sent == [f"rpool is 85% full - free space with: {zark_command()} prune"]


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

    def test_a_second_install_is_already_current(self) -> None:
        """M4aP.8: nothing to write and the timer enabled and running: the
        same "already current" as the guards, and no enable."""
        mock = MockShell()
        mock.on("systemctl is-enabled zark-status.timer").succeeds("enabled")
        mock.on("systemctl is-active zark-status.timer").succeeds("active")
        mock.on_prefix("systemctl").succeeds("")
        with tempfile.TemporaryDirectory() as root, patch_sh(mock):
            with redirect_stdout(StringIO()):
                alerts_mod.install(make_log(), root, "/opt/zark/zark")
            enables = mock.calls.count("systemctl enable --now zark-status.timer")
            out = StringIO()
            with redirect_stdout(out):
                alerts_mod.install(make_log(), root, "/opt/zark/zark")
        assert "Space and backup-age alerts already current" in out.getvalue()
        assert mock.calls.count("systemctl enable --now zark-status.timer") == enables == 1

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
        """prerm runs `zark setup --remove` on remove, not on upgrade (M3P.2)."""
        prerm = (Path(__file__).resolve().parent.parent / "debian/prerm").read_text(
            encoding="utf-8"
        )
        assert "remove)" in prerm and "/usr/share/zark/zark setup --remove" in prerm
        assert "upgrade)" not in prerm.split("remove)")[0]
        assert "alerts remove" not in prerm

    def test_setup_remove_takes_what_setup_installed(self) -> None:
        """M3P.2 (M4d-7 7c): the alerts, the apt guard and the grub guard go;
        nothing of zark's is left; it ends 0."""
        mock = MockShell()
        mock.on_prefix("systemctl").succeeds("")
        with tempfile.TemporaryDirectory() as root, patch_sh(mock), redirect_stdout(StringIO()):
            apt_guard.install(target_root=root, log=make_log())
            grub_guard.install(target_root=root, log=make_log())
            alerts_mod.install(make_log(), root, "/opt/zark/zark")
            assert setup_mod.remove_installed(make_log(), root) == 0
            left = [str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file()]
        assert left == [], left
        assert "systemctl disable --now zark-status.timer" in mock.calls

    def test_setup_remove_ends_1_when_a_file_is_not_zarks(self) -> None:
        """M4dR-8: a file that is not zark's stays and is named, and the
        status is 1 (the package's prerm goes on, it does not fail on it)."""
        mock = MockShell()
        mock.on_prefix("systemctl").succeeds("")
        out = StringIO()
        with tempfile.TemporaryDirectory() as root, patch_sh(mock), redirect_stdout(out):
            apt_guard.install(target_root=root, log=make_log())
            hook = Path(root, apt_guard.HOOK_RELATIVE_PATH)
            hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
            assert setup_mod.remove_installed(make_log(), root) == 1
            assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
        assert "Left, not zark's" in out.getvalue()
        with patch.object(setup_mod, "remove_installed", return_value=1):
            try:
                setup_mod.run(["--remove"])
            except SystemExit as e:
                assert e.code == 1
            else:
                raise AssertionError("setup --remove ended 0 with a file left")
        prerm = (Path(__file__).resolve().parent.parent / "debian/prerm").read_text(
            encoding="utf-8"
        )
        assert "/usr/share/zark/zark setup --remove ||" in prerm  # prerm goes on

    def test_setup_remove_with_another_argument_is_a_usage_error(self) -> None:
        """M4dR-4: it used to fall through to the full interactive setup."""
        for args in (["--remove", "--foo"], ["--foo", "--remove"], ["--remove", "--remove"]):
            with (
                patch.object(setup_mod, "remove_installed") as removed,
                patch.object(setup_mod, "ZFS") as zfs_cls,
                redirect_stdout(StringIO()),
            ):
                try:
                    setup_mod.run(args)
                except SystemExit as e:
                    assert e.code == 2, args
                else:
                    raise AssertionError(f"setup {args} went on")
            assert not removed.called and not zfs_cls.called, args  # nothing ran


class TestWaitFreed:  # pylint: disable=missing-function-docstring
    """M5a-11: a destroy returns before its blocks are free; the plan of a
    backup counts them as available, so the transfer waits for them."""

    @staticmethod
    def _spy(pool: str, figures: list[int]) -> tuple[MockShell, Any]:
        mock = MockShell()
        values = iter(figures)
        last = {"v": figures[-1]}
        real = mock.mock_run

        def spy(cmd: str, **kw: Any) -> RunResult:
            if cmd == f"zpool get -Hp -o value freeing {pool}":
                mock._calls.append(cmd)  # pylint: disable=protected-access
                last["v"] = next(values, last["v"])
                return RunResult(0, f"{last['v']}\n", "", cmd)
            return real(cmd, **kw)

        return mock, patch.object(mock, "mock_run", side_effect=spy)

    def _wait(self, figures: list[int]) -> tuple[list[str], str, MockShell]:
        mock, spy = self._spy("backup", figures)
        clock = itertools.count(0, 10)  # ten seconds per read of the clock
        out = StringIO()
        with (
            spy,
            patch_sh(mock),
            patch("lib.reclaim.time.sleep"),
            patch("lib.reclaim.time.monotonic", side_effect=lambda: next(clock)),
            redirect_stdout(out),
        ):
            left = reclaim.wait_freed(["backup"], Log())
        return left, Log._STRIP_RE.sub("", out.getvalue()), mock  # pylint: disable=protected-access

    def test_it_waits_until_the_pool_has_freed_what_was_removed(self) -> None:
        left, out, mock = self._wait([5 << 30, 1 << 30, 0])
        assert left == [] and "Waiting for backup to free what was removed..." in out
        assert mock.call_count("zpool get -Hp -o value freeing backup") == 3

    def test_it_gives_up_at_the_cap_and_names_the_pool(self) -> None:
        left, _out, _mock = self._wait([5 << 30])
        assert left == ["backup"]

    def test_a_pool_that_is_not_freeing_is_not_waited_for(self) -> None:
        left, out, mock = self._wait([0])
        assert left == [] and "Waiting" not in out
        assert mock.call_count("zpool get -Hp -o value freeing backup") == 1

    def test_a_figure_that_cannot_be_read_is_nothing_to_wait_for(self) -> None:
        mock = MockShell()
        mock.on("zpool get -Hp -o value freeing backup").fails("no such pool")
        with patch_sh(mock), redirect_stdout(StringIO()):
            assert reclaim.wait_freed(["backup"], Log()) == []

    def test_backup_waits_after_its_changes_and_before_the_transfer(self) -> None:
        order: list[str] = []
        mock, spy = self._spy("backup", [4 << 30, 0])
        out = StringIO()
        with (
            spy,
            patch_sh(mock),
            patch("lib.reclaim.time.sleep"),
            redirect_stdout(out),
        ):
            backup_mod._apply_changes(  # pylint: disable=protected-access
                [lambda: order.append("a"), lambda: order.append("b")], "backup", Log()
            )
            assert order == ["a", "b"]
            assert mock.call_count("zpool get -Hp -o value freeing backup") == 2
            assert "Waiting for backup" in out.getvalue()
            before = mock.call_count("zpool get")
            backup_mod._apply_changes([], "backup", Log())  # pylint: disable=protected-access
            assert mock.call_count("zpool get") == before

    def test_the_banner_space_is_read_again_after_retention_freed_something(self) -> None:
        # M4dP.2 (M5a-10): "Available:" was read before retention ran.
        pool = "backup"
        info = f"zpool list -H -o name,guid,health,size,alloc,free {pool}"
        sizes = iter([(100, 40), (60, 80)])  # (used, avail) before and after the free
        mock, spy = self._spy(pool, [0])

        def reads(cmd: str, **kw: Any) -> RunResult:
            if cmd == f"zfs list -H -o used,avail -p {pool}":
                used, avail = next(sizes)
                return RunResult(0, f"{used << 30}\t{avail << 30}\n", "", cmd)
            return real(cmd, **kw)

        real = mock.mock_run
        mock.on(info).succeeds(f"{pool}\t1\tONLINE\t140G\t100G\t40G")
        with (
            spy,
            patch.object(mock, "mock_run", side_effect=reads),
            patch_sh(mock),
            redirect_stdout(StringIO()),
        ):
            zfs = ZFS(make_log())
            first = zfs.pool_info(pool)
            assert first is not None and first.avail_bytes == 40 << 30
            same = backup_mod._space_after_retention(zfs, pool, first, 0, Log())  # pylint: disable=protected-access
            assert same is first  # nothing removed: not read again
            after = backup_mod._space_after_retention(zfs, pool, first, 3, Log())  # pylint: disable=protected-access
        assert after is not None and after.avail_bytes == 80 << 30
        gone = MockShell()
        gone.on_prefix("zpool get").succeeds("0")
        with patch_sh(gone), redirect_stdout(StringIO()):  # the read fails: the first stays
            kept = backup_mod._space_after_retention(ZFS(make_log()), pool, first, 3, Log())  # pylint: disable=protected-access
        assert kept is first

    def test_backup_says_so_when_the_pool_is_still_freeing(self) -> None:
        mock, spy = self._spy("backup", [4 << 30])
        clock = itertools.count(0, 30)
        out = StringIO()
        with (
            spy,
            patch_sh(mock),
            patch("lib.reclaim.time.sleep"),
            patch("lib.reclaim.time.monotonic", side_effect=lambda: next(clock)),
            redirect_stdout(out),
        ):
            backup_mod._apply_changes([lambda: None], "backup", Log())  # pylint: disable=protected-access
        assert "backup is still freeing what was removed" in out.getvalue()


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


class TestRecoverDelta:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """M4c-17 (M3P.10): the delta column in days from 48 h on."""

    def test_formats(self):
        fmt = restore_plan._fmt_delta  # pylint: disable=protected-access
        assert fmt(0) == "0"
        assert fmt(-(36 * 60 + 30)) == "-36m30s"
        assert fmt(-(5 * 3600 + 2 * 60)) == "-5h02m"
        assert fmt(47 * 3600 + 59 * 60) == "+47h59m"
        assert fmt(-(48 * 3600)) == "-2d00h00m"
        assert fmt(-(38 * 86400 + 23 * 3600 + 59 * 60 + 59)) == "-38d23h59m"


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
            chosen = choose_point(points, make_log(), complete_points(snaps, ""))
        assert chosen is points[0]
        options = shown["options"]
        assert isinstance(options, list)
        assert "INCOMPLETE" in options[-1] and "INCOMPLETE" not in options[0]
        assert "complete backup point is the default" in str(shown["question"])

    def test_a_point_that_lost_datasets_since_is_partly_removed(self) -> None:
        """M4c-16 (M3P.5): complete at its run, part of it gone since: not
        INCOMPLETE but PARTLY REMOVED, and still not the default."""
        snaps = self._e1()
        points = restore_points(snaps, self.BE)
        shown: dict[str, object] = {}

        def choice(_question: str, options: list[str], default: int = 0) -> int:
            shown.update(options=options, default=default)
            return default

        complete = complete_points(snaps, "")
        newest = point_name(points[-1])
        with patch.object(Log, "ask_choice", side_effect=choice), redirect_stdout(StringIO()):
            chosen = choose_point(points, make_log(), complete, complete | {newest})
        options = shown["options"]
        assert isinstance(options, list)
        assert "backup point, PARTLY REMOVED  (2/3 datasets)" in options[-1]
        assert "INCOMPLETE" not in options[-1]
        assert chosen is points[0]

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
            patch.object(restore_plan, "_probe_be", return_value=("", "", "")),
            patch.object(restore_plan, "_referenced", side_effect=lambda n: dict.fromkeys(n, 1)),
            patch.object(restore_plan, "_empty_root", return_value=False),
            redirect_stdout(StringIO()),
        ):
            plan = build_plan("backup", "/dev/sdb1", _BE, newest, snaps, make_log(), {P1})
        row = plan.row(home)
        assert row is not None and row.restored, row
        assert row.snap is not None and row.snap.name.startswith("autosnap_")
        assert "destroyed" not in row.note

    def _check_recover_view(self, fake: FakeZfs, last_point: str) -> None:
        with patch_sh(fake):
            snaps = restore_plan.list_snapshots("backup", Log())  # pylint: disable=protected-access
        complete = complete_points(snaps, last_point)
        points = restore_points(snaps, self.BE)
        with patch("builtins.input", return_value=""), redirect_stdout(StringIO()):
            chosen = choose_point(points, make_log(), complete)
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

    def test_a_tests_output_is_shown_only_when_it_fails(self) -> None:
        """M1P.20 (M4d-31): log lines and FATAL boxes stay off the terminal."""

        def noisy() -> None:
            print("[OK]      a log line")

        def noisy_failure() -> None:
            print("[FATAL]   what went wrong")
            raise AssertionError("no")

        out = StringIO()
        with redirect_stdout(out):
            assert _run_one("t", noisy) == "passed"
        assert "a log line" not in out.getvalue()
        out = StringIO()
        with redirect_stdout(out):
            assert _run_one("t", noisy_failure) == "failed"
        assert "        | [FATAL]   what went wrong" in out.getvalue()

    def test_exists_only_answers_per_path(self) -> None:
        """M4d-31: the listed paths are answered, every other one is real."""
        with tempfile.TemporaryDirectory() as td, exists_only("/dev/zark-none", absent=(f"{td}/",)):
            assert Path("/dev/zark-none").exists() and Path("/dev/zark-none/x").exists() is False
            assert not Path(td, "a").exists() and Path(td).exists()  # td itself is real

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


# ═════════════════════════════════════════════════════════════════════════
#  M4s: zark selftest (lib/selftest*.py, lib/certificates.py,
#  lib/platform_facts.py, lib/zfs_features.py, commands/selftest.py)
# ═════════════════════════════════════════════════════════════════════════


class _FakeBench(stest.Bench):
    """The level-1 bench on tests/fake_zfs.py: pools a and b in the model,
    writes grow ``written``, a cut stream is the fake's injected interrupt."""

    def __init__(self, fake: FakeZfs):
        log = make_log()
        super().__init__(log, Cleanup(log), root="/nonexistent")
        self.fake = fake
        self.key = "/nonexistent/key"

    def setup(self) -> None:
        self.fake.pool(self.a)
        self.fake.pool(self.b)

    def write(self, ds: str, kib: int = 256) -> None:
        self.fake.ds[ds].written += kib * 1024

    def cut(self, send: str, recv: str, keep: int) -> _sh.RunResult:
        self.fake.inject[recv.split()[-1]] = ("interrupt", 0)
        r = self.pipe(send, recv)
        assert not r.ok, "the fake did not cut the stream"
        return r

    def teardown(self) -> None:
        pass


# The accessors' options that take an argument (zfs-get.8, zpool-get.8, zfs-list.8).
_ACCESSOR_ARG_OPTS = {"zfs get": "dost", "zpool get": "o", "zfs list": "dosSt"}


def _accessor_operands(cmd: str) -> list[tuple[str, list[str]]]:
    """Each ``zfs get``, ``zpool get`` or ``zfs list`` in ``cmd``, with the
    objects it names: not its options, nor the property list of a get.

    A pipe is split only when it begins with ``zfs send`` or an accessor: a
    quoted ``sh -c`` script is not an accessor, and shlex would break on it.
    """
    parts = [cmd]
    if " | " in cmd and cmd.startswith(("zfs send", *_PoisonShell.ACCESSORS)):
        parts = cmd.split(" | ")
    found: list[tuple[str, list[str]]] = []
    for piece in parts:
        if not piece.startswith(_PoisonShell.ACCESSORS):
            continue
        argv = shlex.split(piece)
        name = " ".join(argv[:2])
        takes = _ACCESSOR_ARG_OPTS[name]
        operands: list[str] = []
        i = 2
        while i < len(argv):
            arg = argv[i]
            if arg.startswith("-") and len(arg) > 1:
                letters = arg[1:]
                # An option with an argument ends a bundle; last, it takes the next word.
                for j, ch in enumerate(letters):
                    if ch in takes:
                        i += j == len(letters) - 1
                        break
            else:
                operands.append(arg)
            i += 1
        found.append((name, operands if name == "zfs list" else operands[1:]))
    return found


def _foreign_operands(calls: Iterable[str], bench: stest.Bench) -> list[str]:
    """The accessor calls that read anything but the bench's own objects (M4sV-1).

    The poisoned shell answers them with nothing, on the ground that they
    only read the run's pools, what is below them, its vdev files and the
    stick; one with no object reads the whole system.
    """
    pools = "|".join(re.escape(bench.pool(x)) for x in "abcs")
    own = re.compile(rf"({pools})([/@#].*)?")
    stick = getattr(bench, "stick", None)
    allowed = {stick.part1, stick.by_id_path} if stick is not None else set()
    bad: list[str] = []
    for cmd in calls:
        for name, operands in _accessor_operands(cmd):
            if not operands:
                bad.append(f"{cmd}: {name} with no object")
            bad += [
                f"{cmd}: {op}"
                for op in operands
                if not own.fullmatch(op)
                and not op.startswith(f"{bench.dir}/")
                and op not in allowed
            ]
    return bad


class TestSelftestOnTheFake:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """M4s-12: every check marked fake runs against tests/fake_zfs.py and
    passes; a check the fake fails names a divergence from OpenZFS."""

    def test_every_fake_check_passes_on_the_fake(self):
        checks = [c for c in all_selftest_checks() if c.fake]
        assert len(checks) == 19
        failed: list[str] = []
        foreign: list[str] = []
        accessors = 0
        for c in checks:
            fake = FakeZfs()
            with patch_sh(fake):
                bench = _FakeBench(fake)
                bench.setup()
                res = stest.run_one(c, bench)
            if res.result != "PASS":
                failed.append(f"{c.key}: {res.expected} / {res.came}")
            # M4sV-1: on their whole pass path, the reads name the bench's objects.
            foreign += [f"{c.key}: {b}" for b in _foreign_operands(fake.calls, bench)]
            accessors += sum(len(_accessor_operands(cmd)) for cmd in fake.calls)
        assert not failed, failed
        assert not foreign, foreign
        assert accessors > 50, accessors  # the calls are recorded and parsed


class TestSelftestFramework:  # pylint: disable=missing-function-docstring
    """M4s-1, M4s-2, M4s-3: the registry, a check's result, the bench."""

    def test_the_registry_has_the_planned_checks(self):
        checks = all_selftest_checks()
        assert len([c for c in checks if c.level == 1]) == 47
        assert len([c for c in checks if c.level == 2]) == 8
        assert len({c.cid for c in checks}) == len(checks)
        for c in checks:
            assert re.fullmatch(r"[A-Z]{3,4}-\d\d", c.cid), c.cid
            assert c.rev == 1 and c.depends, c.cid
            doc = c.fn.__doc__ or ""
            assert re.search(r"\.(c|py|rules\.in|rules|md|8):\d+", doc) or "sanoid:" in doc, c.cid
        assert [c.level for c in checks] == sorted(c.level for c in checks)

    def test_a_check_gives_pass_fail_skip_or_its_exception(self):
        def fn_pass(_b: stest.Bench) -> None:
            pass

        def fn_fail(_b: stest.Bench) -> None:
            raise stest.Fail("x", "y")

        def fn_skip(_b: stest.Bench) -> None:
            raise stest.Skip("no tool")

        def fn_boom(_b: stest.Bench) -> None:
            raise RuntimeError("boom")

        bench = stest.Bench(make_log(), Cleanup(make_log()))
        got = {}
        for fn in (fn_pass, fn_fail, fn_skip, fn_boom):
            c = stest.Check("T-01", 1, 1, "t", ("zark",), False, fn)
            got[fn.__name__] = stest.run_one(c, bench)
        assert got["fn_pass"].result == "PASS"
        assert (got["fn_fail"].result, got["fn_fail"].expected, got["fn_fail"].came) == (
            "FAIL",
            "x",
            "y",
        )
        assert got["fn_fail"].depends == ("zark",)
        assert (got["fn_skip"].result, got["fn_skip"].reason) == ("SKIP", "no tool")
        assert got["fn_boom"].result == "FAIL"
        assert got["fn_boom"].came == "RuntimeError (in zark.log)"
        assert "RuntimeError: boom" in got["fn_boom"].output

    def test_a_signal_is_not_turned_into_a_fail(self):
        def fn(_b: stest.Bench) -> None:
            raise KeyboardInterrupt

        c = stest.Check("T-01", 1, 1, "t", ("zark",), False, fn)
        state = stest.RunState()
        try:
            _ = stest.run_checks(
                [c], stest.Bench(make_log(), Cleanup(make_log())), make_log(), state
            )
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("the interrupt was swallowed")
        assert not state.results and state.current == "T-01.1"

    def test_teardown_destroys_only_the_pool_with_its_guid(self):
        mock = MockShell()
        with tempfile.TemporaryDirectory() as td, patch_sh(mock):
            bench = stest.Bench(make_log(), Cleanup(make_log()), root=td)
            os.makedirs(bench.dir)
            bench.guids = {bench.a: "111", bench.b: "222"}
            mock.on(f"zpool get -Hp -o value guid {bench.a}").succeeds("111\n")
            mock.on(f"zpool get -Hp -o value guid {bench.b}").succeeds("999\n")
            mock.on(f"zpool get -H -o value readonly {bench.a}").succeeds("off\n")
            mock.on(f"zpool destroy {bench.a}").succeeds()
            mock.on(f"zpool list -H -o name {bench.a}").fails()
            mock.on(f"zpool list -H -o name {bench.b}").succeeds(bench.b)
            mock.on("zpool list -vHP").succeeds("rpool\t1T\n\t/dev/sda3\n")
            mock.on_prefix("rm -rf").succeeds()
            bench.teardown()
            assert mock.was_called(f"zpool destroy {bench.a}")
            assert mock.was_not_called(f"zpool destroy {bench.b}")
            assert mock.was_not_called(f"zpool destroy -f {bench.b}")
            assert mock.was_called(f"rm -rf {bench.dir}")

    def test_a_read_only_pool_is_reimported_read_write_before_its_destroy(self):
        mock = MockShell()
        with tempfile.TemporaryDirectory() as td, patch_sh(mock):
            bench = stest.Bench(make_log(), Cleanup(make_log()), root=td)
            os.makedirs(bench.dir)
            Path(bench.vdev(bench.a)).touch()
            bench.guids = {bench.a: "111"}
            mock.on(f"zpool get -Hp -o value guid {bench.a}").succeeds("111\n")
            mock.on(f"zpool get -H -o value readonly {bench.a}").succeeds("on\n")
            mock.on(f"zpool export {bench.a}").succeeds()
            mock.on_prefix(f"zpool import -d {bench.vdev(bench.a)} -N -R").succeeds()
            mock.on(f"zpool destroy {bench.a}").succeeds()
            mock.on(f"zpool list -H -o name {bench.a}").fails()
            mock.on("zpool list -vHP").succeeds("")
            mock.on_prefix("rm -rf").succeeds()
            bench.teardown()
            calls = mock.calls
            exp = calls.index(f"zpool export {bench.a}")
            imp = next(i for i, c in enumerate(calls) if c.startswith("zpool import -d"))
            assert exp < imp < calls.index(f"zpool destroy {bench.a}")

    def test_a_pool_created_before_its_guid_was_read_goes_only_from_its_own_file(self):
        # M4dF-1: c's create ended, its GUID was never read; d's name has
        # a pool on another disk; e's create never ended.
        mock = MockShell()
        with tempfile.TemporaryDirectory() as td, patch_sh(mock):
            bench = stest.Bench(make_log(), Cleanup(make_log()), root=td)
            os.makedirs(bench.dir)
            c, d, e = bench.pool("c"), bench.pool("d"), bench.pool("e")
            Path(bench.vdev(e)).touch()
            bench.creating = {c, d, e}
            for name, vdev in ((c, bench.vdev(c)), (d, "/dev/sdz1")):
                mock.on(f"zpool list -H -o name {name}").succeeds(name)
                mock.on(f"zpool list -vHP {name}").succeeds(f"{name}\t1G\n\t{vdev}\t1G\n")
            mock.on(f"zpool get -Hp -o value guid {c}").succeeds("333\n")
            mock.on(f"zpool get -Hp -o value guid {d}").succeeds("444\n")
            mock.on(f"zpool get -H -o value readonly {c}").succeeds("off\n")
            mock.on(f"zpool destroy {c}").succeeds()
            mock.on(f"zpool list -H -o name {e}").fails()
            mock.on("zpool list -vHP").succeeds("")
            mock.on_prefix("rm -rf").succeeds()
            bench.teardown()
            assert mock.was_called(f"zpool destroy {c}")
            assert not [x for x in mock.calls if x.startswith(f"zpool destroy {d}")]
            assert not [x for x in mock.calls if x.startswith(f"zpool destroy -f {d}")]
            assert not os.path.exists(bench.vdev(e))
            assert not bench.creating

    def test_the_directory_stays_while_an_imported_pool_uses_it(self):
        mock = MockShell()
        with tempfile.TemporaryDirectory() as td, patch_sh(mock):
            bench = stest.Bench(make_log(), Cleanup(make_log()), root=td)
            os.makedirs(bench.dir)
            mock.on("zpool list -vHP").succeeds(f"zstx\t1G\n\t{bench.dir}/zstx.img\n")
            bench.teardown()
            assert os.path.isdir(bench.dir)
            assert not any(c.startswith("rm -rf") for c in mock.calls)

    def test_setup_refuses_without_space_or_memory(self):
        bench = stest.Bench(make_log(), Cleanup(make_log()))
        small = os.statvfs_result((4096, 4096, 10, 10, 10, 10, 10, 10, 0, 255))
        with patch("lib.selftest.os.statvfs", return_value=small):
            try:
                bench.setup()
            except stest.SetupError as e:
                assert "MiB free" in str(e)
            else:
                raise AssertionError("no refusal")
        big = os.statvfs_result((4096, 4096, 10**7, 10**7, 10**7, 10, 10, 10, 0, 255))
        with (
            patch("lib.selftest.os.statvfs", return_value=big),
            patch("lib.selftest._mem_available", return_value=512 * 1024**2),
        ):
            try:
                bench.setup()
            except stest.SetupError as e:
                assert "memory" in str(e)
            else:
                raise AssertionError("no refusal")

    def test_remove_empty_dirs_keeps_files(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "a", "b").mkdir(parents=True)
            Path(td, "k", "c").mkdir(parents=True)
            Path(td, "k", "file").write_text("x", encoding="ascii")
            stest.remove_empty_dirs(td)
            assert os.path.isdir(td) and not Path(td, "a").exists()
            assert Path(td, "k", "file").exists()


class TestSelftestLock:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """M4s-16: run and warnings take the lock; status, report, certificate do not."""

    def test_the_lock_by_subcommand(self):
        for args in ([], ["run"], ["--stick", "/dev/sdz"], ["warnings", "on"]):
            assert lock_mod.needs_lock("selftest", args), args
        for args in (["status"], ["report"], ["certificate", "x.json"]):
            assert not lock_mod.needs_lock("selftest", args), args
        assert lock_mod.selftest_subcommand(["--only", "POOL-01"]) == "run"
        assert lock_mod.selftest_subcommand(["report", "f"]) == "report"


def _facts(**over: Any) -> pfacts.Facts:
    series = {
        "ubuntu": "26.04",
        "kernel": "7.0",
        "zfs": "2.4.1",
        "zfs_kmod": "2.4.1",
        "util_linux": "2.41",
        "systemd": "259",
    }
    series.update(over.pop("series", {}))
    return pfacts.Facts(
        exact={"zfs": "zfs-2.4.1-1ubuntu5.1"},
        series=series,
        where=over.pop("where", "installed"),
        iso=over.pop("iso", None),
    )


def _report(
    results: list[stest.Result], facts: pfacts.Facts | None = None, **info: Any
) -> dict[str, Any]:
    return certs.build_report(
        facts or _facts(),
        certs.RunInfo(info.get("level", 1), info.get("partial", False)),
        results,
        info.get("masks", {}),
    )


# What a command on a real machine can print that names it (M4sR-1).
_PLANTED_DATASET = "rpool/USERDATA/juanmi_x9q2"
_PLANTED_MOUNT = "/run/media/juanmi/Backup Disk"
_PLANTED_ALIAS = "usb-Vendor_Model_SERIALX1234567890AB-0:0"
_PLANTED = (_PLANTED_DATASET, "juanmi", "Backup Disk", _PLANTED_ALIAS, "SERIALX")


class _PoisonShell(MockShell):
    """Every command answers ``returncode`` with stdout and stderr naming a
    system dataset, a user's mount, the hostname and a bare by-id alias."""

    def __init__(self, returncode: int):
        super().__init__()
        self.returncode = returncode
        text = (
            f"cannot open '{_PLANTED_DATASET}': dataset does not exist\n"
            + f"{_PLANTED_MOUNT} ext4 /dev/mapper/luks-1 {socket.gethostname()}\n"
            + f"{_PLANTED_ALIAS}\n"
        )
        self.text = text

    # Reads of one property or listing of the run's own objects: what they
    # print is the check's observed value, not free text.
    ACCESSORS = ("zfs get ", "zpool get ", "zfs list ")

    def mock_run(self, cmd, check=False, input=None, log=None, timeout=None) -> RunResult:  # pylint: disable=redefined-builtin
        del check, input, log, timeout
        self._calls.append(str(cmd))  # for _foreign_operands (M4sV-1)
        out = "" if str(cmd).startswith(self.ACCESSORS) else self.text
        return RunResult(self.returncode, out, self.text, str(cmd))

    def mock_run_pipe(self, cmd1, cmd2, _log=None) -> RunResult:
        return self.mock_run(f"{cmd1} | {cmd2}")


class TestSelftestReportPrivacy:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """M4sR-1: no command output reaches a report, whatever a check reads."""

    accessors = 0  # accessor calls parsed by _reports

    def _reports(self, returncode: int, foreign: list[str] | None = None) -> dict[str, str]:
        """Each check's report on the poisoned shell; the accessor calls that
        read anything but the bench's objects go to ``foreign``, and one
        entry per accessor call parsed is counted in ``self.accessors``."""
        out: dict[str, str] = {}
        shell = _PoisonShell(returncode)
        ident = DiskIdentity(
            disk="/dev/zarktestnone",
            by_id="usb-Scratch_STICK0042-0:0",
            serial="STICK0042",
            aliases=("usb-Scratch_STICK0042-0:0",),
        )
        with (
            tempfile.TemporaryDirectory() as td,
            patch_sh(shell),
            patch("time.sleep"),
            redirect_stdout(StringIO()),
        ):
            for c in all_selftest_checks():
                log = make_log()
                if c.level == 1:
                    bench: stest.Bench = stest.Bench(log, Cleanup(log), root=td)
                else:
                    bench = st_l2.StickBench(log, Cleanup(log), ident)
                    bench.dir = f"{td}/{c.cid}-stick"
                    bench.key, bench.mnt = f"{bench.dir}/key", f"{bench.dir}/mnt"
                # Benches made in one second share a directory one time in
                # 65536 (stamp + 2 random bytes); nothing here writes to it (M4cF-1).
                os.makedirs(bench.mnt, exist_ok=True)
                shell.reset()
                res = stest.run_one(c, bench)
                if foreign is not None:
                    foreign += [f"{c.key}: {b}" for b in _foreign_operands(shell.calls, bench)]
                    self.accessors += sum(len(_accessor_operands(cmd)) for cmd in shell.calls)
                report = certs.build_report(
                    _facts(), certs.RunInfo(c.level, False), [res], bench.masks()
                )
                out[c.key] = json.dumps(report["results"])
        return out

    def _one(self, cid: str, shell: MockShell) -> tuple[str, str]:
        """The report's results and the run's own log lines for ``cid``."""
        check_ = next(c for c in all_selftest_checks() if c.cid == cid)
        out = StringIO()
        with tempfile.TemporaryDirectory() as td, patch_sh(shell), redirect_stdout(out):
            log = make_log()
            bench = stest.Bench(log, Cleanup(log), root=td)
            os.makedirs(bench.mnt)
            state = stest.RunState()
            _ = stest.run_checks([check_], bench, log, state)
            report = certs.build_report(
                _facts(), certs.RunInfo(1, False), state.results, bench.masks()
            )
        return json.dumps(report["results"]), out.getvalue()

    def test_sanoid_naming_a_system_dataset_stays_out_of_the_report(self):
        mock = MockShell()
        mock.on_prefix("zfs ").succeeds()
        mock.on_prefix("/usr/sbin/sanoid --prune-snapshots").returns(
            1, "", f"cannot open '{_PLANTED_DATASET}': dataset does not exist\n"
        )
        with (
            patch("lib.selftest_host.shutil.which", return_value="/usr/sbin/sanoid"),
            patch("lib.selftest_host.SANOID_DEFAULTS", (__file__,)),
            patch("lib.selftest_host.time.sleep"),
        ):
            report, terminal = self._one("SAN-01", mock)
        assert '"FAIL"' in report and _PLANTED_DATASET not in report, report
        assert '"came": "exit 1"' in report
        assert _PLANTED_DATASET in terminal  # zark.log and the terminal keep it

    def test_the_host_mounts_in_mnt_03_stay_out_of_the_report(self):
        mock = MockShell()
        rows = "".join(f"{_PLANTED_MOUNT}/{i} ext4 /dev/mapper/luks-1\n" for i in range(3))
        mock.on_prefix("unshare").succeeds(f"LIST\n{rows}END\nRC 32\nROOT 1\nBUSY 1\n")
        report, terminal = self._one("MNT-03", mock)
        assert '"FAIL"' in report and "juanmi" not in report, report
        assert "not among 3 line(s)" in report
        assert _PLANTED_MOUNT in terminal

    def test_no_check_puts_command_output_in_the_report(self):
        leaks = []
        foreign: list[str] = []
        self.accessors = 0
        for rc in (0, 1):
            for key, text in self._reports(rc, foreign).items():
                leaks += [f"rc {rc} {key}: {s}" for s in _PLANTED if s in text]
        assert not leaks, leaks
        # M4sV-1: the accessors, answered with nothing, read only the run's own objects.
        assert not foreign, foreign
        assert self.accessors > 20, self.accessors  # the calls are recorded and parsed

    def test_an_accessor_reading_beyond_the_bench_is_found(self):
        with redirect_stdout(StringIO()):
            log = make_log()
            bench = stest.Bench(log, Cleanup(log), root="/nonexistent")
        a = bench.pool("a")
        assert _accessor_operands(
            f"zfs get -H -p -r -s local -t snapshot -o name,value org.zark:t {a}/x"
        ) == [("zfs get", [f"{a}/x"])]
        assert _accessor_operands(f"zfs send -w {a}@s | zfs list -Hp -o name {a}") == [
            ("zfs list", [a])
        ]
        assert not _foreign_operands(
            [f"zpool get -H -o property,value all {a}", f"zfs list -Hp -r {a}@s"], bench
        )
        assert _foreign_operands(["zfs list -H -o name"], bench) == [
            "zfs list -H -o name: zfs list with no object"
        ]
        assert _foreign_operands(["zfs list -Hp -t snapshot -o name -r rpool"], bench) == [
            "zfs list -Hp -t snapshot -o name -r rpool: rpool"
        ]
        assert _foreign_operands([f"zpool get -Hp -o value guid {a}x"], bench)


class TestCertificates:  # pylint: disable=missing-function-docstring
    """M4s-5, M4s-6, M4s-9, M4s-15: report, record, coverage, warnings, link."""

    def test_nothing_that_names_the_machine_reaches_the_report(self):
        host = socket.gethostname()
        planted = (
            f"zdb: name: 'zstdeadbeefa' hostname: '{host}' pool_guid: 16224517364757996670 "
            + "serial 408D5C15CFA5E961091D0CCA /dev/disk/by-id/usb-Kingston_DT_408D5C15-0:0 "
            + "machine 7e57000000000000000000000000beef in /dev/shm/zark-selftest-x"
        )
        res = [stest.Result("POOL-07.1", "FAIL", expected=planted, came=planted, depends=("x",))]
        masks = {"/dev/shm/zark-selftest-x": "<run>", "zstdeadbeefa": "<pool-a>"}
        text = json.dumps(_report(res, masks=masks))
        for secret in (
            host,
            "16224517364757996670",
            "408D5C15CFA5E961091D0CCA",
            "usb-Kingston",
            "7e57000000000000000000000000beef",
            "zstdeadbeefa",
            "zark-selftest-x",
        ):
            assert secret not in text, secret
        assert "<pool-a>" in text and "<run>" in text

    def test_a_mask_replaces_whole_names_only(self):
        # M4sX.3: a short serial is not replaced inside a word, nor inside a
        # mask put in before; a serial after "_" in a by-id name still is.
        masks = {"1": "<serial>", "zstdeadbeefa": "<pool-a>", "pool": "<serial>"}
        assert certs.sanitize("SND-01 a1b exit 1", masks) == "SND-01 a1b exit <serial>"
        assert certs.sanitize("zstdeadbeefa/SND-01@s1", masks) == "<pool-a>/SND-01@s1"
        assert certs.sanitize("zstdeadbeefab", masks) == "zstdeadbeefab"
        serial = {"408D5C15CFA5": "<serial>"}
        assert certs.sanitize("x_408D5C15CFA5-0:0 408D5C15CFA5Z", serial) == (
            "x_<serial>-0:0 408D5C15CFA5Z"
        )
        host = socket.gethostname()
        if len(host) > 1:
            assert certs.sanitize(f"on {host}_x", {}) == "on <host>_x"

    def test_the_longest_name_is_masked_first(self):
        # M4dR-7: a name that is the start of a longer one (a by-id name and
        # the same name with its serial) must not leave the longer one's tail.
        masks = {"usb-S": "<short>", "usb-S_1-0:0": "<by-id>"}
        assert certs.sanitize("on usb-S_1-0:0 now", masks) == "on <by-id> now"
        assert certs.sanitize("on usb-S now", masks) == "on <short> now"

    def test_the_record_keeps_a_complete_run_beside_a_partial_one(self):
        with tempfile.TemporaryDirectory() as td:
            _ = certs.write_report(_report([stest.Result("POOL-01.1", "PASS")]), td)
            _ = certs.write_report(_report([stest.Result("POOL-02.1", "FAIL")], partial=True), td)
            rec = certs.read_record(Path(td, "record.json"))
            assert sorted(bool(e["partial"]) for e in rec) == [False, True]
            assert certs.covered(_facts(), rec) == {"POOL-01.1"}

    def test_a_later_pass_clears_the_failure_line(self):
        # M4sR-4 (the review's E4 b): a complete run with a FAIL, then --only
        # that check passing; and the same across levels.
        checks = [c for c in all_selftest_checks() if c.level == 1]
        one_fail = [
            stest.Result(c.key, "FAIL" if c.cid == "SND-04" else "PASS", depends=c.depends)
            for c in checks
        ]
        for level in (1, 2):
            with tempfile.TemporaryDirectory() as td:
                _ = certs.write_report(_report(one_fail, level=level), td)
                rec = certs.read_record(Path(td, "record.json"))
                assert any(
                    "failed on this machine" in ln
                    for ln in certs.warning_lines(_facts(), checks, rec, [])
                )
                _ = certs.write_report(
                    _report([stest.Result("SND-04.1", "PASS")], partial=True), td
                )
                rec = certs.read_record(Path(td, "record.json"))
                lines = certs.warning_lines(_facts(), checks, rec, [])
                assert not any("failed on this machine" in ln for ln in lines), (level, lines)
                # coverage unchanged: a partial run still covers nothing
                assert certs.uncovered(_facts(), checks, rec) == [
                    c for c in checks if c.cid == "SND-04"
                ]

    def test_a_pass_on_another_series_leaves_the_failure(self):
        checks = [c for c in all_selftest_checks() if c.level == 1]
        with tempfile.TemporaryDirectory() as td:
            _ = certs.write_report(_report([stest.Result("SND-04.1", "FAIL")]), td)
            other = _facts(series={"kernel": "7.1"})
            _ = certs.write_report(
                _report([stest.Result("SND-04.1", "PASS")], other, partial=True), td
            )
            rec = certs.read_record(Path(td, "record.json"))
            lines = certs.warning_lines(_facts(), checks, rec, [])
            assert any("SND-04.1 failed on this machine" in ln for ln in lines), lines

    def test_reports_beyond_ten_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            for i in range(12):
                Path(td, f"report-2026010{i:02d}.json").write_text("{}", encoding="ascii")
            _ = certs.write_report(_report([stest.Result("POOL-01.1", "PASS")]), td)
            assert len(list(Path(td).glob("report-*.json"))) == certs.REPORTS_KEPT

    def test_coverage_needs_the_same_series_and_where(self):
        entry = _report([stest.Result("POOL-01.1", "PASS"), stest.Result("POOL-02.1", "FAIL")])
        cert = entry["certificate"]
        assert certs.covered(_facts(), [cert]) == {"POOL-01.1"}
        assert certs.covered(_facts(where="live"), [cert]) == set()
        assert certs.covered(_facts(series={"zfs": "2.4.2"}), [cert]) == set()
        assert certs.covered(_facts(), [{**cert, "partial": True}]) == set()

    def test_warning_lines(self):
        checks = all_selftest_checks()
        l1 = [c for c in checks if c.level == 1]
        all_pass = _report([stest.Result(c.key, "PASS") for c in l1])["certificate"]
        assert not certs.warning_lines(_facts(), checks, [], [all_pass])
        lines = certs.warning_lines(_facts(), checks, [], [])
        assert lines == [
            f"self-test: no certificate covers this platform ({len(l1)} of {len(l1)} "
            + f"checks): run {zark_command()} selftest"
        ]
        failed = {
            **all_pass,
            "date": "2026-10-18",
            "failed": [
                {"id": "SND-04.1", "depends": ["the MODIFIED state"]},
                {"id": "X", "depends": []},
            ],
        }
        lines = certs.warning_lines(_facts(), checks, [failed], [all_pass])
        assert lines == [
            "self-test: SND-04.1 and 1 more failed on this machine on 2026-10-18: "
            + "the MODIFIED state"
        ]
        mixed = _facts(series={"zfs_kmod": "2.3.4"})
        lines = certs.warning_lines(mixed, checks, [], [])
        assert lines[-1] == (
            "self-test: the ZFS module (2.3.4) and its tools (2.4.1) differ: "
            + "reboot after updating ZFS"
        )

    def test_the_subscription(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td, "settings.json")
            assert not certs.subscribed(path=p)
            _ = certs.set_subscribed(True, p)
            assert certs.subscribed(path=p)
            _ = certs.set_subscribed(False, p)
            assert not certs.subscribed(path=p)
            p.write_text("{not json", encoding="ascii")
            assert not certs.subscribed(make_log(), path=p)
            p.write_text('{"selftest_warnings": "yes", "other": 1}', encoding="ascii")
            assert not certs.subscribed(make_log(), path=p)
            _ = certs.set_subscribed(True, p)
            assert json.loads(p.read_text(encoding="ascii"))["other"] == 1

    def test_warnings_need_the_subscription(self):
        with patch("lib.certificates.subscribed", return_value=False):
            assert not certs.warnings(make_log())
        with (
            patch("lib.certificates.subscribed", return_value=True),
            patch("lib.platform_facts.read", return_value=_facts()),
            patch("lib.certificates.read_record", return_value=[]),
            patch("lib.certificates.read_certificates", return_value=[]),
        ):
            assert certs.warnings(make_log())[0].startswith("self-test: no certificate")

    def test_the_motd_carries_the_lines(self):
        stt = status_lib.Status(selftest=["self-test: x"])
        assert "zark:\033[0m self-test: x" in status_lib.motd_text(stt)

    def test_the_issue_link(self):
        rep = _report([stest.Result("POOL-01.1", "PASS")])
        link, whole = certs.issue_link(rep)
        assert whole and link.startswith(certs.ISSUE_URL + "?template=selftest.yml&title=")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        assert json.loads(q["report"][0]) == rep
        assert q["title"][0] == "selftest: zark " + rep["certificate"]["zark"] + (
            " on Ubuntu 26.04 (installed), 0 FAIL"
        )
        big = _report(
            [
                stest.Result(f"X-{i:02d}.1", "FAIL", expected="e" * 50, came="c" * 50)
                for i in range(80)
            ]
        )
        link, whole = certs.issue_link(big)
        assert not whole and "report=" not in link and len(link) < certs.URL_MAX

    def test_a_certificate_entry_comes_only_from_a_complete_run(self):
        rep = _report([stest.Result("POOL-01.1", "PASS")])
        assert certs.certificate_entry(rep)["checks"] == {"POOL-01.1": "PASS"}
        for bad in ({"format": 9}, _report([], partial=True), {"format": 1}):
            try:
                _ = certs.certificate_entry(bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted {bad}")


class TestPlatformFacts:  # pylint: disable=missing-function-docstring
    """M4s-8, M4s-14: exact versions, series, where."""

    def _mock(self) -> MockShell:
        mock = MockShell()
        mock.on("zfs version").succeeds("zfs-2.4.1-1ubuntu5.1\nzfs-kmod-2.4.1-1ubuntu4\n")
        mock.on("findmnt --version").succeeds("findmnt from util-linux 2.41.3\n")
        mock.on("systemctl --version").succeeds("systemd 259 (259.5-0ubuntu3.4)\n+PAM\n")
        mock.on("sgdisk --version").succeeds("GPT fdisk (sgdisk) version 1.0.10\n")
        return mock

    def test_series_drop_the_ubuntu_revision(self):
        mock = self._mock()
        mock.on("cat /proc/cmdline").succeeds("BOOT_IMAGE=/vmlinuz root=ZFS=rpool")
        mock.on("test -d /rofs").fails()
        mock.on("test -d /cow").fails()
        with tempfile.TemporaryDirectory() as td, patch_sh(mock):
            osr = Path(td, "os-release")
            osr.write_text(
                'VERSION_ID="26.04"\nVERSION="26.04.1 LTS (Resolute Raccoon)"\n', "utf-8"
            )
            with patch("lib.platform_facts.OS_RELEASE", str(osr)):
                f = pfacts.read()
        assert f.series["zfs"] == f.series["zfs_kmod"] == "2.4.1"
        assert not f.zfs_mismatch
        assert f.series["util_linux"] == "2.41" and f.series["systemd"] == "259"
        assert f.series["ubuntu"] == "26.04" and f.where == "installed" and f.iso is None
        assert f.exact["zfs_kmod"] == "zfs-kmod-2.4.1-1ubuntu4"

    def test_a_live_session_reads_its_iso(self):
        mock = self._mock()
        mock.on("cat /proc/cmdline").succeeds("boot=casper quiet")
        with tempfile.TemporaryDirectory() as td, patch_sh(mock):
            info = Path(td, "info")
            info.write_text('Ubuntu 26.04 LTS "Resolute Raccoon" - Release amd64\n', "utf-8")
            with patch("lib.platform_facts.CDROM_INFO", str(info)):
                f = pfacts.read()
        assert f.where == "live" and f.iso and f.iso.startswith("Ubuntu 26.04")

    def test_a_different_release_is_a_mismatch(self):
        assert _facts(series={"zfs_kmod": "2.3.4"}).zfs_mismatch


class TestZfsFeatures:  # pylint: disable=missing-function-docstring
    """M4s-11 and the feature table."""

    LISTING = (
        "  pool: backup\n    id: 16224517364757996670\n state: UNAVAIL\n"
        + "status: The pool uses the following feature(s) not supported on this system:\n"
        + "\tcom.klarasystems:dynamic_gang_header (Variable on-disk size of gang headers.)\n"
        + "\tcom.example:future\n"
        + "action: The pool cannot be imported. Access the pool on a system that supports\n"
        + "\tthe required feature(s), or recreate the pool from backup.\n"
        + " config:\n\n\tbackup  UNAVAIL  unsupported feature(s)\n"
    )

    def test_the_table(self):
        assert len(zfeat.FEATURES) == 47
        assert zfeat.BY_GUID["com.klarasystems:dynamic_gang_header"].since == (2, 4)
        assert "dynamic_gang_header" not in zfeat.COMPAT_2_2
        assert "head_errlog" in zfeat.COMPAT_2_2 and "longname" not in zfeat.COMPAT_2_2
        # M4sR-6: what drive_compatibility checks is newer than 2.2, in one place.
        newer = {f.name for f in zfeat.FEATURES if f.since > (2, 2)}
        assert set(zfeat.BEYOND_2_2_STREAM) <= newer
        assert not set(zfeat.BEYOND_2_2_STREAM) & set(zfeat.COMPAT_2_2)
        assert zfeat.release_tuple("zfs-kmod-2.3.4-1ubuntu2") == (2, 3)
        assert zfeat.release_tuple("?") == (0, 0)

    def test_parse_upgrade_v(self):
        text = (
            "This system supports ZFS pool feature flags.\n\n"
            + "The following features are supported:\n\nFEAT DESCRIPTION\n"
            + "-------------------------------------------------------------\n"
            + "async_destroy                         (read-only compatible)\n"
            + "     Destroy filesystems asynchronously.\n"
            + "dynamic_gang_header\n     Variable on-disk size of gang headers.\n\n"
            + "The following legacy versions are also supported:\n\nVER  DESCRIPTION\n"
            + " 1   Initial ZFS version\n"
        )
        assert zfeat.parse_upgrade_v(text) == ["async_destroy", "dynamic_gang_header"]

    def test_parse_unsupported_for_read_and_for_write(self):
        found = zfeat.parse_unsupported(self.LISTING)
        assert found == [
            zfeat.Unsupported(
                "backup", True, ("com.klarasystems:dynamic_gang_header", "com.example:future")
            )
        ]
        write = self.LISTING.replace(
            "The pool uses the following feature(s) not supported on this system:",
            "The pool can only be accessed in read-only mode on this system. It\n"
            + "\tcannot be accessed in read-write mode because it uses the following\n"
            + "\tfeature(s) not supported on this system:",
        )
        assert not zfeat.parse_unsupported(write)[0].read
        assert not zfeat.parse_unsupported("  pool: backup\n    id: 1\n state: ONLINE\n")

    def test_names_and_releases(self):
        assert zfeat.describe("com.klarasystems:dynamic_gang_header") == (
            "dynamic_gang_header (OpenZFS 2.4)"
        )
        assert "newer than OpenZFS 2.4" in zfeat.describe("com.example:future")
        assert zfeat.needed_release(("org.zfsonlinux:longname",)) == "OpenZFS 2.3"
        assert zfeat.needed_release(("com.example:future",)) == "a release newer than OpenZFS 2.4"

    def test_recover_stops_before_importing_an_unreadable_drive(self):
        mock = MockShell()
        dev = "/dev/disk/by-id/usb-X-0:0-part1"
        mock.on(f"zpool import -d {dev}").succeeds(self.LISTING)
        mock.on("zfs version").succeeds("zfs-2.2.2-0ubuntu9\nzfs-kmod-2.2.2-0ubuntu9\n")
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out), patch("builtins.input", return_value=""):
            try:
                zfeat.check_drive("backup", dev, make_log())
            except SystemExit:
                pass
            else:
                raise AssertionError("no FATAL")
        text = out.getvalue()
        assert "dynamic_gang_header (OpenZFS 2.4)" in text
        assert "zfs-2.2.2-0ubuntu9" in text and "Nothing was imported" in text
        assert not any(c.startswith("zpool import -N") for c in mock.calls)

    def test_recover_goes_on_for_a_write_only_gap_or_none(self):
        mock = MockShell()
        write = self.LISTING.replace(
            "The pool uses the following feature(s) not supported on this system:",
            "The pool can only be accessed in read-only mode on this system. It\n"
            + "\tcannot be accessed in read-write mode because it uses the following\n"
            + "\tfeature(s) not supported on this system:",
        )
        mock.on("zpool import -d /dev/x").succeeds(write)
        mock.on("zpool import -d /dev/y").succeeds("  pool: backup\n    id: 1\n state: ONLINE\n")
        mock.on("zpool import -d /dev/z").fails("no pools available to import")
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out):
            for dev in ("/dev/x", "/dev/y", "/dev/z"):
                zfeat.check_drive("backup", dev, make_log())
        assert "recover imports it read-only" in out.getvalue()

    def test_mount_read_write_refuses_a_drive_it_can_only_read(self):
        # M4c-23 (M4sP.3): read-only mode is the INFO line, read-write a FATAL.
        mock = MockShell()
        write = self.LISTING.replace(
            "The pool uses the following feature(s) not supported on this system:",
            "The pool can only be accessed in read-only mode on this system. It\n"
            + "\tcannot be accessed in read-write mode because it uses the following\n"
            + "\tfeature(s) not supported on this system:",
        )
        mock.on("zpool import -d /dev/x").succeeds(write)
        mock.on("zfs version").succeeds("zfs-2.2.2-0ubuntu9\n")
        out = StringIO()
        with patch_sh(mock), redirect_stdout(out), patch("builtins.input", return_value=""):
            zfeat.check_drive("backup", "/dev/x", make_log(), "zark mount")
            assert "zark mount imports it read-only" in out.getvalue()
            try:
                zfeat.check_drive("backup", "/dev/x", make_log(), "zark mount", writing=True)
            except SystemExit:
                pass
            else:
                raise AssertionError("no FATAL")
        assert "can only read backup, not write it" in out.getvalue()
        assert "sudo ./zark mount, then choose Read-only" in out.getvalue()

    def test_mount_checks_the_drive_before_its_import(self):
        src = Path(mount_mod.__file__).read_text(encoding="utf-8")
        check = (
            'zfs_features.check_drive(pool_name, device, log, "zark mount", writing=not readonly)'
        )
        assert src.index(check) < src.index("if not zfs.import_backup_pool(")

    def test_recover_checks_the_drive_before_its_first_import(self):
        src = Path(recover_mod.__file__).read_text(encoding="utf-8")
        assert src.index("zfs_features.check_drive(pool_name, device, log)") < src.index(
            "if not zfs.import_backup_pool(pool_name, device, readonly=True, guid=drive.guid)"
        )


class TestPrepareCompatibility:  # pylint: disable=missing-function-docstring
    """M4s-10: new drives get compatibility=openzfs-2.2 unless origin needs more."""

    def test_the_create_command(self):
        cmd = zfs_create_command("backup3", "/dev/disk/by-id/usb-X-0:0", "/run/zark/altroot/b", "")
        assert cmd == (
            "zpool create -f -o ashift=12 -O atime=off -O xattr=sa -O dnodesize=auto "
            + "-O normalization=formD -m none -R /run/zark/altroot/b backup3 "
            + "/dev/disk/by-id/usb-X-0:0"
        )
        cmd = zfs_create_command("b", "/d", "/r", "openzfs-2.2")
        assert "-o ashift=12 -o compatibility=openzfs-2.2 -O atime=off" in cmd

    def test_the_compatibility_chosen(self):
        plain, longname = MockShell(), MockShell()
        longname.on("zpool get -H -o value feature@longname rpool").succeeds("active\n")
        for mock in (plain, longname):
            mock.on_prefix("zpool get -H -o value feature@").succeeds("enabled\n")
        with tempfile.TemporaryDirectory() as td, patch("lib.zfs.COMPAT_DIRS", (td,)):
            with patch_sh(plain), redirect_stdout(StringIO()):
                assert zfs_drive_compatibility(("rpool", "bpool"), make_log()) == ""
            Path(td, "openzfs-2.2").write_text("lz4_compress\n", encoding="ascii")
            with patch_sh(plain):
                assert zfs_drive_compatibility(("rpool", "bpool"), make_log()) == "openzfs-2.2"
            with patch_sh(longname), redirect_stdout(StringIO()):
                assert zfs_drive_compatibility(("rpool", "bpool"), make_log()) == ""


class TestSelftestCommand:  # pylint: disable=missing-function-docstring,protected-access
    """M4s-1, M4s-4: options, the stick's protections, the exit status."""

    def test_options(self):
        assert selftest_cmd._parse_run(
            ["--stick", "/dev/sdz", "--only", "pool-01,DSK-01"], make_log()
        ) == (
            "/dev/sdz",
            ["POOL-01", "DSK-01"],
        )
        # M4dF-2: an empty --stick (an unset shell variable) would run level 1.
        for bad in (["--bogus"], ["--stick"], ["--stick", ""], ["--stick", " "]):
            try:
                _ = selftest_cmd._parse_run(bad, make_log())
            except SystemExit as e:
                assert e.code == 2
            else:
                raise AssertionError(bad)

    def test_only_and_levels(self):
        l1 = selftest_cmd._select([], "", make_log())
        assert {c.level for c in l1} == {1}
        both = selftest_cmd._select([], "/dev/sdz", make_log())
        assert {c.level for c in both} == {1, 2}
        assert [c.cid for c in selftest_cmd._select(["SND-04"], "", make_log())] == ["SND-04"]
        for only, stick in ((["NOPE-01"], ""), (["DSK-01"], "")):
            try:
                _ = selftest_cmd._select(only, stick, make_log())
            except SystemExit as e:
                assert e.code == 2
            else:
                raise AssertionError(only)

    def _ident(self) -> DiskIdentity:
        return DiskIdentity(
            disk="/dev/sdz",
            by_id="usb-Scratch_1-0:0",
            model="Scratch",
            serial="S1",
            size="1G",
            transport="usb",
            aliases=("usb-Scratch_1-0:0",),
        )

    def _refused(self, mock: MockShell, cfg: Config, answer: str = "sdz") -> str:
        out = StringIO()
        with (
            patch_sh(mock),
            redirect_stdout(out),
            patch("commands.selftest.validate_external_block_device", return_value=self._ident()),
            patch("commands.selftest.Config.load", return_value=cfg),
            patch("builtins.input", return_value=answer),
        ):
            try:
                _ = selftest_cmd._stick_identity("/dev/sdz", make_log())
            except SystemExit:
                return out.getvalue()
        raise AssertionError("not refused")

    def test_a_registered_drive_is_refused(self):
        cfg = make_config()
        cfg.known_drives = {"backup": DriveInfo("backup", "1", "usb-Scratch_1-0:0")}
        assert "registered backup drive backup" in self._refused(MockShell(), cfg)

    def test_a_disk_that_is_not_blank_is_refused(self):
        mock = MockShell()
        mock.on("lsblk -nr -o NAME,TYPE /dev/sdz").succeeds("sdz disk\nsdz1 part\n")
        mock.on("blkid -p /dev/sdz").returns(0, 'PTTYPE="gpt"')
        assert "is not blank" in self._refused(mock, make_config())
        mock = MockShell()
        mock.on("lsblk -nr -o NAME,TYPE /dev/sdz").succeeds("sdz disk\n")
        mock.on("blkid -p /dev/sdz").returns(0, 'TYPE="vfat"')
        assert "is not blank" in self._refused(mock, make_config())

    def test_a_stick_without_a_by_id_name_is_refused_before_the_question(self):
        out = StringIO()
        ident = DiskIdentity(disk="/dev/sdz", by_id="", model="NoName", size="1G")
        mock = MockShell()
        mock.on("lsblk -nr -o NAME,TYPE /dev/sdz").succeeds("sdz disk\n")
        mock.on("blkid -p /dev/sdz").returns(2)
        with (
            patch_sh(mock),
            redirect_stdout(out),
            patch("commands.selftest.validate_external_block_device", return_value=ident),
            patch("commands.selftest.Config.load", return_value=make_config()),
            patch("builtins.input", return_value="sdz") as asked,
        ):
            try:
                _ = selftest_cmd._stick_identity("/dev/sdz", make_log())
            except SystemExit:
                pass
            else:
                raise AssertionError("accepted")
        assert "has no stable /dev/disk/by-id name" in out.getvalue()
        assert not [c for c in asked.call_args_list if "Type sdz" in str(c)]

    def test_the_kernel_name_must_be_typed(self):
        mock = MockShell()
        mock.on("lsblk -nr -o NAME,TYPE /dev/sdz").succeeds("sdz disk\n")
        mock.on("blkid -p /dev/sdz").returns(2)
        assert "Aborted: nothing was touched" in self._refused(mock, make_config(), "yes")
        out = StringIO()
        with (
            patch_sh(mock),
            redirect_stdout(out),
            patch("commands.selftest.validate_external_block_device", return_value=self._ident()),
            patch("commands.selftest.Config.load", return_value=make_config()),
            patch("builtins.input", return_value="sdz"),
        ):
            assert selftest_cmd._stick_identity("/dev/sdz", make_log()).disk == "/dev/sdz"

    def _run_with(self, fn: Callable[[stest.Bench], None], td: str) -> tuple[int, str]:
        check = stest.Check("T-01", 1, 1, "t", ("zark",), False, fn)
        out = StringIO()
        code = 0
        write = certs.write_report
        with (
            redirect_stdout(out),
            patch("commands.selftest.all_checks", return_value=[check]),
            patch("lib.platform_facts.read", return_value=_facts()),
            patch("commands.selftest.Bench.setup"),
            patch("commands.selftest.Bench.teardown"),
            patch("commands.selftest._system_pools", return_value=[]),
            patch("lib.certificates.RECORD_DIR", td),
            patch("lib.certificates.write_report", side_effect=lambda r: write(r, td)),
        ):
            try:
                selftest_cmd.run([])
            except SystemExit as e:
                code = int(e.code or 0)
        return code, out.getvalue()

    def test_the_exit_status_and_the_record(self):
        with tempfile.TemporaryDirectory() as td:
            code, out = self._run_with(lambda _b: None, td)
            assert code == 0 and "SELF-TEST PASSED" in out
            code, out = self._run_with(lambda _b: (_ for _ in ()).throw(stest.Fail("a", "b")), td)
            assert code == 1 and "T-01.1" in out
            rec = certs.read_record(Path(td, "record.json"))
            assert rec[-1]["failed"] == [{"id": "T-01.1", "depends": ["zark"]}]

    def test_a_stopped_run_records_that_it_covers_nothing(self):
        def stop(_b: stest.Bench) -> None:
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as td:
            try:
                _ = self._run_with(stop, td)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("the interrupt was swallowed")
            data = json.loads(sorted(Path(td).glob("report-*.json"))[-1].read_text("utf-8"))
            assert data["certificate"]["partial"] and data["certificate"]["stopped"]

    def test_a_stopped_level_2_run_masks_the_stick_in_its_report(self):
        # M4sR-1: the stick's bare by-id alias and its serial (no word
        # boundary before it) are masked only by the bench's masks.
        alias = "usb-Kingston_DT_microDuo_3C_408D5C15CFA5E961091D0CCA-0:0"
        serial = "408D5C15CFA5E961091D0CCA"
        ident = DiskIdentity(disk="/dev/sdz", by_id=alias, serial=serial, aliases=(alias,))

        def fail(_b: stest.Bench) -> None:
            raise stest.Fail("a usb-...-0:0 by-id name", alias)

        def stop(_b: stest.Bench) -> None:
            raise KeyboardInterrupt

        checks = [
            stest.Check("T-01", 1, 2, "t", ("zark",), False, fail),
            stest.Check("T-02", 1, 2, "t", ("zark",), False, stop),
        ]
        write = certs.write_report
        with (
            tempfile.TemporaryDirectory() as td,
            redirect_stdout(StringIO()),
            patch("commands.selftest.all_checks", return_value=checks),
            patch("commands.selftest._stick_identity", return_value=ident),
            patch("commands.selftest.usb_ids", return_value=("0951", "16ae")),
            patch("lib.platform_facts.read", return_value=_facts()),
            patch("commands.selftest.StickBench.setup"),
            patch("commands.selftest.StickBench.teardown"),
            patch("commands.selftest._system_pools", return_value=[]),
            patch("lib.certificates.write_report", side_effect=lambda r: write(r, td)),
        ):
            try:
                selftest_cmd.run(["--stick", "/dev/sdz"])
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("the interrupt was swallowed")
            text = sorted(Path(td).glob("report-*.json"))[-1].read_text("utf-8")
        assert json.loads(text)["certificate"]["stopped"]
        assert alias not in text and serial not in text, text
        assert "<by-id>" in text

    def test_certificate_merges_into_a_file(self):
        with tempfile.TemporaryDirectory() as td:
            rep = Path(td, "r.json")
            rep.write_text(json.dumps(_report([stest.Result("POOL-01.1", "PASS")])), "utf-8")
            target = Path(td, "selftest.json")
            target.write_text("[]\n", "utf-8")
            with redirect_stdout(StringIO()):
                selftest_cmd.run(["certificate", "--into", str(target), str(rep), str(rep)])
            data = json.loads(target.read_text("utf-8"))
            assert len(data) == 1 and data[0]["checks"] == {"POOL-01.1": "PASS"}
            partial = Path(td, "p.json")
            partial.write_text(json.dumps(_report([], partial=True)), "utf-8")
            with redirect_stdout(StringIO()):
                try:
                    selftest_cmd.run(["certificate", str(partial)])
                except SystemExit as e:
                    assert e.code == 1
                else:
                    raise AssertionError("a partial run certified")

    def test_warnings_on_and_off(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td, "settings.json")
            with (
                patch("lib.certificates.settings_path", return_value=p),
                redirect_stdout(StringIO()),
            ):
                selftest_cmd.run(["warnings", "on"])
                assert certs.subscribed(path=p)
                selftest_cmd.run(["warnings", "off"])
                assert not certs.subscribed(path=p)

    def test_the_shipped_certificates_file_is_valid(self):
        data = json.loads(certs.certificates_file().read_text("utf-8"))
        assert isinstance(data, list)
        for e in data:
            assert certs.certificate_entry({"format": 1, "certificate": e})


class TestSelftestStick:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """M4s-4: the level-2 teardown wipes only the disk it began with."""

    def test_the_stick_is_wiped_only_when_it_is_the_same_disk(self):
        ident = DiskIdentity(disk="/dev/sdz", by_id="usb-S_1-0:0", serial="S1")
        mock = MockShell()
        mock.on_prefix("blkid -p").returns(2)
        mock.on("zpool list -vHP").succeeds("")
        with (
            patch_sh(mock),
            redirect_stdout(StringIO()),
            patch("commands.purge._held_from_udev", _held_taken),
        ):
            bench = st_l2.StickBench(make_log(), Cleanup(make_log()), ident)
            other = DiskIdentity(disk="/dev/sdy", by_id="usb-S_1-0:0", serial="S1")
            with patch("lib.identity.resolve_disk", return_value=other):
                bench.teardown()
            assert mock.was_not_called("wipefs -a /dev/sdz")
            bench._torn_down = False  # pylint: disable=protected-access
            with patch("lib.identity.resolve_disk", return_value=ident):
                bench.teardown()
            assert mock.was_called("wipefs -a /dev/sdz")
            assert mock.was_called("sgdisk --zap-all /dev/sdz")

    def test_a_stick_bench_has_no_directory(self):
        # M4sX.1, M4sX.2: its pools live on the stick; by_id is its by-id path.
        ident = DiskIdentity(disk="/dev/sdz", by_id="usb-S_1-0:0", serial="S1")
        with redirect_stdout(StringIO()):
            bench = st_l2.StickBench(make_log(), Cleanup(make_log()), ident)
        assert bench.dir == bench.key == bench.mnt == ""
        assert "<run>" not in bench.masks().values() and "" not in bench.masks()
        assert bench.by_id == "/dev/disk/by-id/usb-S_1-0:0"


class TestSelftestStickCreate:  # pylint: disable=missing-function-docstring
    """M4dR-1: a stop between the stick pool's create and its GUID."""

    IDENT = DiskIdentity(disk="/dev/sdz", by_id="usb-S_1-0:0", serial="S1")

    def _forget(self, vdev: str, cut_short: bool = True) -> tuple[MockShell, st_l2.StickBench]:
        mock = MockShell()
        with patch_sh(mock), redirect_stdout(StringIO()):
            bench = st_l2.StickBench(make_log(), Cleanup(make_log()), self.IDENT)
            mock.on(f"zpool list -vHP {bench.s}").succeeds(f"{bench.s}\t1G\n\t{vdev}\n")
            mock.on(f"lsblk -nrs -o NAME,TYPE {vdev}").succeeds(
                "sdza1 part\nsdza disk\n" if "sdza" in vdev else "sdz1 part\nsdz disk\n"
            )
            mock.on(f"zpool get -Hp -o value guid {bench.s}").succeeds("4242\n")
            if cut_short:
                bench.creating.add(bench.s)
            bench.forget()
        return mock, bench

    def test_a_pool_whose_create_was_cut_short_is_destroyed(self):
        mock, bench = self._forget("/dev/sdz1")
        assert mock.was_called(f"zpool destroy -f {bench.s}"), mock.calls
        assert not bench.creating

    def test_a_pool_that_is_not_the_runs_is_left_alone(self):
        # The same name on another disk is not the stick's pool.
        mock, bench = self._forget("/dev/sdza1")
        assert mock.was_not_called(f"zpool destroy -f {bench.s}"), mock.calls
        assert not bench.creating

    def test_without_a_create_in_flight_nothing_is_destroyed(self):
        mock, bench = self._forget("/dev/sdz1", cut_short=False)
        assert mock.was_not_called(f"zpool destroy -f {bench.s}"), mock.calls

    def test_prepare_clears_the_mark_once_the_guid_is_read(self):
        mock = MockShell()
        mock.on_prefix("mkdir -p").succeeds()
        mock.on_prefix("zpool create").succeeds()
        mock.on_prefix("zpool get -Hp -o value guid").succeeds("77\n")
        mock.on_prefix("zpool export").succeeds()
        mock.on_prefix("udevadm settle").succeeds()
        with patch_sh(mock), redirect_stdout(StringIO()):
            bench = st_l2.StickBench(make_log(), Cleanup(make_log()), self.IDENT)
            assert bench.prepare() == "77"
        assert not bench.creating


class TestSelftestStickBlank:  # pylint: disable=missing-function-docstring
    """M4sR-2: the level-2 teardown clears part1's ZFS labels, as purge does."""

    IDENT = DiskIdentity(
        disk="/dev/sdz", by_id="usb-S_1-0:0", serial="S1", aliases=("usb-S_1-0:0",)
    )

    def _teardown(self, mock: MockShell, out: StringIO | None = None) -> list[str]:
        mock.on_prefix("blkid -p").returns(2)
        mock.on("blockdev --getsize64 /dev/sdz").succeeds(f"{1024**3}\n")
        mock.on_prefix("dd ").succeeds()
        mock.on_prefix("wipefs").succeeds()
        mock.on_prefix("sgdisk").succeeds()
        mock.on("lsblk -nr -o NAME,TYPE /dev/sdz").succeeds("sdz disk\n")
        with (
            patch_sh(mock),
            redirect_stdout(out or StringIO()),
            patch("lib.identity.resolve_disk", return_value=self.IDENT),
            patch("commands.purge._present", return_value=False),
            patch("commands.purge._held_from_udev", _held_taken),
        ):
            st_l2.StickBench(make_log(), Cleanup(make_log()), self.IDENT).teardown()
        return mock.calls

    def test_the_teardown_wipe_has_no_numbered_steps(self):
        mock = MockShell()
        mock.on("zpool list -vHP").succeeds("")
        out = StringIO()
        calls = self._teardown(mock, out)
        assert "wipefs -a /dev/sdz" in calls
        assert "Wiping filesystem signatures..." in out.getvalue()
        assert "/5]" not in out.getvalue()

    def test_both_ends_are_overwritten_before_wipefs(self):
        mock = MockShell()
        mock.on("zpool list -vHP").succeeds("rpool\t100G\n\t/dev/disk/by-id/ata-X-part3\n")
        calls = self._teardown(mock)
        first = next(i for i, c in enumerate(calls) if c.startswith("dd ") and "count=10" in c)
        last = next(i for i, c in enumerate(calls) if c.startswith("dd ") and "seek=" in c)
        wipe = calls.index("wipefs -a /dev/sdz")
        assert first < wipe and last < wipe, calls
        assert int(calls[last].split("seek=")[1].split()[0]) * 512 == 1024**3 - 10 * 1024**2

    def test_nothing_is_written_while_a_pool_uses_the_stick(self):
        # M4c-27 (M4sV-2): each vdev resolved to its disks, a dm device included.
        for vdev in (
            "/dev/disk/by-id/usb-S_1-0:0-part1",
            "/dev/sdz1",
            "/dev/mapper/luks-on-stick",
            "/dev/disk/by-id/wwn-0x5000-part1",  # a by-id name the stick's list lacks
        ):
            mock = MockShell()
            mock.on("zpool list -vHP").succeeds(f"zsts\t1G\n\t{vdev}\n")
            mock.on(f"lsblk -nrs -o NAME,TYPE {vdev}").succeeds("x part\nsdz disk\n")
            calls = self._teardown(mock)
            assert not [c for c in calls if c.startswith(("dd ", "wipefs", "sgdisk"))], calls
        mock = MockShell()
        mock.on("zpool list -vHP").fails()
        assert not [c for c in self._teardown(mock) if c.startswith(("dd ", "wipefs"))]
        mock = MockShell()
        mock.on("zpool list -vHP").succeeds("zstx\t1G\n\t/dev/sdza1\n")
        mock.on("lsblk -nrs -o NAME,TYPE /dev/sdza1").succeeds("sdza1 part\nsdza disk\n")
        assert "wipefs -a /dev/sdz" in self._teardown(mock)


class TestSelftestStopsEndToEnd:  # pylint: disable=missing-function-docstring
    """M4sR-6: ``zark selftest run`` through ``zark``, stopped while a child
    runs: tests/selftest_stop_harness.py, in its own mount namespace, on fake
    zpool and zfs tools. Needs root and mount namespaces; skipped otherwise."""

    @staticmethod
    def _lock_path() -> Path:
        # One lock file for every suite on the host (M4dR-10): /run/lock is
        # shared by all processes, the temp dir is the fallback without it.
        base = Path("/run/lock") if Path("/run/lock").is_dir() else Path(tempfile.gettempdir())
        return base / "zark-tests-host-dirs.lock"

    @classmethod
    @contextmanager
    def _host_dirs_lock(cls) -> Iterator[None]:
        """Exclusive lock around the harness and the removal of the mount
        points it made: they are on the host, shared by every suite as root.
        Two suites at once otherwise remove each other's directories while
        the other's namespace uses them. The kernel drops it on any death."""
        fd = os.open(cls._lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _run_harness(self, case: str) -> dict[str, Any]:
        with self._host_dirs_lock(), tempfile.TemporaryDirectory() as td:
            harness = Path(__file__).parent / "selftest_stop_harness.py"
            r = subprocess.run(
                [sys.executable, str(harness), td, case],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
            assert r.returncode == 0, (case, r.returncode, r.stderr[-2000:])
            result: dict[str, Any] = json.loads(r.stdout.strip().splitlines()[-1])
            for d in result["made"]:  # empty mount points the harness had to make
                os.rmdir(d)
        return result

    def _run(self, case: str) -> dict[str, Any]:
        TestUmountPropagation._skip_unless_possible()  # pylint: disable=protected-access
        if _sh.is_live_usb():
            raise SkipTest("on a live session zark.log is the checkout's, outside the tmpfs")
        avail = stest._mem_available()  # pylint: disable=protected-access
        if avail is not None and avail < stest.MIN_MEM_AVAILABLE:
            raise SkipTest("too little memory for the bench")
        return self._run_harness(case)

    def test_the_host_directories_are_made_and_removed_under_a_lock(self):
        # M4dR-10: needs no root (the harness call is replaced). A second
        # opener of the lock file must be refused for as long as the
        # harness runs and the made directories are removed, and admitted
        # after.
        seen: list[bool] = []

        def other_can_lock() -> bool:
            fd = os.open(self._lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
            finally:
                os.close(fd)

        def fake_run(*_a: Any, **_k: Any) -> Any:
            seen.append(other_can_lock())
            out = json.dumps({"made": ["/nonexistent/zark-made"]})
            return subprocess.CompletedProcess([], 0, out, "")

        real_rmdir = os.rmdir

        def fake_rmdir(d: str, *a: Any, **k: Any) -> None:
            if d == "/nonexistent/zark-made":
                seen.append(other_can_lock())
            else:  # the temporary directory's own cleanup
                real_rmdir(d, *a, **k)

        # A lock file of its own: another suite on the host may hold the real
        # one at this very moment (that is what the lock is for).
        with tempfile.TemporaryDirectory() as td:
            private = Path(td, "host-dirs.lock")
            with (
                patch.object(type(self), "_lock_path", staticmethod(lambda: private)),
                patch("subprocess.run", fake_run),
                patch("os.rmdir", fake_rmdir),
            ):
                self._run_harness("sig:INT")
                assert seen == [False, False], seen
                assert other_can_lock()  # released afterwards

    def _stopped(self, name: str) -> None:
        r = self._run(f"sig:{name}")
        assert r["reached"], r
        assert r["rc"] == 128 + getattr(signal, f"SIG{name}"), r
        # The bench's two pools, by name; POOL-01's own create was stopped.
        assert len(r["created"]) == 2 and r["destroyed"] == r["created"], r
        assert not r["left"] and not r["benches"] and not r["child_alive"], r
        assert r["rm_after_destroys"], r["log"]
        assert sum("Interrupted by" in ln for ln in r["log"]) == 1, r["log"]
        assert not [ln for ln in r["log"] if "Traceback" in ln], r["log"]
        assert r["report"] == {"partial": True, "stopped": True, "checks": {}}, r

    def test_sigint_while_a_child_runs(self):
        self._stopped("INT")

    def test_sigterm_while_a_child_runs(self):
        self._stopped("TERM")

    def test_sighup_while_a_child_runs(self):
        self._stopped("HUP")

    def test_a_reader_gone_after_a_create_leaves_no_pool(self):
        # M4dF-1: the dead pipe is seen right after POOL-01's create
        # returned, before its GUID was read: the pool is still the run's.
        r = self._run("deadpipe:")
        assert r["reached"], r
        assert len(r["created"]) == 3 and sorted(r["destroyed"]) == sorted(r["created"]), r
        assert not r["left"] and not r["benches"] and not r["child_alive"], r
        assert r["rm_after_destroys"], r["log"]
        assert not [ln for ln in r["log"] if "Traceback" in ln], r["log"]


class TestTypography:  # pylint: disable=missing-function-docstring
    """M4c-3: no em dash, en dash or U+2026 in any tracked text file."""

    _REPO = Path(__file__).resolve().parent.parent
    _CHARS = {"\u2014": "em dash", "\u2013": "en dash", "\u2026": "U+2026"}
    # Without git (a tarball, an unpacked source package): the tracked
    # top-level directories, and everything a build or a tool leaves out.
    _TOP_DIRS = frozenset(
        {".github", "certificates", "commands", "debian", "docs", "etc", "lib", "tests"},
    )
    _SKIP_DIRS = frozenset(
        {".git", ".tox", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"},
    )
    _SKIP_PATHS = frozenset({"debian/zark", "debian/.debhelper", "debian/tmp"})

    def _files(self) -> list[Path]:
        try:
            r = subprocess.run(
                ["git", "-C", str(self._REPO), "ls-files", "-z"],
                capture_output=True,
                check=False,
            )
        except OSError:
            r = None
        if r is not None and r.returncode == 0 and r.stdout:
            names = [n for n in r.stdout.decode("utf-8", "surrogateescape").split("\0") if n]
            return [self._REPO / n for n in names]
        out: list[Path] = []
        for root, dirs, files in os.walk(self._REPO):
            rel = Path(root).relative_to(self._REPO)
            if rel == Path("."):
                dirs[:] = [d for d in dirs if d in self._TOP_DIRS]
                # Generated or local-only top-level files (see .gitignore).
                files = [
                    f
                    for f in files
                    if f != "PROJECT-NOTES.md" and not f.endswith((".log", ".tar.gz"))
                ]
            else:
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in self._SKIP_DIRS and str(rel / d) not in self._SKIP_PATHS
                ]
            out += [Path(root) / f for f in files]
        return out

    def test_no_typographic_dashes_or_ellipsis(self):
        found: list[str] = []
        for path in self._files():
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue  # binary
            for n, line in enumerate(text.splitlines(), 1):
                found += [
                    f"{path.relative_to(self._REPO)}:{n}: {name}"
                    for ch, name in self._CHARS.items()
                    if ch in line
                ]
        assert not found, "\n".join(found)

    def test_every_script_carries_the_apache_header(self):
        # H22 (M4d-27): the shell scripts had none, and one named another licence.
        found: list[str] = []
        for path in self._files():
            rel = str(path.relative_to(self._REPO))
            if not (rel.endswith((".py", ".sh")) or rel == "zark") or not path.is_file():
                continue
            head = "\n".join(path.read_text("utf-8").splitlines()[:16])
            if "Licensed under the Apache License, Version 2.0" not in head:
                found.append(f"{rel}: no Apache header")
            if re.search("GPL" + r"-\d", path.read_text("utf-8")):
                found.append(f"{rel}: names a GPL version")
        assert not found, found

    def test_the_readme_tree_lists_every_module(self):
        # M2P.5, M4cP.1 (M4d-30): the tree lacked 31 modules of lib/ and 7 of commands/.
        readme = (self._REPO / "README.md").read_text("utf-8")
        tree = readme.split("```\nzark/\n", 1)[1].split("```", 1)[0]
        listed = {m.group(1) for m in re.finditer(r"[├└]── (\S+)", tree)}
        missing = [
            str(f.relative_to(self._REPO))
            for d in ("lib", "commands")
            for f in sorted((self._REPO / d).glob("*.py"))
            if f.name != "__init__.py" and f.name not in listed
        ]
        assert not missing, missing

    def test_the_fallback_walk_sees_the_tracked_tree(self):
        with patch("subprocess.run", side_effect=OSError("no git")):
            walked = {str(p.relative_to(self._REPO)) for p in self._files()}
        want = {"zark", "README.md", "lib/sh.py", "debian/changelog", "tests/test_unit.py"}
        assert want <= walked
        assert not [w for w in walked if w.startswith(("m4s-review/", ".git/", "debian/zark/"))]


def _run_one(name: str, method: Callable[[], object]) -> str:
    """Run one test and print its line; return "passed", "skipped" or "failed".

    A ``SystemExit`` escaping a test is a failure: caught by nothing else, it
    would end the whole run with that exit status and no summary line.
    What the test prints and does not capture itself (log lines, a FATAL
    box) is kept off the terminal and shown only when it fails (M1P.20).
    """
    seen = StringIO()
    try:
        with redirect_stdout(seen), redirect_stderr(seen):
            method()
    except SkipTest as e:
        print(f"    \033[0;33m-\033[0m {name}: skipped ({e})")
        return "skipped"
    except SystemExit as e:
        print(f"    \033[0;31m✗\033[0m {name}: raised SystemExit({e.code!r})")
        _show_output(seen)
        return "failed"
    except Exception as e:  # pylint: disable=broad-except
        print(f"    \033[0;31m✗\033[0m {name}: {e}")
        _show_output(seen)
        return "failed"
    print(f"    \033[0;32m✓\033[0m {name}")
    return "passed"


def _show_output(seen: StringIO) -> None:
    """A failed test's own output, indented under its line."""
    for line in seen.getvalue().splitlines()[-40:]:
        print(f"        | {line}")


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
