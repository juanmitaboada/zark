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
zark's command line, declared once: every command, its group and its
one-line description, as ``zark --help`` prints them and the manpage's
COMMANDS section orders them; and every word each command's parser
compares its arguments with, with the kind of value each option and
positional argument takes, as the shell completion offers them.

Each parser takes its words from here (``SPEC.word("--rw")``), and a test
checks both ways that a parser uses every word its command declares and
declares every word it uses (M14-9), so the two cannot drift.

Standard library only and no zark import, so the shell completion can read
it without loading the rest of zark.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Group:
    """A heading of ``zark --help`` and of the manpage's COMMANDS section."""

    title: str
    commands: tuple[str, ...]


# The groups in the order they are shown; commands sorted by name in each (M14-7 7a).
GROUPS: tuple[Group, ...] = (
    Group("Backup", ("backup", "exclude", "prepare", "prune", "purge", "registry")),
    Group(
        "Recovery (live USB)",
        ("chroot", "finish", "fix-rpool-mountpoint", "recover", "repair-boot", "simulate"),
    ),
    Group("Inspection", ("explore", "fullcheck", "health", "monitor", "mount", "status", "umount")),
    Group("Setup and upkeep", ("alerts", "clean", "selftest", "setup")),
)

DESCRIPTIONS: dict[str, str] = {
    "alerts": "Install or remove the MOTD and timer for status alerts",
    "backup": "Run incremental backup to connected drive",
    "chroot": "Open an interactive chroot into the installed ZFS system (live USB)",
    "clean": "Release what an interrupted zark run left mounted or imported",
    "exclude": "Leave datasets out of backups or of local snapshots (list/add/remove)",
    "explore": "Scan for ZFS pools, show known/unknown status",
    "finish": "Post-recovery finalization (run from recovered system)",
    "fullcheck": "Scrub a backup drive and read a point back, changing no data (scrub/restore)",
    "fix-rpool-mountpoint": "Give a restored rpool back mountpoint=/ (live USB)",
    "health": "Non-destructive risk checks on a backup drive",
    "monitor": "Live view of a running command and the backups' state (no root)",
    "mount": "Mount a backup pool for inspection / chroot / recovery",
    "prepare": "Prepare a new blank drive for backup",
    "prune": "Reclaim space in origin or on a backup drive (interactive)",
    "purge": "Securely wipe a managed backup drive",
    "recover": "Full bare-metal recovery from backup drive",
    "registry": "List, forget, fix or verify drives in known_drives.json, or set a retention",
    "repair-boot": "Fix boot issues from live USB (grub.cfg, initrd)",
    "selftest": "Check that this platform behaves as zark relies on",
    "setup": "Install dependencies, configure sanoid snapshots (--remove: undo the guards, alerts)",
    "simulate": "Boot recovered system in QEMU/KVM (test without rebooting)",
    "status": "Source pools' fill level and each drive's backup age",
    "umount": "Unmount a previously mounted backup pool",
}


def command_names() -> list[str]:
    """Every command, in the help's order."""
    return [name for group in GROUPS for name in group.commands]


# ── the words each command's parser reads (M14-9) ────────────────────────

# Kinds of value, as the completion lists them: a whole disk, a registered
# drive, a drive waiting for its read-back, a filesystem of rpool, one
# carrying an exclusion, a file, a directory, one of fixed words, or free
# text (nothing offered).
KINDS = ("device", "drive", "pending", "dataset", "excluded", "file", "dir", "words", "text")


@dataclass(frozen=True)
class Arg:
    """A value: an option's or a positional argument's."""

    kind: str
    words: tuple[str, ...] = ()
    repeat: bool = False


@dataclass(frozen=True)
class Opt:
    """An option and the value it takes, if any."""

    name: str
    value: Arg | None = None


