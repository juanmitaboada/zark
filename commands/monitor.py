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
zark monitor - a live view of what zark is doing and how the backups stand.

A reader of the status files (``docs/STATUS-FILES.md``), run as any user:
``/run/zark/progress.json`` is checked every second, ``status.json`` too, and
the source pools are listed live every interval. It never writes either file,
takes no lock and never imports, exports or changes a pool (M8).

Full screen with curses when stdin and stdout are a terminal; otherwise one
plain frame per change. ``--once`` prints one frame; ``--once --state`` only
the indicator state.
"""

import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib import live_pools, monitor_view as view, status_reader as sr
from lib.config import VERSION, zark_command
from lib.transfer_eta import TransferEta

INTERVALS = (15, 30, 60, 120, 300, 600)
MIN_INTERVAL, MAX_INTERVAL = 5, 3600
EX_USAGE = 2
USAGE = """\
Usage: zark monitor [--once [--state]] [--interval N] [--root DIR] [--no-live]

  --once         print one frame and exit
  --state        with --once: print only the indicator state
                 (waiting, running, attention, edge, overdue, none, ok)
  --interval N   seconds between live pool listings (5-3600, default 60)
  --root DIR     read DIR/run/zark/progress.json and DIR/var/lib/zark/status.json
                 (for testing, and for looking at the fixtures)
  --no-live      do not list the pools (zfs and zpool are not run)

Keys: q quit, d datasets, s sort, - / + interval, r refresh, b bell, h help.
No root needed.
"""

_SGR = {
    "bold": "1",
    "dim": "2",
    "ok": "32",
    "warn": "33",
    "crit": "31",
    "wait": "34",
    "run": "36",
    "head": "1;36",  # a section's title: bold cyan (M8F-9)
}


@dataclass(frozen=True)
class Options:
    """The command line."""

    once: bool = False
    state: bool = False
    interval: int = 60
    root: Path = Path("/")
    live: bool = True


def _value(arg: str, value: str, opts: Options) -> Options | str:
    if arg == "--root":
        if not Path(value).is_dir():
            return f"--root: not a directory: {value}"
        return replace(opts, root=Path(value))
    if not value.isdigit() or not MIN_INTERVAL <= int(value) <= MAX_INTERVAL:
        return f"--interval: a number of seconds from {MIN_INTERVAL} to {MAX_INTERVAL}"
    return replace(opts, interval=int(value))


def parse_args(args: list[str]) -> Options | str:
    """The options, or the error to print."""
    opts: Options | str = Options()
    rest = list(args)
    while rest and isinstance(opts, Options):
        arg = rest.pop(0)
        if arg == "--once":
            opts = replace(opts, once=True)
        elif arg == "--state":
            opts = replace(opts, state=True)
        elif arg == "--no-live":
            opts = replace(opts, live=False)
        elif arg in ("--interval", "--root"):
            opts = _value(arg, rest.pop(0), opts) if rest else f"{arg} needs a value"
        else:
            opts = f"unknown option: {arg}"
    if isinstance(opts, Options) and opts.state and not opts.once:
        return "--state goes with --once"
    return opts


def next_interval(current: int, up: bool) -> int:
    """The next interval of :data:`INTERVALS` up or down from ``current``."""
    if up:
        return next((i for i in INTERVALS if i > current), INTERVALS[-1])
    return next((i for i in reversed(INTERVALS) if i < current), INTERVALS[0])


class Session:  # pylint: disable=too-many-instance-attributes
    """The files as last read, the live figures and the worker that lists."""

    def __init__(
        self,
        opts: Options,
        runner: live_pools.Runner = live_pools.run_command,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        alive: Callable[[Mapping[str, Any]], bool] = sr.process_alive,
        eta_model: bool = True,
        ticks: Callable[[], float] = time.monotonic,
    ):
        self.opts = opts
        self.interval = opts.interval
        self.runner = runner
        self.clock = clock
        self.alive = alive
        self.ticks = ticks
        self.progress_path, self.status_path = sr.paths(opts.root)
        self.progress = sr.FileRead("absent")
        self.status = sr.FileRead("absent")
        self._read_once = False
        self.last_good: Mapping[str, Any] | None = None
        self.live: live_pools.LiveFigures | None = None
        self._live_started = 0.0
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self.eta = TransferEta()
        self.eta_model = eta_model

    def poll(self) -> sr.Reading:
        """Read again whatever changed, then classify at the reader's clock."""
        if not self._read_once or sr.file_key(self.progress_path) != self.progress.key:
            self.progress = sr.read_progress(self.progress_path)
        if not self._read_once or sr.file_key(self.status_path) != self.status.key:
            self.status = sr.read_status(self.status_path)
        self._read_once = True
        now = self.clock()
        pc = sr.classify_progress(self.progress, now, self.alive)
        sc = sr.classify_status(self.status, now)
        if sc in ("ok", "stale") and self.status.doc is not None:
            self.last_good = self.status.doc
        self._feed_eta(pc)
        return sr.Reading(
            now=now,
            progress=self.progress,
            status=self.status,
            progress_class=pc,
            status_class=sc,
            indicator=sr.indicator(pc, self.progress.doc, sc, self.status.doc),
        )

    def _feed_eta(self, progress_class: str) -> None:
        doc = self.progress.doc
        transfer = doc.get("transfer") if isinstance(doc, dict) else None
        if progress_class == "running" and isinstance(transfer, dict):
            self.eta.feed(self.ticks(), transfer)
        elif progress_class != "running":
            self.eta.feed(self.ticks(), None)  # the run is over: forget its pace

    def inputs(self, reading: sr.Reading) -> view.Inputs:
        """What one frame shows."""
        with self._lock:
            live = self.live
        return view.Inputs(
            reading=reading,
            live=live,
            last_good_status=self.last_good,
            eta_model=self.eta_model,
            eta=self.eta.seconds(),
        )

    def live_now(self) -> None:
        """List the pools in this thread (``--once``)."""
        if self.opts.live:
            self.live = live_pools.read_live(self.runner, self.clock())

    def start_live(self, force: bool = False) -> None:
        """Start a listing in the background when one is due."""
        if not self.opts.live or (self._worker is not None and self._worker.is_alive()):
            return
        if (
            not force
            and self._live_started
            and time.monotonic() - self._live_started < self.interval
        ):
            return
        self._live_started = time.monotonic()
        self._worker = threading.Thread(target=self._list, name="zark-monitor-live", daemon=True)
        self._worker.start()

    def _list(self) -> None:
        figures = live_pools.read_live(self.runner, self.clock())
        with self._lock:
            self.live = figures


