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
``/run/zark/progress.json``: what the running command is doing, for other
programs (docs/STATUS-FILES.md, schema version 1; M7-3 to M7-9).

Every command that takes the lock writes it (D2). ``zark``'s ``main`` calls
:func:`start` right after the lock is taken:

  * an existing file is an orphan, since the lock admits one run: a backup's
    becomes that drive's ``last_run`` ``interrupted`` in status.json, unless
    its run had already recorded a result (M7-7 7a), then it is replaced;
  * the first write is made at once; then a daemon thread writes every
    :data:`HEARTBEAT_S` and after every event (a step, a terminal read, the
    drive, the point, a transfer), never more than once a second (M7-3 3a).
    The main thread waits in ``communicate()``, ``input()``, ``getpass`` or
    ``select()``, which release the GIL, so the heartbeat goes on during a
    send and during a prompt; signals still reach the main thread only;
  * the end is registered with ``atexit`` before the command registers its
    Cleanup, so it runs after it (M7-5 5a): status.json is written with what
    the run recorded, then the file goes. A SIGKILL runs nothing: the
    orphan's case.

Nothing here fails a command: a write that fails is one warning per distinct
error, and the run goes on.
"""

import atexit
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib import log as log_mod, sh, signals, status_file, statusdirs
from lib.config import VERSION, Config, now_utc_iso
from lib.log import Log
from lib.status_file import DriveUpdate, LastRun
from lib.zfs import Space

KIND = "zark-progress"
SCHEMA_VERSION = 1
PROGRESS_PATH = statusdirs.RUN_DIR / "progress.json"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
HEARTBEAT_S = 2.0  # the contract's limit is 5 s
MIN_GAP_S = 1.0  # never more than one write a second
RATE_WINDOW_S = 30.0  # rate_bytes_per_s is bytes_done's rate over this window


def start_ticks(pid: int, proc: Path = Path("/proc")) -> int:
    """Field 22 (``starttime``) of ``/proc/<pid>/stat``, counted after the
    last ``)``: the name may hold spaces and parentheses (contract rule 5)."""
    text = (proc / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
    return int(text[text.rindex(")") + 2 :].split()[19])


def group_wchar(pgid: int, proc: Path = Path("/proc")) -> int:
    """What the live members of process group ``pgid`` wrote: ``sh -c`` and
    the ``zfs send`` it runs (M7-2 2a). Raises OSError when a member's
    counters cannot be read (not root)."""
    total = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_line = (entry / "stat").read_text(encoding="ascii", errors="replace")
            if int(stat_line[stat_line.rindex(")") + 2 :].split()[2]) != pgid:
                continue
            total += sh.wchar(int(entry.name), str(proc))
        except (FileNotFoundError, ProcessLookupError):
            # It ended between the listing and the open (ENOENT) or between the
            # open and the read (ESRCH, fs/proc/base.c:808-810): M7R-2.
            continue
        except ValueError as e:
            raise OSError(f"{entry}: {e}") from e
    return total


class ByteMeter:
    """``bytes_done`` and ``rate_bytes_per_s`` (M7-2 2a): the kernel's count
    of what each ``zfs send`` wrote, sampled live from its process group and
    read exactly when it ends (:func:`lib.sh.run_pipe`). One read that fails
    leaves both null for the rest of the run: a null is never a zero."""

    def __init__(self, proc: Path = Path("/proc")):
        self._proc = proc
        self._lock = threading.Lock()
        self._done = 0  # sends that ended
        self._live = 0  # the running one, as last sampled
        self._pgid: int | None = None
        self._broken = False
        self._samples: deque[tuple[float, int]] = deque()

    def begin(self, pgid: int) -> None:
        """A send started in process group ``pgid``."""
        with self._lock:
            self._pgid, self._live = pgid, 0

    def end(self, written: int | None) -> None:
        """The send ended, having written ``written`` (None: not read)."""
        with self._lock:
            if written is None:
                self._broken = True
            else:
                self._done += written
            self._pgid, self._live = None, 0

    def sample(self, now: float | None = None) -> None:
        """Read the running send's count (from the heartbeat)."""
        with self._lock:
            if self._pgid is not None and not self._broken:
                try:
                    # Never backwards: a member may be reaped between two reads.
                    self._live = max(self._live, group_wchar(self._pgid, self._proc))
                except OSError:
                    self._broken = True
            if self._broken:
                return
            t = time.monotonic() if now is None else now
            self._samples.append((t, self._done + self._live))
            while self._samples and t - self._samples[0][0] > RATE_WINDOW_S:
                _ = self._samples.popleft()

    def bytes_done(self) -> int | None:
        """Stream bytes sent so far in this run, None when not measured."""
        with self._lock:
            return None if self._broken else self._done + self._live

    def rate(self) -> int | None:
        """``bytes_done``'s rate over the last 30 s; None until two samples."""
        with self._lock:
            if self._broken or len(self._samples) < 2:
                return None
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
            return int((b1 - b0) / (t1 - t0)) if t1 > t0 else None


