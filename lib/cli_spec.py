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
GLOBAL_OPTIONS = (Opt(DEBUG),)

SPECS: dict[str, Spec] = {
    s.name: s
    for s in (
        Spec("alerts", subcommands=(Spec("install"), Spec("remove"))),
        Spec("backup", options=(Opt("--no-snapshot"),)),
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
        Spec("simulate", options=(Opt("--rw"), Opt("--display", Arg("text"))), args=(_DEVICE,)),
        Spec("status", options=(Opt("--refresh"), Opt("--notify"))),
        Spec("umount", args=(_SYSTEM,)),
    )
}


def command(name: str) -> Spec:
    """The spec of command ``name``."""
    return SPECS[name]
