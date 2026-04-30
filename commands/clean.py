"""
zark clean — Emergency cleanup.

Unmounts everything, exports all pools. Use when a previous
operation left things in a dirty state.
"""

from pathlib import Path

from lib import sh
from lib.log import Log


def run(args: list[str]):  # pylint: disable=too-many-branches
    """Run the cleanup."""
    del args  # Unused
    log = Log()

    log.banner("EMERGENCY CLEANUP", "Unmount everything, export all pools")

    # Unmount /mnt/recover, /mnt/zark, /mnt/grub_*
    for pattern in ("/mnt/recover", "/mnt/zark", "/mnt/grub_*"):
        for d in Path("/mnt").glob(pattern.replace("/mnt/", "")):
            if d.is_dir():
                log.info(f"Unmounting {d}...")
                _ = sh.run(f"umount -R {d}")
                _ = sh.run(f"rm -rf {d}")

    # Unmount any temp mounts
    for pattern in ("/tmp/bpool_*", "/tmp/grub_*", "/tmp/hostid_*", "/tmp/rpool_*"):
        for d in Path("/tmp").glob(pattern.replace("/tmp/", "")):
            _ = sh.run(f"umount {d}")
            _ = sh.run(f"rmdir {d}")

    # Unmount all ZFS
    log.info("Unmounting all ZFS datasets...")
    _ = sh.run("zfs unmount -a")

    # Close any zark keystores
    r = sh.run("ls /dev/mapper/zark_ks_* 2>/dev/null")
    for line in r.lines:
        name = Path(line.strip()).name
        _ = sh.run("umount /run/keystore/rpool")
        _ = sh.run(f"cryptsetup close {name}")
        log.dbg(f"Closed keystore: {name}")

    # Export all non-system pools
    log.info("Exporting pools...")
    r = sh.run("zpool list -H -o name")
    for pool in r.lines:
        pool = pool.strip()
        if pool in ("rpool", "bpool"):
            continue
        r2 = sh.run(f"zpool export -f {pool}")
        if r2.ok:
            log.ok(f"Exported {pool}")

    # Also try system pools if on live USB
    if not sh.run("zpool list rpool").ok:
        log.dbg("No rpool — likely on live USB, nothing more to export")
    else:
        for pool in ("bpool", "rpool"):
            r2 = sh.run(f"zpool export -f {pool}")
            if r2.ok:
                log.ok(f"Exported {pool}")

    log.ok("Cleanup complete")
    log.blank()