class TransferProgress:
    """The engine's transfer, as progress.json's ``transfer`` says it (M7-8):
    backup only (D2). ``datasets_total`` counts a transfer per dataset per
    round; an estimate of 0 is unknown and makes ``bytes_total`` a floor."""

    def __init__(self, meter: ByteMeter | None = None):
        self.meter = meter or ByteMeter()
        self._fields: dict[str, Any] = {}

    def _show(self) -> None:
        set_transfer(self._fields, self.meter)

    def begin(self) -> None:
        """The transfers start (step 7)."""
        self._fields = {
            "datasets_total": 0,
            "datasets_done": 0,
            "datasets_failed": 0,
            "dataset": None,
            "dataset_state": None,
            "bytes_total": None,
            "bytes_total_partial": False,
        }
        self._show()

    def planned(self, estimate: int) -> None:
        """One more transfer, of ``estimate`` stream bytes (0: unknown)."""
        f = self._fields
        f["datasets_total"] += 1
        if estimate > 0:
            f["bytes_total"] = (f["bytes_total"] or 0) + estimate
        else:
            f["bytes_total_partial"] = True
        self._show()

    def sending(self, dataset: str, state: str) -> None:
        """``dataset`` is being sent, as its plan ``state`` says."""
        self._fields.update(dataset=dataset, dataset_state=state)
        self._show()

    def ended(self, ok: bool) -> None:
        """The current transfer ended."""
        f = self._fields
        f["datasets_done"] += 1
        f["datasets_failed"] += 0 if ok else 1
        f.update(dataset=None, dataset_state=None)
        self._show()

    def end(self) -> None:
        """No more data moves: ``transfer`` is null again."""
        set_transfer(None)


@dataclass
class _State:  # pylint: disable=too-many-instance-attributes
    """The document's fields, guarded by ``lock``."""

    command: str
    pid: int
    pid_start_ticks: int
    boot_id: str
    started_at: str
    path: Path
    log: Log
    waiting_for: str | None = None
    step: dict[str, Any] | None = None
    drive: dict[str, Any] | None = None
    point: str | None = None
    transfer: dict[str, Any] | None = None
    meter: ByteMeter | None = None
    updates: dict[str, DriveUpdate] = field(default_factory=dict)
    error: str = ""  # the last fatal error's message, or a crash's
    on_end: list[Callable[[], None]] = field(default_factory=list)
    errors: set[str] = field(default_factory=set)
    dirty: bool = True
    stopping: bool = False
    last_write: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    wake: threading.Condition = field(init=False)

    def __post_init__(self) -> None:
        self.wake = threading.Condition(self.lock)

    def document(self) -> dict[str, Any]:
        """The file's content now (call with ``lock`` held)."""
        return {
            "kind": KIND,
            "schema_version": SCHEMA_VERSION,
            "zark_version": VERSION,
            "command": self.command,
            "pid": self.pid,
            "pid_start_ticks": self.pid_start_ticks,
            "boot_id": self.boot_id,
            "started_at": self.started_at,
            "updated_at": now_utc_iso(),
            "waiting_for_input": self.waiting_for is not None,
            "waiting_for": self.waiting_for,
            "step": self.step,
            "drive": self.drive,
            "point": self.point,
            "transfer": self._transfer(),
        }

    def _transfer(self) -> dict[str, Any] | None:
        if self.transfer is None:
            return None
        done = self.meter.bytes_done() if self.meter else None
        rate = self.meter.rate() if self.meter else None
        return {**self.transfer, "bytes_done": done, "rate_bytes_per_s": rate}


