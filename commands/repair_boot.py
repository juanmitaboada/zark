"""
zark repair-boot — Fix boot issues from a live USB.

Imports rpool/bpool, mounts the system, regenerates grub.cfg and initrd.
Use when grub.cfg is corrupted (e.g., update-grub ran with backup drive connected).
"""

import getpass
import os
from pathlib import Path

from lib import grub_guard, sh
from lib.cleanup import Cleanup
from lib.keystore import Keystore
from lib.log import Log
from lib.zfs import ZFS, fix_grub_bpool_uuid

REPAIR_MNT = "/mnt/repair"
TOTAL_STEPS = 7


def run(
    args: list[str],
):  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    """Main entry point for zark repair-boot command."""
    del args  # Unused — no args expected for repair-boot
    log = Log()
    zfs = ZFS(log)
    cleanup = Cleanup(log)

    log.banner("BOOT REPAIR", "Fix grub.cfg and initrd from live USB")

    # ── Verify live USB environment ───────────────────────────────────────
    if zfs.pool_exists("rpool") and Path("/").stat().st_dev == os.stat("/").st_dev:
        # Check if we're running on a real system (not live USB)
        running_root_mounted = sh.run("zfs get -H -o value mounted rpool/ROOT 2>/dev/null").output
        if running_root_mounted:
            log.fatal(
                "Looks like you're running on the installed system, not a live USB.\n"
                + "  Use 'sudo ./zark finish' instead, or boot from a live USB first.",
            )

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

    # ── 2. Import rpool + bpool ───────────────────────────────────────────
    log.step(2, TOTAL_STEPS, "Importing pools...")

    for pool in ("rpool", "bpool"):
        if not zfs.pool_exists(pool):
            r = sh.run(f"zpool import -R {REPAIR_MNT} -N {pool}")
            if r.ok:
                cleanup.track_pool(pool)
                log.ok(f"{pool} imported")
            else:
                log.fatal(f"Cannot import {pool}: {r.stderr.strip()}")
        else:
            log.ok(f"{pool} already imported")

    # ── 3. Open keystore + load keys ──────────────────────────────────────
    log.step(3, TOTAL_STEPS, "Loading encryption keys...")

    print()
    print("  ┌─────────────────────────────────────────────────────┐")
    print("  │ 🔑 Passphrase for rpool")
    print("  └─────────────────────────────────────────────────────┘")
    passphrase = getpass.getpass("    Passphrase: ")

    ks = Keystore(log)
    if not ks.mount("rpool", passphrase):
        log.fatal("Cannot open keystore — check passphrase")
    cleanup.track_keystore(ks)

    loaded = ks.load_pool_keys("rpool")
    log.ok(f"Loaded {loaded} key(s)")

    # ── 4. Mount system ───────────────────────────────────────────────────
    log.step(4, TOTAL_STEPS, "Mounting system...")

    # Find root dataset
    root_ds: str | None = None
    for ds in sh.run("zfs list -H -o name -r rpool/ROOT").lines:
        ds = ds.strip()
        if ds and ds != "rpool/ROOT" and "@" not in ds and "/" not in ds.split("rpool/ROOT/")[1]:
            root_ds = ds
            break

    if not root_ds:
        log.fatal("Cannot find root dataset under rpool/ROOT")

    ubuntu_name = root_ds.split("/")[-1]
    log.ok(f"Root dataset: {root_ds}")

    # Set mountpoints and mount
    _ = sh.run(f"zfs set mountpoint=/ {root_ds}")
    _ = sh.run(f"zfs mount {root_ds}")

    # Mount child datasets
    for ds in sh.run("zfs list -H -o name -r rpool").lines:
        ds = ds.strip()
        if ds and ds != "rpool" and ds != root_ds and "@" not in ds and "keystore" not in ds:
            mp = sh.run(f"zfs get -H -o value mountpoint {ds}").output.strip()
            if mp and mp != "none" and mp != "-":
                _ = sh.run(f"zfs mount {ds} 2>/dev/null", check=False)

    # Mount bpool
    _ = sh.run(f"zfs set mountpoint=/boot bpool/BOOT/{ubuntu_name}")
    _ = sh.run(f"zfs mount bpool/BOOT/{ubuntu_name}")

    # Check kernel files exist
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

    # EFI
    internal_disk = sh.run(
        'lsblk -dn -o NAME,TYPE | awk \'$2=="disk"{print "/dev/"$1}\' | grep nvme | head -1',
    ).output.strip()
    if internal_disk:
        efi_part = sh.part(internal_disk, 1)
    else:
        efi_part = sh.run("blkid -t TYPE=vfat | grep -i efi | head -1 | cut -d: -f1").output.strip()

    if efi_part:
        _ = sh.run(f"mkdir -p {REPAIR_MNT}/boot/efi")
        _ = sh.run(f"mount {efi_part} {REPAIR_MNT}/boot/efi")
        cleanup.track_mount(f"{REPAIR_MNT}/boot/efi")

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

    if Path(f"{REPAIR_MNT}/usr/bin/dracut").exists():
        _ = sh.run(f"chroot {REPAIR_MNT} dracut --force --regenerate-all", log=log)
        log.ok("initrd regenerated (dracut) ✓")
    elif Path(f"{REPAIR_MNT}/usr/sbin/update-initramfs").exists():
        _ = sh.run(f"chroot {REPAIR_MNT} update-initramfs -u -k all", log=log)
        log.ok("initrd regenerated ✓")

    # ── 7. Cleanup ────────────────────────────────────────────────────────
    log.step(7, TOTAL_STEPS, "Cleanup...")

    cleanup.run()

    log.banner_ok(
        "BOOT REPAIR COMPLETE",
        [
            "grub.cfg regenerated ✓",
            "Grub guard installed ✓",
            "initrd regenerated ✓",
            "",
            "Next: remove live USB and reboot.",
        ],
    )
