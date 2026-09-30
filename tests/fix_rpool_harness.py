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
Run the real ``fix_rpool_mountpoint.run()`` in its own process, with signals.

Used by the unit suite through a subprocess, so that a regression in the
signal handling kills this process and not the test runner. Import, export
and the module parameter are fakes; nothing touches ZFS.

    python tests/fix_rpool_harness.py <workdir> <scenario>

Scenarios (the signals are sent before ``_fix`` runs, or during the export):

    sig:TERM                one signal
    pending:INT,TERM        both blocked, sent in that order, then unblocked
    export:INT              a KeyboardInterrupt in _fix, then SIGINT during the export
    export:TERM,HUP         both pending, delivered during the export
    deadpipe:INT            stdout becomes a pipe with no reader, then SIGINT

``<workdir>/events`` gets one line per export with the parameter's
value at that moment; ``<workdir>/zvol_inhibit_dev`` is the parameter.
"""

import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

import commands.fix_rpool_mountpoint as fix_rpool_mod  # pylint: disable=wrong-import-position # noqa: E402
from lib.log import Log  # pylint: disable=wrong-import-position # noqa: E402
from lib.zfs import ZFS  # pylint: disable=wrong-import-position # noqa: E402
from tests.mock_sh import MockShell, patch_sh  # pylint: disable=wrong-import-position # noqa: E402


def _signals(names: str) -> list[signal.Signals]:
    return [signal.Signals[f"SIG{n}"] for n in names.split(",")]


def _send_pending(sigs: list[signal.Signals]) -> None:
    """Make every signal pending at once, then let CPython run the handlers."""
    _ = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
    for s in sigs:
        os.kill(os.getpid(), s)
    _ = signal.pthread_sigmask(signal.SIG_UNBLOCK, sigs)


def _kill_stdout() -> None:
    """Point stdout at a pipe whose reader is gone (a `| tee` killed by Ctrl-C)."""
    sys.stdout.flush()
    r, w = os.pipe()
    os.dup2(w, 1)
    os.close(r)
    os.close(w)


def main() -> None:  # pylint: disable=too-many-locals
    """Run one scenario; the exit status is whatever the command ends with."""
    work = Path(sys.argv[1])
    kind, names = sys.argv[2].split(":")
    sigs = _signals(names)
    param = work / "zvol_inhibit_dev"
    events = work / "events"
    param.write_text("0\n", encoding="utf-8")
    # As zark:39 sets it: a write to a dead pipe kills the process.
    _ = signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    def note(what: str) -> None:
        with events.open("a", encoding="utf-8") as f:
            _ = f.write(f"{what}(inhibit={param.read_text(encoding='utf-8').strip()})\n")

    def fake_export(zfs: ZFS, name: str) -> bool:
        if kind == "export":
            if sigs == [signal.SIGINT]:
                os.kill(os.getpid(), signal.SIGINT)
            else:
                _send_pending(sigs)
        # As sh.run does: the command line is logged before it runs.
        zfs.log.cmd(f"zpool export {name}")
        note("export")
        return True

    def fix(_zfs: ZFS, _log: Log, _turned_off: list[str]) -> bool:
        if kind == "sig":
            os.kill(os.getpid(), sigs[0])
        elif kind == "pending":
            _send_pending(sigs)
        elif kind == "export":
            raise KeyboardInterrupt
        elif kind == "deadpipe":
            _kill_stdout()
            os.kill(os.getpid(), sigs[0])
        return False

    mock = MockShell()
    mock.on("modprobe zfs").succeeds()
    with (
        patch_sh(mock),
        patch.object(Log, "default_file", classmethod(lambda _c: str(work / "zark.log"))),
        patch.object(fix_rpool_mod, "ZVOL_INHIBIT", param),
        patch.object(fix_rpool_mod, "_fix", side_effect=fix),
        patch.object(fix_rpool_mod.sh, "is_live_usb", return_value=True),
        patch.object(fix_rpool_mod.glob, "glob", return_value=[]),
        patch.object(ZFS, "pool_exists", return_value=False),
        patch.object(ZFS, "pool_export", autospec=True, side_effect=fake_export),
    ):
        try:
            fix_rpool_mod.run([])
        except KeyboardInterrupt:
            sys.exit(130)  # as zark's main() does


if __name__ == "__main__":
    main()
