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
zark integration test — Full backup → recover → boot in QEMU.

Everything runs inside QEMU. Nothing touches the host system
except creating disk image files in the work directory.

REQUIREMENTS:
  - QEMU/KVM: apt install qemu-system-x86 ovmf genisoimage
  - An Ubuntu Live ISO (24.04 or 26.04)
  - ~25 GB free disk space
  - KVM access (/dev/kvm)

WORKFLOW:
  Phase 1 — CREATE + BACKUP (inside QEMU)
    Boot QEMU with live ISO + source.img + backup.img + zark ISO.
    Run create_test_system.sh to build a minimal encrypted ZFS system,
    then zark prepare + zark backup.

  Phase 2 — RECOVER (inside QEMU)
    Boot QEMU with live ISO + backup.img + target.img + zark ISO.
    Run zark recover from backup to target.

  Phase 3 — VERIFY BOOT (inside QEMU)
    Boot QEMU from target.img alone.
    User verifies system boots and data is intact.

RUN:
  sudo python3 tests/test_integration.py --iso /path/to/ubuntu.iso
  sudo python3 tests/test_integration.py --phase 2 --iso /path/to/ubuntu.iso
  sudo python3 tests/test_integration.py --cleanup
"""

import argparse
import os
import shutil
import subprocess
import sys

ZARK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ═════════════════════════════════════════════════════════════════════════
#  Constants
# ═════════════════════════════════════════════════════════════════════════

DEFAULT_WORKDIR = "/tmp/zark_integration_test"
SOURCE_IMG = "source.img"
BACKUP_IMG = "backup.img"
TARGET_IMG = "target.img"
ZARK_ISO = "zark.iso"

SOURCE_SIZE_GB = 8
BACKUP_SIZE_GB = 10
TARGET_SIZE_GB = 16

QEMU_RAM_MB = 4096
QEMU_RAM_MARGIN_MB = 1024  # keep at least 1GB for host

OVMF_CODE_CANDIDATES = [
    "/usr/share/OVMF/OVMF_CODE_4M.fd",
    "/usr/share/OVMF/OVMF_CODE.fd",
    "/usr/share/qemu/OVMF_CODE_4M.fd",
    "/usr/share/edk2/ovmf/OVMF_CODE.fd",
]
OVMF_VARS_CANDIDATES = [
    "/usr/share/OVMF/OVMF_VARS_4M.fd",
    "/usr/share/OVMF/OVMF_VARS.fd",
    "/usr/share/qemu/OVMF_VARS_4M.fd",
    "/usr/share/edk2/ovmf/OVMF_VARS.fd",
]


# ═════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════

C_GREEN = "\033[0;32m"
C_YELLOW = "\033[0;33m"
C_RED = "\033[0;31m"
C_CYAN = "\033[0;36m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"


def info(msg):
    """
    Print an informational message.
    """
    print(f"{C_CYAN}[TEST]{C_RESET}  {msg}")


def ok(msg):
    """Print a success message."""
    print(f"{C_GREEN}[OK]{C_RESET}    {msg}")


def warn(msg):
    """Print a warning message."""
    print(f"{C_YELLOW}[WARN]{C_RESET}  {msg}")


def fail(msg):
    """Print an error message and exit."""
    print(f"{C_RED}[FAIL]{C_RESET}  {msg}")
    sys.exit(1)


def banner(title):
    """
    Print a banner with the given title.
    """
    w = 58
    print()
    print(f"{C_BOLD}{C_CYAN}╔{'═' * w}╗{C_RESET}")
    print(
        f"{C_BOLD}{C_CYAN}║{C_RESET}  "
        f"{title}{' ' * max(0, w - len(title) - 2)}"
        f"{C_BOLD}{C_CYAN}║{C_RESET}",
    )
    print(f"{C_BOLD}{C_CYAN}╚{'═' * w}╝{C_RESET}")
    print()


def sh_argv(argv, check=True):
    """Run a command as argument list (safe with spaces in paths)."""
    info(f"$ {' '.join(argv[:3])}...")
    r = subprocess.run(argv, check=False)
    if check and r.returncode != 0:
        fail(f"Command failed (rc={r.returncode}): {argv[0]}")
    return r


def find_file(candidates, label):
    """
    Find the first existing file from candidates. If none found, print error and exit.
    """
    for path in candidates:
        if os.path.exists(path):
            return path
    fail(f"Cannot find {label}. Tried:\n" + "\n".join(f"  - {c}" for c in candidates))
    return None


def ask_yn(question, default=True):
    """
    Ask a yes/no question to the user, return True for yes and False for no.
    default: True for yes, False for no (used if user just presses Enter)
    """
    prompt = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"\n  {C_YELLOW}?{C_RESET} {question} {prompt}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer.startswith("y")


def create_sparse(path, size_gb, label):
    """Create a sparse image file."""
    if os.path.exists(path):
        info(f"{label} already exists: {path}")
        if not ask_yn(f"Recreate {label}?", default=True):
            ok(f"Reusing existing {label}")
            return
        os.remove(path)
    sh_argv(["truncate", "-s", f"{size_gb}G", path])
    ok(f"Created {label}: {path} ({size_gb}GB sparse)")


def create_zark_iso(workdir):
    """Package zark code as ISO for QEMU access."""
    zark_iso = os.path.join(workdir, ZARK_ISO)
    info("Packaging zark code as ISO...")
    mkiso = "genisoimage" if shutil.which("genisoimage") else "mkisofs"
    sh_argv([mkiso, "-quiet", "-o", zark_iso, "-R", "-J", "-V", "ZARK", ZARK_ROOT])
    ok("zark.iso created")
    return zark_iso


def find_ovmf(workdir, tag=""):
    """Find OVMF firmware and create a writable copy of VARS."""
    ovmf_code = find_file(OVMF_CODE_CANDIDATES, "OVMF_CODE firmware")
    ovmf_vars_src = find_file(OVMF_VARS_CANDIDATES, "OVMF_VARS firmware")
    ovmf_vars_tmp = os.path.join(workdir, f"OVMF_VARS{tag}.fd")
    assert ovmf_vars_src is not None
    shutil.copy2(ovmf_vars_src, ovmf_vars_tmp)
    return ovmf_code, ovmf_vars_tmp


def launch_qemu(ovmf_code, ovmf_vars, drives, boot="d"):
    """Launch QEMU with proper argument handling (spaces in paths OK).

    drives: list of (file, opts) tuples, e.g.:
        [("target.img", "format=raw,if=virtio,cache=none"),
         ("ubuntu.iso", "media=cdrom")]
    """
    # ── RAM preflight check ──────────────────────────────────────────
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
                    avail_mb = avail_kb // 1024
                    needed_mb = QEMU_RAM_MB + QEMU_RAM_MARGIN_MB
                    if avail_mb < needed_mb:
                        warn(
                            f"Low memory: {avail_mb}MB available, "
                            f"QEMU needs {QEMU_RAM_MB}MB + {QEMU_RAM_MARGIN_MB}MB margin",
                        )
                        warn("Close other applications or reduce RAM with --ram flag")
                        if not ask_yn("Launch anyway? (risk of freeze)", default=False):
                            fail("Aborted — not enough RAM")
                    else:
                        ok(f"RAM check: {avail_mb}MB available, QEMU will use {QEMU_RAM_MB}MB")
                    break
    except (OSError, ValueError):
        pass  # can't check, proceed anyway

    argv = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-m",
        str(QEMU_RAM_MB),
        "-cpu",
        "host",
        "-smp",
        "2",
        "-vga",
        "virtio",
        "-display",
        "gtk",
        "-global",
        "isa-fdc.fdtypeA=none",
        "-drive",
        f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
        "-drive",
        f"if=pflash,format=raw,file={ovmf_vars}",
        # SSH: host port 2222 → guest port 22
        "-nic",
        "user,hostfwd=tcp::2222-:22",
    ]

    for filepath, opts in drives:
        argv += ["-drive", f"file={filepath},{opts}"]

    argv += ["-boot", boot]

    info("Launching QEMU (SSH: ssh -p 2222 ubuntu@localhost)...")
    return subprocess.run(argv, check=True)


# ═════════════════════════════════════════════════════════════════════════
#  Phase 1: CREATE + BACKUP (inside QEMU)
# ═════════════════════════════════════════════════════════════════════════


def phase_1(workdir, iso_path):
    """Boot QEMU, create minimal ZFS system, prepare + backup."""
    banner("PHASE 1 — CREATE + BACKUP (inside QEMU)")

    source_img = os.path.join(workdir, SOURCE_IMG)
    backup_img = os.path.join(workdir, BACKUP_IMG)

    create_sparse(source_img, SOURCE_SIZE_GB, "source disk")
    create_sparse(backup_img, BACKUP_SIZE_GB, "backup disk")

    zark_iso = create_zark_iso(workdir)
    ovmf_code, ovmf_vars = find_ovmf(workdir, "_phase1")

    print()
    print(f"  {C_BOLD}{'═' * 58}{C_RESET}")
    print(f"  {C_BOLD}  QEMU will boot Ubuntu Live.{C_RESET}")
    print(f"  {C_BOLD}  In the VM console, run this ONE command:{C_RESET}")
    print()
    print(
        f"  {C_YELLOW}  sudo mkdir -p /mnt/zark && sudo mount /dev/sr1 /mnt/zark "
        f"&& sudo bash /mnt/zark/tests/qemu_setup.sh{C_RESET}",
    )
    print()
    print(f"  {C_BOLD}  Then from your host terminal:{C_RESET}")
    print(
        f"  {C_YELLOW}  ssh -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -p 2222 ubuntu@localhost{C_RESET}",
    )
    print(f"  {C_YELLOW}  # password: ubuntu{C_RESET}")
    print()
    print(f"  {C_BOLD}  Once connected via SSH:{C_RESET}")
    print(f"  {C_YELLOW}  sudo bash /mnt/zark/tests/create_test_system.sh{C_RESET}")
    print(f"  {C_YELLOW}  sudo poweroff{C_RESET}")
    print()
    print(f"  {C_BOLD}  Disk mapping inside QEMU:{C_RESET}")
    print(f"    /dev/vda  = source system ({SOURCE_SIZE_GB}GB)")
    print(f"    /dev/vdb  = backup drive   ({BACKUP_SIZE_GB}GB)")
    print("    /dev/sr0  = Ubuntu Live ISO")
    print("    /dev/sr1  = zark code ISO")
    print(f"  {C_BOLD}{'═' * 58}{C_RESET}")
    print()

    if not ask_yn("Ready to launch QEMU?"):
        info("Skipped. Re-run with --phase 1 when ready.")
        return

    launch_qemu(
        ovmf_code,
        ovmf_vars,
        [
            (source_img, "format=raw,if=virtio,cache=none"),
            (backup_img, "format=raw,if=virtio,cache=none"),
            (iso_path, "media=cdrom"),
            (zark_iso, "media=cdrom,index=1"),
        ],
        boot="d",
    )

    ok("QEMU session ended")
    if ask_yn("Did Phase 1 complete successfully (create + prepare + backup)?"):
        ok("Phase 1 complete — backup image ready")
    else:
        warn("Phase 1 reported as failed. Re-run with --phase 1 to retry.")
        sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════
#  Phase 2: RECOVER (inside QEMU)
# ═════════════════════════════════════════════════════════════════════════


def phase_2(workdir, iso_path):
    """Boot QEMU with backup + empty target, user runs recover."""
    banner("PHASE 2 — RECOVER (inside QEMU)")

    backup_img = os.path.join(workdir, BACKUP_IMG)
    target_img = os.path.join(workdir, TARGET_IMG)

    if not os.path.exists(backup_img):
        fail(f"Backup image not found: {backup_img}\n  Run phase 1 first.")

    create_sparse(target_img, TARGET_SIZE_GB, "target disk")

    zark_iso = create_zark_iso(workdir)
    ovmf_code, ovmf_vars = find_ovmf(workdir, "_phase2")

    print()
    print(f"  {C_BOLD}{'═' * 58}{C_RESET}")
    print(f"  {C_BOLD}  QEMU will boot Ubuntu Live.{C_RESET}")
    print(f"  {C_BOLD}  In the VM console, run this ONE command:{C_RESET}")
    print()
    print(
        f"  {C_YELLOW}  sudo mkdir -p /mnt/zark && sudo mount /dev/sr1 /mnt/zark "
        f"&& sudo bash /mnt/zark/tests/qemu_setup.sh{C_RESET}",
    )
    print()
    print(f"  {C_BOLD}  Then from your host terminal:{C_RESET}")
    print(
        f"  {C_YELLOW}  ssh -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -p 2222 ubuntu@localhost{C_RESET}",
    )
    print(f"  {C_YELLOW}  # password: ubuntu{C_RESET}")
    print()
    print(f"  {C_BOLD}  Once connected via SSH:{C_RESET}")
    print(f"  {C_YELLOW}  sudo cp -a /mnt/zark /tmp/zark && cd /tmp/zark{C_RESET}")
    print(f"  {C_YELLOW}  sudo ./zark recover{C_RESET}")
    print(f"  {C_YELLOW}  sudo poweroff{C_RESET}")
    print()
    print(f"  {C_BOLD}  Disk mapping inside QEMU:{C_RESET}")
    print(f"    /dev/vda  = target disk  ({TARGET_SIZE_GB}GB) ← recover writes here")
    print(f"    /dev/vdb  = backup drive ({BACKUP_SIZE_GB}GB)")
    print("    /dev/sr0  = Ubuntu Live ISO")
    print("    /dev/sr1  = zark code ISO")
    print(f"  {C_BOLD}{'═' * 58}{C_RESET}")
    print()

    if not ask_yn("Ready to launch QEMU?"):
        info("Skipped. Re-run with --phase 2 when ready.")
        return

    launch_qemu(
        ovmf_code,
        ovmf_vars,
        [
            (target_img, "format=raw,if=virtio,cache=none"),
            (backup_img, "format=raw,if=virtio,cache=none"),
            (iso_path, "media=cdrom"),
            (zark_iso, "media=cdrom,index=1"),
        ],
        boot="d",
    )

    ok("QEMU session ended")
    if ask_yn("Did recovery complete successfully?"):
        ok("Phase 2 complete — target disk ready for boot test")
    else:
        warn("Recovery reported as failed. Re-run with --phase 2 to retry.")
        sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════
#  Phase 3: VERIFY BOOT
# ═════════════════════════════════════════════════════════════════════════


def phase_3(workdir):
    """Boot the recovered target disk in QEMU, user verifies."""
    banner("PHASE 3 — VERIFY BOOT")

    target_img = os.path.join(workdir, TARGET_IMG)
    if not os.path.exists(target_img):
        fail(f"Target image not found: {target_img}\n  " "Run phases 1 and 2 first.")

    ovmf_code, ovmf_vars = find_ovmf(workdir, "_phase3")

    print()
    print(f"  {C_BOLD}{'═' * 58}{C_RESET}")
    print(f"  {C_BOLD}  Booting recovered system from target disk.{C_RESET}")
    print()
    print(f"  {C_BOLD}  Expected sequence:{C_RESET}")
    print("    1. UEFI → GRUB → Linux kernel")
    print("    2. Passphrase prompt (enter the test passphrase)")
    print("    3. System boots to login/desktop")
    print()
    print(f"  {C_BOLD}  Verify inside the VM:{C_RESET}")
    print("    - No emergency shell")
    print("    - ZFS datasets mounted (df -h)")
    print("    - cat /home/testuser/test_data.txt")
    print("    - systemctl --failed")
    print()
    print(f"  {C_BOLD}  When done: poweroff{C_RESET}")
    print(f"  {C_BOLD}{'═' * 58}{C_RESET}")
    print()

    if not ask_yn("Ready to boot?"):
        info("Skipped. Re-run with --phase 3 when ready.")
        return

    launch_qemu(
        ovmf_code,
        ovmf_vars,
        [
            (target_img, "format=raw,if=virtio,cache=none"),
        ],
        boot="c",
    )

    ok("QEMU session ended")
    print()

    # ── Checklist ─────────────────────────────────────────────────────
    print(f"  {C_BOLD}{'═' * 58}{C_RESET}")
    print(f"  {C_BOLD}  INTEGRATION TEST CHECKLIST{C_RESET}")
    print(f"  {C_BOLD}{'═' * 58}{C_RESET}")
    print()

    checks = {
        "boot": "Did the system boot without emergency shell?",
        "passphrase": "Did the passphrase prompt appear and work?",
        "desktop": "Did you reach the desktop or login screen?",
        "data": "Was test data present? (cat /home/testuser/test_data.txt)",
        "services": "Were there 0 failed systemd units?",
    }

    results = {}
    for key, question in checks.items():
        results[key] = ask_yn(question)

    print()
    print(f"  {C_BOLD}{'─' * 58}{C_RESET}")
    all_ok = True
    for key, passed in results.items():
        status = f"{C_GREEN}PASS{C_RESET}" if passed else f"{C_RED}FAIL{C_RESET}"
        if not passed:
            all_ok = False
        print(f"    [{status}]  {key}")
    print(f"  {C_BOLD}{'─' * 58}{C_RESET}")
    print()

    if all_ok:
        print(
            f"  {C_GREEN}{C_BOLD}"
            + "╔══════════════════════════════════════════════════════════╗"
            + f"{C_RESET}",
        )
        print(
            f"  {C_GREEN}{C_BOLD}"
            + f"║  ✅ INTEGRATION TEST PASSED                              ║{C_RESET}",
        )
        print(
            f"  {C_GREEN}{C_BOLD}"
            + f"║  Full create → backup → recover → boot verified.        ║{C_RESET}",
        )
        print(
            f"  {C_GREEN}{C_BOLD}"
            + "╚══════════════════════════════════════════════════════════╝"
            + f"{C_RESET}",
        )
    else:
        print(
            f"  {C_RED}{C_BOLD}"
            + "╔══════════════════════════════════════════════════════════╗"
            + f"{C_RESET}",
        )
        print(
            f"  {C_RED}{C_BOLD}"
            + "║  ❌ INTEGRATION TEST FAILED                              ║"
            + f"{C_RESET}",
        )
        print(
            f"  {C_RED}{C_BOLD}"
            + "║  Check failed items above.                               ║"
            + f"{C_RESET}",
        )
        print(
            f"  {C_RED}{C_BOLD}"
            + "╚══════════════════════════════════════════════════════════╝"
            + f"{C_RESET}",
        )
        sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════
#  Cleanup
# ═════════════════════════════════════════════════════════════════════════


def cleanup(workdir):
    """Remove all test artifacts."""
    banner("CLEANUP")
    if not os.path.exists(workdir):
        ok("Nothing to clean")
        return

    size = sum(
        os.path.getsize(os.path.join(workdir, f))
        for f in os.listdir(workdir)
        if os.path.isfile(os.path.join(workdir, f))
    )

    info(f"Work directory: {workdir}")
    info(f"Total size: {size / (1024**3):.1f} GB")

    if ask_yn("Delete all test files?"):
        shutil.rmtree(workdir)
        ok("Cleaned up")
    else:
        info("Cleanup skipped")


# ═════════════════════════════════════════════════════════════════════════
#  Main
# ═════════════════════════════════════════════════════════════════════════


def main():
    """
    Main entry point: parse arguments, run phases, handle cleanup.
    """
    global QEMU_RAM_MB  # pylint: disable=global-statement
    parser = argparse.ArgumentParser(
        description="zark integration test — full cycle in QEMU (nothing touches host)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Phases (all run inside QEMU):
  1  CREATE + BACKUP  — Build minimal ZFS system, prepare + backup
  2  RECOVER          — Recover from backup to empty target disk
  3  VERIFY           — Boot recovered disk, confirm it works

Examples:
  sudo python3 tests/test_integration.py --iso ubuntu.iso         # all phases
  sudo python3 tests/test_integration.py --phase 1 --iso ubuntu.iso
  sudo python3 tests/test_integration.py --phase 2 --iso ubuntu.iso
  sudo python3 tests/test_integration.py --phase 3
  sudo python3 tests/test_integration.py --cleanup
""",
    )

    parser.add_argument(
        "--workdir",
        default=DEFAULT_WORKDIR,
        help=f"Directory for disk images (default: {DEFAULT_WORKDIR})",
    )
    parser.add_argument("--iso", default=None, help="Path to Ubuntu Live ISO")
    parser.add_argument(
        "--phase",
        type=int,
        choices=[1, 2, 3],
        default=None,
        help="Run a specific phase only",
    )
    parser.add_argument("--cleanup", action="store_true", help="Remove all test files")
    parser.add_argument(
        "--ram",
        type=int,
        default=QEMU_RAM_MB,
        help=f"QEMU RAM in MB (default: {QEMU_RAM_MB})",
    )

    args = parser.parse_args()

    # Apply RAM setting
    QEMU_RAM_MB = args.ram

    # ── Preflight ─────────────────────────────────────────────────────
    if os.geteuid() != 0:
        fail("Must run as root (for KVM access): sudo python3 tests/test_integration.py ...")

    if args.cleanup:
        cleanup(args.workdir)
        return

    if not shutil.which("qemu-system-x86_64"):
        fail("qemu-system-x86_64 not found.\n  Install: sudo apt install qemu-system-x86 ovmf")

    if not any(os.path.exists(p) for p in OVMF_CODE_CANDIDATES):
        fail("OVMF firmware not found.\n  Install: sudo apt install ovmf")

    mkiso = shutil.which("genisoimage") or shutil.which("mkisofs")
    if not mkiso:
        fail("genisoimage not found.\n  Install: sudo apt install genisoimage")

    # ISO required for phases 1 and 2
    needs_iso = args.phase in (None, 1, 2)
    if needs_iso and not args.iso:
        fail(
            "Ubuntu Live ISO required.\n"
            "  Usage: sudo python3 tests/test_integration.py --iso /path/to/ubuntu.iso",
        )
    if needs_iso and not os.path.exists(args.iso):
        fail(f"ISO not found: {args.iso}")

    banner("ZARK INTEGRATION TEST (all inside QEMU)")
    info(f"Work directory: {args.workdir}")
    info(f"Nothing on this machine will be modified (except {args.workdir})")

    os.makedirs(args.workdir, exist_ok=True)

    # ── Run phases ───────────────────────────────────────────────────
    if args.phase is None or args.phase == 1:
        phase_1(args.workdir, args.iso)

    if args.phase is None or args.phase == 2:
        phase_2(args.workdir, args.iso)

    if args.phase is None or args.phase == 3:
        phase_3(args.workdir)


if __name__ == "__main__":
    main()
