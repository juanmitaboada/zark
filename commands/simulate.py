"""
zark simulate — Boot the recovered (or live) system in QEMU/KVM.

Tests the boot chain without physically rebooting. Uses OVMF (UEFI firmware)
so the EFI boot path is exercised identically to real hardware.

Usage:
  sudo ./zark simulate              # boot /dev/nvme0n1 in QEMU
  sudo ./zark simulate /dev/sda     # boot a specific disk
  sudo ./zark simulate --ro         # read-only (-snapshot): disk is never written

WARNING: Without --ro, QEMU writes to the real disk exactly as a real boot would.
Use --ro to safely inspect the boot chain without side effects.

Requirements:
  apt install qemu-system-x86 ovmf
"""

import os
import shutil
import subprocess
from pathlib import Path

from lib import sh
from lib.log import Log

# OVMF firmware search paths (Ubuntu / Debian)
OVMF_CODE_CANDIDATES = [
    "/usr/share/OVMF/OVMF_CODE_4M.fd",
    "/usr/share/OVMF/OVMF_CODE.fd",
    "/usr/share/edk2/x64/OVMF_CODE.fd",
    "/usr/share/qemu/OVMF.fd",
]
OVMF_VARS_CANDIDATES = [
    "/usr/share/OVMF/OVMF_VARS_4M.fd",
    "/usr/share/OVMF/OVMF_VARS.fd",
    "/usr/share/edk2/x64/OVMF_VARS.fd",
]


def _find_file(candidates: list[str]) -> str | None:
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _detect_nvme() -> str | None:
    """Return the first NVMe disk that is not the live USB boot device."""
    r = sh.run("lsblk -dn -o NAME,TYPE")
    for line in r.lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "disk" and parts[0].startswith("nvme"):
            return f"/dev/{parts[0]}"
    return None


def _install_deps(log: Log):
    log.info("Installing qemu-system-x86 and ovmf...")
    r = sh.run("apt-get install -y qemu-system-x86 ovmf", log=log)
    if not r.ok:
        log.fatal("Failed to install QEMU/OVMF — install manually and retry")


