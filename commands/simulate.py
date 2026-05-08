"""
zark simulate — Boot a recovered (or arbitrary) disk in QEMU/KVM.

Tests the boot chain without physically rebooting. Uses OVMF (UEFI
firmware) so the EFI boot path is exercised identically to real hardware.

Usage:
  sudo ./zark simulate                # ro by default; you choose the disk
  sudo ./zark simulate /dev/sdX       # ro by default; targets /dev/sdX
  sudo ./zark simulate /dev/sdX --rw  # WRITES to /dev/sdX (confirm by path)
  sudo ./zark simulate --display 3840x2160   # custom QEMU resolution

Safety model (in order of precedence):

  1. Discs in use are NEVER touched. A disk is considered in use when any
     of these is true:
       - it carries the live system's root partition,
       - any of its partitions is currently mounted,
       - any of its partitions belongs to a pool currently imported,
       - any of its partitions is an active swap.
     Such disks are excluded from the candidate list AND, if explicitly
     named on the command line, refused with a fatal error. Neither
     --ro nor --rw bypasses this.

  2. No silent auto-detection. With no /dev/... argument, simulate lists
     the eligible disks and prompts for one. To choose without prompt,
     pass the path explicitly.

  3. Read-only by default. Without --rw, QEMU runs with -snapshot: every
     write the guest performs goes to a temporary overlay that is
     discarded on exit. The backing disk is never modified by the guest.

  4. --rw is opt-in and noisy. It prints a large banner naming the disk
     and asks the operator to type the disk path verbatim to confirm
     (typing 'YES' is not enough — re-typing the path forces re-reading
     it).

Requirements:
  apt install qemu-system-x86 ovmf

GL acceleration is auto-detected. When the host has virtio-vga-gl
available and a /dev/dri/renderD* node, simulate uses virtio-vga-gl +
gtk,gl=on for crisp, scalable, hardware-accelerated rendering.
Otherwise it falls back to virtio-vga + EDID with a warning.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
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


# ── GL acceleration detection ───────────────────────────────────────────
#
# virtio-vga without OpenGL passthrough produces a poor experience: the
# guest cannot negotiate a modern resolution via virtio-gpu's DRM/KMS
# path and falls back to a legacy VGA mode (typically 1024×768 or
# smaller). The framebuffer is then drawn into a corner of the GTK
# window, the rest staying black, with no scaling. virtio-vga-gl plus
# `-display gtk,gl=on` activates virgl: the guest's framebuffer is
# delivered over OpenGL, GTK scales it to the window, and the guest
# sees a real GPU with full virtio-gpu DRM support (so it picks up the
# advertised EDID resolution from the first frame).
#
# Three things must be true on the host for GL to actually work:
#   1. The QEMU build must include `virtio-vga-gl`. Most distros do,
#      but minimal/server qemu builds may strip it.
#   2. A render node must exist (/dev/dri/renderD128). Without it
#      virgl cannot get a GPU context — virtio-vga-gl is built but
#      QEMU will refuse to instantiate it.
#   3. Mesa userland must be present so the GTK frontend can talk to
#      the GPU. Almost always true on a desktop Ubuntu, but not
#      guaranteed on a headless rescue system.
#
# We detect (1) and (2) cheaply. (3) only fails on hosts that lack a
# desktop entirely, in which case `-display gtk` itself would fail
# anyway, so we don't add a separate probe for it.


def _detect_gl() -> tuple[bool, str]:
    """Detect whether QEMU + host can run virtio-vga-gl.

    Returns (gl_supported, reason). When False, `reason` is a short
    human-readable explanation suitable for a warn-level log line.
    """
    # Render node must exist. /dev/dri/renderD128 is the canonical
    # primary GPU render node on Linux; subsequent GPUs get D129+.
    # We accept any renderD12N entry — virgl just needs at least one.
    dri = Path("/dev/dri")
    if not dri.is_dir() or not any(p.name.startswith("renderD") for p in dri.iterdir()):
        return False, "no /dev/dri/renderD* node (no GPU available to virgl)"

    # QEMU build must list virtio-vga-gl in its device catalog. The
    # device is included in upstream builds since QEMU 2.6; the only
    # realistic failure mode is a minimal/server build that strips it.
    # `-device help` exits 0 and dumps the catalog to stdout. We grep
    # for an exact word boundary to avoid false-positives on
    # `virtio-vga` (the non-GL device) or future renames.
    r = sh.run("qemu-system-x86_64 -device help")
    if not r.ok:
        return False, "qemu-system-x86_64 -device help failed"
    if "virtio-vga-gl" not in r.stdout:
        return False, "qemu build lacks virtio-vga-gl (minimal/server build?)"

    return True, ""


# ── Safety: detect disks in use by the running host ─────────────────────


def _root_disk() -> str:
    """Return the whole-disk device backing the live '/' filesystem.

    Examples: /dev/nvme0n1 (when / is on nvme0n1p3), /dev/sda (when /
    is on sda1). Returns "" if it cannot be determined — the caller
    should treat that as "unknown, be conservative".
    """
    r = sh.run("findmnt -no SOURCE /")
    src = r.output.strip() if r.ok else ""
    if not src:
        return ""
    # ZFS root: SOURCE looks like 'rpool/ROOT/ubuntu_xxx' — convert
    # the imported pool to its backing disk via zpool status.
    if "/" in src and not src.startswith("/dev/"):
        pool = src.split("/", 1)[0]
        return _disk_of_pool(pool)
    # Block-device root (LUKS or plain): walk up from the partition
    # via lsblk to the parent disk.
    return _parent_disk(src)


def _disk_of_pool(pool: str) -> str:
    """Return the whole-disk device backing the given imported pool, or ""."""
    r = sh.run(f"zpool status -P {pool}")
    if not r.ok:
        return ""
    for line in r.lines:
        # Lines listing a vdev start with whitespace and contain a path
        # like /dev/disk/by-id/... or /dev/nvme0n1p4.
        parts = line.split()
        if not parts:
            continue
        cand = parts[0]
        if cand.startswith("/dev/"):
            return _parent_disk(cand)
    return ""


def _parent_disk(part_or_disk: str) -> str:
    """Walk lsblk up to the parent disk device for a partition path."""
    # lsblk -no PKNAME gives the parent device name (empty for whole disks)
    r = sh.run(f"lsblk -no PKNAME {part_or_disk}")
    pkname = r.output.strip() if r.ok else ""
    if pkname:
        return f"/dev/{pkname}"
    # Already a whole disk (or lsblk failed). Resolve symlinks for safety.
    try:
        return str(Path(part_or_disk).resolve())
    except OSError:
        return part_or_disk


def _disk_partitions(disk: str) -> list[str]:
    """Return the partition device paths under the given whole-disk device."""
    r = sh.run(f"lsblk -lnp -o NAME,TYPE {disk}")
    if not r.ok:
        return []
    parts: list[str] = []
    for line in r.lines:
        cols = line.split()
        if len(cols) >= 2 and cols[1] == "part":
            parts.append(cols[0])
    return parts


def _mounted_partitions(disk: str) -> list[str]:
    """Return partitions of `disk` that are currently mounted."""
    parts = _disk_partitions(disk)
    if not parts:
        return []
    r = sh.run("findmnt -rno SOURCE")
    sources = set(r.lines) if r.ok else set()
    return [p for p in parts if p in sources]


def _swap_partitions(disk: str) -> list[str]:
    """Return partitions of `disk` that are active swap."""
    parts = _disk_partitions(disk)
    if not parts:
        return []
    r = sh.run("cat /proc/swaps")
    if not r.ok:
        return []
    active: set[str] = set()
    for line in r.lines:
        # Format: "Filename  Type  Size  Used  Priority"; skip header
        cols = line.split()
        if cols and cols[0].startswith("/dev/"):
            active.add(cols[0])
    return [p for p in parts if p in active]


def _zfs_imported_partitions(disk: str) -> list[str]:
    """Return partitions of `disk` that belong to an imported ZFS pool."""
    parts = _disk_partitions(disk)
    if not parts:
        return []
    # Get all imported-pool vdev paths in one call. zpool status -P prints
    # absolute device paths for every vdev.
    r = sh.run("zpool status -P")
    if not r.ok:
        return []
    in_use: set[str] = set()
    for line in r.lines:
        cols = line.split()
        if cols and cols[0].startswith("/dev/"):
            in_use.add(cols[0])
    return [p for p in parts if p in in_use]


def _disk_in_use_reasons(disk: str) -> list[str]:
    """Return human-readable reasons the given disk is in use, or []."""
    reasons: list[str] = []

    root = _root_disk()
    if root and Path(root).resolve() == Path(disk).resolve():
        reasons.append(f"carries the live system root ({root})")

    mounted = _mounted_partitions(disk)
    if mounted:
        reasons.append(f"has mounted partitions: {', '.join(mounted)}")

    zfs_in_use = _zfs_imported_partitions(disk)
    if zfs_in_use:
        reasons.append(
            f"has partitions in imported ZFS pools: {', '.join(zfs_in_use)}",
        )

    swap = _swap_partitions(disk)
    if swap:
        reasons.append(f"has active swap on: {', '.join(swap)}")

    return reasons


# ── Disk enumeration & selection ────────────────────────────────────────


def _list_candidate_disks() -> list[tuple[str, str]]:
    """Return [(device, description)] for whole disks NOT in use.

    Description is the lsblk line (size + model) so the operator has
    enough context to choose. Disks whose state is uncertain are kept
    out of the list — better fewer choices than a wrong one.
    """
    r = sh.run("lsblk -dn -o NAME,SIZE,MODEL")
    if not r.ok:
        return []
    candidates: list[tuple[str, str]] = []
    for line in r.lines:
        cols = line.split(None, 2)
        if not cols:
            continue
        name = cols[0]
        # Skip zd* (ZFS volumes) and loops
        if name.startswith(("zd", "loop")):
            continue
        dev = f"/dev/{name}"
        if not Path(dev).exists():
            continue
        if _disk_in_use_reasons(dev):
            continue
        candidates.append((dev, line.strip()))
    return candidates


# ── Argument parsing ────────────────────────────────────────────────────


# Default QEMU virtio-vga resolution. Targets mainstream modern monitors
# (the typical ZFS-on-Ubuntu 24.04+ machine has at least 1080p, often
# higher). Override per-run with `--display WxH`. We don't ship 1080p
# as the default because 2560×1440 feels right on the 4K screens
# common on developer hardware and still fits cleanly inside any
# monitor down to 1440p; on a 1366×768 laptop QEMU's GTK display
# scrolls/scales the window so it does not overflow.
DEFAULT_DISPLAY_W = 2560
DEFAULT_DISPLAY_H = 1440


@dataclass
class SimulateArgs:
    """Parsed command-line arguments for `zark simulate`."""

    disk: str | None = None
    rw: bool = False
    display_w: int = DEFAULT_DISPLAY_W
    display_h: int = DEFAULT_DISPLAY_H


def _parse_display(spec: str) -> tuple[int, int]:
    """Parse a 'WxH' string into (W, H). Raises ValueError if malformed."""
    if "x" not in spec.lower():
        raise ValueError(f"Display spec must be WxH, got: {spec!r}")
    w_s, h_s = spec.lower().split("x", 1)
    w, h = int(w_s), int(h_s)
    if w <= 0 or h <= 0:
        raise ValueError(f"Display dimensions must be positive: {spec!r}")
    # Coherence bounds — virtio-vga refuses obscene values and the
    # operator probably typo'd if they reach here.
    if w > 7680 or h > 4320:
        raise ValueError(f"Display dimensions exceed 8K (7680x4320): {spec!r}")
    return w, h


def _parse_args(args: list[str]) -> SimulateArgs:
    """Parse simulate's command-line arguments.

    Recognised arguments (all optional, any order):
      /dev/...        target disk (path, must start with /dev/)
      --rw            allow writes to the target disk (opt-in)
      --ro            accepted silently for backwards compatibility
                      (read-only is the default now, --ro is a no-op)
      --display WxH   QEMU virtio-vga resolution (default 2560x1440)

    Unknown flags are ignored — defensive against future additions and
    consistent with the rest of the codebase which never argparses
    rigidly.
    """
    parsed = SimulateArgs()
    parsed.rw = "--rw" in args
    parsed.disk = next((a for a in args if a.startswith("/dev/")), None)

    # --display WxH (two-token form). We avoid argparse to stay
    # consistent with the project's lightweight CLI style.
    for i, a in enumerate(args):
        if a == "--display" and i + 1 < len(args):
            try:
                parsed.display_w, parsed.display_h = _parse_display(args[i + 1])
            except ValueError as e:
                # Caller (run()) will surface this — we keep the
                # parser pure of side effects (no logging here).
                raise ValueError(f"Invalid --display: {e}") from e
            break

    return parsed


# ── Confirmation flow for --rw ──────────────────────────────────────────


def _confirm_rw_or_abort(disk: str, log: Log) -> None:
    """Print a loud warning and require the operator to retype the disk path.

    Typing 'YES' would be too cheap — by re-typing the actual /dev path,
    the operator is forced to read it again. If the disk was wrong, this
    is the moment they notice.
    """
    log.blank()
    log.warn("=" * 60)
    log.warn(f"  --rw requested: QEMU will WRITE to {disk}")
    log.warn("  Every write the guest performs hits the real device.")
    log.warn("  UEFI boot variables, filesystem journals, and any user")
    log.warn("  changes inside the simulated boot will PERSIST.")
    log.warn("  This is intended for: writing back boot variables to a")
    log.warn("  spare/recovered disk you are deliberately modifying.")
    log.warn("=" * 60)
    log.blank()
    log.info(f"  To confirm, retype the disk path verbatim: {disk}")
    try:
        typed = input("  > ").strip()
    except EOFError:
        typed = ""
    if typed != disk:
        log.fatal(
            "Aborted — typed path did not match the target disk",
            causes=[f"You typed: {typed!r}", f"Expected:  {disk!r}"],
            solutions=[
                "Re-run and type the path exactly, or omit --rw to use",
                "the safe read-only default.",
            ],
        )


# ── QEMU lifecycle ──────────────────────────────────────────────────────


def _install_deps(log: Log):
    """Install qemu-system-x86 + ovmf via apt-get, isolating stdin.

    apt-get is spawned with stdin=DEVNULL so it cannot consume the
    parent's terminal stdin. Otherwise apt's interactive prompt
    machinery (or any leftover from non-interactive runs) leaves the
    parent shell's stdin in a state where the next input() call
    immediately receives EOF — making the disk-selection prompt
    silently abort with "invalid selection".
    """
    log.info("Installing qemu-system-x86 and ovmf...")
    cmd = ["apt-get", "install", "-y", "qemu-system-x86", "ovmf"]
    log.dbg(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        log.fatal("apt-get not found — install QEMU and OVMF manually")
    if result.returncode != 0:
        log.fatal("Failed to install QEMU/OVMF — install manually and retry")


def run(
    args: list[str],
):  # pylint: disable=too-many-branches,too-many-statements,too-many-locals
    """Main entry point for "zark simulate" command."""
    log = Log()

    log.banner("SIMULATE BOOT", "Boot a target disk in QEMU/KVM")

    # ── Parse arguments ──────────────────────────────────────────────────
    try:
        opts = _parse_args(args)
    except ValueError as e:
        log.fatal(
            f"Bad arguments: {e}",
            solutions=[
                "Use --display WxH with positive integers (e.g. --display 1920x1080).",
                "Default is 2560x1440 if you omit the flag entirely.",
            ],
        )
    disk_arg, rw_requested = opts.disk, opts.rw

    # ── Check for QEMU ───────────────────────────────────────────────────
    # Use `which` rather than `command -v`: the latter is a shell builtin
    # and sh.run() chooses shell=False when there are no shell metachars,
    # so `command` would be exec'd as if it were a binary and always fail
    # with rc=127. `which` is a real /usr/bin executable and behaves
    # consistently across that decision.
    if not sh.run("which qemu-system-x86_64").ok:
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

    # ── Detect GL acceleration (virtio-vga-gl) ───────────────────────────
    # When available, the guest gets a real virtio-gpu DRM device and
    # the GTK frontend scales the framebuffer to the window. When not,
    # we fall back to virtio-vga + EDID, which works but typically
    # leaves the guest stuck in a small VGA mode rendered into a
    # corner of the window.
    gl_ok, gl_reason = _detect_gl()
    if gl_ok:
        log.ok("GL acceleration available — using virtio-vga-gl + gtk,gl=on")
    else:
        log.warn(f"GL acceleration unavailable — falling back to software rendering ({gl_reason})")

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

    # ── Select disk: explicit, or interactive among elegible ─────────────
    target_disk: str
    if disk_arg:
        if not Path(disk_arg).exists():
            log.fatal(f"Disk not found: {disk_arg}")
        # SAFETY LAYER 1: never touch a disk in use, no matter what.
        reasons = _disk_in_use_reasons(disk_arg)
        if reasons:
            log.fatal(
                f"Refusing to simulate against {disk_arg} — disk is in use",
                causes=reasons,
                solutions=[
                    "Use a different disk (a recovered USB, a target.img,",
                    "or another physical disk that is not part of the",
                    "running system).",
                ],
            )
        target_disk = disk_arg
    else:
        # SAFETY LAYER 2: list only eligible disks and require selection.
        candidates = _list_candidate_disks()
        if not candidates:
            log.fatal(
                "No eligible disks for simulation",
                causes=[
                    "All disks on this system are in use (mounted, swap,",
                    "ZFS-imported, or backing the live root).",
                ],
                solutions=[
                    "Connect a spare disk (e.g. a recovered USB), or run",
                    "this command from a live USB to simulate the installed",
                    "system safely.",
                ],
            )
        log.info("Eligible disks (in-use disks are not listed):")
        for i, (_dev, desc) in enumerate(candidates):
            log.raw(f"  {log.W}{i + 1}{log.N}) {desc}")
        try:
            sel = input(f"  Select disk [1-{len(candidates)}]: ").strip()
        except EOFError:
            sel = ""
        if not sel.isdigit() or not 1 <= int(sel) <= len(candidates):
            log.fatal("Aborted — invalid selection")
        target_disk = candidates[int(sel) - 1][0]

    log.ok(f"Target disk: {target_disk}")

    if not os.access(target_disk, os.R_OK):
        log.fatal(
            f"Cannot read {target_disk}",
            causes=["Run as root: sudo ./zark simulate"],
        )

    # ── Read-only by default; --rw is opt-in and confirmed by path ───────
    if rw_requested:
        _confirm_rw_or_abort(target_disk, log)
        use_snapshot = False
        log.warn(f"--rw confirmed: writes to {target_disk} will PERSIST")
    else:
        use_snapshot = True
        log.ok(f"Read-only mode (default): {target_disk} will NOT be modified")
        log.dbg("QEMU runs with -snapshot; guest writes go to a temporary overlay")

    # ── Build QEMU command ───────────────────────────────────────────────
    disk_options = f"file={target_disk},format=raw,if=virtio"

    # Per-PID serial log file. Replaces -serial mon:stdio so QEMU does
    # not steal the parent terminal's TTY: with mon:stdio the guest's
    # kernel writes ANSI escape sequences (clear, color, cursor moves)
    # to the same TTY the operator is staring at, leaving it in a
    # corrupt state both during and after the run. Routing serial to a
    # file keeps the parent terminal intact and still preserves the
    # kernel boot log for inspection.
    serial_log = f"/tmp/zark_simulate_{os.getpid()}.log"

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
        # Display. Two paths depending on host capability:
        #
        # GL path (virtio-vga-gl + gtk,gl=on): the guest gets a real
        # virtio-gpu DRM device, negotiates a modern resolution, and
        # the GTK frontend scales its framebuffer to the window via
        # OpenGL. Fast, crisp, resizable.
        #
        # Software path (virtio-vga + EDID + plain gtk): no GL on the
        # host. We still set edid=on so the guest sees an advertised
        # preferred resolution, but in practice many guest kernels
        # ignore the EDID without a working DRM/KMS path and stay in
        # a legacy VGA mode. Functional for boot-chain validation;
        # cosmetically poor.
        #
        # xres/yres are silently ignored unless edid=on is also set:
        # QEMU only feeds them through the (synthetic) EDID block.
        "-device",
        (
            f"virtio-vga-gl,edid=on,xres={opts.display_w},yres={opts.display_h}"
            if gl_ok
            else f"virtio-vga,edid=on,xres={opts.display_w},yres={opts.display_h}"
        ),
        "-display",
        "gtk,gl=on" if gl_ok else "gtk",
        # Boot menu (F12 to open, shows EFI boot entries)
        "-boot",
        "menu=on",
        # Serial console to a file — see serial_log comment above
        "-serial",
        f"file:{serial_log}",
    ]

    if kvm_ok:
        cmd.insert(1, "-enable-kvm")

    if use_snapshot:
        cmd.append("-snapshot")

    log.blank()
    log.info("Starting QEMU...")
    log.info(
        f"Disk: {log.W}{target_disk}{log.N}  "
        f"({'read-only (overlay)' if use_snapshot else 'READ-WRITE'})",
    )
    log.info(f"KVM:  {log.W}{'enabled' if kvm_ok else 'disabled (slow)'}{log.N}")
    log.info(f"RAM:  {log.W}4 GB{log.N}  CPUs: {log.W}4{log.N}")
    log.info(
        f"Display: {log.W}{opts.display_w}x{opts.display_h}{log.N}  "
        f"({log.W}{'GL accelerated' if gl_ok else 'software (no GL)'}{log.N})",
    )
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

    # Surface the serial log path so the operator can inspect kernel
    # output if anything went wrong inside the guest. The file is left
    # in /tmp deliberately: tmpfs cleanup on next reboot, and the
    # operator may want to diff successive runs.
    if Path(serial_log).exists():
        log.info(f"Kernel/serial log saved at: {serial_log}")

    log.ok("Simulation ended")
