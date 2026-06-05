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
Non-destructive drive health checks.

This module inspects kernel/sysfs state for *risk factors* that are known
to threaten ZFS backup integrity on removable drives. It does NOT write to
the device and therefore cannot, by construction, prove a drive is safe —
the lethal property (a USB-SATA bridge that lies about FUA cache flushing)
only manifests when metadata is written under load and read back. What these
checks do is flag the conditions correlated with that failure so the
operator can act (see docs/HARDWARE.md) before trusting the drive.

The design is intentionally a small, declarative list of checks so it can
grow as new problematic hardware is identified: each check is a function
that reads some state and returns a Finding. The destructive,
write-and-verify confirmation lives elsewhere (commands/prepare.py performs
it implicitly, since a full raw send is exactly the write-under-load that
reproduces the failure; a standalone `zark health --destructive` is a
possible future addition).
"""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from lib.log import Log
from lib.sh import run

# ── Known problematic USB-SATA bridges ───────────────────────────────────────
#
# Keyed by "VID:PID" (lowercase hex, as printed by lsusb / sysfs). These are
# bridges observed to misreport cache semantics (FUA), risking pool
# corruption under write load. Kept as a module constant rather than an
# external data file: the list is tiny and changes rarely (YAGNI). Add a new
# entry when a bridge is confirmed problematic, with a short note.
KNOWN_BAD_BRIDGES: dict[str, str] = {
    "0634:5604": (
        "Micron CT2000X10* USB-SATA enclosure — ignores FUA, can corrupt "
        "pools under write load (spacemap loss -> metaslab_init error 52)."
    ),
}

# Severity levels, ordered. WARN is the strongest a non-destructive check can
# emit: we are flagging risk, never asserting certain failure.
OK = "OK"
INFO = "INFO"
WARN = "WARN"


@dataclass
class Finding:
    """One check's result for one drive."""

    level: str  # OK | INFO | WARN
    title: str  # short, e.g. "Cache flush (FUA)"
    detail: str  # human explanation
    see_hardware_doc: bool = False  # point operator at docs/HARDWARE.md


@dataclass
class HealthReport:
    """Aggregate of all findings for a single device."""

    device: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def worst_level(self) -> str:
        """Highest severity present (WARN > INFO > OK)."""
        if any(f.level == WARN for f in self.findings):
            return WARN
        if any(f.level == INFO for f in self.findings):
            return INFO
        return OK

    @property
    def has_risk(self) -> bool:
        """True when at least one WARN finding is present."""
        return self.worst_level == WARN


def _base_disk(dev: str) -> str:
    """Strip a partition suffix to get the base disk name (sda1 -> sda)."""
    name = Path(dev).name
    return re.sub(r"p?\d+$", "", name)


def _usb_ids_for(base: str) -> tuple[str, str]:
    """Return (vendor, product) lowercase hex for a USB-backed block device.

    Walks /sys/block/<base> back to the owning USB device and reads
    idVendor/idProduct. Returns ("", "") for non-USB devices or when the
    attributes cannot be found.
    """
    sys_block = Path(f"/sys/block/{base}")
    if not sys_block.exists():
        return "", ""
    # Resolve the real sysfs path and walk up looking for idVendor/idProduct.
    node = sys_block.resolve()
    for _ in range(12):  # bounded walk up the device tree
        vid = node / "idVendor"
        pid = node / "idProduct"
        if vid.exists() and pid.exists():
            try:
                return (
                    vid.read_text().strip().lower(),
                    pid.read_text().strip().lower(),
                )
            except OSError:
                return "", ""
        if node.parent == node:
            break
        node = node.parent
    return "", ""


def _check_fua(base: str) -> Finding | None:
    """Flag drives whose bridge reports it does not support DPO/FUA.

    Read from the most recent kernel attach line for the device. A bridge
    advertising "doesn't support DPO or FUA" is the precondition for the
    cache-flush-lie failure. This is a risk signal, not proof: an honest
    bridge can also report no-FUA and simply flush synchronously.
    """
    r = run(f"sh -c \"dmesg | grep -F '[{base}]' | tail -40\"")
    if not r.ok or "doesn't support DPO or FUA" not in r.stdout:
        return None
    return Finding(
        level=WARN,
        title="Cache flush (FUA)",
        detail=(
            f"/dev/{base} reports 'doesn't support DPO or FUA'. Bridges with "
            "this property may acknowledge writes from volatile cache before "
            "data reaches NAND, risking pool corruption under load."
        ),
        see_hardware_doc=True,
    )


