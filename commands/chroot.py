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
zark chroot — Open an interactive chroot into the installed ZFS system.

Designed for the live-USB recovery workflow: from a live session where the
system's rpool/bpool are NOT imported, this command imports them under an
altroot, unlocks the keystore, mounts the boot environment, sets up the
bind mounts a chroot needs (/proc /sys /dev /dev/pts /run, efivars, ESP),
and drops the operator into a shell inside the installed system. On exit it
tears everything down and exports both pools cleanly, so the next real boot
imports them without needing -f.

Usage:
    sudo ./zark chroot [device]

`device` is an optional import hint (e.g. /dev/nvme0n1 or a /dev/disk/by-id
path). When omitted, ZFS auto-scans for rpool/bpool, which is the usual case
for the single internal disk.
"""

import subprocess  # nosec B404 — interactive chroot shell; see launch note below

from lib import sh
from lib.cleanup import Cleanup
from lib.keystore import Keystore
from lib.log import Log
from lib.mount import mount_system_pools
from lib.zfs import ZFS

CHROOT_MNT = "/mnt/zark/chroot"


def _setup_bind_mounts(root: str, cleanup: Cleanup, log: Log) -> None:
    """Bind the kernel/runtime filesystems and the ESP a chroot needs.

    Order matters for teardown: these are tracked AFTER the ZFS dataset
    mounts, so Cleanup (which unmounts in reverse) removes the binds before
    unmounting the datasets and exporting the pools.
    """
    for d in ("proc", "sys", "dev", "dev/pts", "run"):
        sh.run(f"mkdir -p {root}/{d}")
        if sh.run(f"mount --bind /{d} {root}/{d}").ok:
            cleanup.track_mount(f"{root}/{d}")
        else:
            log.warn(f"Could not bind-mount /{d}")

    # efivars (best-effort; absent on legacy-BIOS systems)
    efivars = f"{root}/sys/firmware/efi/efivars"
    sh.run(f"mkdir -p {efivars}")
    if sh.run(f"mount -t efivarfs efivarfs {efivars}", check=False).ok:
        cleanup.track_mount(efivars)

    # ESP at /boot/efi so grub/efibootmgr work inside the chroot
    internal_disk = sh.run(
        'lsblk -dn -o NAME,TYPE | awk \'$2=="disk"{print "/dev/"$1}\' | grep nvme | head -1',
    ).output.strip()
    efi_part = sh.part(internal_disk, 1) if internal_disk else ""
    if not efi_part:
        efi_part = sh.run("blkid -t TYPE=vfat | grep -i efi | head -1 | cut -d: -f1").output.strip()
    if efi_part:
        sh.run(f"mkdir -p {root}/boot/efi")
        if sh.run(f"mount {efi_part} {root}/boot/efi").ok:
            cleanup.track_mount(f"{root}/boot/efi")


def run(args: list[str]):  # pylint: disable=too-many-branches,too-many-statements
    """Main entry point for zark chroot command."""
    device = next((a for a in args if not a.startswith("-")), None)

    log = Log()
    zfs = ZFS(log)
    keystore = Keystore(log)
    cleanup = Cleanup(log)
    # Register up front: a clean export on every exit path (shell exit,
    # Ctrl-C, fatal) is what lets the next real boot import without -f.
    cleanup.register()

    log.banner("CHROOT", "Open a shell inside the installed ZFS system")

    # ── Safety: refuse the running system; require a live session ─────────
    if zfs.pool_exists("rpool"):
        log.fatal(
            "rpool is already imported.\n"
            "  If this is your running system, you are already inside it —\n"
            "  no chroot is needed. If it is a leftover from a previous run,\n"
            "  run 'sudo ./zark clean' first, then retry from a live USB.",
        )

    if not sh.is_live_usb():
        log.warn("This does not look like a live USB session.")
        log.info("zark chroot is meant to run from live media. Chrooting the")
        log.info("installed system from itself is not supported.")
        if not log.ask("Continue anyway?", default=False):
            log.fatal("Aborted — boot a live USB and retry.")

    # ── External-pool guard (do not pull a backup drive into the chroot) ──
    available = [
        p.strip()
        for p in sh.run("zpool import 2>/dev/null | awk '/pool:/{print $2}'").lines
        if p.strip()
    ]
    external = [p for p in available if p not in ("rpool", "bpool")]
    if external:
        log.warn(f"External pool(s) detected: {', '.join(external)}")
        log.info("Only rpool/bpool will be imported; external drives are left alone.")

    # ── Import + unlock + mount the system ────────────────────────────────
    log.info("Importing and unlocking the system...")
    passphrase = log.ask_password("Passphrase for rpool")

    result = mount_system_pools(
        CHROOT_MNT,
        passphrase,
        log,
        zfs,
        keystore,
        cleanup,
        device=device,
    )
    if result is None:
        log.fatal("Could not mount the system — see messages above.")
    root_path, ubuntu_name = result

    _setup_bind_mounts(root_path, cleanup, log)
    log.ok(f"System ready at {root_path} (boot env: {ubuntu_name})")

    # ── Inform, then drop into the shell ──────────────────────────────────
    log.banner_ok(
        "ENTERING CHROOT",
        [
            f"Root:      {root_path}",
            f"Boot env:  {ubuntu_name}",
            "",
            f"{log.Y}Inside the chroot you can run apt, update-grub,{log.N}",
            f"{log.Y}dpkg-reconfigure, etc. as if booted normally.{log.N}",
            "",
            "Type 'exit' (or Ctrl-D) to leave. On exit, zark unmounts",
            "everything and exports rpool/bpool cleanly. If a later boot",
            "ever asks to force-import a pool, that is benign — boot",
            "through and, if needed, run 'zark repair-boot'.",
        ],
    )

    # Interactive shell: this is the one place zark does NOT route through
    # sh.run(). sh.run captures stdout/stderr for logging, which would break
    # an interactive terminal. We hand the controlling TTY straight to bash
    # and wait. nosec B603: the command vector is fixed (no shell, no user
    # string interpolation); `root_path` is a constant module path.
    proc = subprocess.run(  # nosec B603
        ["chroot", root_path, "/bin/bash", "--login"],
        check=False,
    )
    log.info(f"Chroot shell exited (status {proc.returncode}). Cleaning up...")

    # ── Teardown (explicit; atexit would also catch it) ───────────────────
    cleanup.run()
    log.banner_ok(
        "CHROOT CLOSED",
        [
            "System unmounted ✓",
            "rpool/bpool exported cleanly ✓",
            "",
            "Safe to remove the live USB and reboot.",
        ],
    )