def run(
    args: list[str],
):  # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    """Main entry point for "zark simulate" command."""
    log = Log()

    log.banner("SIMULATE BOOT", "Boot the recovered system in QEMU/KVM")

    # ── Parse arguments ──────────────────────────────────────────────────
    read_only = "--ro" in args
    disk_arg = next((a for a in args if a.startswith("/dev/")), None)

    # ── Check for QEMU ───────────────────────────────────────────────────
    if not sh.run("command -v qemu-system-x86_64").ok:
        log.warn("qemu-system-x86_64 not found")
        try:
            install = input("  Install qemu-system-x86 + ovmf? [Y/n]: ").strip().lower()
        except EOFError:
            install = "y"
        if install in ("", "y", "yes"):
            _install_deps(log)
        else:
            log.fatal(
                "QEMU is required for simulate — install with: apt install qemu-system-x86 ovmf",
            )

    # ── Check for KVM ────────────────────────────────────────────────────
    kvm_ok = Path("/dev/kvm").exists() and os.access("/dev/kvm", os.R_OK | os.W_OK)
    if not kvm_ok:
        log.warn("KVM not available — simulation will be slow (no hardware acceleration)")
        log.warn("Check: ls -la /dev/kvm  and  modprobe kvm_intel or kvm_amd")

    # ── Find OVMF firmware ───────────────────────────────────────────────
    ovmf_code = _find_file(OVMF_CODE_CANDIDATES)
    ovmf_vars_src = _find_file(OVMF_VARS_CANDIDATES)

    if not ovmf_code or not ovmf_vars_src:
        log.warn("OVMF firmware not found")
        _install_deps(log)
        ovmf_code = _find_file(OVMF_CODE_CANDIDATES)
        ovmf_vars_src = _find_file(OVMF_VARS_CANDIDATES)
        if not ovmf_code or not ovmf_vars_src:
            log.fatal(
                "OVMF firmware still not found after install",
                causes=[
                    "Searched: " + ", ".join(OVMF_CODE_CANDIDATES),
                    "Try: apt install ovmf",
                ],
            )

    log.ok(f"OVMF code: {ovmf_code}")
    log.ok(f"OVMF vars: {ovmf_vars_src}")

    # OVMF vars must be writable (UEFI writes boot entries)
    ovmf_vars_tmp = f"/tmp/zark_ovmf_vars_{os.getpid()}.fd"
    _ = shutil.copy2(ovmf_vars_src, ovmf_vars_tmp)
    os.chmod(ovmf_vars_tmp, 0o600)
    log.dbg(f"OVMF vars copy: {ovmf_vars_tmp}")

    # ── Select disk ──────────────────────────────────────────────────────
    target_disk: str
    if disk_arg:
        target_disk = disk_arg
        if not Path(target_disk).exists():
            log.fatal(f"Disk not found: {target_disk}")
    else:
        detected = _detect_nvme()
        if not detected:
            # Fallback: list all disks and ask
            log.info("No NVMe disk auto-detected. Available disks:")
            r = sh.run("lsblk -dn -o NAME,SIZE,MODEL")
            candidates: list[str] = []
            for line in r.lines:
                parts = line.split(None, 2)
                if parts:
                    candidates.append(line.strip())
                    log.raw(f"  {log.W}{len(candidates)}{log.N}) {line.strip()}")
            if not candidates:
                log.fatal("No disks found")
            try:
                sel = input(f"  Select disk [1-{len(candidates)}]: ").strip()
            except EOFError:
                sel = "1"
            idx = int(sel) - 1 if sel.isdigit() else 0
            target_disk = f"/dev/{candidates[idx].split()[0]}"
        else:
            target_disk = detected

    log.ok(f"Target disk: {target_disk}")

    if not Path(target_disk).exists():
        log.fatal(f"Disk not found: {target_disk}")

    if not os.access(target_disk, os.R_OK):
        log.fatal(
            f"Cannot read {target_disk}",
            causes=["Run as root: sudo ./zark simulate"],
        )

    # ── Build QEMU command ───────────────────────────────────────────────
    disk_options = f"file={target_disk},format=raw,if=virtio"
    use_snapshot = False
    if read_only:
        # Use -snapshot (global) instead of readonly=on on the drive.
        # -snapshot creates a temporary overlay: the guest can write normally
        # (UEFI boot entries, filesystem journals, etc.) but all changes are
        # discarded when QEMU exits. With readonly=on, UEFI firmware and the
        # OS cannot write at all, causing boot failures.
        use_snapshot = True
        log.ok("Read-only mode (--ro): using snapshot — disk will NOT be modified")
    else:
        log.warn(f"QEMU will write to {target_disk} exactly as a real boot would")
        log.warn("Use --ro flag to prevent any writes during testing")
        log.blank()
        try:
            confirm = input("  Proceed with read-write access? [y/N]: ").strip().lower()
        except EOFError:
            confirm = "n"
        if confirm not in ("y", "yes"):
            log.info("Re-run with --ro flag for read-only simulation:")
            log.raw(f"  {log.W}sudo ./zark simulate --ro{log.N}")
            return

    cmd = [
        "qemu-system-x86_64",
        # Firmware
        "-drive",
        f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
        "-drive",
        f"if=pflash,format=raw,file={ovmf_vars_tmp}",
        # Disk
        "-drive",
        disk_options,
        # Hardware
        "-m",
        "4096",
        "-smp",
        "4",
        "-cpu",
        "host" if kvm_ok else "max",
        # Display
        "-vga",
        "virtio",
        "-display",
        "gtk",
        # Boot menu (F12 to open, shows EFI boot entries)
        "-boot",
        "menu=on",
        # Serial console on terminal (useful for early boot messages)
        "-serial",
        "mon:stdio",
    ]

    if kvm_ok:
        cmd.insert(1, "-enable-kvm")

    if use_snapshot:
        cmd.append("-snapshot")

    log.blank()
    log.info("Starting QEMU...")
    log.info(f"Disk: {log.W}{target_disk}{log.N}  ({'read-only' if read_only else 'read-write'})")
    log.info(f"KVM:  {log.W}{'enabled' if kvm_ok else 'disabled (slow)'}{log.N}")
    log.info(f"RAM:  {log.W}4 GB{log.N}  CPUs: {log.W}4{log.N}")
    log.blank()
    log.info("Tips:")
    log.raw(f"    {log.W}Ctrl+Alt+G{log.N}  release mouse from QEMU window")
    log.raw(f"    {log.W}Ctrl+Alt+F{log.N}  toggle fullscreen")
    log.raw(f"    {log.W}F12{log.N}          open UEFI boot menu")
    log.raw(f"    {log.W}Ctrl+C{log.N}       force-quit QEMU (this terminal)")
    log.blank()
    log.info(f"Command: {' '.join(cmd)}")
    log.blank()

    try:
        _ = subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        log.info("QEMU terminated by user")
    except FileNotFoundError:
        log.fatal("qemu-system-x86_64 not found in PATH")
    finally:
        # Clean up temp OVMF vars
        if Path(ovmf_vars_tmp).exists():
            os.remove(ovmf_vars_tmp)

    log.ok("Simulation ended")