_RUN: list[_State] = []
_THREAD: list[threading.Thread] = []


def active() -> bool:
    """Whether this process writes progress.json."""
    return bool(_RUN)


def started_at() -> str:
    """When this command started (the run's ``started_at``), now when none runs."""
    return _RUN[0].started_at if _RUN else now_utc_iso()


def _changed(update: Callable[[_State], None]) -> None:
    """Apply ``update`` to the state and wake the writer; nothing without a run."""
    if not _RUN:
        return
    run = _RUN[0]
    with run.lock:
        update(run)
        run.dirty = True
        run.wake.notify()


def _write(run: _State) -> None:
    """One write of the file (the caller holds ``run.lock``)."""
    if run.meter is not None:
        run.meter.sample()
    try:
        status_file.write_atomic(
            run.path, json.dumps(run.document(), indent=2) + "\n", durable=False
        )
    except OSError as e:
        why = f"{type(e).__name__}: {e.strerror or e}"
        if why not in run.errors:  # one warning per distinct error, not one every 2 s
            run.errors.add(why)
            run.log.warn(f"Could not write {run.path}: {why}")
    run.dirty = False
    run.last_write = time.monotonic()


def _heartbeat(run: _State) -> None:
    with run.lock:
        while not run.stopping:
            gap = MIN_GAP_S - (time.monotonic() - run.last_write)
            if run.dirty and gap <= 0:
                _write(run)
                continue
            due = HEARTBEAT_S - (time.monotonic() - run.last_write)
            if due <= 0:
                _write(run)
                continue
            _ = run.wake.wait(max(gap, 0.05) if run.dirty else due)


def _orphan(path: Path, log: Log) -> dict[str, Any] | None:
    """The file a dead run left, when it parses as schema 1 progress."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warn(f"Replacing an unreadable {path} a dead run left: {e}")
        return None
    version = doc.get("schema_version") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or doc.get("kind") != KIND or not isinstance(version, int):
        log.warn(f"Replacing {path}: not a progress file zark can read")
        return None
    if version > SCHEMA_VERSION:
        log.warn(f"Replacing {path}: written by a newer zark (schema {version})")
        return None
    return doc


def _record_orphan(doc: dict[str, Any], command: str, log: Log) -> None:
    """A dead backup's drive gets ``last_run`` ``interrupted`` (M7-7 7a)."""
    drive = doc.get("drive")
    if doc.get("command") != "backup" or not isinstance(drive, dict):
        return
    guid, began, ended = drive.get("guid"), doc.get("started_at"), doc.get("updated_at")
    if not (isinstance(guid, str) and isinstance(began, str) and isinstance(ended, str)):
        return
    point = doc.get("point")
    run = LastRun(
        "interrupted",
        began,
        ended,
        point if isinstance(point, str) else None,
        message="the run ended without recording a result",
    )
    log.warn(f"An earlier backup to {drive.get('name', guid)} ended without cleaning up")
    if sh.is_live_usb():
        return
    cfg = Config.load()
    if cfg.load_error is not None:
        log.warn(status_file.unusable_registry(cfg.load_error))  # M7R-1: not dropped silently
        return
    if guid not in {i.guid for i in cfg.known_drives.values()}:
        return  # a drive no longer registered is dropped (D4)
    status_file.refresh_or_warn(
        cfg,
        Log(os.devnull),
        command,
        {guid: DriveUpdate(run, unless_started_at=began)},
        warn=log,
    )


