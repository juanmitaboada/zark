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
zark health — interactive drive analysis and debugging tool.

Two modes, both chosen interactively:

  * read-only (default) — inspects kernel/sysfs state for risk factors
    (no-FUA bridge, UAS transport, known-problematic bridge). Writes
    nothing; flags risk, cannot prove a drive safe.

  * destructive — creates a throwaway pool, writes with transaction churn,
    and verifies the pool re-imports. This reproduces the USB-SATA FUA-lie
    failure under load. Optionally adds a cold pass: power the device down,
    wait for the operator to physically reconnect, then re-import (the only
    check that reads back strictly from NAND).

All choices are gathered up front; execution is then unattended except for
the single cold-reconnect pause. Whenever any risk or test failure is found,
a self-contained report is written for filing a GitHub issue.
"""

from pathlib import Path

from lib import sh
from lib.config import Config
from lib.drives import scan_connected_drives, validate_external_block_device
from lib.health import (
    PROFILE_FAST,
    PROFILE_MEDIUM,
    PROFILE_SURFACE,
    check_device,
    device_size_bytes,
    estimate_seconds,
    generate_report,
    measure_write_speed,
    render_report,
    run_destructive_test,
    write_report_file,
)
from lib.log import Log
from lib.zfs import ZFS

_PROFILES = [PROFILE_FAST, PROFILE_MEDIUM, PROFILE_SURFACE]


def _fmt_duration(seconds: int) -> str:
    """Human-friendly duration, rounded up and pessimistic."""
    if seconds <= 0:
        return "unknown"
    if seconds < 90:
        return f"~{seconds}s"
    mins = seconds // 60
    if mins < 90:
        return f"~{mins} min"
    hours = mins / 60
    return f"~{hours:.1f} h"


def _usb_disks() -> list[tuple[str, str]]:
    """List connected USB whole-disks as (device_path, label).

    Independent of the pool/registry scan, so blank disks (the ones a
    destructive test actually wants) are discoverable. Returns the base
    device path (e.g. /dev/sda) and a short human label.
    """
    out = sh.run(
        "lsblk -dn -o NAME,TRAN,MODEL,SIZE",
    ).output
    disks: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 2 or parts[1] != "usb":
            continue
        name = parts[0]
        model = parts[2] if len(parts) > 2 else "?"
        size = parts[3] if len(parts) > 3 else "?"
        disks.append((f"/dev/{name}", f"{name}  ({model}, {size}) [USB disk]"))
    return disks


def _select_device(args: list[str], log: Log) -> str:
    """Resolve the device to analyse, interactively if needed.

    Combines pool/registry drives with bare connected USB disks, so a blank
    enclosure (which has no pool and is not registered) can still be picked —
    that is exactly the device a destructive evaluation targets.
    """
    if args and not args[0].startswith("-"):
        target = args[0]
        if not Path(target).exists():
            log.fatal(f"{target} does not exist")
        return target

    cfg = Config.load()
    devs: list[str] = []
    labels: list[str] = []
    seen: set[str] = set()

    # Registered / pool-bearing drives first (richer labels).
    for d in scan_connected_drives(cfg, log):
        dev = d.dev_path
        if not dev and d.drive_id and d.drive_id != "<unknown>":
            by_id = Path(f"/dev/disk/by-id/{d.drive_id}")
            if by_id.exists():
                dev = str(by_id.resolve())
        if dev:
            real = str(Path(dev).resolve())
            seen.add(real)
            devs.append(dev)
            labels.append(f"{d.name}  ({d.model or d.drive_id}, {d.size})")

    # Then any bare USB disks not already covered above.
    for dev, label in _usb_disks():
        if str(Path(dev).resolve()) not in seen:
            devs.append(dev)
            labels.append(label)

    if not devs:
        log.fatal("No connected drives found to check")
    if len(devs) == 1:
        log.ok(f"Using drive: {labels[0]}")
        return devs[0]
    idx = log.ask_choice("Which drive do you want to check?", labels)
    return devs[idx]


def run(args: list[str]) -> None:
    """Main entry point for the health command."""
    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    log = Log()
    zfs = ZFS(log)
    log.banner("DRIVE HEALTH CHECK")

    dev = _select_device(args, log)

    # ── Phase 1: gather all choices up front ─────────────────────────────
    mode_idx = log.ask_choice(
        "Analysis mode?",
        ["read-only (writes nothing)", "destructive (ERASES the drive)"],
        default=0,
    )
    destructive = mode_idx == 1

    profile = PROFILE_FAST
    cold = False
    write_mb_s = 0.0

    # Asked in both modes: a diagnostic report can be generated either way
    # (on any risk or test failure), and it may carry a drive serial/GUID, so
    # the operator must always get the chance to redact before it lands in a
    # file they might paste into a public issue.
    obfuscate = log.ask("Obfuscate serial/GUID in the diagnostic report?")

    if destructive:
        validate_external_block_device(dev, log, command="health")
        # Refuse if not empty.
        parts = sh.run(f"lsblk -no NAME {dev} | tail -n +2").output
        sigs = sh.run(f"blkid {dev}").output
        if parts or sigs:
            log.fatal(
                "Drive is NOT empty — destructive test needs a blank drive",
                solutions=[f"Wipe first: sudo ./zark purge {dev}"],
            )
        # Confirm destruction BEFORE writing anything (incl. the speed probe).
        if not log.ask(f"This will DESTROY all data on {dev}. Continue?"):
            log.info("Aborted")
            return

        # Now safe to write: measure speed, then present profiles with ETAs.
        log.info("Measuring write speed (brief)...")
        write_mb_s = measure_write_speed(dev, log)
        if write_mb_s > 0:
            log.ok(f"Approx. write speed: {write_mb_s:.0f} MB/s")
        else:
            log.warn("Could not measure speed; estimates unavailable")

        size = device_size_bytes(dev)
        opts = []
        for p in _PROFILES:
            eta = _fmt_duration(estimate_seconds(p, dev, write_mb_s))
            if p == PROFILE_SURFACE:
                opts.append(
                    f"surface — whole disk (~{size // 1024**3} GB), {eta}  "
                    "[tests the MEDIUM, not the bridge; slow]",
                )
            elif p == PROFILE_FAST:
                opts.append(f"fast — ~2 GB churn, {eta}")
            else:
                opts.append(f"medium — ~15 GB churn, {eta}")
        profile = _PROFILES[log.ask_choice("Test profile?", opts, default=0)]

        cold = log.ask(
            "Add a COLD verification (power down, you reconnect, re-import)?",
        )

    # ── Phase 2: unattended execution ────────────────────────────────────
    log.info(f"Checking {dev}...")
    report = check_device(dev)
    render_report(report, log)

    test = None
    if destructive:

        def _wait_for_reconnect() -> None:
            log.warn(
                "Device powered down. Physically UNPLUG it, wait ~3s, "
                "plug it back in, then press Enter.",
            )
            try:
                input("    Press Enter once reconnected... ")
            except EOFError:
                pass

        test = run_destructive_test(
            dev,
            profile,
            cold,
            log,
            zfs=zfs,
            wait_for_reconnect=_wait_for_reconnect if cold else None,
            write_mb_s=write_mb_s,
        )
        if test.passed and not test.transport_errors:
            log.ok(f"Destructive test PASSED ({test.detail})")
        elif test.passed and test.transport_errors:
            # Data survived, but the bus logged errors — a strong caution.
            log.warn(f"Destructive test PASSED WITH TRANSPORT ERRORS: {test.detail}")
            for line in test.transport_errors[:5]:
                log.warn(f"  {line}")
            log.warn(
                "The data read back ONLINE this time, but the enclosure is "
                "failing at the bus level and may corrupt on another run. "
                "See docs/HARDWARE.md (consider a usb-storage quirk).",
            )
        else:
            log.banner_error(
                "DESTRUCTIVE TEST FAILED",
                [
                    "The drive did not read back correctly after writing.",
                    f"Detail: {test.detail}",
                    "",
                    "This is consistent with a bridge that lies about cache",
                    "flushing. Do NOT use this enclosure for backups as-is.",
                    "See docs/HARDWARE.md.",
                ],
            )

    # ── Phase 3: report on any risk or failure ───────────────────────────
    needs_report = report.has_risk or (
        test is not None and (not test.passed or test.transport_errors)
    )
    if needs_report:
        content = generate_report(dev, report, test, obfuscate=obfuscate)
        path = write_report_file(content)
        log.info(f"Diagnostic report written to: {path}")
        log.info(
            "If this is a hardware quirk zark should learn about, please file "
            "it: https://github.com/juanmitaboada/zark/issues/new/choose",
        )
    else:
        log.ok("No risk factors detected.")
