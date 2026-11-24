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
Driving one zark command through its terminal, as a person would.

zark has no non-interactive flags: every answer goes through the terminal,
and it publishes the read it is waiting on in /run/zark/progress.json
(``waiting_for`` and ``question``, docs/STATUS-FILES.md). The session reads
the command's pty output; when the output stops on a prompt, it reads
progress.json, looks the question up in the command's table and writes the
answer once. A question the table does not hold ends the command with
end-of-file, which zark reads as "nobody is there".
"""

from __future__ import annotations

import json
import os
import re
import select
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any, BinaryIO

from bench.console import Reader, clean
from bench.report import Check

# Answers that are not literal text.
ENTER = ""
PASSPHRASE = "<passphrase>"
POINT = "<the expected restore point>"
DISK = "<the blank disk>"

# zark writes progress.json at most once a second (lib/progress.py:62,
# MIN_GAP_S): a prompt on the screen is in the file this long after it, at
# the latest, with room for SSH's round trip.
QUIET_S = 1.5
# After an unexpected question's end-of-file: how long zark gets to end.
GRACE_S = 120.0
# Kept of the output since the last answer: enough for any prompt line.
_TAIL_BYTES = 4096

# The last line each kind of read leaves on the terminal (lib/log.py at
# 66cce62: ask :550, ask_input :725, ask_choice :792, ask_text :745 with
# its callers' prompts, ask_password :768, the wait :682-684, the
# countdown :601-602).
PROMPT_TAILS: dict[str, re.Pattern[str]] = {
    "yes_no": re.compile(r"\[(?:Y/n|y/N)\]: $"),
    "input": re.compile(r"> $"),
    "choice": re.compile(r"Select \[1-\d+, default=\d+\]: $"),
    "word": re.compile(r"Type \S+ to .+: $"),
    "passphrase": re.compile(r"Passphrase: $"),
    "wait": re.compile(r"Enter to keep waiting\): $"),
    "timed": re.compile(r"Enter/y/n to decide\): $"),
}


class Unexpected(Exception):
    """A read the command's table does not answer."""


@dataclass(frozen=True)
class Row:
    """One read a command may make, and the bench's answer to it."""

    kind: str
    text: str  # how ``waiting_for`` starts, as zark writes it
    answer: str
    required: bool = False
    # A row that may be read again (a wait); every other row is answered once.
    again: bool = False


# From zark's source at 66cce62. A row is
# "required" when the step fails without it.
TABLES: dict[str, tuple[Row, ...]] = {
    "setup": (
        # commands/setup.py:1037, :1018, :892, :715, :790
        Row("yes_no", "Write the policy above to /etc/sanoid/sanoid.conf?", "y"),
        Row("yes_no", "Apply these changes?", "y"),
        Row("yes_no", "No sanoid snapshots found. Take the first ones now?", "y"),
        Row("yes_no", "Enable feature@bookmark_v2 on rpool now?", "y"),
        # The bench leaves the boot chain as the installer made it.
        Row("yes_no", "Switch to .latest and reinstall to ESP now?", "n"),
    ),
    "prepare": (
        # commands/prepare.py:150, :181, :533, :537, :189; lib/zfs.py:898;
        # lib/cleanup.py:199-212 (prepare's eject default is no)
        Row("input", "Pool name for this backup drive", ENTER, required=True),
        Row("yes_no", "Enable auto-eject (timed eject prompt) for this drive?", "n", True),
        Row("input", "Days in which this drive keeps every backup", ENTER, required=True),
        Row("input", "Days after which this drive keeps nothing", ENTER, required=True),
        Row("yes_no", "Proceed with drive preparation?", "y", required=True),
        Row("wait", "Skip the read-back of", ENTER, again=True),
        # The backup follows on the same drive.
        Row("yes_no", "Eject drive 'backup' now?", "n", required=True),
    ),
    "backup": (
        # lib/keystore.py:290; commands/backup.py:1562; lib/zfs.py:898;
        # lib/cleanup.py:199-212
        Row("passphrase", "Passphrase for backup", PASSPHRASE, required=True),
        Row("yes_no", "Key already loaded. Proceed with backup?", "y"),
        Row("wait", "Skip the read-back of", ENTER, again=True),
        Row("yes_no", "Eject drive 'backup' now?", ENTER, required=True),
    ),
    "recover": (
        # lib/keystore.py:290; lib/restore_plan.py:249-254;
        # commands/recover.py:614, :1188-1192; lib/cleanup.py:199-212
        Row("passphrase", "Passphrase for backup", PASSPHRASE, required=True),
        Row("choice", "Restore point", POINT, required=True),
        Row("choice", "Internal disk to ERASE and restore to:", DISK, required=True),
        Row("word", "Type YES to erase", "YES", required=True),
        Row("yes_no", "Eject drive 'backup' now?", ENTER, required=True),
    ),
    # commands/finish.py reads nothing from the terminal.
    "finish": (),
}

