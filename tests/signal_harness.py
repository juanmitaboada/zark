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
A command as zark runs it, stopped by signals (H12, M4b-1):

    python tests/signal_harness.py <workdir> <scenario>

``lib.signals`` installed as ``zark`` installs it, a Cleanup registered as
the commands register it, and a child pipeline (``sh.run_pipe``) or command
(``sh.run``) that would run for a minute. A helper process outside the
harness sends the signals, so nothing reaches the children unless zark
passes it on. Scenarios:

    pipe:<SIGS>     signals while sh.run_pipe waits (several = pending together)
    run:<SIGS>      the same while sh.run waits
    cleanup:<SIG>   one signal to stop, a second one while the cleanup runs
    deadpipe:       lines printed until the reader of stdout goes away; the
                    cleanup prints too
    quietend:       one line, then the command ends normally after the reader
                    went away; only the cleanup prints
    interactive:<SIGS>  signals one second apart while a child that owns the
                    terminal runs (lib.signals.run_interactive)
    stubborn:<SIGS> the same as run:, with a child that ignores SIGTERM and a
                    stop wait of half a second (STOP_WAIT_SECONDS patched)
    orphan:<SIGS>   the same, but the shell sh.run starts dies with the signal
                    and only its command (a grandchild) ignores it
    afterexit:<SIG>,<SIG>  one signal stops the pipeline; the second arrives
                    from an atexit handler that runs after the cleanup
    resumed:        the command runs cleanup.run() itself, then prints lines
                    until the reader of stdout goes away
    normalend:<SIG>[,<SIG>]  the command ends normally and runs cleanup.run()
                    itself; the first signal arrives while a cleanup step's
                    child runs, the second (if any) after the cleanup

Writes ``<workdir>/result.json``: ``cleanup`` (how many times it ran),
``children_alive`` (children still running when it ran), ``continued``
(whether the command went on after the interrupted wait), ``cleanup_done``
(whether the cleanup reached its end) and, for ``run``, ``stubborn`` and
``orphan``, ``child_status`` (the wait status of the child zark started: -9
when SIGKILL ended it, -2 for SIGINT, -15 for SIGTERM).
"""

import atexit
import io
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

from lib import sh, signals  # pylint: disable=wrong-import-position # noqa: E402
from lib.cleanup import Cleanup  # pylint: disable=wrong-import-position # noqa: E402
from lib.log import Log  # pylint: disable=wrong-import-position # noqa: E402


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0): read its state.
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def _sender(names: list[str], delay: float, apart: bool = False) -> None:
    """Send the signals from outside: all pending at once when several, or
    one second apart."""
    me = os.getpid()
    if apart:
        script = f"sleep {delay}; " + "; sleep 1; ".join(f"kill -{n} {me}" for n in names)
    else:
        kills = "; ".join(f"kill -{n} {me}" for n in names)
        script = f"sleep {delay}; kill -STOP {me}; {kills}; kill -CONT {me}"
    _ = subprocess.Popen(["sh", "-c", script], start_new_session=True)  # pylint: disable=consider-using-with


def main() -> None:  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Run one scenario; exit as zark's main does."""
    work, scenario = sys.argv[1], sys.argv[2]
    kind, _, names = scenario.partition(":")
    sigs = [n for n in names.split(",") if n]
    result: dict[str, object] = {"cleanup": 0, "continued": False, "cleanup_done": False}
    pids = [os.path.join(work, "p1"), os.path.join(work, "p2")]

    def write() -> None:
        with open(os.path.join(work, "result.json"), "w", encoding="ascii") as f:
            json.dump(result, f)

    def action() -> None:
        result["cleanup"] = int(str(result["cleanup"])) + 1
        alive = []
        for p in pids:
            if os.path.exists(p):
                with open(p, encoding="ascii") as f:
                    pid = int(f.read().strip())
                if _alive(pid):
                    alive.append(pid)
        result["children_alive"] = alive
        write()
        if kind == "cleanup":
            _sender([sigs[1]], 0.0)
            time.sleep(1.5)
        if kind in ("deadpipe", "quietend"):
            print("cleaning up with nobody reading", flush=True)
        result["cleanup_done"] = True
        write()

    log = Log(os.path.join(work, "zark.log"))
    signals.install(log)
    if kind == "afterexit":
        # Registered before the Cleanup, so atexit runs it after: a second
        # signal once the signal-started cleanup is over.
        def after() -> None:
            os.kill(os.getpid(), signal.Signals[f"SIG{sigs[1]}"])
            time.sleep(0.5)
            result["after_cleanup"] = True
            write()

        _ = atexit.register(after)
    cleanup = Cleanup(log)
    cleanup.register()
    cleanup.track_action(action)
    if kind == "normalend":
        # Runs before `action` (newest first): the signal lands in its child.
        def slow() -> None:
            _ = sh.run("sleep 2")

        cleanup.track_action(slow)
    if kind in ("run", "stubborn", "orphan"):
        real_stop = signals.stop_children

        def recording_stop(
            procs: list[subprocess.Popen[str]] | list[subprocess.Popen[bytes]],
            sig: int | None = None,
            wait: float | None = None,
        ) -> None:
            real_stop(procs, sig, **({} if wait is None else {"wait": wait}))
            result["child_status"] = procs[0].returncode
            write()

        signals.stop_children = recording_stop
    if kind in ("stubborn", "orphan"):
        # Something ignores the stop: half a second, then SIGKILL. The wait
        # is read when stop_children runs, so the module constant counts.
        signals.STOP_WAIT_SECONDS = 0.5
    if sigs:
        one = kind in ("cleanup", "normalend", "afterexit")
        _sender(sigs[:1] if one else sigs, 0.5, apart=kind == "interactive")
    try:
        if kind == "normalend":
            cleanup.run()
            result["continued"] = True
            write()
            if len(sigs) > 1:
                _sender([sigs[1]], 0.0)
                time.sleep(3.0)
            sys.exit(0)
        if kind == "resumed":
            # A normal-end cleanup (its lines kept off stdout, so the reader
            # is still there after it), then the command goes on.
            guarded, sys.stdout = sys.stdout, io.StringIO()
            cleanup.run()
            sys.stdout = guarded
            for i in range(600):
                print(f"line {i}", flush=True)
                time.sleep(0.05)
        elif kind == "interactive":
            _ = signals.run_interactive(["sh", "-c", f"echo $$ > {pids[0]}; exec sleep 60"])
        elif kind == "quietend":
            print("started", flush=True)
            time.sleep(1.0)
        elif kind == "deadpipe":
            for i in range(600):
                print(f"line {i}", flush=True)
                time.sleep(0.05)
        elif kind == "run":
            _ = sh.run(f"sh -c 'echo $$ > {pids[0]}; exec sleep 60'")
        elif kind == "stubborn":
            # The shell sh.run starts is the one that traps: sleep inherits it.
            _ = sh.run(f"trap '' TERM; echo $$ > {pids[0]}; exec sleep 60")
        elif kind == "orphan":
            # The outer shell dies with the signal; only the inner one traps.
            _ = sh.run(f"sh -c 'trap \"\" TERM; echo $$ > {pids[0]}; exec sleep 60'")
        else:
            _ = sh.run_pipe(
                f"echo $$ > {pids[0]}; exec sleep 60",
                f"echo $$ > {pids[1]}; exec cat",
            )
        result["continued"] = True
        write()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
