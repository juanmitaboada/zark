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
the device and therefore cannot, by construction, prove a drive is safe -
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

import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lib.identity import (
    BY_ID_DIR,
    by_id_names,
    preferred_by_id,
    read_pool_label,
    usb_ids,
    whole_disk,
)
from lib.log import Log
from lib.sh import part, run
from lib.zfs import backup_altroot, parse_damage

# ── Known problematic USB-SATA bridges ───────────────────────────────────────
#
# Keyed by "VID:PID" (lowercase hex, as printed by lsusb / sysfs). These are
# bridges observed to misreport cache semantics (FUA), risking pool
# corruption under write load. Kept as a module constant rather than an
# external data file: the list is tiny and changes rarely (YAGNI). Add a new
# entry when a bridge is confirmed problematic, with a short note.
KNOWN_BAD_BRIDGES: dict[str, str] = {
    "0634:5604": (
        "Micron CT2000X10PROSSD9 USB-SATA enclosure - ignores FUA, can "
        "corrupt pools under write load (spacemap loss -> metaslab_init "
        "error 52). Use a usb-storage quirk to disable UAS."
    ),
    "0634:5607": (
        "Micron CT2000X10SSD9 USB-SATA enclosure - same vendor as 0634:5604, "
        "also reports no FUA and defaults to UAS; shows DID_ERROR on the "
        "disconnect cache flush. Treat as corruption-prone under write load; "
        "use a usb-storage quirk to disable UAS."
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
    """Kernel name of the whole disk holding ``dev`` (any spelling, by-id included)."""
    disk = whole_disk(dev)
    return Path(disk).name if disk else Path(dev).name


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
    # INFO, not WARN (M2P.12, M4a-12): most USB bridges report this, and an
    # honest one flushes synchronously; only the known-bad list warns.
    return Finding(
        level=INFO,
        title="Cache flush (FUA)",
        detail=(
            f"/dev/{base} reports 'doesn't support DPO or FUA', as most USB "
            "bridges do. Harmless on an honest bridge; the known-problematic "
            "ones are listed separately."
        ),
        see_hardware_doc=True,
    )


def _check_transport(base: str) -> Finding | None:
    """Report the USB storage transport (UAS vs usb-storage).

    UAS is faster but, on bridges that mishandle it, stalls under load with
    SCSI aborts/resets - the path that corrupts. usb-storage is the
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
        # INFO (M4a-12): UAS is the normal driver; a bridge that mishandles
        # it is flagged by the known-bad list, not by the driver alone.
        return Finding(
            level=INFO,
            title="USB transport",
            detail=(
                f"/dev/{base} is using the UAS driver. On bridges that "
                "mishandle UAS this stalls under write load; such bridges "
                "are flagged separately."
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
    vid, pid = usb_ids(base)
    if not vid or not pid:
        return None
    key = f"{vid}:{pid}"
    note = KNOWN_BAD_BRIDGES.get(key)
    if note is None:
        return Finding(
            level=INFO,
            title="USB bridge",
            detail=f"Bridge {key} - not in the known-problematic list.",
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


def _labelled_pools(dev: str) -> dict[str, tuple[str, str]]:
    """Pool guid -> (name, partition) for every ZFS label on the disk of ``dev``."""
    disk = whole_disk(dev) or dev
    r = run(f"lsblk -nrpo NAME {disk}")
    found: dict[str, tuple[str, str]] = {}
    for node in r.lines if r.ok else []:
        node = node.strip()
        label = read_pool_label(node) if node else None
        if label is not None and label.guid not in found:
            found[label.guid] = (label.name, node)
    return found


def _import_state(part_node: str, guid: str) -> str:
    """State ``zpool import -d`` reports for an exported pool (it lists, never imports)."""
    r = run(f"zpool import -d {part_node}")
    block_guid = ""
    for line in r.lines if r.ok else []:
        key, _, value = line.strip().partition(":")
        if key == "id":
            block_guid = value.strip()
        elif key == "state" and block_guid == guid:
            return value.strip().split()[0] if value.strip() else ""
    return ""


def _imported_finding(title: str, pool: str) -> Finding:
    """Health and non-zero counters of an imported pool."""
    health = run(f"zpool list -H -o health {pool}").output or "UNKNOWN"
    st = run(f"zpool status -vp {pool}")
    damage = parse_damage(st.stdout if st.ok else "")
    if health == "ONLINE" and not damage.any:
        return Finding(OK, title, "imported, ONLINE, no errors")
    parts = [f"imported, {health}"]
    parts += [f"errors (READ WRITE CKSUM): {c}" for c in damage.counters]
    if damage.errors:
        parts.append(f"permanent errors in {len(damage.errors)} object(s)")
    parts.append(f"see: zpool status -v {pool}")
    return Finding(WARN, title, "; ".join(parts))


def pool_findings(dev: str) -> list[Finding]:
    """Health of the pools on the disk of ``dev``, without importing anything (I11).

    An imported pool is read from its counters (``zpool status -vp``); an
    exported one from the state ``zpool import -d`` reports, with the
    commands that would read its counters.
    """
    imported: dict[str, str] = {}
    r = run("zpool list -H -o guid,name")
    for line in r.lines if r.ok else []:
        guid, _, name = line.strip().partition("\t")
        imported[guid.strip()] = name.strip()
    findings: list[Finding] = []
    for guid, (name, node) in _labelled_pools(dev).items():
        title = f"Pool {name}"
        if guid in imported:
            findings.append(_imported_finding(title, imported[guid]))
            continue
        state = _import_state(node, guid) or "unknown (zpool import -d did not list it)"
        # zark's own altroot, so `zark clean` releases it if left imported.
        probe = backup_altroot(name)
        detail = (
            f"exported, zpool import reports {state}. Its error counters are "
            "only readable while imported: "
            f"zpool import -o readonly=on -N -R {probe} -d {node} {guid}; "
            f"zpool status -v {name}; zpool export {name}"
        )
        findings.append(Finding(INFO if state == "ONLINE" else WARN, title, detail))
    return findings


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
class TestResult:  # pylint: disable=too-many-instance-attributes
    """Outcome of a destructive write-and-verify run."""

    profile: str
    cold: bool
    bytes_written: int
    write_mb_s: float
    passed: bool
    detail: str = ""
    # Kernel transport errors (DID_ERROR, UAS aborts/resets, failed cache
    # flushes) observed on the device *during* the test. A test can pass the
    # data read-back yet still log these - the signature of a bridge that
    # happened to persist this time but is failing at the bus level and may
    # corrupt on another run. Non-empty means "passed, but the enclosure is
    # not trustworthy".
    transport_errors: list[str] = field(default_factory=list)
    # Why the throwaway pool was not destroyed afterwards; "" when it was.
    cleanup_error: str = ""
    # The same kind of lines, logged between the cold pass's eject and the
    # reconnect: the unplug's own (M4aP.9), not counted as transport errors.
    at_unplug: list[str] = field(default_factory=list)
    # The run ended before the drive could be judged (nobody was there to
    # reconnect it): neither a pass nor evidence against the bridge (M5aF-2).
    inconclusive: bool = False


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
        f"dd if=/dev/zero of=/dev/{base} bs=1M count={count} oflag=direct conv=fdatasync",
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
    the target, syncs, then deletes half before the next cycle - exercising
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


# Kernel log patterns that signal a transport-level failure on a USB-SATA
# bridge - distinct from a data-integrity failure. These can appear even when
# the pool reads back ONLINE (the bridge persisted this time but the bus
# stalled), so they are reported as a strong caution rather than a hard fail.
_TRANSPORT_ERROR_PATTERNS = (
    "DID_ERROR",
    "uas_eh_abort",
    "uas_eh_device_reset",
    "Synchronize Cache(10) failed",
    "command aborted",
    "reset SuperSpeed",
    "reset high-speed",  # bridges that negotiate lower (M5a-18)
    "reset full-speed",
)


def _dmesg_now_ts() -> float:
    """Return the timestamp (kernel seconds) of the latest dmesg line, or 0.0.

    Used to mark a point before the test so we can scan only the lines emitted
    *during* it. dmesg timestamps are seconds since boot in brackets.
    """
    r = run("sh -c 'dmesg | tail -1'")
    if not r.ok:
        return 0.0
    m = re.match(r"\[\s*([0-9]+\.[0-9]+)\]", r.output)
    return float(m.group(1)) if m else 0.0


def _names_disk(line: str, base: str) -> bool:
    """Whether a kernel line names the disk ``base`` as a token (``[sda]``,
    ``sda:``, ``sda1``): ``sda`` is not in a line about ``sdaa`` (M5a-17)."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(base)}(?![A-Za-z])", line) is not None


def _usb_id(name: str) -> str:
    """USB device id (``2-1``, ``2-1.4``) of the disk ``name``, or "" if it is not USB.

    The kernel's reset line is ``usb 2-1: reset SuperSpeed USB device number
    3 ...``: it has neither the disk's name nor "scsi host" nor "uas", so it
    is found by this id (M5a-18). The deepest path component that looks like
    a device id is the disk's own bridge, behind any hub.
    """
    parts = os.path.realpath(f"/sys/block/{name}/device").split("/")
    ids = [c for c in parts if re.fullmatch(r"[0-9]+-[0-9]+(\.[0-9]+)*", c)]
    return ids[-1] if ids else ""


def _transport_lines(
    base: str, since_ts: float, renamed: tuple[float, str] | None = None
) -> list[tuple[float, str]]:
    """Return kernel transport-error lines for ``base`` newer than ``since_ts``.

    Scans dmesg for lines mentioning the device (``[sdX]`` or its host) that
    match a known transport-failure pattern and whose timestamp is after the
    marker, each with its timestamp. DID_NO_CONNECT (a clean unplug) is
    deliberately not in the pattern list, so a normal disconnect does not
    raise a false alarm. ``renamed``: the kernel name the disk got when it
    came back and the timestamp from which it has it; after that, the old
    name belongs to whatever else was plugged in.
    """
    r = run("sh -c 'dmesg'")
    if not r.ok:
        return []
    hits: list[tuple[float, str]] = []
    usb = {n: _usb_id(n) for n in (base, renamed[1] if renamed else base)}
    for line in r.output.splitlines():
        m = re.match(r"\[\s*([0-9]+\.[0-9]+)\]", line)
        if not m or float(m.group(1)) <= since_ts:
            continue
        name = renamed[1] if renamed and float(m.group(1)) > renamed[0] else base
        # The bridge's own reset line names its USB device, not the disk.
        on_bridge = bool(usb[name]) and f"usb {usb[name]}: " in line
        if (
            not on_bridge
            and not _names_disk(line, name)
            and "scsi host" not in line
            and "uas" not in line
        ):
            continue
        # A failed cache flush with DID_NO_CONNECT is the harmless "device
        # already unplugged" case, not a transport fault - never flag it.
        if "DID_NO_CONNECT" in line:
            continue
        if any(p in line for p in _TRANSPORT_ERROR_PATTERNS):
            hits.append((float(m.group(1)), line.strip()))
    return hits


def _within(ts: float, window: tuple[float, float] | None) -> bool:
    return window is not None and window[0] < ts <= window[1]


def _transport_errors_since(
    base: str,
    since_ts: float,
    unplug: tuple[float, float] | None = None,
    renamed: tuple[float, str] | None = None,
) -> list[str]:
    """The transport-error lines since ``since_ts``, except those in ``unplug``.

    ``unplug`` is the cold pass's window, from the last kernel line before
    the eject to the last one once the links are back: the unplug's own
    lines (a failed cache flush with DID_ERROR on some bridges) are in it,
    and the pool was exported and synced before it began (M4aP.9).
    """
    return [
        line for ts, line in _transport_lines(base, since_ts, renamed) if not _within(ts, unplug)
    ]


def _at_unplug(
    base: str, unplug: tuple[float, float] | None, renamed: tuple[float, str] | None = None
) -> list[str]:
    """The transport-error lines inside the cold pass's ``unplug`` window."""
    if unplug is None:
        return []
    return [line for ts, line in _transport_lines(base, unplug[0], renamed) if _within(ts, unplug)]


# How long the cold pass waits for the disk's by-id links after Enter.
LINK_WAIT_SEC = 10


def _wait_for_path(path: str) -> bool:
    """Whether ``path`` exists within LINK_WAIT_SEC, polled every 0.5 s."""
    for _ in range(LINK_WAIT_SEC * 2):
        if Path(path).exists():
            return True
        time.sleep(0.5)
    return Path(path).exists()


def run_destructive_test(
    dev: str,
    profile: str,
    cold: bool,
    log: Log,
    *,
    zfs,  # lib.zfs.ZFS - typed loosely to avoid an import cycle
    wait_for_reconnect=None,  # callable() -> bool at the cold-reconnect pause; False: not confirmed
    write_mb_s: float = 0.0,
) -> TestResult:
    """Create a throwaway pool, write with churn, and verify re-import.

    DESTRUCTIVE: the caller must have confirmed and validated an empty device.
    The cleanup runs after the test, even on error: it destroys the test pool
    and wipes the disk (see :func:`_remove_test_pool`); a pool it could not
    destroy is named in the result's ``cleanup_error``. ``wait_for_reconnect``
    is called (cold only) after the eject, to let the command prompt the
    operator to physically reconnect; it must block until the drive is back.
    """
    # pylint: disable=too-many-arguments,too-many-locals
    # pylint: disable=too-many-positional-arguments,too-many-return-statements,too-many-statements
    base = _base_disk(dev)
    # Held by its stable name: after the cold reconnect the kernel name can
    # belong to another disk, and the cleanup below wipes what it points at.
    by_id = preferred_by_id(by_id_names(f"/dev/{base}"))
    vdev = f"{BY_ID_DIR}/{by_id}" if by_id else f"/dev/{base}"
    import_from = f"{vdev}-part1" if by_id else BY_ID_DIR
    test_pool = f"zarktest_{int(time.time())}"
    # zark's own altroot: a run killed with the pool imported leaves it where
    # `zark clean` finds and exports it.
    altroot = backup_altroot(test_pool)
    target = profile_target_bytes(profile, dev)
    cycles = _PROFILE_CYCLES.get(profile, 1)
    if cold and not by_id:
        return TestResult(
            profile,
            cold,
            0,
            write_mb_s,
            False,
            f"/dev/{base} has no /dev/disk/by-id name: the cold pass could not find it again",
        )
    # Mark the kernel-log position so we can later report only the transport
    # errors emitted during this test.
    dmesg_mark = _dmesg_now_ts()
    created = False
    unplug: tuple[float, float] | None = None
    renamed: tuple[float, str] | None = None  # the kernel name after the reconnect

    def _attempt() -> TestResult:
        nonlocal created, unplug, renamed
        bytes_written = 0
        log.info(f"Creating throwaway test pool '{test_pool}'...")
        # cachefile=none: the test pool never enters /etc/zfs/zpool.cache.
        # -m / is relative to the altroot: the pool mounts at the altroot.
        r = run(
            "zpool create -f -o ashift=12 -o cachefile=none -O atime=off "
            + f"-R {altroot} -m / {test_pool} {vdev}",
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
                _transport_errors_since(base, dmesg_mark, unplug, renamed),
            )
        created = True

        log.info(f"Writing ~{target // (1024**3)} GB with churn ({cycles} cycle(s))...")
        bytes_written = _write_churn(altroot, target, cycles, log)

        log.info("Exporting test pool...")
        run("sync")
        exported = run(f"zpool export {test_pool}", log=log)
        run("sync")
        if not exported.ok:
            # Ejecting or re-importing a pool that is still imported tests
            # nothing: the pool was never read back from the disk (M5a-17).
            return TestResult(
                profile,
                cold,
                bytes_written,
                write_mb_s,
                False,
                f"could not export the test pool: {exported.stderr.strip()}",
                _transport_errors_since(base, dmesg_mark, unplug, renamed),
            )

        if cold:
            before_eject = _dmesg_now_ts()
            log.info("Powering the device down (eject)...")
            run(f"eject /dev/{base}", log=log)
            if wait_for_reconnect is not None and not wait_for_reconnect():
                # Blocks until the operator reconnects; False is a closed
                # stdin, where nobody did (M5a-13).
                return TestResult(
                    profile,
                    cold,
                    bytes_written,
                    write_mb_s,
                    False,
                    "the reconnect was not confirmed (stdin closed at the pause)",
                    _transport_errors_since(base, dmesg_mark, unplug, renamed),
                    inconclusive=True,
                )
            # The kernel announces the disk, then each partition, and udev
            # makes each by-id link from that device's own event (block/
            # genhd.c disk_uevent; 60-persistent-storage.rules). settle waits
            # only for the events udevd has already queued, so the links are
            # waited for too: zpool import -d skips a path that does not exist.
            run("udevadm settle --timeout=10", log=log)
            missing = ""
            if not _wait_for_path(vdev):
                missing = f"the disk did not reappear as {by_id} after the reconnect"
            elif not _wait_for_path(import_from):
                missing = (
                    f"the partition link {import_from} did not appear within "
                    + f"{LINK_WAIT_SEC} s of the reconnect"
                )
            back = _dmesg_now_ts()
            # Without both timestamps every line counts, as before (M4aP.9).
            if before_eject and back:
                unplug = (before_eject, back)
            if missing:
                return TestResult(
                    profile,
                    cold,
                    bytes_written,
                    write_mb_s,
                    False,
                    missing,
                    _transport_errors_since(base, dmesg_mark, unplug, renamed),
                )

            # The kernel may have given the disk another name (sdb, then sdc):
            # the lines after the reconnect are about the new one (M5a-17).
            now_base = _base_disk(os.path.realpath(vdev))
            if back and now_base != base:
                renamed = (back, now_base)

        # Verify re-import, from the disk's own partition (by-id survives the
        # reconnect; the kernel name may not).
        log.info("Re-importing to verify persistence...")
        run("sync")
        run("sh -c 'echo 3 > /proc/sys/vm/drop_caches'")
        imp = run(
            f"zpool import -o readonly=on -o cachefile=none -N -R {altroot} "
            + f"-d {import_from} {test_pool}",
            log=log,
        )
        # Transport errors are read AFTER the re-import attempt so the import's
        # own bus traffic (which is where a flaky bridge tends to stumble) is
        # included in the window.
        terrs = _transport_errors_since(base, dmesg_mark, unplug, renamed)
        if not imp.ok:
            return TestResult(
                profile,
                cold,
                bytes_written,
                write_mb_s,
                False,
                f"re-import failed: {imp.stderr.strip()}",
                terrs,
            )
        health = zfs.pool_health(test_pool)
        passed = health == "ONLINE"
        detail = "ONLINE" if passed else f"health={health}"
        if passed and terrs:
            # Data survived this time, but the bus stumbled - flag it loudly.
            detail = (
                "ONLINE, but transport errors were logged during the test "
                "(the bridge persisted this run but is failing at the bus "
                "level and may corrupt on another)"
            )
        return TestResult(
            profile,
            cold,
            bytes_written,
            write_mb_s,
            passed,
            detail,
            terrs,
        )

    try:
        result = _attempt()
    finally:
        cleanup_error = _remove_test_pool(test_pool, vdev, import_from, altroot, created, log)
    result.cleanup_error = cleanup_error
    result.at_unplug = _at_unplug(base, unplug, renamed)
    return result


def _destroy_test_pool(test_pool: str, import_from: str, altroot: str, log: Log) -> str:
    """Destroy the test pool read-write; return why it was not, or "".

    A pool imported read-only (the read-back's import) is exported and
    imported read-write first: ``zpool destroy`` of a read-only pool returns
    0 and writes nothing to its labels (module/zfs/spa.c, M4aR-1).
    """
    rw_import = f"zpool import -o cachefile=none -N -R {altroot} -d {import_from} {test_pool}"
    imported = run(f"zpool list -H -o name {test_pool}").ok
    if imported and run(f"zpool get -H -o value readonly {test_pool}").output == "on":
        r = run(f"zpool export {test_pool}", log=log)
        if not r.ok:
            return f"the read-only pool could not be exported: {r.stderr.strip()}"
        imported = False
    if not imported:
        r = run(rw_import, log=log)
        if not r.ok:
            return f"it could not be imported read-write to destroy it: {r.stderr.strip()}"
    r = run(f"zpool destroy -f {test_pool}", log=log)
    if not r.ok or run(f"zpool list -H -o name {test_pool}").ok:
        return f"zpool destroy failed: {r.stderr.strip() or 'the pool is still imported'}"
    log.ok(f"Test pool {test_pool} destroyed")
    return ""


def _remove_test_pool(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    test_pool: str, vdev: str, import_from: str, altroot: str, created: bool, log: Log
) -> str:
    """Destroy the test pool and leave its disk blank: no orphan zfs_member
    partitions that the empty-drive check would later reject.

    The wipe goes to the disk ``vdev`` resolves to now, and only when that
    disk's label is the test pool's or there is none; a disk that is not
    connected, or that carries another pool, is left alone and named.
    Returns why the test pool was not destroyed, or "" when it was (or was
    never created). The wipe does not reach labels inside the old first
    partition, so a pool that was not destroyed may still be listed there.
    """
    if not Path(vdev).exists():
        log.warn(f"{vdev} is not connected: test pool {test_pool} not destroyed, nothing wiped")
        return f"{vdev} is not connected: nothing destroyed or wiped" if created else ""
    disk = os.path.realpath(vdev)
    error = _destroy_test_pool(test_pool, import_from, altroot, log) if created else ""
    if error:
        error = (
            f"{error}. Its labels may remain inside the first partition: "
            + f"zpool import -d {import_from} lists it"
        )
        log.error(f"Test pool {test_pool} was not destroyed: {error}")
    if not error:  # an undestroyed pool keeps its directory: it is how `zark clean` finds it
        run(f"rmdir {altroot} 2>/dev/null")
    label = read_pool_label(part(disk, 1)) or read_pool_label(disk)
    if label is not None and label.name != test_pool:
        log.warn(f"{disk} carries pool '{label.name}', not {test_pool}: not wiped")
        return error
    run(f"wipefs -a {disk} 2>/dev/null")
    run(f"sgdisk --zap-all {disk} 2>/dev/null")
    run("sync")
    return error


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

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    zver = run("./zark --version 2>/dev/null || zark --version 2>/dev/null").output
    ubuntu = run("lsb_release -d 2>/dev/null").output
    zfsver = run("zfs version 2>/dev/null").output
    kernel = run("uname -a").output
    model = run(f"lsblk -dn -o MODEL /dev/{base}").output
    size = run(f"lsblk -dn -o SIZE /dev/{base}").output
    tran = run(f"lsblk -dn -o TRAN /dev/{base}").output
    vid, pid = usb_ids(base)
    dmesg = run(
        "sh -c \"dmesg | grep -iE 'sd |uas|usb-storage|synchronize cache|"
        "did_error|did_no_connect|metaslab_init' | tail -40\"",
    ).output

    lines = [
        f"ZARK HEALTH REPORT - {ts}",
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
        if test.cleanup_error:
            lines.append(f"Test pool    : not destroyed: {_redact(test.cleanup_error)}")
        if test.transport_errors:
            lines.append(
                f"Transport errors during test ({len(test.transport_errors)}):",
            )
            lines += [f"  {_redact(e)}" for e in test.transport_errors]
        if test.at_unplug:
            lines.append(
                f"At the unplug, not counted ({len(test.at_unplug)}):",
            )
            lines += [f"  {_redact(e)}" for e in test.at_unplug]

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
            "sharing if you prefer - re-run and choose to obfuscate.)",
        ]
    return "\n".join(lines)


def write_report_file(content: str) -> str:
    """Write a report to /tmp and return its path."""
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
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
