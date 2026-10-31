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
COMMANDS section orders them.

Standard library only and no zark import, so the shell completion can read
it without loading the rest of zark.
"""

from dataclasses import dataclass


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
    Group("Inspection", ("explore", "health", "monitor", "mount", "status", "umount")),
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
    "fix-rpool-mountpoint": "Give a restored rpool back mountpoint=/ (live USB)",
    "health": "Non-destructive risk checks on a backup drive",
    "monitor": "Live view of a running command and the backups' state (no root)",
    "mount": "Mount a backup pool for inspection / chroot / recovery",
    "prepare": "Prepare a new blank drive for backup",
    "prune": "Reclaim space in origin or on a backup drive (interactive)",
    "purge": "Securely wipe a managed backup drive",
    "recover": "Full bare-metal recovery from backup drive",
    "registry": "List, forget or fix entries in known_drives.json, or set a drive's retention",
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