def _check_transport(base: str) -> Finding | None:
    """Report the USB storage transport (UAS vs usb-storage).

    UAS is faster but, on bridges that mishandle it, stalls under load with
    SCSI aborts/resets — the path that corrupts. usb-storage is the
    conservative transport (e.g. forced by a modprobe quirk). We only emit a
    finding for USB devices; SATA/NVMe are silent here.
    """
    tran = run(f"lsblk -dn -o TRAN /dev/{base}").output
    if tran != "usb":
        return None
    drv = run(
        "sh -c \"dmesg | grep -F 'scsi host' | grep -iE 'uas|usb-storage' | tail -1\"",
    ).output
    if "uas" in drv.lower() and "ignored" not in drv.lower():
        return Finding(
            level=WARN,
            title="USB transport",
            detail=(
                f"/dev/{base} is using the UAS driver. On bridges that "
                "mishandle UAS this stalls under write load. Consider "
                "forcing usb-storage via a modprobe quirk."
            ),
            see_hardware_doc=True,
        )
    return Finding(
        level=INFO,
        title="USB transport",
        detail=f"/dev/{base} is using the usb-storage driver (conservative).",
    )


def _check_known_bridge(base: str) -> Finding | None:
    """Flag the device if its USB bridge VID:PID is in the known-bad list."""
    vid, pid = _usb_ids_for(base)
    if not vid or not pid:
        return None
    key = f"{vid}:{pid}"
    note = KNOWN_BAD_BRIDGES.get(key)
    if note is None:
        return Finding(
            level=INFO,
            title="USB bridge",
            detail=f"Bridge {key} — not in the known-problematic list.",
        )
    return Finding(
        level=WARN,
        title="USB bridge",
        detail=f"Bridge {key} is known-problematic: {note}",
        see_hardware_doc=True,
    )


def check_device(dev: str) -> HealthReport:
    """Run all non-destructive checks against a device path.

    ``dev`` may be a whole disk or a partition; checks normalise to the base
    disk. Findings with no signal are simply omitted.
    """
    base = _base_disk(dev)
    report = HealthReport(device=dev)
    for check in (_check_fua, _check_transport, _check_known_bridge):
        finding = check(base)
        if finding is not None:
            report.findings.append(finding)
    return report


# ── Destructive write-and-verify test ────────────────────────────────────────
#
# A non-destructive check can only flag risk; the FUA-lie is observable only
# under write load followed by a read-back from NAND. This section creates a
# throwaway pool, writes with transaction churn (the pattern that corrupts
# spacemaps), and verifies the pool re-imports. Two depths:
#   - hot:  export -> drop kernel cache -> re-import. The bridge's own DRAM may
#           still satisfy reads, so a pass here is necessary but not sufficient.
#   - cold: export -> eject (power-down) -> operator physically reconnects ->
#           re-import. The bridge lost its state, so reads must come from NAND.
#           This is the only depth that fully reproduces the black incident.

# Profiles: (key, label, target_bytes_or_None, cycles). None target = whole disk.
PROFILE_FAST = "fast"
PROFILE_MEDIUM = "medium"
PROFILE_SURFACE = "surface"

