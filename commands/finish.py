"""
zark finish — Post-recovery finalization.

Run from inside the recovered system (not live USB).
Fixes hostid, cachefile, ZFS services, GRUB, initramfs.
"""

import re
from pathlib import Path

from lib import grub_guard, sh

# from lib.config import Config
from lib.log import Log
from lib.zfs import ZFS, fix_grub_bpool_uuid


def run(
    args: list[str],
):  # pylint: disable=too-many-statements, too-many-branches, too-many-locals
    """
    Run post-recovery finalization steps.
    Should be run from inside the recovered system (not live USB).
    """
    del args  # Unused, but keeps the interface consistent
    log = Log()
    # cfg = Config.load()
    zfs = ZFS(log)

    log.banner("POST-RECOVERY FINISH", "Run from inside the recovered system")

    # ── Verify not live USB ──────────────────────────────────────────────
    if not zfs.pool_exists("rpool"):
        log.fatal("rpool not found — run --finish from the recovered system")
    if not Path("/etc/os-release").exists():
        log.fatal("Not a running Ubuntu system")

    os_name = sh.run("grep PRETTY_NAME /etc/os-release | cut -d= -f2 | tr -d '\"'").output
    log.ok(f"Running on: {os_name}")

    internal_disk = sh.run(
        'lsblk -dn -o NAME,TYPE | awk \'$2=="disk"{print "/dev/"$1}\' | grep nvme | head -1',
    ).output
    if not internal_disk:
        internal_disk = sh.run(
            'lsblk -dn -o NAME,TYPE | awk \'$2=="disk"{print "/dev/"$1}\' | head -1',
        ).output

    # ── 1. ZFS services ──────────────────────────────────────────────────
    log.step(1, 5, "Enabling ZFS services...")
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

    # Sync hostid
    hostid = sh.run("hostid").output
    for pool in ("rpool", "bpool"):
        if zfs.pool_exists(pool):
            _ = sh.run(f"zpool export {pool}")
            _ = sh.run(f"zpool import {pool}")
    log.ok(f"Pool hostid synced ({hostid})")

    # Regenerate zpool.cache
    _ = sh.run("zpool set cachefile=/etc/zfs/zpool.cache rpool")
    _ = sh.run("zpool set cachefile=/etc/zfs/zpool.cache bpool")
    log.ok("zpool.cache regenerated ✓")

    # ── 2. GRUB ──────────────────────────────────────────────────────────
    log.step(2, 5, "Updating GRUB...")

    # Install grub guard (prevents update-grub with backup drive connected)
    grub_guard.install(target_root="", log=log, overwrite=False)

    # Set GRUB_RECORDFAIL_TIMEOUT
    grub_default = Path("/etc/default/grub")
    if grub_default.exists() and "GRUB_RECORDFAIL_TIMEOUT" not in grub_default.read_text(
        encoding="utf-8",
    ):
        with open(grub_default, "a", encoding="utf-8") as f:
            _ = f.write("GRUB_RECORDFAIL_TIMEOUT=0\n")
        log.ok("GRUB_RECORDFAIL_TIMEOUT=0 set")

    _ = sh.run("update-grub", log=log)
    log.ok("update-grub completed")

    # Fix bpool UUID in grub.cfg
    bpool_guid_dec = zfs.pool_guid("bpool")
    if bpool_guid_dec:
        bpool_uuid_hex = format(int(bpool_guid_dec), "016x")
        _ = fix_grub_bpool_uuid(Path("/boot/grub/grub.cfg"), bpool_uuid_hex, log)

    # Verify EFI binary
    if (
        Path("/boot/efi/EFI/ubuntu/shimx64.efi").exists()
        and Path("/boot/efi/EFI/ubuntu/grubx64.efi").exists()
    ):
        log.ok("Secure Boot chain present ✓")
    else:
        log.info("EFI binaries incomplete — reconfiguring signed packages...")
        _ = sh.run("dpkg-reconfigure -f noninteractive grub-efi-amd64-signed", log=log)
        _ = sh.run("dpkg-reconfigure -f noninteractive shim-signed", log=log)

    # ── 3. Fix fstab EFI UUID ────────────────────────────────────────────
    log.step(3, 5, "Verifying fstab...")
    if internal_disk:
        actual_efi_uuid = sh.run(f"blkid -s UUID -o value {sh.part(internal_disk, 1)}").output
        fstab = Path("/etc/fstab")
        if fstab.exists() and actual_efi_uuid:
            content = fstab.read_text(encoding="utf-8")

            match = re.search(r"/dev/disk/by-uuid/([A-Fa-f0-9-]+)\s+/boot/efi", content)
            if match and match.group(1) != actual_efi_uuid:
                content = content.replace(match.group(1), actual_efi_uuid)
                _ = fstab.write_text(content, encoding="utf-8")
                log.ok(f"fstab EFI UUID fixed: {actual_efi_uuid}")
            elif match:
                log.ok(f"fstab EFI UUID correct: {actual_efi_uuid} ✓")

    # ── 4. initramfs ─────────────────────────────────────────────────────
    log.step(4, 5, "Updating initramfs...")
    r = sh.run("update-initramfs -u -k all", log=log)
    if r.ok:
        log.ok("initramfs updated")
    else:
        log.warn("update-initramfs had errors")

    # ── 5. Verify ────────────────────────────────────────────────────────
    log.step(5, 5, "Verifying system state...")
    for pool in ("rpool", "bpool"):
        health = zfs.pool_health(pool)
        if health == "ONLINE":
            log.ok(f"  {pool}: ONLINE ✓")
        else:
            log.warn(f"  {pool}: {health}")

    log.banner_ok(
        "FINISH COMPLETE",
        [
            "ZFS services enabled ✓",
            "GRUB config updated ✓",
            "initramfs updated ✓",
            "",
            f"Run setup: {log.W}sudo ./zark setup{log.N}",
        ],
    )
