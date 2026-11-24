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
The autoinstall seed: a NoCloud ``CIDATA`` disk holding the user-data.

The user-data is ``#cloud-config`` followed by JSON, which is YAML, so no
YAML library is needed. It is the only file the passphrase is written to;
the run deletes it when it ends.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

HOSTNAME = "bench"
USERNAME = "bench"

# A drop-in that /etc/default/grub's readers source after it (Ubuntu's
# grub-mkconfig reads /etc/default/grub.d/*.cfg): GRUB and the kernel on the
# serial console too, without quiet splash, so the unlock prompt and the
# kernel's Secure Boot line reach the bench (Q2-4a).
GRUB_DROPIN = "\n".join(
    (
        'GRUB_TERMINAL="console serial"',
        'GRUB_SERIAL_COMMAND="serial --unit=0 --speed=115200"',
        'GRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyS0,115200n8"',
        "GRUB_TIMEOUT_STYLE=menu",
        "GRUB_TIMEOUT=3",
    ),
)
SUDOERS_LINE = f"{USERNAME} ALL=(ALL) NOPASSWD:ALL"


def _write_in_target(path: str, content: str, mode: str) -> list[str]:
    script = f"printf '%s\\n' {shlex.quote(content)} > /target{path} && chmod {mode} /target{path}"
    return ["sh", "-c", script]


def late_commands() -> list[list[str]]:
    """
    What the installer runs in the installed system before it powers off:
    the serial console and the bench user's sudo rule. Nothing here touches
    the boot chain's binaries, the pools or the keystore.
    """
    in_target = ["curtin", "in-target", "--target=/target", "--"]
    return [
        ["mkdir", "-p", "/target/etc/default/grub.d"],
        _write_in_target("/etc/default/grub.d/90-zark-bench.cfg", GRUB_DROPIN, "0644"),
        [*in_target, "update-grub"],
        _write_in_target(f"/etc/sudoers.d/{USERNAME}", SUDOERS_LINE, "0440"),
        [*in_target, "visudo", "-cf", f"/etc/sudoers.d/{USERNAME}"],
    ]


def error_commands() -> list[list[str]]:
    """On a failed install, the installer's logs to the serial console."""
    return [["sh", "-c", "tail -n 200 /var/log/installer/*.log > /dev/ttyS0 2>&1 || true"]]


def autoinstall(passphrase: str, password_hash: str, public_key: str) -> dict[str, Any]:
    """The autoinstall document: encrypted ZFS as the installer lays it out."""
    return {
        "version": 1,
        # The ISO's installer, not a newer one from the store: the run
        # checks what the ISO ships.
        "refresh-installer": {"update": False},
        "storage": {"layout": {"name": "zfs", "password": passphrase}},
        "identity": {
            "hostname": HOSTNAME,
            "realname": "zark bench",
            "username": USERNAME,
            "password": password_hash,
        },
        "ssh": {"install-server": True, "authorized-keys": [public_key], "allow-pw": False},
        "late-commands": late_commands(),
        "error-commands": error_commands(),
        "shutdown": "poweroff",
    }


def live_user_data(public_key: str) -> dict[str, Any]:
    """
    The live session's cloud-config, with no ``autoinstall`` key: the bench
    user with the run's public key and sudo, and the SSH server the 26.04
    live does not carry (Q2's ISO read), installed from the archive.
    """
    return {
        "users": [
            {
                "name": USERNAME,
                "gecos": "zark bench",
                "shell": "/bin/bash",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "lock_passwd": True,
                "ssh_authorized_keys": [public_key],
            }
        ],
        "ssh_pwauth": False,
        "package_update": True,
        "packages": ["openssh-server"],
    }


def render_cloud_config(doc: dict[str, Any]) -> str:
    """cloud-init user-data from a cloud-config document."""
    return "#cloud-config\n" + json.dumps(doc, indent=2) + "\n"


def render_user_data(doc: dict[str, Any]) -> str:
    """cloud-init user-data carrying the autoinstall document."""
    return "#cloud-config\n" + json.dumps({"autoinstall": doc}, indent=2) + "\n"


def meta_data(instance_id: str) -> str:
    """NoCloud meta-data; a new instance id per run."""
    return f"instance-id: {instance_id}\nlocal-hostname: {HOSTNAME}\n"


def cloud_localds_cmd(seed: Path, user_data: Path, meta: Path) -> list[str]:
    """cloud-image-utils' tool writing the CIDATA image."""
    return ["cloud-localds", str(seed), str(user_data), str(meta)]
