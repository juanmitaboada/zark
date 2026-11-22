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
QEMU's process and the run's time: every wait ends.

The job's ``timeout-minutes`` cancels the whole job, and a cancelled job runs
no leak check and uploads nothing (run 45147805605). So the bench keeps its
steps inside a budget of its own, enforced by a signal that interrupts even a
blocking call, and keeps time back for the leak check and the upload.
"""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from bench.qmp import Qmp, QmpError

# The job's timeout-minutes (.github/workflows/bench.yml), split three ways.
JOB_TIMEOUT_S = 120 * 60
# Checkout and runner preparation, before ``python3 -m bench`` starts
# (137 s in run 45134324398).
PREPARE_RESERVE_S = 10 * 60
# After the steps: the machine's power-off, the leak check over out/ and the
# disk image, then the upload (about 30 s so far).
FINISH_RESERVE_S = 10 * 60
STEPS_BUDGET_S = JOB_TIMEOUT_S - PREPARE_RESERVE_S - FINISH_RESERVE_S


class BudgetExpired(Exception):
    """The steps' budget ran out; the run goes straight to its leak check."""


@contextmanager
def watchdog(seconds: int) -> Iterator[None]:
    """
    Raise :class:`BudgetExpired` in the main thread after ``seconds``, also
    inside a blocking read: Python retries an interrupted call only when the
    signal's handler returns, and this one raises.
    """

    def fire(_signum: int, _frame: FrameType | None) -> None:
        raise BudgetExpired(f"the steps' budget of {seconds} s ran out")

    previous = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def stop(
    proc: subprocess.Popen[bytes],
    qmp_socket: Path,
    *,
    qmp_timeout_s: float = 5.0,
    quit_wait_s: float = 30.0,
    term_wait_s: float = 10.0,
) -> str:
    """
    End QEMU, every wait bounded: ``quit`` over QMP, then SIGTERM, then
    SIGKILL. Returns how it ended: ``exited`` (before any of it), ``quit``,
    ``terminated`` or ``killed``.
    """
    if proc.poll() is not None:
        return "exited"
    try:
        q = Qmp.connect(qmp_socket, timeout_s=qmp_timeout_s, io_timeout_s=qmp_timeout_s)
        try:
            q.execute("quit")
        finally:
            q.close()
    except (OSError, QmpError):
        pass  # a monitor that does not answer is what SIGTERM is for
    if _ended(proc, quit_wait_s):
        return "quit"
    proc.terminate()
    if _ended(proc, term_wait_s):
        return "terminated"
    proc.kill()
    proc.wait()
    return "killed"


def _ended(proc: subprocess.Popen[bytes], wait_s: float) -> bool:
    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        return False
    return True