def start(command: str, log: Log | None = None, path: Path | None = None) -> bool:
    """Start writing progress.json for ``command`` (the lock is held).

    False, and nothing written, when this process cannot be told apart
    (``/proc`` unreadable); the command runs on.
    """
    if _RUN:
        return True
    log = log or Log()
    path = path or PROGRESS_PATH
    try:
        pid = os.getpid()
        run = _State(
            command,
            pid,
            start_ticks(pid),
            BOOT_ID.read_text(encoding="ascii").strip(),
            now_utc_iso(),
            path,
            log,
        )
        _ = statusdirs.ensure(path.parent)
    except (OSError, ValueError, IndexError) as e:
        log.warn(f"Not writing {path}: {e}")
        return False
    orphan = _orphan(path, log)
    if orphan is not None:
        _record_orphan(orphan, command, log)
    _RUN[:] = [run]
    with run.lock:
        _write(run)
    log_mod.watch(on_step, on_wait, note_error)
    thread = threading.Thread(target=_heartbeat, args=(run,), name="zark-progress", daemon=True)
    _THREAD[:] = [thread]
    thread.start()
    atexit.register(end)
    return True


def on_step(index: int, total: int, label: str) -> None:
    """A numbered step began."""
    if index >= 1 and total >= 1:
        _changed(lambda r: setattr(r, "step", {"index": index, "total": total, "label": label}))


def on_wait(prompt: str | None) -> None:
    """A terminal read began (its prompt) or ended (None)."""
    _changed(lambda r: setattr(r, "waiting_for", prompt))


def note_error(message: str) -> None:
    """Why the run is ending: a fatal error's message, or a crash's."""
    _changed(lambda r: setattr(r, "error", message))


def error() -> str:
    """The run's last :func:`note_error`, "" when none."""
    if not _RUN:
        return ""
    with _RUN[0].lock:
        return _RUN[0].error


def set_drive(name: str, guid: str, drive_id: str) -> None:
    """The backup drive the command works on."""
    entry = {"name": name, "guid": guid, "drive_id": drive_id or "<unknown>", "space": None}
    _changed(lambda r: setattr(r, "drive", entry))


def set_space(space: Space | None) -> None:
    """The drive's space, as just read."""
    if space is None:
        return

    def update(r: _State) -> None:
        if r.drive is not None:
            r.drive = {**r.drive, "space": space.to_json()}

    _changed(update)


def set_point(point: str | None) -> None:
    """The run's backup point, once taken."""
    _changed(lambda r: setattr(r, "point", point))


def set_transfer(transfer: dict[str, Any] | None, meter: ByteMeter | None = None) -> None:
    """The transfer's counters while a backup moves data, with the meter of
    its bytes; None once it ended."""

    def update(r: _State) -> None:
        r.transfer = dict(transfer) if transfer is not None else None
        r.meter = meter if transfer is not None else None

    _changed(update)


def record(guid: str, update: DriveUpdate) -> None:
    """What this run measured for a drive, written to status.json at the end
    (a later record for the same drive replaces it)."""
    _changed(lambda r: r.updates.__setitem__(guid, update))


def updates() -> dict[str, DriveUpdate]:
    """What the run recorded so far, by GUID."""
    if not _RUN:
        return {}
    with _RUN[0].lock:
        return dict(_RUN[0].updates)


def on_end(action: Callable[[], None]) -> None:
    """Run ``action`` first at the end, before status.json is written (the
    run's ``last_run`` when no result was recorded)."""
    _changed(lambda r: r.on_end.append(action))


def end() -> None:
    """The run is over: status.json with what it recorded, then the file
    goes (docs/STATUS-FILES.md, "Lifecycle"). From atexit, after the
    command's Cleanup; the exit signals are ignored from here on."""
    if not _RUN:
        return
    signals.teardown()
    run = _RUN[0]
    for action in list(run.on_end):
        try:
            action()
        except Exception as e:  # pylint: disable=broad-exception-caught
            run.log.warn(f"Could not record the run's end: {e}")
    with run.lock:
        run.stopping = True
        run.wake.notify()
    for t in _THREAD:
        t.join(timeout=5)
    log_mod.watch(None, None)
    if not sh.is_live_usb():
        try:
            cfg = Config.load()
            _ = status_file.refresh(cfg, Log(os.devnull), run.command, dict(run.updates))
        except Exception as e:  # pylint: disable=broad-exception-caught
            run.log.warn(f"Could not refresh the status files: {e}")
    try:
        run.path.unlink(missing_ok=True)
    except OSError as e:
        run.log.warn(f"Could not remove {run.path}: {e}")
    _RUN.clear()
    _THREAD.clear()
