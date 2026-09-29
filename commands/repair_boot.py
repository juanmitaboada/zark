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
zark repair-boot — Fix boot issues from a live USB.

Imports rpool/bpool, mounts the system, regenerates grub.cfg and initrd.
Use when grub.cfg is corrupted (e.g., update-grub ran with backup drive connected).
"""

from pathlib import Path

from lib import grub_guard, sh
from lib.cleanup import Cleanup
from lib.identity import disks_under
from lib.initrd import regenerate_initrd
from lib.keystore import Keystore
from lib.log import Log
from lib.mount import mount_system_pools
from lib.zfs import ZFS, fix_grub_bpool_uuid

REPAIR_MNT = "/mnt/repair"
TOTAL_STEPS = 7


_ESP_PARTTYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"


def _esp_of_rpool_disk() -> str:
    """EFI System Partition on the disk holding rpool's vdev, or ""."""
    vdevs = sh.run("zpool list -vHP rpool")
    for line in vdevs.lines[1:] if vdevs.ok else []:
        path = line.strip().split("\t")[0]
        if not path.startswith("/dev/"):
            continue
        for disk in sorted(disks_under(path)):
            parts = sh.run(f"lsblk -nr -o NAME,PARTTYPE {disk}")
            for p in parts.lines if parts.ok else []:
                fields = p.split()
                if len(fields) == 2 and fields[1].lower() == _ESP_PARTTYPE:
                    return f"/dev/{fields[0]}"
    return ""


