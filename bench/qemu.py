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
QEMU's command lines, built as lists and never through a shell.

One machine shape for the install, the installed boot and the negative
control: q35 with SMM, the Secure Boot firmware with its flash marked
secure, the serial console on a Unix socket (and a log file), QMP on
another, a VGA device for the graphical installer with no window.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from bench.firmware import Firmware

QEMU = "qemu-system-x86_64"


class SecretInArgv(Exception):
    """A secret appeared in a command line: the machine is not started."""


def opt_path(path: Path) -> str:
    """A path inside a QEMU option value: a comma is written twice."""
    return str(path).replace(",", ",,")


@dataclass(frozen=True)
class Machine:  # pylint: disable=too-many-instance-attributes  # one per QEMU option
    """What every boot of the bench shares."""

    firmware: Firmware
    vars_file: Path
    serial_socket: Path
    serial_log: Path
    qmp_socket: Path
    accel: str = "kvm"
    smp: int = 4
    memory_mib: int = 8192


def base_argv(m: Machine, name: str) -> list[str]:
    """The machine without disks or network."""
    return [
        QEMU,
        "-name",
        name,
        "-machine",
        f"q35,smm=on,accel={m.accel}",
        # The firmware's flash may only be written from SMM: without this the
        # OS could rewrite the Secure Boot variables.
        "-global",
        "driver=cfi.pflash01,property=secure,value=on",
        "-cpu",
        "host" if m.accel == "kvm" else "max",
        "-smp",
        str(m.smp),
        "-m",
        str(m.memory_mib),
        "-drive",
        f"if=pflash,format=raw,unit=0,readonly=on,file={opt_path(m.firmware.code)}",
        "-drive",
        f"if=pflash,format=raw,unit=1,file={opt_path(m.vars_file)}",
        "-chardev",
        f"socket,id=ser0,path={opt_path(m.serial_socket)},server=on,wait=off,"
        f"logfile={opt_path(m.serial_log)}",
        "-serial",
        "chardev:ser0",
        "-qmp",
        f"unix:{opt_path(m.qmp_socket)},server=on,wait=off",
        "-vga",
        "std",
        "-display",
        "none",
        "-no-reboot",
    ]


def _cdrom(drive_id: str, image: Path, bus: str, bootindex: int | None) -> list[str]:
    boot = f",bootindex={bootindex}" if bootindex is not None else ""
    return [
        "-drive",
        f"if=none,id={drive_id},media=cdrom,readonly=on,format=raw,file={opt_path(image)}",
        "-device",
        f"ide-cd,drive={drive_id},bus={bus}{boot}",
    ]


def _target(disk: Path, bootindex: int) -> list[str]:
    return [
        "-drive",
        f"if=none,id=target,format=qcow2,discard=unmap,file={opt_path(disk)}",
        "-device",
        f"virtio-blk-pci,drive=target,bootindex={bootindex}",
    ]


def _user_net(ssh_port: int) -> list[str]:
    # Only the host's loopback reaches the guest's SSH.
    return [
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
        "-device",
        "virtio-net-pci,netdev=net0",
    ]


def install_argv(m: Machine, iso: Path, seed: Path, disk: Path, ssh_port: int) -> list[str]:
    """The live ISO first, the seed beside it, the blank target second."""
    return [
        *base_argv(m, "bench-install"),
        *_cdrom("cd0", iso, "ide.0", 1),
        *_cdrom("seed", seed, "ide.1", None),
        *_target(disk, 2),
        *_user_net(ssh_port),
    ]


def installed_argv(m: Machine, disk: Path, ssh_port: int, extra: tuple[str, ...] = ()) -> list[str]:
    """The installed disk alone, as the machine it was installed on (``extra``: a USB port)."""
    return [*base_argv(m, "bench-installed"), *_target(disk, 1), *_user_net(ssh_port), *extra]


# The backup drive: a qcow2 node QEMU keeps while the USB device comes and
# goes, so it is plugged in by QMP after the guest is up, as a person plugs
# a drive in, and can be pulled by name later (Q4).
USB_NODE = "usbdisk"
USB_DEVICE = "usbdrive"


def usb_port(image: Path, node: str = USB_NODE) -> list[str]:
    """An xHCI controller and the backup drive's image as a block node, no device yet."""
    return [
        "-device",
        "qemu-xhci,id=xhci",
        "-blockdev",
        f"driver=qcow2,node-name={node},file.driver=file,file.filename={opt_path(image)}",
    ]


def usb_plug(serial: str, node: str = USB_NODE) -> dict[str, str]:
    """QMP ``device_add``'s arguments: a USB disk with an invented serial on the node."""
    return {
        "driver": "usb-storage",
        "bus": "xhci.0",
        "drive": node,
        "serial": serial,
        "id": USB_DEVICE,
    }


def new_disk(image: Path, serial: str, bootindex: int | None) -> list[str]:
    """The disk that replaces the dead one: blank for the live, recovered after it."""
    boot = f",bootindex={bootindex}" if bootindex is not None else ""
    return [
        "-drive",
        f"if=none,id=newdisk,format=qcow2,discard=unmap,file={opt_path(image)}",
        "-device",
        f"virtio-blk-pci,drive=newdisk,serial={serial}{boot}",
    ]


def live_argv(  # pylint: disable=too-many-arguments
    m: Machine, iso: Path, seed: Path, disk: Path, serial: str, usb: Path, ssh_port: int
) -> list[str]:
    """The live ISO first, its seed beside it, the new disk and the USB port; no old disk."""
    return [
        *base_argv(m, "bench-live"),
        *_cdrom("cd0", iso, "ide.0", 1),
        *_cdrom("seed", seed, "ide.1", None),
        *new_disk(disk, serial, 2),
        *usb_port(usb),
        *_user_net(ssh_port),
    ]


def recovered_argv(m: Machine, disk: Path, serial: str, usb: Path, ssh_port: int) -> list[str]:
    """The recovered disk alone, with the USB port its next backup uses."""
    return [
        *base_argv(m, "bench-recovered"),
        *new_disk(disk, serial, 1),
        *usb_port(usb),
        *_user_net(ssh_port),
    ]


def negative_argv(m: Machine, esp_image: Path) -> list[str]:
    """A FAT image holding an unsigned EFI binary, and no network."""
    return [
        *base_argv(m, "bench-negative"),
        "-drive",
        f"if=virtio,format=raw,readonly=on,file={opt_path(esp_image)}",
        "-net",
        "none",
    ]


def assert_no_secret(argv: list[str], secrets: Iterable[str]) -> None:
    """Refuse a command line that carries any secret, even inside a value."""
    for secret in secrets:
        if secret and any(secret in arg for arg in argv):
            raise SecretInArgv("a secret is in a QEMU argument; the machine was not started")
