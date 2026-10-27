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
Logging and terminal output for zark.

Always verbose - no silent mode. Every operation is logged to both
terminal and log file for full traceability.
"""

import datetime
import getpass
import os
import re
import select
import shutil
import stat
import sys
import unicodedata
from collections.abc import Callable, Collection, Sequence
from types import TracebackType
from typing import NoReturn

_YES_NO = ("y", "yes", "n", "no")
_SGR = re.compile(r"\x1b\[[0-9;]*m")
# What a continuation line is indented by: the line's own indent and its bullet.
_HANG = re.compile(r" *(?:[•→✓✗-] |\d+\. )?")
_RESET = "\033[0m"

# What lib.progress hears (M7-4, M7-9): a numbered step, the terminal
# reads, with their prompt while one waits and None once it returned or
# raised, and a fatal error's message. Set by progress.start; log itself
# imports nothing of zark's.
_ON_STEP: list[Callable[[int, int, str], None]] = []
_ON_WAIT: list[Callable[[str | None], None]] = []
_ON_FATAL: list[Callable[[str], None]] = []


def watch(
    on_step: Callable[[int, int, str], None] | None,
    on_wait: Callable[[str | None], None] | None,
    on_fatal: Callable[[str], None] | None = None,
) -> None:
    """Hear every :meth:`Log.step`, every terminal read and every
    :meth:`Log.fatal`; None stops it."""
    _ON_STEP[:] = [on_step] if on_step else []
    _ON_WAIT[:] = [on_wait] if on_wait else []
    _ON_FATAL[:] = [on_fatal] if on_fatal else []


class _Reading:
    """Around one read of the terminal: the watcher hears its prompt (no
    colour codes) before it, and None after it, whatever ends it."""

    def __init__(self, prompt: str):
        self._prompt = " ".join(_SGR.sub("", prompt).split())

    def __enter__(self) -> None:
        for hear in _ON_WAIT:
            hear(self._prompt)

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        for hear in _ON_WAIT:
            hear(None)


def _read_line(prompt: str) -> str:
    """``input(prompt)``, and a newline after the answer when stdin is a
    terminal and stdout is not (``| tee``): the terminal echoes the typed
    newline to itself only, so the transcript would join the prompt and the
    next line (M5bP.6, M9b-12 12a). The screen shows one blank line there."""
    answer = input(prompt)
    if sys.stdin.isatty() and not sys.stdout.isatty():
        sys.stdout.write("\n")
        sys.stdout.flush()
    return answer


def text_width(s: str) -> int:
    """Terminal columns of ``s``: colour codes none, wide characters (the
    title emoji) 2, combining and format characters 0, the rest 1."""
    n = 0
    for ch in _SGR.sub("", s):
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def wrap(line: str, width: int) -> list[str]:
    """``line`` broken at spaces into lines of at most ``width`` columns.

    Not textwrap: it measures with len(), which counts colour codes and wide
    characters wrong. A continuation keeps the line's indent and bullet
    width and the colour active at the break; a word wider than ``width``
    (a by-id path people copy) stays whole on its own line.
    """
    if text_width(line) <= width:
        return [line]
    m = _HANG.match(_SGR.sub("", line))
    hang = " " * (m.end() if m else 0)
    lead = len(line) - len(line.lstrip(" "))
    words = line[lead:].split(" ")
    out: list[str] = []
    current = line[:lead] + words[0]
    for word in words[1:]:
        if text_width(f"{current} {word}") <= width:
            current = f"{current} {word}"
            continue
        codes = _SGR.findall(current)
        active = codes[-1] if codes and codes[-1] != _RESET else ""
        out.append(current + (_RESET if active else ""))
        current = f"{hang}{active}{word}"
    out.append(current)
    return out


def frame(  # pylint: disable=too-many-arguments,too-many-locals
    body: Sequence[str],
    *,
    heavy: bool,
    min_inner: int,
    color: str,
    head: Sequence[str] = (),
    columns: int | None = None,
) -> list[str]:
    """The lines of one box (``heavy``: a banner; else a question's box).

    The inner width fits the widest line, at least ``min_inner`` (the boxes'
    width before M4d-21) and at most what the terminal has room for
    (``shutil.get_terminal_size``: COLUMNS, then stdout, else 80); longer
    lines are wrapped. ``head`` rows come above a rule (banner_ok/error).
    """
    if heavy:
        tl, h, tr, v, bl, br, ml, mr = "╔", "═", "╗", "║", "╚", "╝", "╠", "╣"
        indent, left, right = "", 2, 1  # a full-width line keeps a space before the edge
    else:
        tl, h, tr, v, bl, br, ml, mr = "┌", "─", "┐", "│", "└", "┘", "├", "┤"
        indent, left, right = "  ", 1, 1
    # A line with a newline in it is several rows: measured and wrapped one by
    # one, so the box keeps its right edge (M4dR-2).
    head = [piece for ln in head for piece in ln.split("\n")]
    body = [piece for ln in body for piece in ln.split("\n")]
    cols = columns or shutil.get_terminal_size().columns
    room = max(min_inner, cols - len(indent) - 2)
    widest = max((text_width(ln) for ln in (*head, *body)), default=0)
    inner = min(max(widest + left + right, min_inner), room)
    area = inner - left - right
    edge = f"{color}{v}{_RESET}"

    def rows(lines: Sequence[str]) -> list[str]:
        out: list[str] = []
        for line in lines:
            for piece in wrap(line, area):
                pad = area - text_width(piece)
                tail = f"{' ' * (pad + right)}{edge}" if pad >= 0 else ""  # overflow: no edge
                out.append(f"{indent}{edge}{' ' * left}{piece}{_RESET}{tail}")
        return out

    lines = [f"{indent}{color}{tl}{h * inner}{tr}{_RESET}"]
    if head:
        lines += rows(head)
        lines.append(f"{indent}{color}{ml}{h * inner}{mr}{_RESET}")
    lines += rows(body)
    lines.append(f"{indent}{color}{bl}{h * inner}{br}{_RESET}")
    return lines


class Log:
    """Structured terminal + file logger. Always verbose."""

    # Colors
    R = "\033[0;31m"
    G = "\033[0;32m"
    Y = "\033[0;33m"
    C = "\033[0;36m"
    W = "\033[1;37m"
    M = "\033[0;35m"
    N = "\033[0m"
    BOLD = "\033[1m"

    _STRIP_RE = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, log_file: str | None = None):
        self.log_file = log_file if log_file is not None else self.default_file()
        self._mode_checked = False

    @classmethod
    def default_file(cls) -> str:
        """Where a Log without an explicit file writes."""
        if cls._is_live_usb():
            # Live USB: log next to the script (on the pendrive - survives reboot)
            zark_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
            return os.path.join(zark_root, "zark.log")
        return "/var/log/zark.log"

    @staticmethod
    def _is_live_usb() -> bool:
        try:
            with open("/proc/cmdline", encoding="utf-8") as f:
                cmdline = f.read()
            if any(k in cmdline for k in ("boot=casper", "boot=live", "live-media")):
                return True
        except OSError:
            pass
        return os.path.isdir("/rofs") or os.path.isdir("/cow")

    def _ts(self) -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _strip(self, msg: str) -> str:
        return self._STRIP_RE.sub("", msg)

    def _to_file(self, msg: str):
        try:
            # Answers can hold a mistyped passphrase: the log is readable by root only.
            fd = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                if not self._mode_checked:
                    self._mode_checked = True
                    self._make_private(fd)
                f.write(f"[{self._ts()}] {self._strip(msg)}\n")
        except OSError:
            pass

    @staticmethod
    def _make_private(fd: int) -> None:
        """Drop group/other access from a log file created before 0600 (never /dev/null)."""
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_mode & 0o077:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass

    def _emit(self, msg: str):
        try:
            print(msg, flush=True)
        except OSError:
            pass  # terminal closed (EIO): the log file still records it, the work goes on
        self._to_file(msg)

    # ── Public API ───────────────────────────────────────────────────────

    def ok(self, msg: str):
        """Green success message."""
        self._emit(f"{self.G}[OK]{self.N}      {msg}")

    def info(self, msg: str):
        """Blue info message."""
        self._emit(f"{self.C}[INFO]{self.N}    {msg}")

    def warn(self, msg: str):
        """Yellow warning message."""
        self._emit(f"{self.Y}[WARN]{self.N}    {msg}")

    def error(self, msg: str):
        """Red error message."""
        self._emit(f"{self.R}[ERROR]{self.N}   {msg}")

    def dbg(self, msg: str):
        """Debug/verbose - always shown (we're always verbose)."""
        self._emit(f"  {self.C}[DBG]{self.N}   {msg}")

    def step(self, num: int, total: int, msg: str):
        """Numbered step indicator."""
        self._emit(f"\n{self.C}[{num}/{total}]{self.N} {msg}")
        for hear in _ON_STEP:
            hear(num, total, self._strip(msg))

    def cmd(self, command: str):
        """Log a shell command being executed."""
        self.dbg(f"$ {command}")

    def file(self, msg: str):
        """Write to zark.log only (detail too long for the terminal)."""
        self._to_file(msg)

    def raw(self, msg: str):
        """Raw output (e.g. command stdout)."""
        self._emit(msg)

    def blank(self):
        """Blank line for spacing."""
        print()

    # ── Banners ──────────────────────────────────────────────────────────

    def banner(self, title: str, subtitle: str = "", *, warn: bool = False):
        """Big colored banner for command start; ``warn`` shows the subtitle in yellow."""
        body = [f"{self.W}{title}{self.N}"]
        if subtitle:
            body.append(f"{self.Y}{subtitle}{self.N}" if warn else subtitle)
        self._show(frame(body, heavy=True, min_inner=58, color=self.BOLD + self.C))
        self._to_file(f"{'═' * 62}")
        self._to_file(f"  {title}  {subtitle}")
        self._to_file(f"{'═' * 62}")

    @staticmethod
    def _show(lines: list[str], *, after: bool = True, err: bool = False) -> None:
        """Print a frame after a blank line, and one after it unless ``after`` is False."""
        out = sys.stderr if err else sys.stdout
        print(file=out)
        for line in lines:
            print(line, file=out)
        if after:
            print(file=out)

    def banner_ok(self, title: str, lines: list[str] | None = None):
        """Green success banner."""
        head = [f"{self.G}✅ {title}{self.N}"]
        self._show(
            frame(lines or [], heavy=True, min_inner=58, color=self.BOLD + self.G, head=head)
        )
        self._verdict_to_file("OK", title, lines)

    def _verdict_to_file(self, kind: str, title: str, lines: list[str] | None) -> None:
        """Record a verdict banner in the log file (I19): the terminal is not
        the audit trail."""
        self._to_file(f"[{kind}] ══ {title} ══")
        for line in lines or []:
            if self._strip(line).strip():
                self._to_file(f"[{kind}]    {line}")

    def banner_safe_unplug(self, drive: str):
        """Distinct prominent banner indicating a drive is safe to unplug.

        Emitted at the very end of a successful operation, *after* the
        operation's own ``banner_ok`` and *after* the device-flush window
        (sync + sleep + eject) has elapsed. Serves as the unambiguous
        end-of-output signal that the operator may now physically
        disconnect the USB drive without risking pool corruption.

        Without this signal, a hurried operator can act on the
        operation's success banner and unplug while the device firmware
        still holds dirty pages in its DRAM cache, with the well-known
        consequence that the four pool labels never reach NAND and the
        pool comes back FAULTED on next import.
        """
        text = f"💾  Safe to unplug drive '{drive}'"
        self._show(
            frame([f"{self.W}{text}{self.N}"], heavy=True, min_inner=58, color=self.BOLD + self.G)
        )
        self._to_file(f"Safe to unplug drive '{drive}'")

    def banner_drive_attached(self, drive: str):
        """Banner for the "flushed but still attached" path.

        Counterpart to ``banner_safe_unplug``: when the operator
        declines the post-operation eject (typical after ``prepare`` if
        a ``backup`` will follow against the same drive, or after
        ``backup`` if the operator plans to back up other drives in the
        same session), the bridge stays powered on and the drive
        remains visible in ``/dev``. This banner makes that state
        explicit so the operator knows the kernel-side flush is done
        but a physical unplug right now is *not* recommended - they
        should run ``zark umount`` (or accept the eject in a later
        command) first.

        The cyan colour (matching ``banner()``, distinct from the green
        of ``banner_safe_unplug``) and the 🔌 icon reinforce
        "still plugged in" visually.
        """
        text = f"🔌  Drive '{drive}' flushed, still attached"
        self._show(
            frame([f"{self.W}{text}{self.N}"], heavy=True, min_inner=58, color=self.BOLD + self.C)
        )
        self._to_file(f"Drive '{drive}' flushed, still attached")

    def banner_error(self, title: str, lines: list[str] | None = None):
        """Red error banner."""
        head = [f"{self.R}❌ {title}{self.N}"]
        self._show(
            frame(lines or [], heavy=True, min_inner=58, color=self.BOLD + self.R, head=head)
        )
        self._verdict_to_file("FAIL", title, lines)

    def _box(self, *rows: str) -> list[str]:
        """A question's box: magenta, 53 columns at least, as before M4d-21."""
        return frame(rows, heavy=False, min_inner=53, color=self.M)

    def _answer_to_file(self, question: str, answer: str) -> None:
        """Record an interactive prompt and the operator's answer (I19)."""
        self._to_file(f"[PROMPT]  {self._strip(question)} → {answer!r}")

    @staticmethod
    def _loggable(answer: str, accepted: Collection[str]) -> str:
        """``answer`` when it is one of the accepted words, else only its length.

        Anything else may be a passphrase typed at the wrong prompt.
        """
        if answer.casefold() in {a.casefold() for a in accepted}:
            return answer
        return f"<other answer, {len(answer)} chars>"

    # ── Interactive ──────────────────────────────────────────────────────

    def ask(self, question: str, default: bool = False, *, on_eof: bool | None = None) -> bool:
        """Yes/no question. Returns bool.

        At EOF (a closed terminal) the answer is ``on_eof`` when given, else
        ``default``: a question whose default is an action that changes the
        system passes ``on_eof=False``, since nobody pressed Enter.
        """
        prompt = "[Y/n]" if default else "[y/N]"
        self._show(self._box(f"{self.Y}?{self.N} {question}"), after=False)
        eof = False
        try:
            with _Reading(question):
                answer = _read_line(f"    {prompt}: ").strip().lower()
        except EOFError:
            answer, eof = "", True
        if eof and on_eof is not None:
            result, shown = on_eof, "<eof>"
        else:
            result = default if not answer else answer.startswith("y")
            shown = self._loggable(answer, _YES_NO) if answer else "<default>"
        self._answer_to_file(question, f"{shown} ({'yes' if result else 'no'})")
        return result

    def ask_enter(self, prompt: str) -> bool:
        """Wait for Enter (after a physical step). True on a line, False at
        EOF, where nobody did the step; the log says which."""
        try:
            with _Reading(prompt):
                _ = _read_line(prompt)
        except EOFError:
            self._answer_to_file(prompt.strip(), "<eof>")
            return False
        self._answer_to_file(prompt.strip(), "<enter>")
        return True

    def ask_timeout(self, question: str, default: bool, timeout: int) -> bool:
        """Yes/no question that auto-applies ``default`` after ``timeout`` seconds.

        A live countdown is shown on a single line. The first line the operator
        submits during the countdown IS the answer (empty line = the default,
        like a normal prompt); the countdown then stops. With no interactive
        stdin (cron, systemd timer, scripted run) the default is applied
        immediately - there is nobody to watch a countdown.

        Returns the boolean decision (True = yes/eject).
        """
        prompt = "[Y/n]" if default else "[y/N]"
        self._show(self._box(f"{self.Y}?{self.N} {question}"), after=False)

        # No TTY: apply default immediately, like EOF in ask().
        if not sys.stdin.isatty():
            self._answer_to_file(question, f"<no tty> ({'yes' if default else 'no'})")
            return default

        with _Reading(question):
            return self._countdown(question, default, timeout, prompt)

    def _countdown(self, question: str, default: bool, timeout: int, prompt: str) -> bool:
        """:meth:`ask_timeout`'s wait on a terminal."""
        default_word = "yes" if default else "no"
        for remaining in range(timeout, 0, -1):
            # \r keeps the countdown on one line; pad to overwrite prior text.
            sys.stdout.write(
                f"\r    {prompt} (auto-{default_word} in {remaining:2d}s, Enter/y/n to decide): ",
            )
            sys.stdout.flush()
            ready, _, _ = select.select([sys.stdin], [], [], 1)
            if ready:
                # The operator typed something - that line IS the answer.
                # Do not re-prompt or redraw the banner; just interpret it.
                answer = sys.stdin.readline().strip().lower()
                sys.stdout.write("\n")
                sys.stdout.flush()
                result = default if not answer else answer.startswith("y")
                shown = self._loggable(answer, _YES_NO) if answer else "<default>"
                self._answer_to_file(question, f"{shown} ({'yes' if result else 'no'})")
                return result
        # Timed out: clear the countdown line and apply the default.
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()
        self.info(f"No response - applying default ({default_word}).")
        self._answer_to_file(question, f"<timeout> ({default_word})")
        return default

    def ask_input(
        self,
        question: str,
        default: str = "",
        *,
        accept: Collection[str] = (),
    ) -> str:
        """Free-text input question.

        The log gets the answer only when it is the default or in ``accept``.
        """
        rows = [f"{self.Y}?{self.N} {question}"]
        if default:
            rows.append(f"  (default: {self.W}{default}{self.N})")
        self._show(self._box(*rows), after=False, err=True)
        try:
            with _Reading(question):
                answer = _read_line("    > ").strip()
        except EOFError:
            answer = ""
        shown = self._loggable(answer, (*accept, default)) if answer else f"<default> {default}"
        self._answer_to_file(question, shown)
        return answer or default

    def ask_text(self, prompt: str, *, accept: Collection[str], label: str = "") -> str:
        """Read one typed line (a confirmation word) and record it in the log.

        ``accept`` lists the words this prompt acts on; any other answer is
        logged by length only. ``label`` names the question in the log when
        ``prompt`` alone does not (a bare ``>``). EOF reads as an empty answer,
        which no confirmation accepts, so a closed terminal aborts instead of
        raising.
        """
        try:
            with _Reading(label or prompt):
                answer = _read_line(prompt).strip()
        except EOFError:
            answer = ""
        shown = self._loggable(answer, accept) if answer else "<empty>"
        self._answer_to_file(label or prompt.strip(), shown)
        return answer

    def ask_password(self, question: str) -> str:
        """Password input (no echo)."""

        self._show(self._box(f"{self.Y}🔑{self.N} {question}"), after=False)
        self._to_file(f"[PROMPT]  {self._strip(question)} → (passphrase, not logged)")
        # The prompt goes through stdout, after the box: getpass writes it to
        # /dev/tty otherwise, a second writer a piped (| tee) stdout's reader
        # can overtake (M2V-6). Input is still read from /dev/tty, echo off
        # (CPython Lib/getpass.py:52-58).
        sys.stdout.flush()
        try:
            with _Reading(question):
                return getpass.getpass("    Passphrase: ", stream=sys.stdout)
        finally:
            sys.stdout.flush()  # getpass writes its closing newline unflushed (:101)

    def ask_choice(self, question: str, options: list[str], default: int = 0) -> int:
        """Numbered choice. Returns 0-based index. Enter = default."""
        print()
        print(f"  {self.W}{question}{self.N}")
        print()
        for i, opt in enumerate(options):
            marker = " ←" if i == default else ""
            print(f"    {self.W}{i + 1}){self.N} {opt}{marker}")
        print()
        with _Reading(question):
            return self._choose(question, options, default)

    def _choose(self, question: str, options: list[str], default: int) -> int:
        """:meth:`ask_choice`'s reads, until an answer it accepts."""
        while True:
            try:
                raw = _read_line(f"    Select [1-{len(options)}, default={default + 1}]: ").strip()
            except EOFError:
                # No operator (stdin closed): never loop forever, never guess.
                self._answer_to_file(question, "<EOF>")
                self.error("No input available (stdin closed) - aborting")
                raise SystemExit(1) from None
            if not raw:
                self._answer_to_file(question, f"<default> {options[default]}")
                return default
            if raw.isdigit() and 0 <= int(raw) - 1 < len(options):
                sel = int(raw) - 1
                self._answer_to_file(question, f"{raw} {options[sel]}")
                return sel
            print(f"    {self.R}Invalid selection - try again{self.N}")

    # ── Fatal (stops execution) ──────────────────────────────────────────

    def fatal(
        self,
        message: str,
        causes: list[str] | None = None,
        solutions: list[str] | None = None,
    ) -> NoReturn:
        """Print fatal error banner, pause for debugging, then raise SystemExit."""
        for hear in _ON_FATAL:
            hear(self._strip(message).split("\n", 1)[0])
        lines = [message, ""]
        if causes:
            lines.append(f"{self.Y}Possible causes:{self.N}")
            for c in causes:
                lines.append(f"  • {c}")
            lines.append("")
        if solutions:
            lines.append(f"{self.G}What to do:{self.N}")
            for s in solutions:
                lines.append(f"  → {s}")
        self.banner_error("FATAL ERROR", lines)
        print(f"  {self.Y}System state preserved for inspection.{self.N}")
        print(f"  {self.Y}Open another terminal to debug if needed.{self.N}")
        print()
        try:
            with _Reading("Press Enter to cleanup and exit"):
                _ = _read_line("  Press Enter to cleanup and exit... ")
        except EOFError:
            pass
        raise SystemExit(1)