@dataclass(frozen=True)
class Spec:
    """A command or a subcommand: its options, positional arguments and
    subcommands (the first one is the default when none is typed)."""

    name: str
    options: tuple[Opt, ...] = ()
    args: tuple[Arg, ...] = ()
    subcommands: tuple["Spec", ...] = ()
    default_sub: str = field(default="")

    def words(self) -> set[str]:
        """Every word this spec and its subcommands declare for the parser:
        option names, fixed positional words and subcommand names. An
        option's value words are only offered (monitor's intervals), not
        compared with: the parser checks the value itself."""
        out = {o.name for o in self.options}
        out |= {w for a in self.args for w in a.words}
        for sub in self.subcommands:
            out |= {sub.name} | sub.words()
        return out

    def word(self, text: str) -> str:
        """``text``, which must be one of :meth:`words`: a parser compares its
        arguments only with words its command declares."""
        if text not in self.words():
            raise KeyError(f"{self.name}: {text!r} is not a declared word")
        return text

    def sub(self, name: str) -> "Spec":
        """The subcommand ``name``."""
        return next(s for s in self.subcommands if s.name == name)


_DEVICE = Arg("device")
_DRIVE = Arg("drive")
_FILE = Arg("file")
_SYSTEM = Arg("words", ("local", "system", "rpool"))
_WHAT = Arg("words", ("backups", "snapshots", "both"))

# What zark itself reads before the command: the help, the version, and
# --debug after any command (zark's entry point).
HELP_WORDS = ("--help", "-h", "help")
VERSION_WORDS = ("--version", "-v")
DEBUG = "--debug"
# The exit status of a command line zark refuses (sysexits' EX_USAGE).
EX_USAGE = 2
GLOBAL_OPTIONS = (Opt(DEBUG),)

SPECS: dict[str, Spec] = {
    s.name: s
    for s in (
        Spec("alerts", subcommands=(Spec("install"), Spec("remove"))),
        Spec("backup", options=(Opt("--no-snapshot"), Opt("--drive", _DRIVE))),
        Spec("chroot", args=(_DEVICE,)),
        Spec("clean"),
        Spec(
            "exclude",
            subcommands=(
                Spec("list"),
                Spec("add", args=(Arg("dataset"), _WHAT)),
                Spec("remove", args=(Arg("excluded"), _WHAT)),
            ),
            default_sub="list",
        ),
        Spec("explore"),
        Spec("finish"),
        Spec("fix-rpool-mountpoint"),
        Spec("fullcheck", args=(Arg("words", ("scrub", "restore")),)),
        Spec("health", args=(_DEVICE,)),
        Spec(
            "monitor",
            options=(
                Opt("--once"),
                Opt("--state"),
                Opt("--interval", Arg("words", ("15", "30", "60", "120", "300", "600"))),
                Opt("--root", Arg("dir")),
                Opt("--no-live"),
                Opt("--help"),
                Opt("-h"),
            ),
        ),
        Spec("mount", args=(_SYSTEM,)),
        Spec("prepare", args=(_DEVICE,)),
        Spec("prune"),
        Spec("purge", args=(_DEVICE,)),
        Spec("recover"),
        Spec(
            "registry",
            subcommands=(
                Spec("list"),
                Spec("forget", args=(_DRIVE,)),
                Spec("fix", args=(_DRIVE,)),
                Spec("verify", args=(Arg("pending"),)),
                Spec(
                    "retention",
                    args=(_DRIVE, Arg("words", ("window", "max_age", "margin"), repeat=True)),
                ),
            ),
            default_sub="list",
        ),
        Spec("repair-boot"),
        Spec(
            "selftest",
            subcommands=(
                Spec(
                    "run",
                    options=(Opt("--stick", _DEVICE), Opt("--only", Arg("text")), Opt("--debug")),
                ),
                Spec("status"),
                Spec("report", args=(_FILE,)),
                Spec(
                    "certificate", options=(Opt("--into", _FILE),), args=(Arg("file", repeat=True),)
                ),
                Spec("warnings", args=(Arg("words", ("on", "off")),)),
            ),
            default_sub="run",
        ),
        Spec("setup", options=(Opt("--remove"),)),
        Spec(
            "simulate",
            options=(Opt("--rw"), Opt("--ro"), Opt("--display", Arg("text"))),
            args=(_DEVICE,),
        ),
        Spec("status", options=(Opt("--refresh"), Opt("--notify"))),
        Spec("umount", args=(_SYSTEM,)),
    )
}


def command(name: str) -> Spec:
    """The spec of command ``name``."""
    return SPECS[name]


# ── what every command's arguments are checked against (M14P.3) ─────────

# Kinds of value that name a path: made absolute before zark leaves the
# caller's directory for / (M26R-50).
PATH_KINDS = ("device", "file", "dir")


