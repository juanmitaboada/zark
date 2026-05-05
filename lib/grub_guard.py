"""
GRUB guard installer — single source of truth for /etc/grub.d/09_zfs_backup_guard.

The guard is a small shell script dropped into /etc/grub.d that runs as part
of `update-grub`. Its purpose is to abort the run when external ZFS pools are
visible to the system: Ubuntu's `10_linux_zfs` auto-imports every visible pool
and tries to mount their encrypted datasets, fails, and leaves grub.cfg with
no kernel entries — a hard-to-debug brick.

This module centralises both the script body and the install logic so all
three callers (`zark recover`, `zark repair-boot`, `zark finish`) deposit the same
file. Previously each command duplicated the script in a heredoc, with the
obvious risk that fixing a typo in one wouldn't reach the others.
"""

from pathlib import Path

from lib.log import Log

GUARD_RELATIVE_PATH = "etc/grub.d/09_zfs_backup_guard"

GUARD_SCRIPT = """\
#!/bin/sh
# 09_zfs_backup_guard — Prevent update-grub from running when external
# ZFS backup pools are connected.  10_linux_zfs auto-imports ALL visible
# pools and tries to mount their (encrypted) datasets.  When it fails,
# the bootlist parsing breaks and grub.cfg ends up with NO kernel entries.
#
# Installed by zark.  Safe to remove if you don't use zark.

set -e

# Pools that should be importable — only rpool and bpool
ALLOWED_POOLS="rpool bpool"

# List pools available for import (exported or on connected drives)
available="$(zpool import 2>/dev/null | awk '/pool:/{print $2}' | sort -u)"

extra=""
for pool in $available; do
    is_allowed=false
    for a in $ALLOWED_POOLS; do
        [ "$pool" = "$a" ] && is_allowed=true
    done
    $is_allowed || extra="$extra $pool"
done

if [ -n "$extra" ]; then
    echo "ERROR: External ZFS pool(s) detected:$extra" >&2
    echo "" >&2
    echo "  update-grub cannot run safely while external ZFS pools are visible." >&2
    echo "  The 10_linux_zfs script will try to mount their encrypted datasets," >&2
    echo "  fail, and generate a grub.cfg WITHOUT kernel entries." >&2
    echo "" >&2
    echo "  Fix: disconnect the external drive(s) and run update-grub again." >&2
    echo "" >&2
    exit 1
fi
"""


def install(target_root: str, log: Log, *, overwrite: bool = True) -> None:
    """
    Write the GRUB guard script to {target_root}/etc/grub.d/09_zfs_backup_guard
    and make it executable.

    Args:
        target_root: filesystem root where the guard should be installed.
            Use "" or "/" for the running system; use a chroot/altroot path
            (e.g. "/mnt/recover") when installing into a recovered system.
        log: logger for the success message.
        overwrite: if False, leave an existing file untouched. Defaults to
            True because every caller in zark wants the canonical version.
    """
    root = target_root.rstrip("/")
    guard_path = Path(f"{root}/{GUARD_RELATIVE_PATH}") if root else Path(f"/{GUARD_RELATIVE_PATH}")

    if guard_path.exists() and not overwrite:
        return

    guard_path.parent.mkdir(parents=True, exist_ok=True)
    _ = guard_path.write_text(GUARD_SCRIPT, encoding="utf-8")
    guard_path.chmod(0o755)
    log.ok("Grub guard installed (09_zfs_backup_guard) ✓")