# What a command prints when it did what it was run for, and what it prints
# instead when it did not (checked first: "RECOVERY COMPLETED WITH ERRORS"
# holds "RECOVERY COMPLETE").
BANNERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "setup": ("SETUP COMPLETE", ()),
    "prepare": ("DRIVE PREPARED", ("DRIVE NOT REGISTERED", "DRIVE NOT VERIFIED")),
    "backup": ("BACKUP COMPLETED", ("BACKUP NOT VERIFIED", "BACKUP INCOMPLETE")),
    "recover": ("RECOVERY COMPLETE", ("RECOVERY COMPLETED WITH ERRORS",)),
    "finish": ("FINISH COMPLETE", ("FINISH INCOMPLETE",)),
}


@dataclass
class Expect:
    """What a command's answers depend on."""

    point: str = ""  # zark_YYYY-MM-DD_HH:MM:SSZ, the restore point recover must offer
    disk_serial: str = ""  # the blank disk recover must erase


def norm(text: str) -> str:
    """Whitespace collapsed, as zark writes ``waiting_for``."""
    return " ".join(text.split())


def point_time(point: str) -> datetime | None:
    """The UTC time in a backup point's name, or None."""
    m = re.fullmatch(r"zark_(\d{4}-\d\d-\d\d_\d\d:\d\d:\d\d)Z", point)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d_%H:%M:%S").replace(tzinfo=UTC)


def point_option_ok(option: str, point: str, slack_s: int = 300) -> bool:
    """
    The restore point option names a complete backup point taken with
    ``point``: zark labels it by its snapshot's creation time
    (lib/restore_points.py:125-127), seconds from the name's stamp.
    """
    if "backup point" not in option or "INCOMPLETE" in option or "PARTLY REMOVED" in option:
        return False
    when = point_time(point)
    m = re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC", option)
    if when is None or m is None:
        return False
    shown = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return abs((shown - when).total_seconds()) <= slack_s


def disk_option(options: list[str], serial: str) -> int:
    """The index of the one option naming the disk with ``serial``."""
    pattern = re.compile(rf"\bserial {re.escape(serial)}(?:\s|$)")
    found = [i for i, o in enumerate(options) if serial and pattern.search(o)]
    if len(found) != 1:
        raise Unexpected(f"{len(found)} disks offered with serial {serial!r}: {options}")
    return found[0]


def find_row(table: tuple[Row, ...], waiting_for: str, kind: str) -> Row:
    """The table's row for this read."""
    shown = norm(waiting_for)
    for row in table:
        if row.kind == kind and shown.startswith(norm(row.text)):
            return row
    raise Unexpected(f"{kind}: {shown!r}")


def decide(
    table: tuple[Row, ...],
    waiting_for: str,
    question: dict[str, Any],
    expect: Expect,
    answered: list[Row],
) -> tuple[Row, str]:
    """The row and the text to type (``PASSPHRASE`` for the passphrase)."""
    kind = str(question.get("kind", ""))
    row = find_row(table, waiting_for, kind)
    if row in answered and not row.again:
        raise Unexpected(f"asked again: {norm(waiting_for)!r}")
    options = [str(o) for o in question.get("options") or []]
    if row.answer == POINT:
        default = question.get("default")
        if not isinstance(default, int) or not 0 <= default < len(options):
            raise Unexpected(f"no default restore point: {options}")
        if not point_option_ok(options[default], expect.point):
            raise Unexpected(f"the default restore point is not {expect.point}: {options}")
        return row, ENTER
    if row.answer == DISK:
        return row, str(disk_option(options, expect.disk_serial) + 1)
    if kind == "word":
        if question.get("word") != row.answer:
            raise Unexpected(f"the word asked is {question.get('word')!r}")
        # The erase is confirmed only once the blank disk was the one chosen.
        if row.answer == "YES" and not any(r.answer == DISK for r in answered):
            raise Unexpected("YES asked before the disk was chosen by its serial")
    return row, row.answer


def missing(table: tuple[Row, ...], answered: list[Row]) -> list[str]:
    """The required rows never read."""
    return [r.text for r in table if r.required and r not in answered]