@dataclass(frozen=True)
class Word:
    """One argument as the spec reads it: an option (``opt``), an option's
    value or a positional argument (``arg``, None for a positional the spec
    does not declare)."""

    text: str
    opt: Opt | None = None
    arg: Arg | None = None


def _selected(name: str, args: list[str]) -> tuple[Spec, list[str]]:
    """The spec the arguments are read with: a subcommand named first, else
    the default one; and the arguments after the subcommand's name."""
    spec = SPECS[name]
    if not spec.subcommands:
        return spec, list(args)
    if args and any(s.name == args[0] for s in spec.subcommands):
        return spec.sub(args[0]), list(args[1:])
    if spec.default_sub:
        return spec.sub(spec.default_sub), list(args)
    return spec, list(args)


def _is_option(text: str) -> bool:
    return text.startswith("-") and text != "-"


def words(name: str, args: list[str]) -> Iterator[Word]:
    """``args`` of command ``name`` as its spec reads them, the subcommand's
    name left out: an option takes the next argument as its value whatever
    it looks like; ``--debug`` is an option anywhere after the command."""
    spec, rest = _selected(name, args)
    options = {o.name: o for o in (*spec.options, *GLOBAL_OPTIONS)}
    positional = 0
    i = 0
    while i < len(rest):
        text = rest[i]
        opt = options.get(text) if _is_option(text) else None
        if opt is not None:
            yield Word(text, opt=opt)
            if opt.value is not None and i + 1 < len(rest):
                i += 1
                yield Word(rest[i], arg=opt.value)
        elif _is_option(text):
            yield Word(text)
        else:
            arg: Arg | None = None
            if positional < len(spec.args):
                arg = spec.args[positional]
            elif spec.args and spec.args[-1].repeat:
                arg = spec.args[-1]
            positional += 1
            yield Word(text, arg=arg)
        i += 1


def unknown_option(name: str, args: list[str]) -> str:
    """The first option command ``name`` does not declare, "" when none: an
    argument that starts with ``-`` (``-`` alone is a positional) and is not
    an option's value."""
    return next(
        (
            w.text
            for w in words(name, args)
            if w.opt is None and w.arg is None and _is_option(w.text)
        ),
        "",
    )


def asks_help(name: str, args: list[str]) -> bool:
    """Whether ``args`` hold ``--help`` or ``-h`` as an option command
    ``name`` does not declare itself (the monitor does): zark then prints
    the command's usage and runs nothing."""
    return any(w.text in HELP_WORDS and w.opt is None and w.arg is None for w in words(name, args))


def absolute(name: str, args: list[str]) -> list[str]:
    """``args`` with every value of a path kind made absolute (without
    following links), so they mean the same once zark is in ``/``."""
    out = list(args)
    _, rest = _selected(name, args)
    skip = len(args) - len(rest)  # the subcommand's name
    for n, w in enumerate(words(name, args)):
        if w.arg is not None and w.arg.kind in PATH_KINDS and not w.text.startswith("/"):
            out[skip + n] = os.path.abspath(w.text)
    return out


def _value(arg: Arg) -> str:
    shown = f"<{'|'.join(arg.words) if arg.words else arg.kind}>"
    return f"{shown}..." if arg.repeat else shown


def _synopsis(spec: Spec) -> str:
    parts = [f"[{o.name}{' ' + _value(o.value) if o.value else ''}]" for o in spec.options]
    parts += [_value(a) for a in spec.args]
    return " ".join(parts)


def usage(name: str, cmd: str = "zark") -> str:
    """Command ``name``'s usage, from its spec: what ``zark <command>
    --help`` prints (M14P.3)."""
    spec = SPECS[name]
    lines: list[str] = []
    if spec.subcommands:
        lines.append(f"Usage: {cmd} {name} <subcommand> ...")
        for sub in spec.subcommands:
            mark = "  (the default)" if sub.name == spec.default_sub else ""
            line = f"  {cmd} {name} {sub.name} {_synopsis(sub)}".rstrip()
            lines.append(line + mark)
    else:
        lines.append(f"Usage: {cmd} {name} {_synopsis(spec)}".rstrip())
    lines += ["", DESCRIPTIONS[name], "", "More: man zark"]
    return "\n".join(lines) + "\n"
