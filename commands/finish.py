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
zark finish - Post-recovery finalization.

Run from inside the recovered system (not live USB). Enables the ZFS
services, writes zpool.cache, checks the pools' label hostid, rebuilds every
installed kernel's initrd and then grub.cfg, fixes the ESP in fstab and
checks the EFI binaries. It never exports or re-imports a pool: the system
runs from rpool, and an export unmounts every dataset that is not busy
before it fails (M4c-4).
"""

import os
import re
from pathlib import Path

from lib import apt_guard, grub_guard, sh
from lib.boot_chain import esp_of_rpool_disk
from lib.grub_cfg import regenerate_grub_cfg
from lib.identity import read_pool_label
from lib.initrd import installed_kernels, regenerate_initrd
from lib.log import Log
from lib.mount import warn_kernel_named_vdevs, warn_rpool_mountpoint_lost
from lib.zfs import ZFS, fix_grub_bpool_uuid

TOTAL_STEPS = 4
EFI_DIR = "/boot/efi/EFI/ubuntu"
REPAIR_ADVICE = "Boot the live USB and run: sudo ./zark repair-boot"


def _system_hostid() -> int | None:
    """/etc/hostid as a number (4 bytes, little-endian), or None."""
    try:
        raw = Path("/etc/hostid").read_bytes()
    except OSError:
        return None
    return int.from_bytes(raw[:4], "little") if len(raw) >= 4 else None


def _first_vdev(pool: str) -> str:
    """The first device path of ``pool``'s vdevs, or ""."""
    r = sh.run(f"zpool list -vHP {pool}")
    for line in r.lines[1:] if r.ok else []:
        path = line.strip().split("\t")[0]
        if path.startswith("/dev/"):
            return path
    return ""


def check_label_hostid(pool: str, log: Log) -> None:
    """The hostid in ``pool``'s label against /etc/hostid, read-only (M4c-4).

    The labels get the importing system's hostid at every import, the
    boot's included, and the initrd carries /etc/hostid: a difference here
    is rewritten by the next boot. Nothing is changed and nothing fails.
    """
    vdev = _first_vdev(pool)
    label = read_pool_label(vdev) if vdev else None
    if label is None:
        log.warn(f"  {pool}: cannot read its label (zdb -l {vdev or '<no vdev>'})")
        return
    system = _system_hostid()
    if system is None:
        log.warn(f"  {pool}: /etc/hostid cannot be read")
        return
    if not label.hostid.isdigit():
        log.warn(f"  {pool}: its label carries no hostid; the next boot writes {system:08x}")
        return
    if int(label.hostid) == system:
        log.ok(f"  {pool}: labels carry this system's hostid ({system:08x}) ✓")
    else:
        log.warn(
            f"  {pool}: labels carry hostid {int(label.hostid):08x}, this system's is "
            + f"{system:08x}: the next boot rewrites them",
        )


def _fix_fstab_esp(esp: str, log: Log) -> None:
    """Point /etc/fstab's /boot/efi line at ``esp``'s UUID."""
    actual_efi_uuid = sh.run(f"blkid -s UUID -o value {esp}").output
    fstab = Path("/etc/fstab")
    if not (fstab.exists() and actual_efi_uuid):
        return
    content = fstab.read_text(encoding="utf-8")
    match = re.search(r"/dev/disk/by-uuid/([A-Fa-f0-9-]+)\s+/boot/efi", content)
    if match and match.group(1) != actual_efi_uuid:
        content = content.replace(match.group(1), actual_efi_uuid)
        _ = fstab.write_text(content, encoding="utf-8")
        log.ok(f"fstab EFI UUID fixed: {actual_efi_uuid}")
    elif match:
        log.ok(f"fstab EFI UUID correct: {actual_efi_uuid} ✓")


def check_esp_and_efi(esp: str, log: Log) -> str:
    """M4c-7: "" when /boot/efi is rpool's disk's ESP (vfat) and holds
    shimx64.efi and grubx64.efi, after the signed packages' reconfigure
    when one is missing; else what is wrong."""
    mount = sh.run("findmnt -n -o SOURCE,FSTYPE /boot/efi")
    fields = mount.output.split() if mount.ok else []
    if len(fields) != 2 or fields[1] != "vfat":
        return "the ESP is not mounted on /boot/efi"
    if esp and os.path.realpath(fields[0]) != os.path.realpath(esp):
        return f"/boot/efi is {fields[0]}, not the ESP of rpool's disk ({esp})"

    def missing() -> list[str]:
        return [f for f in ("shimx64.efi", "grubx64.efi") if not Path(f"{EFI_DIR}/{f}").exists()]

    if not missing():
        log.ok("Secure Boot chain present ✓")
        return ""
    log.info("EFI binaries incomplete - reconfiguring signed packages...")
    rcs = []
    for pkg in ("grub-efi-amd64-signed", "shim-signed"):
        r = sh.run(f"dpkg-reconfigure -f noninteractive {pkg}", log=log)
        rcs.append(f"{pkg} rc={r.returncode}")
    still = missing()
    if not still:
        log.ok("Secure Boot chain present after the reconfigure ✓")
        return ""
    return f"{', '.join(still)} missing in {EFI_DIR} ({', '.join(rcs)})"


