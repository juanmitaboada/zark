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
from pathlib import Path
from unittest.mock import patch

from lib import health


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