def view_options(session: Session, width: int, **extra: Any) -> view.ViewOptions:
    """The frame's options for this session."""
    base = view.ViewOptions(
        width=width,
        version=VERSION,
        host=os.uname().nodename,
        interval=session.interval,
        live=session.opts.live,
        clean_hint=f"{zark_command()} clean",
    )
    return replace(base, **extra)


def render_plain(lines: list[view.Line], color: bool) -> str:
    """The frame as text, with ANSI colour when ``color``; nothing is cut (a
    terminal wraps, a file keeps it whole)."""
    out: list[str] = []
    for line in lines:
        parts: list[str] = []
        for text, style in line:
            codes = ";".join(_SGR[s] for s in style.split() if s in _SGR)
            parts.append(f"\033[{codes}m{text}\033[0m" if color and codes else text)
        out.append("".join(parts).rstrip())
    return "\n".join(out)


def _utf8() -> bool:
    encoding = str(getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    return encoding == "utf8"


def _no_color() -> bool:
    return bool(os.environ.get("NO_COLOR"))


def _width() -> int:
    return max(40, shutil.get_terminal_size((80, 24)).columns)


def _signature(r: sr.Reading, session: Session) -> tuple[object, ...]:
    pdoc = r.progress.doc or {}
    step = pdoc.get("step") if isinstance(pdoc.get("step"), dict) else {}
    transfer = pdoc.get("transfer") if isinstance(pdoc.get("transfer"), dict) else {}
    live = session.live.measured_at if session.live else None
    return (
        r.indicator,
        r.progress_class,
        r.status_class,
        (step or {}).get("index"),
        (transfer or {}).get("dataset"),
        pdoc.get("waiting_for_input"),
        r.status.key,
        live,
    )


def run_once(session: Session) -> int:
    """``--once``: one frame, or the state word."""
    reading = session.poll()
    if session.opts.state:
        print(reading.indicator)
        return 0
    session.live_now()
    width = _width() if sys.stdout.isatty() else 80
    lines = view.frame(session.inputs(reading), view_options(session, width))
    print(render_plain(lines, sys.stdout.isatty() and not _no_color()))
    return 0


def run_plain(session: Session, sleep: Callable[[float], None] = time.sleep) -> int:
    """Not a terminal: a frame at start and on every change, each live
    interval included, until stopped."""
    last: tuple[object, ...] | None = None
    printed = 0.0
    while True:
        session.start_live()
        reading = session.poll()
        sig = _signature(reading, session)
        timer = not session.opts.live and time.monotonic() - printed >= session.interval
        if sig != last or timer:
            lines = view.frame(session.inputs(reading), view_options(session, 80))
            print(render_plain(lines, False) + "\n", flush=True)
            last, printed = sig, time.monotonic()
        sleep(1)


_PAIRS = {"ok": 1, "warn": 2, "crit": 3, "wait": 4, "run": 5}


@dataclass
class Screen:  # pylint: disable=too-many-instance-attributes
    """What the keys change."""

    show_datasets: bool = True
    sort: str = "own"
    bell: bool = True
    help_line: bool = False
    color: bool = True
    last_state: str = ""


def handle_key(key: int, screen: Screen, session: Session) -> bool:
    """Apply one key; False to quit."""
    ch = chr(key) if 0 <= key < 0x110000 else ""
    if ch in ("q", "Q"):
        return False
    if ch == "d":
        screen.show_datasets = not screen.show_datasets
    elif ch == "s":
        screen.sort = view.SORTS[(view.SORTS.index(screen.sort) + 1) % len(view.SORTS)]
    elif ch in ("+", "="):
        session.interval = next_interval(session.interval, up=True)
    elif ch in ("-", "_"):
        session.interval = next_interval(session.interval, up=False)
    elif ch == "r":
        session.start_live(force=True)
    elif ch == "b":
        screen.bell = not screen.bell
    elif ch in ("h", "?"):
        screen.help_line = not screen.help_line
    return True


def screen_lines(
    session: Session, screen: Screen, reading: sr.Reading, size: tuple[int, int]
) -> list[view.Line]:
    """The frame for a ``size`` (rows, columns) terminal: the datasets table
    gets the rows the rest leaves, and the last row says when more is cut."""
    rows, cols = size
    width = max(20, cols - 1)
    opt = view_options(
        session,
        width,
        show_datasets=screen.show_datasets,
        sort=screen.sort,
        bell=screen.bell,
        help_line=False,
        dataset_rows=0,
        unicode_bars=_utf8(),
    )
    inp = session.inputs(reading)
    room = rows - (1 if screen.help_line else 0)
    fixed = len(view.frame(inp, opt))
    pools = len(session.live.datasets) if session.live and session.live.datasets else 2
    opt = replace(opt, dataset_rows=max(0, (room - fixed) // pools))
    lines = view.frame(inp, opt)
    if len(lines) > room:
        lines = lines[: max(0, room - 1)] + [[("(more below: a taller terminal shows it)", "dim")]]
    if screen.help_line:
        lines.append(view.key_help(replace(opt, sort=screen.sort, bell=screen.bell)))
    return [view.truncate(line, width) for line in lines]


def _attr(curses: Any, style: str, color: bool) -> int:
    attr = 0
    for word in style.split():
        if word == "bold":
            attr |= curses.A_BOLD
        elif word == "dim":
            attr |= curses.A_DIM
        elif word == "head":
            attr |= curses.A_BOLD | (curses.color_pair(_PAIRS["run"]) if color else 0)
        elif color and word in _PAIRS:
            attr |= curses.color_pair(_PAIRS[word])
    return attr


def _paint(curses: Any, stdscr: Any, lines: list[view.Line], color: bool) -> None:
    stdscr.erase()
    for y, line in enumerate(lines):
        x = 0
        for text, style in line:
            try:
                stdscr.addstr(y, x, text, _attr(curses, style, color))
            except curses.error:
                # The bottom-right cell cannot be written without scrolling.
                pass
            x += len(text)
    stdscr.refresh()


def _screen_loop(curses: Any, stdscr: Any, session: Session, screen: Screen) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if screen.color and curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        for name, color in (
            ("ok", "GREEN"),
            ("warn", "YELLOW"),
            ("crit", "RED"),
            ("wait", "BLUE"),
            ("run", "CYAN"),
        ):
            curses.init_pair(_PAIRS[name], getattr(curses, f"COLOR_{color}"), background)
    else:
        screen.color = False
    stdscr.timeout(200)
    drawn = 0.0
    while True:
        if time.monotonic() - drawn >= 1:
            session.start_live()
            reading = session.poll()
            if reading.indicator == "waiting" and screen.last_state != "waiting" and screen.bell:
                curses.beep()
            screen.last_state = reading.indicator
            _paint(
                curses,
                stdscr,
                screen_lines(session, screen, reading, stdscr.getmaxyx()),
                screen.color,
            )
            drawn = time.monotonic()
        key = stdscr.getch()
        if key == -1:
            continue
        if key != curses.KEY_RESIZE and not handle_key(key, screen, session):
            return
        drawn = 0.0


def run_screen(session: Session, color: bool) -> str | None:
    """Full screen until ``q``; the reason when curses cannot run here."""
    try:
        # pylint: disable-next=import-outside-toplevel
        import curses
    except ImportError as e:
        return str(e)
    try:
        curses.setupterm()
    except curses.error as e:
        return str(e) or "no terminal description"
    curses.wrapper(lambda stdscr: _screen_loop(curses, stdscr, session, Screen(color=color)))
    return None


def _run_session(session: Session) -> int:
    if session.opts.once:
        return run_once(session)
    if sys.stdin.isatty() and sys.stdout.isatty():
        failure = run_screen(session, color=not _no_color())
        if failure is None:
            return 0
        print(f"zark monitor: no full screen ({failure}); plain output follows", file=sys.stderr)
    return run_plain(session)


def run(args: list[str]) -> int:
    """Run the monitor; the exit status."""
    if args and args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    opts = parse_args(args)
    if isinstance(opts, str):
        print(f"zark monitor: {opts}", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return EX_USAGE
    try:
        # One reading has no pace to learn from: --once keeps the contract's ETA.
        return _run_session(Session(opts, eta_model=not opts.once))
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        # The reader of a pipe went away: nothing left to show it.
        return 0