def run(
    args: list[str],
):  # pylint: disable=too-many-statements, too-many-branches, too-many-locals
    """
    Run post-recovery finalization steps.
    Should be run from inside the recovered system (not live USB).
    """
    del args  # Unused, but keeps the interface consistent
    log = Log()
    zfs = ZFS(log)

    log.banner("POST-RECOVERY FINISH", "Run from inside the recovered system")
    warn_rpool_mountpoint_lost(log)
    warn_kernel_named_vdevs(log)

    # ── Verify not live USB ──────────────────────────────────────────────
    if not zfs.pool_exists("rpool"):
        log.fatal("rpool not found - run --finish from the recovered system")
    if not Path("/etc/os-release").exists():
        log.fatal("Not a running Ubuntu system")

    os_name = sh.run("grep PRETTY_NAME /etc/os-release | cut -d= -f2 | tr -d '\"'").output
    log.ok(f"Running on: {os_name}")

    # ── 1. ZFS services and pools ────────────────────────────────────────
    log.step(1, TOTAL_STEPS, "Enabling ZFS services...")
    for svc in (
        "zfs-import-cache",
        "zfs-import-scan",
        "zfs-mount",
        "zfs-zed",
        "zfs-share",
    ):
        r = sh.run(f"systemctl enable {svc}", timeout=10)
        if r.ok:
            log.ok(f"  {svc} enabled")

    # zpool.cache first: the initrds built below carry it.
    pools = [p for p in ("rpool", "bpool") if zfs.pool_exists(p)]
    cache_failed = [
        p for p in pools if not sh.run(f"zpool set cachefile=/etc/zfs/zpool.cache {p}").ok
    ]
    if cache_failed:
        log.warn(f"zpool.cache not written for {', '.join(cache_failed)}")
    else:
        log.ok("zpool.cache regenerated ✓")
    for pool in pools:
        check_label_hostid(pool, log)

    # ── 2. initrd and GRUB ───────────────────────────────────────────────
    log.step(2, TOTAL_STEPS, "Rebuilding the initrds and grub.cfg...")

    # Install grub guard (prevents update-grub with backup drive connected)
    grub_guard.install(target_root="", log=log, overwrite=False)

    # Install the apt guard (non-clobbering on first boot of the recovered
    # system, in case recover already wrote it or the operator edited it).
    apt_guard.install(target_root="", log=log, overwrite=False)

    # Set GRUB_RECORDFAIL_TIMEOUT
    grub_default = Path("/etc/default/grub")
    if grub_default.exists() and "GRUB_RECORDFAIL_TIMEOUT" not in grub_default.read_text(
        encoding="utf-8",
    ):
        with open(grub_default, "a", encoding="utf-8") as f:
            _ = f.write("GRUB_RECORDFAIL_TIMEOUT=0\n")
        log.ok("GRUB_RECORDFAIL_TIMEOUT=0 set")

    # Initrds before update-grub: 10_linux_zfs lists only kernels that have one.
    initrd_failures = regenerate_initrd("", log)

    grub_cfg = Path("/boot/grub/grub.cfg")
    grub_failure = regenerate_grub_cfg(
        grub_cfg,
        "update-grub",
        Path(f"{grub_cfg}.pre-finish"),
        installed_kernels(""),
        log,
    )

    # Fix bpool UUID in grub.cfg
    bpool_guid_dec = zfs.pool_guid("bpool")
    if bpool_guid_dec:
        bpool_uuid_hex = format(int(bpool_guid_dec), "016x")
        _ = fix_grub_bpool_uuid(grub_cfg, bpool_uuid_hex, log)

    # ── 3. ESP: fstab, then the EFI binaries ─────────────────────────────
    log.step(3, TOTAL_STEPS, "Checking the ESP...")
    esp = esp_of_rpool_disk()
    if esp:
        _fix_fstab_esp(esp, log)
    else:
        log.warn("No EFI System Partition found on rpool's disk - fstab not checked")
    efi_failure = check_esp_and_efi(esp, log)

    # ── 4. Verify ────────────────────────────────────────────────────────
    log.step(4, TOTAL_STEPS, "Verifying system state...")
    unhealthy: list[str] = []
    for pool in ("rpool", "bpool"):
        health = zfs.pool_health(pool)
        if health == "ONLINE":
            log.ok(f"  {pool}: ONLINE ✓")
        else:
            log.warn(f"  {pool}: {health}")
            unhealthy.append(f"{pool}: {health}")

    # The banner reports what happened: "updated" over a failed update-grub
    # sent the operator to a reboot with a stale grub.cfg (J, 2026-09-30).
    failed = bool(grub_failure or initrd_failures or efi_failure or unhealthy)
    lines = [
        "ZFS services enabled ✓",
        *(["initrds rebuilt ✓"] if not initrd_failures else []),
        *(f"✗ initrd: {f}" for f in initrd_failures),
        (
            "GRUB config updated ✓"
            if not grub_failure
            else f"✗ grub.cfg not regenerated: {grub_failure}"
        ),
        "EFI binaries present ✓" if not efi_failure else f"✗ EFI: {efi_failure}",
        *(f"✗ pool {u}" for u in unhealthy),
        "",
    ]
    if failed:
        advice: list[str] = []
        if grub_failure.startswith("update-grub failed"):
            advice.append("Disconnect the external drive(s) if any,")
        if efi_failure.startswith(("the ESP is not mounted", "/boot/efi is")):
            advice.append("Mount the ESP of rpool's disk on /boot/efi,")
        advice.append(f"then run again: {log.W}sudo ./zark finish{log.N}")
        if initrd_failures or (grub_failure and not grub_failure.startswith("update-grub failed")):
            # A kernel without an initrd gets no grub.cfg entry, and the next
            # boot may not reach this system: the live USB rebuilds both (R2-7).
            advice = [REPAIR_ADVICE]
        log.banner_error("FINISH INCOMPLETE", [*lines, *advice])
        raise SystemExit(1)
    log.banner_ok("FINISH COMPLETE", [*lines, f"Run setup: {log.W}sudo ./zark setup{log.N}"])