# Target write volume per profile. Surface uses the device size (capped).
_PROFILE_BYTES: dict[str, int | None] = {
    PROFILE_FAST: 2 * 1024**3,  # ~2 GB
    PROFILE_MEDIUM: 15 * 1024**3,  # ~15 GB
    PROFILE_SURFACE: None,  # whole disk (capped by SURFACE_CAP_BYTES)
}
_PROFILE_CYCLES: dict[str, int] = {
    PROFILE_FAST: 2,
    PROFILE_MEDIUM: 4,
    PROFILE_SURFACE: 1,
}
# Hard cap so "surface" on a huge disk does not run unbounded. Operators who
# want the literal whole disk can raise this; default protects against a
# multi-day run by accident.
SURFACE_CAP_BYTES = 500 * 1024**3  # 500 GB
# Empirical slowdown of churn (scattered writes + sync) vs sequential dd.
# Estimates are deliberately pessimistic so the test never overruns its quote.
CHURN_SLOWDOWN_FACTOR = 3.0
_CHUNK_FILE_BYTES = 64 * 1024**2  # 64 MB per written file


@dataclass
class TestResult:
    """Outcome of a destructive write-and-verify run."""

    profile: str
    cold: bool
    bytes_written: int
    write_mb_s: float
    passed: bool
    detail: str = ""


def device_size_bytes(dev: str) -> int:
    """Return the byte size of a block device (0 if unknown)."""
    base = _base_disk(dev)
    r = run(f"lsblk -bdn -o SIZE /dev/{base}")
    if not r.ok or not r.output.isdigit():
        return 0
    return int(r.output)


def profile_target_bytes(profile: str, dev: str) -> int:
    """Resolve a profile to a concrete byte target for this device."""
    target = _PROFILE_BYTES.get(profile)
    if target is not None:
        return target
    # Surface: whole disk, capped.
    size = device_size_bytes(dev)
    if size <= 0:
        return SURFACE_CAP_BYTES
    return min(size, SURFACE_CAP_BYTES)