def run(
    args: list[str],
):  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    """Main entry point for zark repair-boot command."""
    del args  # Unused — no args expected for repair-boot
    log = Log()
    zfs = ZFS(log)
    cleanup = Cleanup(log)
    # Register atexit/SIGTERM handlers up front. This is the load-bearing
    # fix for the "boot still needs -f" problem the operator hit: every exit
    # path — including log.fatal() mid-operation, after a pool has already
    # been (possibly force-) imported — now runs Cleanup.run(), which exports
    # both pools cleanly and flushes the device. Without this, an early fatal
    # left rpool/bpool imported and marked in-use, so the next real boot's
    # initramfs import (which carries no -f) failed. A clean export here is
    # what lets the next boot import without -f at all.
    cleanup.register()

    log.banner("BOOT REPAIR", "Fix grub.cfg and initrd from live USB")

    # ── Verify live USB environment ───────────────────────────────────────
    # repair-boot rewrites the installed system's grub.cfg/initrd from the
    # outside and import/exports the very rpool/bpool it targets. Doing that
    # against the *running* installed system is both unnecessary (use
    # `zark finish`) and unsafe. We warn and require explicit confirmation
    # rather than a hard abort, so a false-negative on exotic live media
    # (custom remaster without the usual casper markers) does not lock the
    # operator out of a legitimate repair.
    if not sh.is_live_usb():
        log.warn("This does not look like a live USB session.")
        log.info("repair-boot is meant to run from live media; on the running")
        log.info("installed system use 'sudo ./zark finish' instead.")
        if not log.ask("Continue anyway?", default=False):
            log.fatal("Aborted — boot a live USB and retry, or use 'zark finish'.")

    # ── 1. Check for external pools ───────────────────────────────────────
    log.step(1, TOTAL_STEPS, "Checking for external ZFS pools...")

    available = sh.run("zpool import 2>/dev/null | awk '/pool:/{print $2}'").lines
    available = [p.strip() for p in available if p.strip()]

    external = [p for p in available if p not in ("rpool", "bpool")]
    if external:
        log.warn(f"External pool(s) detected: {', '.join(external)}")
        log.info("These must NOT be imported during grub repair.")
        log.info("If a backup drive is connected, disconnect it now.")
        answer = input("\n    Continue without external pools? [y/N]: ").strip().lower()
        if answer != "y":
            log.fatal("Aborted — disconnect external drives and retry.")

    # ── 2-4. Import rpool + bpool, unlock, mount (all tracked by Cleanup) ──
    # mount_system_pools imports under the altroot (clean first, then -f for
    # a pool left "in use" by the unclean shutdown that brought the operator
    # here), asks the passphrase with retries, and registers every pool,
    # mount and the keystore with Cleanup, so the final export unmounts
    # /boot and every dataset before exporting rpool (P0-6). Mountpoints are
    # never changed: the stored ones are mounted under the altroot.
    log.step(2, TOTAL_STEPS, "Importing pools...")
    log.step(3, TOTAL_STEPS, "Loading encryption keys...")
    log.step(4, TOTAL_STEPS, "Mounting system...")
    result = mount_system_pools(REPAIR_MNT, None, log, zfs, Keystore(log), cleanup)
    if result is None:
        log.fatal("Could not import and mount the system — see messages above.")
    _, ubuntu_name = result
    log.ok(f"Boot environment: {ubuntu_name}")

    kernels = list(Path(f"{REPAIR_MNT}/boot").glob("vmlinuz-*"))
    if kernels:
        log.ok(f"System mounted at {REPAIR_MNT} ({len(kernels)} kernel(s))")
    else:
        log.fatal(f"No kernels found in {REPAIR_MNT}/boot — bpool may not be mounted")

    # ── 5. Chroot setup + update-grub ─────────────────────────────────────
    log.step(5, TOTAL_STEPS, "Regenerating grub.cfg...")

    # Bind mounts
    for d in ("proc", "sys", "dev", "dev/pts", "run"):
        _ = sh.run(f"mkdir -p {REPAIR_MNT}/{d}")
        _ = sh.run(f"mount --bind /{d} {REPAIR_MNT}/{d}")
        cleanup.track_mount(f"{REPAIR_MNT}/{d}")

    # EFI: the ESP on the disk that holds rpool (never "the first vfat",
    # which on a live session can be the live USB's own ESP)
    efi_part = _esp_of_rpool_disk()
    if efi_part:
        _ = sh.run(f"mkdir -p {REPAIR_MNT}/boot/efi")
        if sh.run(f"mount {efi_part} {REPAIR_MNT}/boot/efi", log=log).ok:
            cleanup.track_mount(f"{REPAIR_MNT}/boot/efi")
    else:
        log.warn("No EFI System Partition found on rpool's disk — /boot/efi not mounted")

    _ = sh.run(f"mkdir -p {REPAIR_MNT}/sys/firmware/efi/efivars")
    _ = sh.run(
        f"mount -t efivarfs efivarfs {REPAIR_MNT}/sys/firmware/efi/efivars 2>/dev/null",
        check=False,
    )
    cleanup.track_mount(f"{REPAIR_MNT}/sys/firmware/efi/efivars")

    # zpool.cache
    cache_path = f"{REPAIR_MNT}/etc/zfs/zpool.cache"
    zfs.write_zpool_cache(cache_path, ["rpool", "bpool"])

    # Backup current grub.cfg
    grub_cfg = Path(f"{REPAIR_MNT}/boot/grub/grub.cfg")
    if grub_cfg.exists():
        _ = sh.run(f"cp {grub_cfg} {grub_cfg}.pre-repair")
        log.dbg("Backed up grub.cfg → grub.cfg.pre-repair")

    # Run update-grub
    r = sh.run(f"chroot {REPAIR_MNT} update-grub", log=log)
    if r.ok:
        # Verify it generated kernel entries
        content = grub_cfg.read_text(encoding="utf-8") if grub_cfg.exists() else ""
        if "vmlinuz" in content:
            log.ok("grub.cfg regenerated with kernel entries ✓")
        else:
            log.warn("update-grub ran but no kernel entries found!")
            log.info("Restoring previous grub.cfg...")
            _ = sh.run(f"cp {grub_cfg}.pre-repair {grub_cfg}")
            log.warn("Restored pre-repair grub.cfg")
    else:
        log.warn(f"update-grub failed: {r.stderr.strip()}")
        if Path(f"{grub_cfg}.pre-repair").exists():
            _ = sh.run(f"cp {grub_cfg}.pre-repair {grub_cfg}")
            log.warn("Restored pre-repair grub.cfg")

    # Fix bpool UUID in grub.cfg
    bpool_guid = zfs.pool_guid("bpool")
    if bpool_guid:
        bpool_hex = format(int(bpool_guid), "016x")
        _ = fix_grub_bpool_uuid(grub_cfg, bpool_hex, log)

    # ── 6. Install grub guard + regenerate initrd ─────────────────────────
    log.step(6, TOTAL_STEPS, "Installing grub guard and regenerating initrd...")

    grub_guard.install(target_root=REPAIR_MNT, log=log)

    initrd_failures = regenerate_initrd(REPAIR_MNT, log)

    # ── 7. Cleanup ────────────────────────────────────────────────────────
    log.step(7, TOTAL_STEPS, "Cleanup...")

    cleanup.run()

    exported = cleanup.exported_pools()
    still = [p for p in ("rpool", "bpool") if p not in exported and zfs.pool_exists(p)]
    lines = [
        "grub.cfg regenerated ✓",
        "Grub guard installed ✓",
        *(["initrd regenerated ✓"] if not initrd_failures else []),
        *(f"✗ {f}" for f in initrd_failures),
        *(["Pools exported cleanly ✓"] if not still else []),
        *(f"✗ {p} is still imported — run: sudo zpool export {p}" for p in still),
        "",
        "Next: remove live USB and reboot.",
    ]
    if still or initrd_failures:
        log.banner_error("BOOT REPAIR INCOMPLETE", lines)
        raise SystemExit(1)
    log.banner_ok("BOOT REPAIR COMPLETE", lines)