def parse_progress(text: str) -> tuple[str, dict[str, Any]] | None:
    """``(waiting_for, question)`` while zark waits on a read, else None."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict) or not doc.get("waiting_for_input"):
        return None
    question = doc.get("question")
    waiting_for = doc.get("waiting_for")
    if not isinstance(question, dict) or not isinstance(waiting_for, str):
        return None
    return waiting_for, question


def pty_reader(fd: int) -> Reader:
    """A :data:`Reader` over a non-blocking file descriptor (b"" at its end)."""

    def read(timeout_s: float) -> bytes | None:
        ready, _, _ = select.select([fd], [], [], max(timeout_s, 0.0))
        if not ready:
            return None
        try:
            return os.read(fd, 65536)
        except BlockingIOError:
            return None

    return read


def pipe_writer(stream: IO[bytes]) -> Callable[[bytes], None]:
    """Writes to a pipe at once (no buffering left behind)."""

    def write(data: bytes) -> None:
        stream.write(data)
        stream.flush()

    return write


def tail_line(raw: bytes) -> str:
    """The last line of the output, escapes and carriage returns removed."""
    text = clean(raw).decode("utf-8", "replace")
    return text.rsplit("\n", 1)[-1]


def on_prompt(raw: bytes) -> bool:
    """The output ends on a line some kind of read leaves."""
    line = tail_line(raw)
    return any(p.search(line) for p in PROMPT_TAILS.values())


@dataclass
class Outcome:
    """What one command's session saw."""

    asked: list[dict[str, Any]] = field(default_factory=list)
    unexpected: str = ""
    timed_out: bool = False
    rows: list[Row] = field(default_factory=list)


@dataclass
class Session:  # pylint: disable=too-many-instance-attributes  # one command's I/O
    """One zark command under its pty: reads its output, answers its reads."""

    table: tuple[Row, ...]
    expect: Expect
    read: Reader
    write: Callable[[bytes], None]
    poll: Callable[[], str]
    passphrase: str
    transcript: BinaryIO
    clock: Callable[[], float] = time.monotonic
    quiet_s: float = QUIET_S

    def run(self, limit_s: float) -> Outcome:
        """Until the command's output ends, or ``limit_s``; ends it on an unexpected read."""
        out = Outcome()
        since = b""  # output since the last answer (its tail only)
        last_data = self.clock()
        deadline = last_data + limit_s
        polled_at = 0.0
        while True:
            now = self.clock()
            if now >= deadline:
                out.timed_out = True
                return out
            data = self.read(min(0.25, deadline - now))
            if data == b"":
                return out
            now = self.clock()
            if data:
                self.transcript.write(data)
                since = (since + data)[-_TAIL_BYTES:]
                last_data = now
                continue
            if out.unexpected or not since or now - last_data < self.quiet_s:
                continue
            if not on_prompt(since) or now - polled_at < 1.0:
                continue
            polled_at = now
            if self._answer(since, out):
                since = b""

    def _answer(self, since: bytes, out: Outcome) -> bool:
        """Answer the read on the screen; True once something was typed."""
        seen = parse_progress(self.poll())
        if seen is None:
            return False
        waiting_for, question = seen
        kind = str(question.get("kind", ""))
        tail = PROMPT_TAILS.get(kind)
        if tail is None or not tail.search(tail_line(since)):
            # progress.json still shows the read before this one.
            return False
        try:
            row, text = decide(self.table, waiting_for, question, self.expect, out.rows)
        except Unexpected as err:
            out.unexpected = str(err)
            out.asked.append({"waiting_for": waiting_for, "question": question, "answer": "EOF"})
            # zark reads end-of-file as nobody there (lib/log.py:551, :726, :746, :793).
            self.write(b"\x04")
            deadline = self.clock() + GRACE_S
            while self.clock() < deadline and self.read(1.0) != b"":
                pass
            return True
        out.rows.append(row)
        shown = PASSPHRASE if text == PASSPHRASE else text
        out.asked.append({"waiting_for": waiting_for, "question": question, "answer": shown})
        value = self.passphrase if text == PASSPHRASE else text
        self.write((value + "\n").encode())
        return True


def verdict(command: str, status: int, transcript: bytes, outcome: Outcome) -> Check:
    """Exit 0, the command's banner, every required read answered, nothing unexpected."""
    ok_banner, bad = BANNERS[command]
    text = clean(transcript).decode("utf-8", "replace")
    problems: list[str] = []
    if outcome.timed_out:
        problems.append("did not end in time")
    if outcome.unexpected:
        problems.append(f"unexpected read: {outcome.unexpected}")
    if status != 0:
        problems.append(f"exit {status}")
    found_bad = [b for b in bad if b in text]
    if found_bad:
        problems.append(", ".join(found_bad))
    elif ok_banner not in text:
        problems.append(f"no {ok_banner}")
    if gaps := missing(TABLES[command], outcome.rows):
        problems.append("never asked: " + "; ".join(gaps))
    if problems:
        return Check(command, False, "; ".join(problems))
    return Check(command, True, f"exit 0, {ok_banner}, {len(outcome.asked)} read(s) answered")