def measure_write_speed(dev: str, log: Log, sample_bytes: int = 1024**3) -> float:
    """Measure sequential write speed in MB/s by writing a sample to the device.

    DESTRUCTIVE: writes ``sample_bytes`` to the raw device. Caller must have
    already confirmed destruction. Uses ``oflag=direct`` to bypass the kernel
    page cache and ``conv=fdatasync`` so the closing flush is included in the
    timing (the bridge may still absorb some into its DRAM, which is why
    estimates apply CHURN_SLOWDOWN_FACTOR on top). Returns MB/s, or 0.0 on
    failure.
    """
    base = _base_disk(dev)
    count = max(1, sample_bytes // (1024**2))
    start = time.time()
    r = run(
        f"dd if=/dev/zero of=/dev/{base} bs=1M count={count} " "oflag=direct conv=fdatasync",
        log=log,
    )
    elapsed = time.time() - start
    if not r.ok or elapsed <= 0:
        return 0.0
    return (count) / elapsed  # MB / s


def estimate_seconds(profile: str, dev: str, write_mb_s: float) -> int:
    """Estimate test duration in seconds (pessimistic, rounded up)."""
    if write_mb_s <= 0:
        return 0
    target_mb = profile_target_bytes(profile, dev) / (1024**2)
    cycles = _PROFILE_CYCLES.get(profile, 1)
    # Churn rewrites part of the data each cycle; approximate total written as
    # target * (1 + 0.5*(cycles-1)), then apply the slowdown factor.
    total_mb = target_mb * (1 + 0.5 * max(0, cycles - 1))
    return int((total_mb / write_mb_s) * CHURN_SLOWDOWN_FACTOR) + 1


def _write_churn(  # pylint: disable=too-many-locals
    mountpoint: str,
    target_bytes: int,
    cycles: int,
    log: Log,
) -> int:
    """Write incompressible data with transaction churn under ``mountpoint``.

    Returns total bytes written. Each cycle writes files up to a fraction of
    the target, syncs, then deletes half before the next cycle — exercising
    spacemap allocation and free, the pattern that exposes the FUA lie. Stops
    early if writes start failing (a corrupting bridge may error out).
    """
    written = 0
    per_cycle = max(_CHUNK_FILE_BYTES, target_bytes // max(1, cycles))
    chunk_mb = _CHUNK_FILE_BYTES // (1024**2)
    for cycle in range(cycles):
        cycle_written = 0
        idx = 0
        while cycle_written < per_cycle:
            path = f"{mountpoint}/churn_{cycle}_{idx}.bin"
            r = run(
                f"dd if=/dev/urandom of={path} bs=1M count={chunk_mb} "
                "oflag=direct 2>/dev/null || "
                f"dd if=/dev/urandom of={path} bs=1M count={chunk_mb}",
            )
            if not r.ok:
                log.warn(f"Write failed at {written + cycle_written} bytes")
                run("sync")
                return written + cycle_written
            cycle_written += _CHUNK_FILE_BYTES
            idx += 1
            if idx % 4 == 0:
                run("sync")  # force transactions periodically
        run("sync")
        written += cycle_written
        # Delete half to force free-space churn before the next cycle.
        if cycle < cycles - 1:
            for j in range(0, idx, 2):
                run(f"rm -f {mountpoint}/churn_{cycle}_{j}.bin")
            run("sync")
    return written


def run_destructive_test(
    dev: str,
    profile: str,
    cold: bool,
    log: Log,
    *,
    zfs,  # lib.zfs.ZFS — typed loosely to avoid an import cycle
    wait_for_reconnect=None,  # callable() invoked at the cold-reconnect pause
    write_mb_s: float = 0.0,
) -> TestResult:
    """Create a throwaway pool, write with churn, and verify re-import.

    DESTRUCTIVE: the caller must have confirmed and validated an empty device.
    The test pool is always destroyed in the ``finally`` block, even on error,
    so the device is left empty. ``wait_for_reconnect`` is called (cold only)
    after the eject, to let the command prompt the operator to physically
    reconnect; it must block until the drive is back.
    """
    # pylint: disable=too-many-arguments,too-many-locals
    # pylint: disable=too-many-positional-arguments,too-many-return-statements
    base = _base_disk(dev)
    test_pool = f"zarktest_{int(time.time())}"
    mountpoint = f"/run/{test_pool}"
    target = profile_target_bytes(profile, dev)
    cycles = _PROFILE_CYCLES.get(profile, 1)
    bytes_written = 0
    try:
        log.info(f"Creating throwaway test pool '{test_pool}'...")
        r = run(
            f"zpool create -f -o ashift=12 -O atime=off "
            f"-m {mountpoint} {test_pool} /dev/{base}",
            log=log,
        )
        if not r.ok:
            return TestResult(
                profile,
                cold,
                0,
                write_mb_s,
                False,
                f"could not create test pool: {r.stderr.strip()}",
            )

        log.info(f"Writing ~{target // (1024**3)} GB with churn ({cycles} cycle(s))...")
        bytes_written = _write_churn(mountpoint, target, cycles, log)

        log.info("Exporting test pool...")
        run("sync")
        run(f"zpool export {test_pool}", log=log)
        run("sync")

        if cold:
            log.info("Powering the device down (eject)...")
            run(f"eject /dev/{base}", log=log)
            if wait_for_reconnect is not None:
                wait_for_reconnect()  # blocks until operator reconnects

        # Verify re-import. For the cold path the device name may change after
        # reconnect, so import by pool name with a directory scan.
        log.info("Re-importing to verify persistence...")
        run("sync")
        run("sh -c 'echo 3 > /proc/sys/vm/drop_caches'")
        imp = run(
            f"zpool import -o readonly=on -N -d /dev/disk/by-id {test_pool}",
            log=log,
        )
        if not imp.ok:
            return TestResult(
                profile,
                cold,
                bytes_written,
                write_mb_s,
                False,
                f"re-import failed: {imp.stderr.strip()}",
            )
        health = zfs.pool_health(test_pool)
        passed = health == "ONLINE"
        return TestResult(
            profile,
            cold,
            bytes_written,
            write_mb_s,
            passed,
            "ONLINE" if passed else f"health={health}",
        )
    finally:
        # Always destroy the test pool and leave the device empty.
        run(f"zpool import -N -d /dev/disk/by-id {test_pool} 2>/dev/null")
        run(f"zpool destroy -f {test_pool} 2>/dev/null")
        run(f"rm -rf {mountpoint}")
        run("sync")


# ── Diagnostic report ─────────────────────────────────────────────────────────


def generate_report(  # pylint: disable=too-many-locals
    dev: str,
    report: HealthReport,
    test: TestResult | None,
    *,
    obfuscate: bool,
) -> str:
    """Build a self-contained diagnostic report for a GitHub bug report.

    Mirrors the fields of .github/ISSUE_TEMPLATE/bug_report.yml so the
    operator can paste sections straight in. When ``obfuscate`` is True,
    serials and pool GUIDs are redacted.
    """
    base = _base_disk(dev)

    def _redact(text: str) -> str:
        if not obfuscate:
            return text
        # Redact long hex/serial-like tokens.
        return re.sub(r"[0-9A-Fa-f]{8,}", "<redacted>", text)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    zver = run("./zark --version 2>/dev/null || zark --version 2>/dev/null").output
    ubuntu = run("lsb_release -d 2>/dev/null").output
    zfsver = run("zfs version 2>/dev/null").output
    kernel = run("uname -a").output
    model = run(f"lsblk -dn -o MODEL /dev/{base}").output
    size = run(f"lsblk -dn -o SIZE /dev/{base}").output
    tran = run(f"lsblk -dn -o TRAN /dev/{base}").output
    vid, pid = _usb_ids_for(base)
    dmesg = run(
        "sh -c \"dmesg | grep -iE 'sd |uas|usb-storage|synchronize cache|"
        "did_error|did_no_connect|metaslab_init' | tail -40\"",
    ).output

    lines = [
        f"ZARK HEALTH REPORT — {ts}",
        "=" * 60,
        "",
        "## Environment",
        f"zark version : {zver or '?'}",
        f"Ubuntu       : {ubuntu or '?'}",
        f"ZFS          : {zfsver or '?'}",
        f"Kernel       : {kernel or '?'}",
        "",
        "## Drive",
        f"Device       : {dev}",
        f"Model        : {model or '?'}",
        f"Size         : {size or '?'}",
        f"Transport    : {tran or '?'}",
        f"USB bridge   : {vid}:{pid}" if vid else "USB bridge   : (not USB)",
        "",
        "## Health findings",
    ]
    if report.findings:
        for f in report.findings:
            lines.append(f"[{f.level}] {f.title}: {f.detail}")
    else:
        lines.append("(none)")

    if test is not None:
        lines += [
            "",
            "## Destructive test",
            f"Profile      : {test.profile}",
            f"Mode         : {'cold (with reconnect)' if test.cold else 'hot'}",
            f"Bytes written: {test.bytes_written}",
            f"Write speed  : {test.write_mb_s:.0f} MB/s",
            f"Result       : {'PASS' if test.passed else 'FAIL'}",
            f"Detail       : {test.detail}",
        ]

    lines += [
        "",
        "## dmesg (relevant tail)",
        _redact(dmesg) if dmesg else "(none)",
        "",
        "## How to report this",
        "If this looks like a hardware quirk zark should learn about, please",
        "open an issue using the Bug report template:",
        "  https://github.com/juanmitaboada/zark/issues/new/choose",
        "Paste this file's contents. The USB bridge VID:PID above is the most",
        "useful field for cataloguing problematic enclosures.",
        "For security-sensitive reports, see SECURITY.md instead.",
    ]
    if not obfuscate:
        lines += [
            "",
            "(Note: this report may contain a drive serial. Redact it before",
            "sharing if you prefer — re-run and choose to obfuscate.)",
        ]
    return "\n".join(lines)


def write_report_file(content: str) -> str:
    """Write a report to /tmp and return its path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = f"/tmp/zark-health-report-{ts}.txt"
    Path(path).write_text(content, encoding="utf-8")
    return path


def render_report(report: HealthReport, log: Log) -> None:
    """Print a health report via the logger, one line per finding."""
    if not report.findings:
        log.ok(f"{report.device}: no risk factors detected")
        return
    for f in report.findings:
        line = f"{f.title}: {f.detail}"
        if f.level == WARN:
            log.warn(line)
        elif f.level == INFO:
            log.info(line)
        else:
            log.ok(line)
    if any(f.see_hardware_doc for f in report.findings):
        log.info("See docs/HARDWARE.md for details and mitigations.")
