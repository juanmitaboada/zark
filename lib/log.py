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

Always verbose — no silent mode. Every operation is logged to both
terminal and log file for full traceability.
"""

import datetime
import getpass
import os
import re
import sys
from typing import NoReturn


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
        if log_file is None:
            if self._is_live_usb():
                # Live USB: log next to the script (on the pendrive — survives reboot)
                zark_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
                log_file = os.path.join(zark_root, "zark.log")
            else:
                log_file = "/var/log/zark.log"
        self.log_file = log_file

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
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"[{self._ts()}] {self._strip(msg)}\n")
        except OSError:
            pass

    def _emit(self, msg: str):
        print(msg, flush=True)
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
        """Debug/verbose — always shown (we're always verbose)."""
        self._emit(f"  {self.C}[DBG]{self.N}   {msg}")

    def step(self, num: int, total: int, msg: str):
        """Numbered step indicator."""
        self._emit(f"\n{self.C}[{num}/{total}]{self.N} {msg}")

    def cmd(self, command: str):
        """Log a shell command being executed."""
        self.dbg(f"$ {command}")

    def raw(self, msg: str):
        """Raw output (e.g. command stdout)."""
        self._emit(msg)

    def blank(self):
        """Blank line for spacing."""
        print()

    # ── Banners ──────────────────────────────────────────────────────────

    def banner(self, title: str, subtitle: str = ""):
        """Big colored banner for command start."""
        w = 58
        self.blank()
        print(f"{self.BOLD}{self.C}╔{'═' * w}╗{self.N}")
        print(
            f"{self.BOLD}{self.C}║{self.N}  "
            f"{self.W}{title}{self.N}{' ' * (w - len(title) - 2)}{self.BOLD}{self.C}║{self.N}",
        )
        if subtitle:
            print(
                f"{self.BOLD}{self.C}║{self.N}  "
                f"{subtitle}{' ' * (w - len(subtitle) - 2)}{self.BOLD}{self.C}║{self.N}",
            )
        print(f"{self.BOLD}{self.C}╚{'═' * w}╝{self.N}")
        self.blank()
        self._to_file(f"{'═' * 62}")
        self._to_file(f"  {title}  {subtitle}")
        self._to_file(f"{'═' * 62}")

    def banner_ok(self, title: str, lines: list[str] | None = None):
        """Green success banner."""
        w = 58
        self.blank()
        print(f"{self.BOLD}{self.G}╔{'═' * w}╗{self.N}")
        print(
            f"{self.BOLD}{self.G}║{self.N}  {self.G}✅ "
            f"{title}{self.N}{' ' * max(0, w - len(title) - 5)}{self.BOLD}{self.G}║{self.N}",
        )
        print(f"{self.BOLD}{self.G}╠{'═' * w}╣{self.N}")
        for line in lines or []:
            clean = self._strip(line)
            print(
                f"{self.BOLD}{self.G}║{self.N}  "
                f"{line}{' ' * max(0, w - len(clean) - 2)}{self.BOLD}{self.G}║{self.N}",
            )
        print(f"{self.BOLD}{self.G}╚{'═' * w}╝{self.N}")
        self.blank()

    def banner_error(self, title: str, lines: list[str] | None = None):
        """Red error banner."""
        w = 58
        self.blank()
        print(f"{self.BOLD}{self.R}╔{'═' * w}╗{self.N}")
        print(
            f"{self.BOLD}{self.R}║{self.N}  {self.R}❌ "
            f"{title}{self.N}{' ' * max(0, w - len(title) - 5)}{self.BOLD}{self.R}║{self.N}",
        )
        print(f"{self.BOLD}{self.R}╠{'═' * w}╣{self.N}")
        for line in lines or []:
            clean = self._strip(line)
            print(
                f"{self.BOLD}{self.R}║{self.N}  "
                f"{line}{' ' * max(0, w - len(clean) - 2)}{self.BOLD}{self.R}║{self.N}",
            )
        print(f"{self.BOLD}{self.R}╚{'═' * w}╝{self.N}")
        self.blank()

    # ── Interactive ──────────────────────────────────────────────────────

    def ask(self, question: str, default: bool = False) -> bool:
        """Yes/no question. Returns bool."""
        prompt = "[Y/n]" if default else "[y/N]"
        print()
        print(f"{self.M}  ┌─────────────────────────────────────────────────────┐{self.N}")
        print(f"{self.M}  │ {self.Y}?{self.N} {question}")
        print(f"{self.M}  └─────────────────────────────────────────────────────┘{self.N}")
        try:
            answer = input(f"    {prompt}: ").strip().lower()
        except EOFError:
            answer = ""
        if not answer:
            return default
        return answer.startswith("y")

    def ask_input(self, question: str, default: str = "") -> str:
        """Free-text input question."""
        print(file=sys.stderr)
        print(
            f"{self.M}  ┌─────────────────────────────────────────────────────┐{self.N}",
            file=sys.stderr,
        )
        print(f"{self.M}  │ {self.Y}?{self.N} {question}", file=sys.stderr)
        if default:
            print(
                f"{self.M}  │   (default: {self.W}{default}{self.N}{self.M}){self.N}",
                file=sys.stderr,
            )
        print(
            f"{self.M}  └─────────────────────────────────────────────────────┘{self.N}",
            file=sys.stderr,
        )
        try:
            answer = input("    > ").strip()
        except EOFError:
            answer = ""
        return answer or default

    def ask_password(self, question: str) -> str:
        """Password input (no echo)."""

        print()
        print(f"{self.M}  ┌─────────────────────────────────────────────────────┐{self.N}")
        print(f"{self.M}  │ {self.Y}🔑{self.N} {question}")
        print(f"{self.M}  └─────────────────────────────────────────────────────┘{self.N}")
        return getpass.getpass("    Passphrase: ")

    def ask_choice(self, question: str, options: list[str], default: int = 0) -> int:
        """Numbered choice. Returns 0-based index. Enter = default."""
        print()
        print(f"  {self.W}{question}{self.N}")
        print()
        for i, opt in enumerate(options):
            marker = " ←" if i == default else ""
            print(f"    {self.W}{i + 1}){self.N} {opt}{marker}")
        print()
        while True:
            try:
                raw = input(f"    Select [1-{len(options)}, default={default + 1}]: ").strip()
                if not raw:
                    return default
                sel = int(raw) - 1
                if 0 <= sel < len(options):
                    return sel
            except (ValueError, EOFError):
                pass
            print(f"    {self.R}Invalid selection — try again{self.N}")

    # ── Fatal (stops execution) ──────────────────────────────────────────

    def fatal(
        self,
        message: str,
        causes: list[str] | None = None,
        solutions: list[str] | None = None,
    ) -> NoReturn:
        """Print fatal error banner, pause for debugging, then raise SystemExit."""
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
            input("  Press Enter to cleanup and exit... ")
        except EOFError:
            pass
        raise SystemExit(1)
