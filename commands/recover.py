"""
zark recover — Full bare-metal system recovery from backup drive.

Must run from Ubuntu live USB with backup drive connected.

CRITICAL RULES:
  - ZERO 'zfs set mountpoint/canmount' while ANY pool with zvols is imported.
  - ZERO rapid pool export/import cycles (corrupts live USB overlay).
  - Keystore zvol (rpool/keystore) is restored LAST to avoid the kernel
    udev/systemd crash (chase.c:648).

Recovery order:
  1-5.   Find drive, import, unlock, sync hostid, select snapshot
  6-7.   Partition disk, create bpool + rpool
  8.     Raw send ROOT + USERDATA (no keystore = no zvols)
  9.     Export backup pool (removes its zvols)
  10.    Load keys from saved system.key
  11.    Set mountpoints + canmount (safe: no zvols anywhere)
  12.    Reimport backup pool:
           12a. Restore bpool content  (NO zvols on rpool yet — safe)
           12b. Restore keystore zvol  (adds zvol — LAST zfs operation)
         Export backup pool.
  13.    Mount system, write EFI/crypttab/fstab
  14.    Restore hostid to target
  15.    Chroot: cachefile, grub.cfg UUID fix, grub-install, initrd
  16.    Cleanup and summary
"""

import os
import re
import shutil
import time
from pathlib import Path
from typing import NoReturn

from lib import grub_guard, sh
from lib.cleanup import Cleanup
from lib.config import VERSION, Config
from lib.drives import scan_connected_drives, select_drive
from lib.keystore import SYSTEM_KEY_PATH, Keystore
from lib.log import Log
from lib.zfs import ZFS, fix_grub_bpool_uuid

# bpool features safe for GRUB 2.12 (Ubuntu 24.04 / initramfs-tools)
BPOOL_FEATURES_BASE = (
    "async_destroy bookmarks embedded_data empty_bpobj enabled_txg "
    "extensible_dataset filesystem_limits hole_birth large_blocks "
    "lz4_compress spacemap_histogram"
)

# Additional bpool features for Ubuntu 25.04+ (dracut systems).
#
# These are the bpool features Ubuntu's installer activates by default on
# 25.04+. Enabling them on the recovered bpool keeps the pool layout
# consistent with a stock install and avoids surprises on the first
# `apt upgrade` (which may auto-enable them).
#
# CRITICAL: this list MUST be a subset of /usr/share/zfs/compatibility.d/grub2
# (the features GRUB2's built-in ZFS reader supports). GRUB reads bpool to
# load the kernel during boot. If bpool has features GRUB doesn't recognize,
# GRUB rejects the entire filesystem as unreadable and boot fails with
# "file '/BOOT/.../vmlinuz-...' not found" — even though Linux can read the
# pool fine.
#
# Specifically excluded: head_errlog, vdev_zaps_v2 — both unsupported by
# GRUB up to and including 2.14 (the version shipped in Ubuntu 26.04).
# rpool is unaffected by this restriction since rpool is read by the
# kernel's full ZFS module, never by GRUB.
BPOOL_FEATURES_EXTENDED = "userobj_accounting project_quota spacemap_v2 log_spacemap"


RECOVER_MNT = "/mnt/recover"
TOTAL_STEPS = 16

# Expected layout under rpool/ROOT/<ubuntu_name>/ in a stock Ubuntu ZFS install
# (24.04 / 25.xx). Names are stored relative to the root dataset so they can be
# compared against `zfs list` output without prefix juggling.
#
# The Ubuntu installer marks `usr` and `var` as canmount=off (they are pure
# containers — their data lives in their children). All other expected children
# are leaf datasets that get canmount=on so they auto-mount on boot.
#
# These two sets are also used to detect layout drift during recovery: missing
# datasets (in the list but absent from the source) and extra datasets (present
# in the source but unknown to zark) are reported as warnings at the end of the
# run. zark only applies properties to datasets it recognises — extras keep
# whatever properties the send/receive stream brought in.
_UBUNTU_ROOT_CHILDREN_OFF = frozenset({"usr", "var"})
_UBUNTU_ROOT_CHILDREN_ON = frozenset(
    {
        "usr/local",
        "var/lib",
        "var/log",
        "var/mail",
        "var/snap",
        "var/spool",
        "var/www",
        "var/games",
        "var/lib/apt",
        "var/lib/dpkg",
        "var/lib/AccountsService",
        "var/lib/NetworkManager",
        "srv",
    },
)
_UBUNTU_ROOT_CHILDREN_ALL = _UBUNTU_ROOT_CHILDREN_OFF | _UBUNTU_ROOT_CHILDREN_ON


def _force_latest_signed_alternative(
    chroot_path: str,
    name: str,
    latest_path: str,
    log: Log,
) -> None:
    """Force update-alternatives to point at the ``.signed.latest`` variant.

    Since shim-signed 1.51 (Ubuntu 22.04 SRU and 24.04+), the package ships
    two variants under update-alternatives: ``shimx64.efi.signed.latest``
    (currently shim 15.8) and ``shimx64.efi.signed.previous`` (currently
    15.4). Same split exists for grub-efi-amd64-signed.

    Subiquity in Ubuntu 26.04 has been observed to leave systems pointing
    at ``.previous``, which is then revoked by SBAT_LEVEL updates and
    causes ``Verifying shim SBAT data failed: Security Policy Violation``
    on the next boot. To avoid recover propagating this state to the
    restored system, we explicitly switch to ``.latest`` before invoking
    ``dpkg-reconfigure`` (which copies the chosen alternative to the ESP).

    Defensive about older releases: if the ``.latest`` variant doesn't
    exist in the chroot (e.g. very old Ubuntu without this split), the
    function logs a debug note and returns without changes. The
    subsequent ``dpkg-reconfigure`` still runs and uses whatever the
    default alternative is.

    Args:
        chroot_path: filesystem root where the recovered Ubuntu lives.
        name: alternative name (e.g. "shimx64.efi.signed").
        latest_path: absolute path inside the chroot to the .latest file
            (e.g. "/usr/lib/shim/shimx64.efi.signed.latest").
        log: logger.
    """
    if not Path(f"{chroot_path}{latest_path}").exists():
        log.dbg(f"  {name}: no .latest variant in chroot — using default alternative")
        return
    r = sh.run(
        f"chroot {chroot_path} update-alternatives --set {name} {latest_path}",
        log=log,
    )
    if r.ok:
        log.ok(f"  {name} → {latest_path} (latest variant forced)")
    else:
        log.warn(f"  {name}: update-alternatives --set failed (rc={r.returncode})")


def _is_live_usb() -> bool:
    r = sh.run("cat /proc/cmdline")
    if r.ok and any(k in r.output for k in ("boot=casper", "boot=live", "live-media")):
        return True
    return sh.run("test -d /rofs").ok or sh.run("test -d /cow").ok


# Free-space margin for the preventive disk-size check. The target NVMe
# is wiped and freshly partitioned, but ~5% is consumed by the EFI System
# Partition (1 GiB), bpool (2 GiB), swap (8 GiB) and ZFS pool metadata.
# Using a flat 5% multiplier on src.used_bytes is a coarse but sufficient
# approximation: the reactive ENOSPC handler in _raw_send catches anything
# the preventive guard misses.
_RECOVER_TARGET_OVERHEAD_PCT = 5  # added to src.used_bytes for the threshold


def _disk_size_bytes(disk: str) -> int:
    """Return the size of a block device in bytes, or 0 on failure.

    Uses `lsblk -bdn -o SIZE` (bytes, no header, no children). Returns 0
    if the device is missing, lsblk is unavailable, or the output cannot
    be parsed — in which case the preventive check defers silently to
    the reactive ENOSPC handler.
    """
    r = sh.run(f"lsblk -bdn -o SIZE {disk}")
    if not r.ok:
        return 0
    try:
        return int(r.output.strip())
    except (ValueError, AttributeError):
        return 0


def _check_target_disk_size(
    target_disk: str,
    pool_name: str,
    log: Log,
    zfs: ZFS,
) -> None:
    """Refuse recovery if the target disk is too small for the backup data.

    Compares the target NVMe's physical size against the backup's
    rpool used_bytes plus a 5% overhead margin (EFI/bpool/swap/metadata).
    If the target cannot fit even the bare data, fatal — recovery is
    guaranteed to ENOSPC mid-stream and waste the operator's time.

    Silently skipped if either size measurement returns 0 (transient
    failure of lsblk or zfs list); the reactive _raw_send ENOSPC
    handler will still catch in-flight exhaustion.
    """
    target_size = _disk_size_bytes(target_disk)
    if target_size <= 0:
        log.dbg(f"Could not measure {target_disk} size — skipping preventive check")
        return

    # The data we are going to copy is everything under {pool_name}/rpool.
    # That's a dataset, not a pool, so we use `zfs list -p -o used`, not
    # `zpool list`. used here includes all child datasets and snapshots
    # (the recursive accounting that `zfs list` reports).
    src_used = zfs.dataset_used_bytes(f"{pool_name}/rpool")
    if src_used <= 0:
        log.dbg(
            f"Could not measure {pool_name}/rpool used_bytes — skipping preventive check",
        )
        return

    threshold = src_used * (100 + _RECOVER_TARGET_OVERHEAD_PCT) // 100
    if target_size < threshold:
        log.fatal(
            f"Target disk {target_disk} too small to fit backup",
            causes=[
                f"Backup {pool_name}/rpool: used={sh.humanize_bytes(src_used)}",
                f"Target {target_disk}: {sh.humanize_bytes(target_size)}",
                f"Required: ≥ {sh.humanize_bytes(threshold)} "
                f"(used + {_RECOVER_TARGET_OVERHEAD_PCT}% overhead)",
            ],
            solutions=[
                "Recover to a larger disk",
                "Or shrink the backup by purging snapshots before recovery",
            ],
        )


def _raw_send(src: str, dst: str, snap: str, log: Log) -> bool:
    """Raw send a single dataset@snap to dst."""
    r = sh.run(f"zfs list -H -o name -t snapshot {src}")
    if not r.ok or not r.lines:
        log.warn(f"No snapshots for {src}")
        return False

    best = None
    for line in r.lines:
        if f"@{snap}" in line:
            best = line.strip()
            break
    if not best:
        best = r.lines[-1].strip()

    short_dst = dst.split("/", 1)[-1] if "/" in dst else dst
    snap_name = best.split("@")[1] if "@" in best else best
    log.info(f"  {short_dst} @ {snap_name}")

    r = sh.run_pipe(f"zfs send -w {best}", f"zfs receive -F {dst}")
    if r.ok:
        log.dbg(f"  raw send OK: {dst}")
        return True
    # Reactive ENOSPC: ENOSPC during recover is always fatal — there is
    # no point in continuing to subsequent datasets when the target is
    # full; they will all fail the same way. The non-ENOSPC `return False`
    # path below is preserved for genuinely optional dataset failures
    # (callers at the rpool/ROOT/USERDATA loops use `_ = _raw_send(...)`
    # to keep going on individual misses).
    if sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout):
        log.fatal(
            f"Recovery ran out of space while restoring {dst}",
            causes=[
                "Target disk filled up during raw send",
                "Backup data exceeds target disk capacity (preventive check missed it)",
            ],
            solutions=[
                "Recover to a larger disk",
                "Or shrink the backup by purging snapshots before retry",
            ],
        )
    log.warn(f"  raw send failed: {dst}: {r.stderr.strip()}")
    return False


def _load_keys_from_file(keyfile: str, pool_root: str, log: Log) -> int:
    """Load encryption keys for all datasets using a key file."""
    zfs = ZFS(log)
    count = 0
    for ds in zfs.datasets_needing_key(pool_root):
        if sh.run(f"zfs load-key -L file://{keyfile} {ds}").ok:
            count += 1
            log.dbg(f"Key loaded: {ds}")
    return count


def _bpool_send_recv(pool_name: str, ubuntu_name: str, log: Log, zfs: ZFS) -> bool:
    """
    Restore bpool/BOOT/{ubuntu_name} via send/receive — no mounting required.

    This sidesteps all mountpoint issues (mountpoint=none, mountpoint=/boot, legacy)
    by never trying to mount the backup bpool dataset at all.

    SAFE WITH ZVOLS: 'zfs send | zfs receive' does NOT trigger udev device events
    and does NOT require 'zfs set mountpoint'. It is safe to call while the backup
    pool (with its keystore zvol) is imported.

    The received dataset will have mountpoint=none (inherited from backup).
    Caller MUST call _bpool_fix_mountpoint() AFTER exporting black (no zvols).

    Both src and dst bpool use the same limited GRUB-compatible feature set,
    so the receive stream is always compatible.
    """
    bpool_src = f"{pool_name}/bpool/BOOT/{ubuntu_name}"

    if not zfs.dataset_exists(f"{pool_name}/bpool"):
        log.warn("No bpool in backup — kernels will need reinstallation")
        return False
    if not zfs.dataset_exists(bpool_src):
        log.warn(f"No {bpool_src} in backup — kernels will need reinstallation")
        return False

    # Find latest snapshot
    bpool_snap = sh.run(f"zfs list -H -o name -t snapshot {bpool_src} | tail -1").output.strip()
    if not bpool_snap:
        log.warn(f"No snapshots in {bpool_src} — kernels will need reinstallation")
        return False

    log.dbg(f"bpool snapshot: {bpool_snap}")

    # Create bpool/BOOT container — no zfs set, safe with zvols present
    _ = sh.run("zfs create -o canmount=off -o mountpoint=none bpool/BOOT", log=log)

    # Send/receive — properties flow in the stream; mountpoint=none is expected
    r = sh.run_pipe(
        f"zfs send {bpool_snap}",
        f"zfs receive -F bpool/BOOT/{ubuntu_name}",
    )
    if r.ok:
        log.ok("bpool received via send/receive ✓")
        return True
    if sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout):
        # bpool ENOSPC during recover: fatal. Unlike backup, recover has
        # no "stale data is better than nothing" tradeoff — the target
        # bpool is freshly created and any partial data is unbootable.
        log.fatal(
            "Recovery ran out of space while restoring bpool",
            causes=[
                "Target disk filled up during bpool send/receive",
                "Backup data exceeds target disk capacity (preventive check missed it)",
            ],
            solutions=[
                "Recover to a larger disk",
                "Or shrink the backup by purging snapshots before retry",
            ],
        )
    log.warn(f"bpool send/receive failed: {r.stderr.strip()}")
    return False


def _bpool_fix_mountpoint(ubuntu_name: str, log: Log, zfs: ZFS) -> bool:
    """
    Set bpool/BOOT/{ubuntu_name} mountpoint to /boot and mount it.

    PRECONDITION: No zvols active anywhere (safe to call zfs set).
    Must be called AFTER the backup pool (black) has been exported.
    """
    ds = f"bpool/BOOT/{ubuntu_name}"
    if not zfs.dataset_exists(ds):
        log.warn(f"{ds} not found — bpool send/receive may have failed")
        return False

    _ = zfs.set_property(ds, "canmount", "on")
    _ = zfs.set_property(ds, "mountpoint", "/boot")

    # Mount — altroot (-R /mnt/recover) makes this land at /mnt/recover/boot
    _ = sh.run(f"mkdir -p {RECOVER_MNT}/boot")
    if not sh.run(f"zfs mount {ds}").ok:
        # Fallback: explicit path
        _ = sh.run(f"mount -t zfs {ds} {RECOVER_MNT}/boot")

    kernel_count = len(list(Path(f"{RECOVER_MNT}/boot").glob("vmlinuz*")))
    log.ok(f"bpool mounted at {RECOVER_MNT}/boot ✓  ({kernel_count} kernel(s))")
    return True


def _abort_missing_keystore(reason: str, pool_name: str, log: Log) -> NoReturn:
    """
    Abort the recovery when the keystore zvol cannot be restored from backup.

    Reaching this point means the security model of the recovered system would
    be silently degraded: the rpool encryption depends on `system.key`, which
    normally lives inside the LUKS-encrypted keystore zvol. Without that zvol,
    the recovered system has no way to load its keys at boot.

    Two unsafe fallbacks were considered and deliberately rejected for v1.0.5:

      (b) Embed system.key in the target rootfs (e.g. /etc/zfs/system.key).
          Trivial to implement but degrades the security model — the raw key
          ends up readable in the initrd, defeating the LUKS layer.

      (c) Switch rpool to keylocation=prompt with `zfs change-key`. Viable
          but requires re-wrapping the master key with a brand-new passphrase
          chosen during recovery, which is a non-trivial UX path and easy to
          get wrong under pressure.

    Both options are recoverable from after the fact (the user can apply
    them manually on a successfully recovered system if needed), so the
    safe default is to refuse and tell the user how to fix the backup.

    `reason` is one of:
      "no_dataset"  → backup pool has no <pool>/keystore dataset
      "no_snapshot" → dataset exists but has no snapshots
      "send_failed" → send/receive of the keystore zvol failed
    """
    causes_by_reason = {
        "no_dataset": [
            f"The backup pool '{pool_name}' has no '{pool_name}/keystore' dataset.",
            "This usually means the backup was prepared with an old zark/backup_zfs",
            "version that didn't sync the keystore, or the dataset was deleted",
            "manually after preparation.",
        ],
        "no_snapshot": [
            f"The '{pool_name}/keystore' dataset exists but has no snapshots.",
            "Without a snapshot there is nothing to send/receive.",
            "This is unusual — `zark prepare` always creates an initial snapshot.",
        ],
        "send_failed": [
            f"`zfs send {pool_name}/keystore@... | zfs receive rpool/keystore` failed.",
            "Possible causes: USB I/O error, corrupted snapshot, ZFS version",
            "mismatch between source and target.",
        ],
    }
    causes = causes_by_reason.get(reason, [f"Unknown failure mode: {reason}"])

    log.banner_error(
        "RECOVERY ABORTED — KEYSTORE MISSING FROM BACKUP",
        [
            f"{log.Y}Why this is fatal:{log.N}",
            "  The rpool is raw-encrypted with a key stored inside a LUKS-",
            "  encrypted zvol called the 'keystore'. Without restoring that",
            "  keystore zvol on the new system, the recovered rpool would",
            "  have no way to load its encryption key at boot.",
            "",
            f"{log.Y}What went wrong:{log.N}",
            *[f"  • {c}" for c in causes],
            "",
            f"{log.G}How to recover:{log.N}",
            "  1. If the original system is still alive:",
            "     → Boot the original system and re-run `zark prepare` on the",
            f"       backup drive. This recreates {pool_name}/keystore properly.",
            "     → Then re-run `zark backup` to refresh data.",
            "     → Then retry `zark recover` from the live USB.",
            "",
            "  2. If the original system is gone:",
            "     → If you have ANOTHER backup drive prepared from the same",
            "       origin, use that one instead.",
            "     → If not, the rpool data on this backup is unreadable",
            "       without the keystore zvol. Sorry.",
            "",
            f"{log.Y}Why zark refuses to continue:{log.N}",
            "  Two fallbacks exist (embedding the raw key in the rootfs, or",
            "  switching to a passphrase prompt at boot). Both silently",
            "  degrade the security model that you set up when you installed",
            "  Ubuntu with ZFS encryption + keystore. zark will not make that",
            "  decision for you.",
        ],
    )
    raise SystemExit(1)


def _install_keystore_dracut(recover_mnt: str, ubuntu_name: str, log: Log):
    """Install dracut 89keystore module (Ubuntu 25.04+)."""
    ks_mod_dir = Path(f"{recover_mnt}/usr/lib/dracut/modules.d/89keystore")
    ks_mod_dir.mkdir(parents=True, exist_ok=True)

    _ = (ks_mod_dir / "module-setup.sh").write_text("""#!/bin/bash
# dracut module: 89keystore — unlock ZFS keystore zvol

check() { require_binaries cryptsetup || return 1; return 0; }
depends() { echo "zfs crypt"; }
install() {
    inst_hook pre-mount 01 "$moddir/keystore-open.sh"
    inst_multiple cryptsetup blkid mount mkdir zfs awk
}
""")
    (ks_mod_dir / "module-setup.sh").chmod(0o755)

    _ = (ks_mod_dir / "keystore-open.sh").write_text(f"""#!/bin/bash
# Open rpool/keystore LUKS zvol and mount at /run/keystore/rpool.
# Runs as dracut pre-mount hook (priority 01 = before ZFS mount at ~90).
# At this point, rpool is imported (zfs-import-cache.service) so the zvol exists.

KEYSTORE_MNT="/run/keystore/rpool"
MAPPER_NAME="keystore-rpool"

# Already mounted? Skip.
[ -f "$KEYSTORE_MNT/system.key" ] && exit 0

# Find the keystore zvol device
ZVOL_DEV=""
if [ -b /dev/zvol/rpool/keystore ]; then
    ZVOL_DEV="/dev/zvol/rpool/keystore"
else
    for dev in /dev/zd*; do
        [ -b "$dev" ] || continue
        case "$dev" in /dev/zd[0-9]*p*) continue;; esac
        if blkid -s TYPE -o value "$dev" 2>/dev/null | grep -q crypto_LUKS; then
            ZVOL_DEV="$dev"
            break
        fi
    done
fi

[ -z "$ZVOL_DEV" ] && exit 0

# Open LUKS — password prompt via systemd-ask-password (integrates with plymouth)
if [ ! -b "/dev/mapper/$MAPPER_NAME" ]; then
    info "zark: Opening ZFS keystore ($ZVOL_DEV)..."
    PW="$(systemd-ask-password 'Passphrase for ZFS keystore:')"
    printf '%s' "$PW" | cryptsetup open --key-file=- "$ZVOL_DEV" "$MAPPER_NAME"
    _rc=$?
    unset PW
    [ "$_rc" -eq 0 ] && info "zark: LUKS keystore opened" || warn "zark: cryptsetup failed (rc=$_rc)"
fi

# Mount the key filesystem
if [ -b "/dev/mapper/$MAPPER_NAME" ] && [ ! -f "$KEYSTORE_MNT/system.key" ]; then
    mkdir -p "$KEYSTORE_MNT"
    mount "/dev/mapper/$MAPPER_NAME" "$KEYSTORE_MNT" 2>/dev/null
fi

if [ -f "$KEYSTORE_MNT/system.key" ]; then
    info "zark: system.key available"
    # Load ZFS encryption keys — try stored keylocation first, then explicit path
    if ! zfs load-key -a 2>/dev/null; then
        for ds in $(zfs list -H -o name,encryptionroot -r rpool 2>/dev/null | \\
                     awk '$2 != "-" && $1 == $2 {{{{print $1}}}}'); do
            zfs load-key -L "file://$KEYSTORE_MNT/system.key" "$ds" 2>/dev/null
        done
    fi
    keystatus="$(zfs get -H -o value keystatus rpool/ROOT/{ubuntu_name} 2>/dev/null)"
    [ "$keystatus" = "available" ] && info "zark: ZFS keys loaded" || warn "zark: key load failed"
else
    warn "zark: system.key NOT found after keystore open"
fi
""")  # noqa: E501
    (ks_mod_dir / "keystore-open.sh").chmod(0o755)
    log.ok("dracut keystore module installed (89keystore)")


def _install_keystore_initramfs(recover_mnt: str, ubuntu_name: str, log: Log):
    """Install initramfs-tools keystore hook + script (Ubuntu 24.04)."""
    hooks_dir = Path(f"{recover_mnt}/etc/initramfs-tools/hooks")
    scripts_dir = Path(f"{recover_mnt}/etc/initramfs-tools/scripts/local-premount")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Hook: include required binaries in the initrd
    _ = (hooks_dir / "keystore").write_text("""#!/bin/sh
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in prereqs) prereqs; exit 0;; esac

. /usr/share/initramfs-tools/hook-functions

copy_exec /sbin/cryptsetup /sbin
copy_exec /sbin/blkid /sbin
copy_exec /sbin/zfs /sbin
copy_exec /usr/bin/awk /usr/bin

# askpass for plymouth-integrated password prompt
if [ -x /lib/cryptsetup/askpass ]; then
    copy_exec /lib/cryptsetup/askpass /lib/cryptsetup
fi
""")
    (hooks_dir / "keystore").chmod(0o755)

    # Boot-time script: runs in local-premount (after ZFS import, before root mount)
    # PREREQ="zfs" ensures this runs after the zfs initramfs script imports pools.
    _ = (scripts_dir / "ORDER-keystore").write_text(f"""#!/bin/sh
# zark: Open rpool/keystore LUKS zvol and load ZFS encryption keys.
# Runs in local-premount phase (after zfs local-top imports pools).
PREREQ="zfs"
prereqs() {{ echo "$PREREQ"; }}
case "$1" in prereqs) prereqs; exit 0;; esac

. /scripts/functions

KEYSTORE_MNT="/run/keystore/rpool"
MAPPER_NAME="keystore-rpool"

# Already mounted? Skip.
[ -f "$KEYSTORE_MNT/system.key" ] && exit 0

# Find the keystore zvol device
ZVOL_DEV=""
if [ -b /dev/zvol/rpool/keystore ]; then
    ZVOL_DEV="/dev/zvol/rpool/keystore"
else
    for dev in /dev/zd*; do
        [ -b "$dev" ] || continue
        case "$dev" in /dev/zd[0-9]*p*) continue;; esac
        if blkid -s TYPE -o value "$dev" 2>/dev/null | grep -q crypto_LUKS; then
            ZVOL_DEV="$dev"
            break
        fi
    done
fi

[ -z "$ZVOL_DEV" ] && exit 0

# Open LUKS — password prompt via askpass (integrates with plymouth)
if [ ! -b "/dev/mapper/$MAPPER_NAME" ]; then
    log_begin_msg "zark: Opening ZFS keystore ($ZVOL_DEV)"
    if [ -x /lib/cryptsetup/askpass ]; then
        PW="$(/lib/cryptsetup/askpass 'Passphrase for ZFS keystore: ')"
    else
        # Fallback: direct read from console
        printf 'Passphrase for ZFS keystore: ' >/dev/console
        read -r PW </dev/console
    fi
    printf '%s' "$PW" | cryptsetup open --key-file=- "$ZVOL_DEV" "$MAPPER_NAME"
    _rc=$?
    unset PW
    if [ "$_rc" -eq 0 ]; then
        log_end_msg 0
    else
        log_end_msg 1
        log_warning_msg "zark: cryptsetup failed (rc=$_rc)"
    fi
fi

# Mount the key filesystem
if [ -b "/dev/mapper/$MAPPER_NAME" ] && [ ! -f "$KEYSTORE_MNT/system.key" ]; then
    mkdir -p "$KEYSTORE_MNT"
    mount "/dev/mapper/$MAPPER_NAME" "$KEYSTORE_MNT" 2>/dev/null
fi

if [ -f "$KEYSTORE_MNT/system.key" ]; then
    log_success_msg "zark: system.key available"
    # Load ZFS encryption keys — try stored keylocation first, then explicit path
    if ! zfs load-key -a 2>/dev/null; then
        for ds in $(zfs list -H -o name,encryptionroot -r rpool 2>/dev/null | \
                     awk '$2 != "-" && $1 == $2 {{print $1}}'); do
            zfs load-key -L "file://$KEYSTORE_MNT/system.key" "$ds" 2>/dev/null
        done
    fi
    keystatus="$(zfs get -H -o value keystatus rpool/ROOT/{ubuntu_name} 2>/dev/null)"
    if [ "$keystatus" = "available" ]; then
        log_success_msg "zark: ZFS keys loaded"
    else
        log_warning_msg "zark: key load failed"
    fi
else
    log_warning_msg "zark: system.key NOT found after keystore open"
fi
""")
    (scripts_dir / "ORDER-keystore").chmod(0o755)
    log.ok("initramfs-tools keystore module installed (hook + local-premount)")


def _detect_root_children(ubuntu_name: str) -> set[str]:
    """
    List actual child datasets under rpool/ROOT/<ubuntu_name>/ as relative
    names (e.g. "usr/local", "var/lib/apt"). Returns an empty set if the
    parent dataset can't be listed.
    """
    prefix = f"rpool/ROOT/{ubuntu_name}/"
    r = sh.run(f"zfs list -H -o name -r rpool/ROOT/{ubuntu_name}")
    if not r.ok:
        return set()
    children: set[str] = set()
    for line in r.output.splitlines():
        name = line.strip()
        if name and name.startswith(prefix):
            children.add(name[len(prefix) :])
    return children


def _apply_root_children_canmount(
    ubuntu_name: str,
    zfs: ZFS,
    log: Log,
) -> tuple[set[str], set[str]]:
    """
    Apply canmount properties to root children that exist in the source system,
    matching what the Ubuntu installer would set:

      - usr, var               → canmount=off (containers)
      - everything else known  → canmount=on  (leaf datasets)

    Datasets present in zark's expected list but missing from the source are
    silently skipped here (returned as `missing` for end-of-run reporting).
    Datasets present in the source but unknown to zark are NOT touched — they
    keep whatever properties the send/receive stream carried (returned as
    `extra` for reporting).

    Returns (missing, extra) — both are sets of relative dataset names.
    """
    del log  # log is only used for debugging inside this function, not for user-facing messages
    actual = _detect_root_children(ubuntu_name)

    missing = set(_UBUNTU_ROOT_CHILDREN_ALL - actual)
    extra = actual - _UBUNTU_ROOT_CHILDREN_ALL

    # Only operate on the intersection so we never log "dataset does not exist".
    for ds in sorted(_UBUNTU_ROOT_CHILDREN_OFF & actual):
        _ = zfs.set_property(f"rpool/ROOT/{ubuntu_name}/{ds}", "canmount", "off")
    for ds in sorted(_UBUNTU_ROOT_CHILDREN_ON & actual):
        _ = zfs.set_property(f"rpool/ROOT/{ubuntu_name}/{ds}", "canmount", "on")

    return missing, extra


def _emit_dataset_layout_warnings(
    ubuntu_name: str,
    missing: set[str],
    extra: set[str],
    log: Log,
) -> None:
    """
    Emit a single end-of-run warning block describing layout drift between
    the source system and zark's expected Ubuntu layout. No-op if both sets
    are empty.
    """
    if not missing and not extra:
        return

    log.warn("Dataset layout differs from expected Ubuntu installer layout:")
    if missing:
        log.warn("  Missing (zark expected, not present in source):")
        for ds in sorted(missing):
            log.warn(f"    - rpool/ROOT/{ubuntu_name}/{ds}")
    if extra:
        log.warn("  Extra (present in source, unknown to zark):")
        for ds in sorted(extra):
            log.warn(f"    - rpool/ROOT/{ubuntu_name}/{ds}")
        log.warn(
            "  Extra datasets were restored via send/receive but their "
            + "canmount/mountpoint properties were NOT modified by zark.",
        )
        log.warn("  Verify with: zfs list -o name,canmount,mountpoint rpool")


def run(
    args: list[str],
):  # pylint: disable= too-many-statements, too-many-branches, too-many-locals
    """Main entry point for 'zark recover'. See module docstring for details."""
    del args  # no CLI args supported (yet)
    log = Log()
    cfg = Config.load()
    zfs = ZFS(log)
    cleanup = Cleanup(log)
    cleanup.register()

    recover_start = time.time()

    log.banner(
        f"FULL SYSTEM RECOVERY v{VERSION}",
        "Run from Ubuntu live USB with backup drive connected",
    )

    uptime = sh.run("uptime -p").output or sh.run("cat /proc/uptime").output
    log.info(f"System uptime: {uptime}")

    # ── Verify live USB ──────────────────────────────────────────────────
    if not _is_live_usb():
        log.warn("NOT running from a live USB environment")
        try:
            confirm = input("  Type IUNDERSTAND to continue anyway: ").strip()
        except EOFError:
            confirm = ""
        if confirm != "IUNDERSTAND":
            return

    if not sh.run("which syncoid").ok:
        log.info("Installing required packages...")
        _ = sh.run(
            "apt-get install -y sanoid zfsutils-linux gdisk bc pv mbuffer lzop",
            log=log,
        )

    # ── 1. Find backup drive ─────────────────────────────────────────────
    log.step(1, TOTAL_STEPS, "Scanning for backup drives...")
    drives = scan_connected_drives(cfg, log)
    if not drives:
        log.fatal("No backup drives detected")

    drive = select_drive(drives, log, known_only=False)
    if not drive:
        return
    pool_name = drive.name

    # ── 2. Import and unlock backup pool ─────────────────────────────────
    log.step(2, TOTAL_STEPS, f"Importing pool {pool_name}...")

    if not zfs.pool_import(pool_name, no_mount=True):
        log.fatal(f"Cannot import pool {pool_name}")
    cleanup.track_pool(pool_name)

    passphrase = log.ask_password(f"Passphrase for {pool_name}")
    ks = Keystore(log)
    if not ks.mount(pool_name, passphrase):
        log.fatal("Cannot open keystore")
    cleanup.track_keystore(ks)
    _ = ks.load_pool_keys(f"{pool_name}/rpool")
    log.ok("Encryption key loaded ✓")

    # Save system.key to temp — survives pool exports, used throughout recovery
    tmp_key = f"/tmp/zark_syskey_{os.getpid()}"
    _ = shutil.copy2(SYSTEM_KEY_PATH, tmp_key)
    os.chmod(tmp_key, 0o600)
    log.dbg(f"Saved system.key to {tmp_key}")

    # ── 3. Sync hostid from backup ───────────────────────────────────────
    log.step(3, TOTAL_STEPS, "Syncing hostid from backup...")

    root_ds = None
    r = sh.run(f"zfs list -H -o name -r {pool_name}/rpool/ROOT")
    for line in r.lines:
        ds = line.strip()
        if ds != f"{pool_name}/rpool/ROOT" and "@" not in ds and ds.count("/") == 3:
            root_ds = ds
            break
    if not root_ds:
        log.fatal("Cannot find root dataset in backup")

    ubuntu_name = root_ds.split("/")[-1]
    log.ok(f"Root dataset: {ubuntu_name}")

    stored_mp = zfs.get_property(root_ds, "mountpoint")
    log.dbg(f"Stored mountpoint: {stored_mp}")
    if stored_mp and stored_mp not in ("none", "-"):
        _ = sh.run(f"mkdir -p {stored_mp}")
        r = sh.run(f"zfs mount {root_ds}")
        if r.ok:
            hostid_file = Path(stored_mp) / "etc/hostid"
            if hostid_file.exists():
                hostid_hex = sh.run(
                    f"od -A n -t x1 {hostid_file} | tr -d ' \\n' | head -c 8",
                ).output
                if len(hostid_hex) == 8:
                    hostid_be = (
                        hostid_hex[6:8] + hostid_hex[4:6] + hostid_hex[2:4] + hostid_hex[0:2]
                    )
                    _ = sh.run(f"zgenhostid -f 0x{hostid_be}", log=log)
                    log.ok(f"Hostid set to: {sh.run('hostid').output}")
                else:
                    log.warn("Hostid file unreadable — generating new")
                    _ = sh.run("zgenhostid -f")
            else:
                log.warn("No /etc/hostid in backup — generating new one")
                _ = sh.run("zgenhostid -f")
            _ = sh.run(f"zfs umount {root_ds}")
        else:
            log.warn(f"Could not mount {root_ds} — generating new hostid")
            _ = sh.run("zgenhostid -f")
    else:
        log.warn("Stored mountpoint is none — generating new hostid")
        _ = sh.run("zgenhostid -f")

    # ── 4. Select restore point ──────────────────────────────────────────
    log.step(4, TOTAL_STEPS, "Available restore points...")
    snap_names = zfs.unique_snap_names(f"{pool_name}/rpool")
    if not snap_names:
        log.fatal("No autosnap snapshots found on backup")

    for i, name in enumerate(snap_names):
        log.raw(f"  {log.W}{i + 1}{log.N}) {name}")
    log.blank()
    try:
        sel = input(
            f"    Select restore point (default: {len(snap_names)} = most recent): ",
        ).strip()
    except EOFError:
        sel = ""
    snap_idx = int(sel) if sel.isdigit() else len(snap_names)
    chosen_snap = snap_names[snap_idx - 1]
    log.ok(f"Restore point: {chosen_snap}")

    # ── 5. Select internal disk ──────────────────────────────────────────
    log.step(5, TOTAL_STEPS, "Selecting internal disk...")

    backup_disk = ""
    if drive.drive_id != "<unknown>":
        bp = Path(f"/dev/disk/by-id/{drive.drive_id}")
        if bp.exists():
            backup_disk = str(bp.resolve()).rstrip("0123456789").rstrip("p")

    candidates: list[str] = []
    r = sh.run("lsblk -dn -o NAME,TYPE,SIZE,MODEL")
    for line in r.lines:
        parts = line.split(None, 3)
        if len(parts) >= 2 and parts[1] == "disk":
            dev = f"/dev/{parts[0]}"
            if dev == backup_disk or parts[0].startswith("zd"):
                continue
            candidates.append(line.strip())

    if not candidates:
        log.fatal("No internal disks found")

    log.info("Available internal disks:")
    for i, c in enumerate(candidates):
        log.raw(f"    {log.W}{i + 1}{log.N}) {c}")

    if len(candidates) == 1:
        internal_disk = f"/dev/{candidates[0].split()[0]}"
        log.ok(f"Auto-selected: {internal_disk}")
    else:
        try:
            sel = input(f"    Select disk [1-{len(candidates)}]: ").strip()
        except EOFError:
            sel = "1"
        idx = int(sel) - 1 if sel.isdigit() else 0
        internal_disk = f"/dev/{candidates[idx].split()[0]}"

    # Preventive disk-size check: refuse if the selected disk is too
    # small for the backup data. Runs BEFORE the YES confirmation so
    # the operator does not commit to wiping a disk we know is too
    # small. Silently defers to the reactive ENOSPC handler if either
    # size measurement is unavailable.
    _check_target_disk_size(internal_disk, pool_name, log, zfs)

    log.blank()
    log.warn(f"This will COMPLETELY ERASE {internal_disk}")
    log.info(f"Restore point: {chosen_snap}")
    log.info(f"Source pool:   {pool_name}")
    try:
        confirm = input("    Type YES to proceed: ").strip()
    except EOFError:
        confirm = ""
    if confirm != "YES":
        log.fatal("Aborted by user")

    restore_start = time.time()

    # ── 6. Cleanup + partition ───────────────────────────────────────────
    log.step(6, TOTAL_STEPS, "Cleaning up and partitioning...")
    for pool in ("rpool", "bpool"):
        _ = sh.run(f"zpool export -f {pool}")
        _ = sh.run(f"zpool destroy -f {pool}")
    _ = sh.run(f"rm -rf {RECOVER_MNT}")
    _ = sh.run(f"mkdir -p {RECOVER_MNT}")

    _ = sh.run(f"wipefs -a {internal_disk}", log=log)
    _ = sh.run(f"sgdisk --zap-all {internal_disk}", log=log)
    _ = sh.run(f"sgdisk -n1:1M:+1G   -t1:EF00 {internal_disk}", log=log)
    _ = sh.run(f"sgdisk -n2:0:+2G    -t2:BE00 {internal_disk}", log=log)
    _ = sh.run(f"sgdisk -n3:0:+8G    -t3:8200 {internal_disk}", log=log)
    _ = sh.run(f"sgdisk -n4:0:0      -t4:BF00 {internal_disk}", log=log)
    _ = sh.run(f"partprobe {internal_disk}")
    _ = sh.run("sleep 3")

    for i in range(1, 5):
        if not Path(sh.part(internal_disk, i)).exists():
            log.fatal(f"Partition {sh.part(internal_disk, i)} not created")
        _ = sh.run(f"zpool labelclear -f {sh.part(internal_disk, i)}")
    log.ok("Partitions created")

    # ── 7. Create pools ──────────────────────────────────────────────────
    log.step(7, TOTAL_STEPS, "Creating bpool + rpool...")

    # Use only GRUB-safe features for bpool (compatible with GRUB 2.12+)
    features = " ".join(f"-o feature@{f}=enabled" for f in BPOOL_FEATURES_BASE.split())
    r = sh.run(
        f"zpool create -f -o ashift=12 -o autotrim=on -d {features} "
        + "-O devices=off -O mountpoint=none -O canmount=off "
        + "-O acltype=posixacl -O xattr=sa -O compression=lz4 -O normalization=formD "
        + f"-R {RECOVER_MNT} bpool {sh.part(internal_disk, 2)}",
        log=log,
    )
    if not r.ok:
        log.fatal("Failed to create bpool")
    cleanup.track_pool("bpool")
    log.ok("bpool created")

    r = sh.run(
        "zpool create -f -o ashift=12 -o autotrim=on "
        + "-O acltype=posixacl -O xattr=sa -O dnodesize=auto "
        + "-O normalization=formD -O relatime=on "
        + "-O canmount=off -O mountpoint=none -m none "
        + "-O encryption=aes-256-gcm -O keyformat=raw "
        + f"-O keylocation=file://{tmp_key} "
        + f"-R {RECOVER_MNT} "
        + f"rpool {sh.part(internal_disk, 4)}",
        log=log,
    )
    if not r.ok:
        log.fatal("Failed to create rpool container")
    cleanup.track_pool("rpool")
    # Set keylocation to standard Ubuntu path (used by dracut keystore module)
    _ = sh.run("zfs set keylocation=file:///run/keystore/rpool/system.key rpool")
    log.ok("rpool container created (encrypted, matching Ubuntu installer)")

    # ── 8. Raw send ROOT + USERDATA (NO keystore = no zvols = safe) ──────
    log.step(8, TOTAL_STEPS, "Restoring datasets via raw send (keystore deferred)...")

    _ = sh.run("zfs create -o canmount=off -o mountpoint=none rpool/ROOT", log=log)

    if not _raw_send(
        f"{pool_name}/rpool/ROOT/{ubuntu_name}",
        f"rpool/ROOT/{ubuntu_name}",
        chosen_snap,
        log,
    ):
        log.fatal("Failed to raw send root dataset")

    r = sh.run(f"zfs list -H -o name -r {pool_name}/rpool/ROOT/{ubuntu_name}")
    for line in r.lines:
        ds = line.strip()
        if ds == f"{pool_name}/rpool/ROOT/{ubuntu_name}" or "@" in ds:
            continue
        rel = ds.replace(f"{pool_name}/rpool/ROOT/{ubuntu_name}/", "")
        _ = _raw_send(ds, f"rpool/ROOT/{ubuntu_name}/{rel}", chosen_snap, log)
    log.ok("ROOT datasets restored ✓")

    if zfs.dataset_exists(f"{pool_name}/rpool/USERDATA"):
        _ = sh.run("zfs create -o canmount=off -o mountpoint=none rpool/USERDATA", log=log)
        r = sh.run(f"zfs list -H -o name -r {pool_name}/rpool/USERDATA")
        for line in r.lines:
            ds = line.strip()
            if ds == f"{pool_name}/rpool/USERDATA" or "@" in ds:
                continue
            rel = ds.replace(f"{pool_name}/rpool/USERDATA/", "")
            _ = _raw_send(ds, f"rpool/USERDATA/{rel}", chosen_snap, log)
        log.ok("USERDATA restored ✓")

    # ── 9. Export backup pool (removes its zvols) ────────────────────────
    log.step(9, TOTAL_STEPS, "Exporting backup pool (prevents kernel crash)...")
    ks.umount()
    _ = sh.run(f"zfs unload-key -r {pool_name}")
    _ = sh.run(f"zpool export {pool_name}")
    cleanup.untrack_pool(pool_name)
    log.ok(f"{pool_name} exported — no zvols active")
    log.dbg(f"zvol devices: {sh.run('ls /dev/zd* 2>/dev/null').output or 'none'}")

    # ── 10. Load keys using saved system.key ─────────────────────────────
    log.step(10, TOTAL_STEPS, "Loading encryption keys...")
    loaded = _load_keys_from_file(tmp_key, "rpool", log)
    log.ok(f"Loaded {loaded} key(s)")

    # ── 10b. Restore encryption hierarchy (encryptionroot=rpool) ─────────
    # Raw send/receive always breaks encryptionroot inheritance: each
    # received dataset becomes its own encryptionroot.  The Ubuntu
    # installer creates all datasets inheriting from rpool, so we must
    # restore that relationship with `zfs change-key -i`.
    log.info("Restoring encryption hierarchy (encryptionroot → rpool)...")
    ds_list = sh.run("zfs list -H -o name,encryptionroot -r rpool").output
    changed = 0
    for line in ds_list.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ds, eroot = parts
        # Skip rpool itself, containers, and datasets already correct
        if ds == "rpool" or eroot == "rpool" or eroot == "-":
            continue
        r = sh.run(f"zfs change-key -i {ds}")
        if r.ok:
            changed += 1
            log.dbg(f"  change-key -i: {ds}")
        else:
            log.warn(f"  change-key -i failed: {ds}: {r.stderr.strip()}")
    if changed:
        log.ok(f"Encryption hierarchy restored ({changed} dataset(s) → encryptionroot=rpool) ✓")
    else:
        log.ok("Encryption hierarchy already correct ✓")

    # ── 11. Set mountpoints + properties (safe: no zvols anywhere!) ──────
    log.step(11, TOTAL_STEPS, "Setting mountpoints and properties...")

    _ = zfs.set_property(f"rpool/ROOT/{ubuntu_name}", "mountpoint", "/")

    # Apply canmount only to datasets that actually exist. Drift between
    # zark's expected layout and the source system is collected here and
    # reported as a single warning block at the end of the run.
    layout_missing, layout_extra = _apply_root_children_canmount(
        ubuntu_name,
        zfs,
        log,
    )

    home_ds = sh.run("zfs list -H -o name -r rpool/USERDATA | grep 'home_' | head -1").output
    root_ds_u = sh.run("zfs list -H -o name -r rpool/USERDATA | grep 'root_' | head -1").output
    if home_ds:
        _ = zfs.set_property(home_ds, "mountpoint", "/home")
    if root_ds_u:
        _ = zfs.set_property(root_ds_u, "mountpoint", "/root")

    _ = sh.run(f"zpool set bootfs=rpool/ROOT/{ubuntu_name} rpool")
    log.ok("Mountpoints set ✓")

    # ── 12. Reimport backup pool → bpool → keystore → export ─────────────
    log.step(12, TOTAL_STEPS, "Reimporting backup pool for bpool + keystore restore...")

    bpool_received = False

    if not zfs.pool_import(pool_name, no_mount=True):
        log.warn(f"Cannot reimport {pool_name}")
        log.warn("bpool and keystore restore skipped — kernels need reinstallation")
    else:
        cleanup.track_pool(pool_name)

        # Reload backup pool keys
        bk_ks = Keystore(log)
        if bk_ks.mount(pool_name, passphrase):
            _ = bk_ks.load_pool_keys(f"{pool_name}/rpool")
            bk_ks.umount()

        # ── 12a. Send/receive bpool (safe with zvols — no zfs set, no mounting)
        # The received dataset inherits mountpoint=none from the stream.
        # Mountpoint is fixed in 12c, AFTER black is exported (no zvols).
        log.info("Restoring bpool via send/receive (pre-keystore)...")
        bpool_received = _bpool_send_recv(pool_name, ubuntu_name, log, zfs)

        # ── 12b. Restore keystore zvol (LAST zfs op — adds zvol to rpool)
        #
        # If anything goes wrong here the recovered system would boot without
        # a way to load its encryption key. Continuing silently in that state
        # is unsafe, so we abort with a detailed explanation and let the user
        # fix the backup before retrying. See _abort_missing_keystore() for
        # the full rationale and the rejected fallback options.
        if not zfs.dataset_exists(f"{pool_name}/keystore"):
            _abort_missing_keystore("no_dataset", pool_name, log)

        log.info("Restoring keystore zvol...")
        ks_snap = sh.run(
            f"zfs list -H -o name -t snapshot {pool_name}/keystore | tail -1",
        ).output
        if not ks_snap:
            _abort_missing_keystore("no_snapshot", pool_name, log)

        r = sh.run_pipe(
            f"zfs send {ks_snap}",
            "zfs receive -o encryption=off rpool/keystore",
        )
        if not r.ok:
            log.error(f"Keystore restore failed: {r.stderr.strip()}")
            if sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout):
                # Keystore is the very last dataset to receive (~12b). If
                # ENOSPC fires here, the target disk is full despite the
                # preventive check passing — bail with an ENOSPC-specific
                # message rather than the generic "send_failed" causes.
                log.fatal(
                    "Recovery ran out of space while restoring keystore",
                    causes=[
                        "Target disk filled up during the final keystore send",
                        "rpool data + keystore zvol exceed target disk capacity",
                    ],
                    solutions=[
                        "Recover to a larger disk",
                        "Or shrink the backup by purging snapshots before retry",
                    ],
                )
            _abort_missing_keystore("send_failed", pool_name, log)
        log.ok("rpool/keystore restored ✓")

        log.info(f"Exporting backup pool {pool_name} (final)...")
        _ = sh.run(f"zfs unload-key -r {pool_name}")
        _ = sh.run(f"zpool export -f {pool_name}")
        cleanup.untrack_pool(pool_name)
        log.ok(f"{pool_name} exported — zvols removed")

    log.dbg(
        "zvol devices after step 12: " + f"{sh.run('ls /dev/zd* 2>/dev/null').output or 'none'}",
    )

    # ── 12c. Fix bpool mountpoint (safe: black is now exported, no zvols) ──
    # MUST happen after black export. 'zfs set mountpoint' while any pool with
    # zvols is imported causes the kernel udev crash (chase.c:648).
    if bpool_received:
        log.info("Fixing bpool mountpoint (post-export, no zvols active)...")
        _ = _bpool_fix_mountpoint(ubuntu_name, log, zfs)

    # ── 13. Mount system ─────────────────────────────────────────────────
    log.step(13, TOTAL_STEPS, "Mounting system...")
    _ = sh.run("zfs mount -a")

    if not Path(f"{RECOVER_MNT}/usr/bin/bash").exists():
        log.warn("Cannot see /usr/bin/bash — rpool mount may be incomplete")

    mounted = sh.run(f"zfs mount | grep -c {RECOVER_MNT}").output
    log.ok(f"System mounted at {RECOVER_MNT} ({mounted} datasets)")

    # Format EFI partition
    _ = sh.run(f"mkfs.vfat -F32 {sh.part(internal_disk, 1)}", log=log)
    _ = sh.run(f"mkdir -p {RECOVER_MNT}/boot/efi")
    _ = sh.run(f"mount {sh.part(internal_disk, 1)} {RECOVER_MNT}/boot/efi")
    cleanup.track_mount(f"{RECOVER_MNT}/boot/efi")

    # crypttab — preserve existing lines, update swap only.
    # NOTE: keystore-rpool is NOT in crypttab — it's handled by the dracut
    # 89keystore module which opens it AFTER ZFS import (correct ordering).
    # A crypttab entry would fail because the zvol doesn't exist yet when
    # systemd-cryptsetup runs (before ZFS import).
    crypttab_path = Path(f"{RECOVER_MNT}/etc/crypttab")
    swap_uuid = sh.run(f"blkid -s PARTUUID -o value {sh.part(internal_disk, 3)}").output
    if swap_uuid:
        swap_line = (
            f"dm_crypt-0 PARTUUID={swap_uuid} /dev/urandom "
            "plain,swap,cipher=aes-xts-plain64,size=512,initramfs"
        )
        existing_lines: list[str] = []
        if crypttab_path.exists():
            for line in crypttab_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and stripped.startswith("dm_crypt-0"):
                    continue
                # Remove any keystore-rpool entry (dracut module handles it)
                if (
                    stripped
                    and not stripped.startswith("#")
                    and stripped.startswith("keystore-rpool")
                ):
                    continue
                existing_lines.append(line)
        existing_lines.append(swap_line)
        _ = crypttab_path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
        ks_handler = (
            "dracut module"
            if Path(f"{RECOVER_MNT}/usr/bin/dracut").exists()
            else "initramfs-tools hook"
        )
        log.ok(f"crypttab written (swap only — keystore handled by {ks_handler})")

    # fstab EFI UUID
    efi_uuid = sh.run(f"blkid -s UUID -o value {sh.part(internal_disk, 1)}").output
    fstab = Path(f"{RECOVER_MNT}/etc/fstab")
    if fstab.exists() and efi_uuid:
        content = fstab.read_text(encoding="utf-8")
        content = re.sub(
            r"/dev/disk/by-uuid/[A-Fa-f0-9-]+\s+/boot/efi",
            f"/dev/disk/by-uuid/{efi_uuid} /boot/efi",
            content,
            flags=re.IGNORECASE,
        )
        _ = fstab.write_text(content, encoding="utf-8")
        log.ok(f"fstab EFI UUID updated: {efi_uuid}")

    # ── 14. Restore hostid to target ─────────────────────────────────────
    log.step(14, TOTAL_STEPS, "Restoring hostid to target...")
    _ = sh.run(f"cp /etc/hostid {RECOVER_MNT}/etc/hostid")
    log.ok(f"hostid: {sh.run('hostid').output}")

    # ── 15. Chroot: cachefile, grub.cfg UUID fix, grub-install, initrd ───
    log.step(15, TOTAL_STEPS, "Installing bootloader and regenerating initrd...")

    # Bind mounts for chroot
    for d in ("proc", "sys", "dev", "dev/pts", "run"):
        _ = sh.run(f"mkdir -p {RECOVER_MNT}/{d}")
        _ = sh.run(f"mount --bind /{d} {RECOVER_MNT}/{d}")
        cleanup.track_mount(f"{RECOVER_MNT}/{d}")

    _ = sh.run(f"mkdir -p {RECOVER_MNT}/sys/firmware/efi/efivars")
    _ = sh.run(f"mount -t efivarfs efivarfs {RECOVER_MNT}/sys/firmware/efi/efivars")
    cleanup.track_mount(f"{RECOVER_MNT}/sys/firmware/efi/efivars")

    # zpool.cache — required so grub and initrd can locate both pools
    cache_path = f"{RECOVER_MNT}/etc/zfs/zpool.cache"
    zfs.write_zpool_cache(cache_path, ["rpool", "bpool"])
    log.ok("zpool.cache written")

    # Fix bpool GUID in grub.cfg (bpool was created fresh — new GUID)
    bpool_guid = zfs.pool_guid("bpool")
    if bpool_guid:
        bpool_hex = format(int(bpool_guid), "016x")
        _ = fix_grub_bpool_uuid(Path(f"{RECOVER_MNT}/boot/grub/grub.cfg"), bpool_hex, log)
    else:
        log.warn("Cannot read bpool GUID — grub.cfg may have stale UUID")

    # ── Install GRUB bootloader (standard Ubuntu procedure) ─────────────
    # Replicate exactly what Ubuntu does on every grub package update:
    #   1. grub-install → GRUB modules + bootstrap grub.cfg + EFI binary
    #   2. reinstall grub-efi-amd64-signed + shim-signed → signed EFI binaries
    #   3. update-grub → regenerate main /boot/grub/grub.cfg
    # This is 100% standard — future apt upgrades work identically.

    # Step 1: grub-install (modules + bootstrap grub.cfg on EFI partition)
    log.info("Running grub-install...")
    r = sh.run(
        f"chroot {RECOVER_MNT} grub-install --target=x86_64-efi "
        + "--efi-directory=/boot/efi --bootloader-id=ubuntu "
        + f"--skip-fs-probe {internal_disk}",
        log=log,
    )
    if r.ok:
        log.ok("grub-install succeeded ✓")
    else:
        log.warn(f"grub-install failed: {r.stderr.strip()}")

    # Step 2: Re-run signed package postinst scripts (no network needed)
    # dpkg-reconfigure triggers the same postinst that apt runs on install:
    # grub-efi-amd64-signed postinst → grub-install with signed binary
    # shim-signed postinst → installs shimx64.efi
    #
    # The postinst tries to mount the ESP at /var/lib/grub/esp using the
    # device path stored in debconf (grub-efi/install_devices).  After a
    # recovery the target disk differs from the original, so the old by-id
    # path no longer exists.  Fix: (a) bind-mount the already-mounted ESP
    # so the postinst finds it, and (b) update debconf so future apt
    # upgrades use the correct device.
    log.info("Configuring signed GRUB + shim (Secure Boot)...")

    # (a) Bind-mount EFI partition at /var/lib/grub/esp inside chroot
    grub_esp = Path(f"{RECOVER_MNT}/var/lib/grub/esp")
    grub_esp.mkdir(parents=True, exist_ok=True)
    efi_bind_ok = sh.run(
        f"mount --bind {RECOVER_MNT}/boot/efi {grub_esp}",
    ).ok
    if efi_bind_ok:
        cleanup.track_mount(str(grub_esp))

    # (b) Update debconf to point to the new EFI partition
    new_efi_part = sh.part(internal_disk, 1)
    new_efi_byid = sh.run(
        f"find /dev/disk/by-id/ -lname '*/{Path(new_efi_part).name}' | head -1",
    ).output.strip()
    if new_efi_byid:
        _ = sh.run(
            f"chroot {RECOVER_MNT} bash -c "
            + f"\"echo 'grub-efi/install_devices string {new_efi_byid}' | debconf-set-selections\"",
            log=log,
        )
        log.dbg(f"debconf grub-efi/install_devices → {new_efi_byid}")
    elif new_efi_part:
        # No by-id link (e.g. virtio) — use raw device
        _ = sh.run(
            f"chroot {RECOVER_MNT} bash -c "
            + f"\"echo 'grub-efi/install_devices string {new_efi_part}' | debconf-set-selections\"",
            log=log,
        )
        log.dbg(f"debconf grub-efi/install_devices → {new_efi_part}")

    # ── Force .latest signed variant before dpkg-reconfigure ────────────
    # shim-signed and grub-efi-amd64-signed ship both .latest and .previous
    # binaries since 1.51. Subiquity may leave the alternative pointing at
    # .previous (revoked by recent SBAT updates), so we explicitly switch
    # to .latest before dpkg-reconfigure copies it to the ESP. See
    # _force_latest_signed_alternative for full rationale.
    log.info("Pinning Secure Boot binaries to .latest variant...")
    _force_latest_signed_alternative(
        RECOVER_MNT,
        "shimx64.efi.signed",
        "/usr/lib/shim/shimx64.efi.signed.latest",
        log,
    )
    _force_latest_signed_alternative(
        RECOVER_MNT,
        "grubx64.efi.signed",
        "/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed.latest",
        log,
    )

    r = sh.run(
        f"chroot {RECOVER_MNT} dpkg-reconfigure -f noninteractive grub-efi-amd64-signed",
        log=log,
    )
    if r.ok:
        log.ok("grub-efi-amd64-signed configured ✓")
    else:
        log.warn("grub-efi-amd64-signed configure failed — manual copy as fallback")
        # Fallback: copy signed binaries manually
        efi_ubuntu = Path(f"{RECOVER_MNT}/boot/efi/EFI/ubuntu")
        grub_signed = Path(f"{RECOVER_MNT}/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed")
        if grub_signed.exists():
            _ = sh.run(f"cp {grub_signed} {efi_ubuntu}/grubx64.efi")

    r = sh.run(
        f"chroot {RECOVER_MNT} dpkg-reconfigure -f noninteractive shim-signed",
        log=log,
    )
    if r.ok:
        log.ok("shim-signed configured ✓")
    else:
        log.warn("shim-signed configure failed — manual copy as fallback")
        efi_ubuntu = Path(f"{RECOVER_MNT}/boot/efi/EFI/ubuntu")
        shim_src = Path(f"{RECOVER_MNT}/usr/lib/shim/shimx64.efi.signed")
        if shim_src.exists():
            _ = sh.run(f"cp {shim_src} {efi_ubuntu}/shimx64.efi")

    # Step 3: grub.cfg — we keep the backup's original grub.cfg (bpool UUID
    # already fixed above).  update-grub cannot run in a chroot with ZFS
    # altroot because 10_linux_zfs fails to mount encrypted datasets
    # ("encryption key not loaded").  The backup grub.cfg is valid: it
    # references rpool/bpool by name, and the first kernel update on the
    # running system will regenerate it via the standard apt trigger.
    log.ok("grub.cfg preserved from backup (bpool UUID updated) ✓")

    # Verify Secure Boot chain
    shim_ok = Path(f"{RECOVER_MNT}/boot/efi/EFI/ubuntu/shimx64.efi").exists()
    grub_ok = Path(f"{RECOVER_MNT}/boot/efi/EFI/ubuntu/grubx64.efi").exists()
    if shim_ok and grub_ok:
        log.ok("Secure Boot chain present (shim + grub) ✓")
    elif grub_ok:
        log.warn("grubx64.efi present but shimx64.efi missing — Secure Boot may fail")
    else:
        log.warn("EFI binaries not found — boot may fail")

    # Install keystore boot module — opens LUKS keystore zvol AFTER ZFS import
    # but BEFORE dataset mounting, so /run/keystore/rpool/system.key is available.
    # This is needed because systemd-cryptsetup runs BEFORE ZFS import (the zvol
    # device doesn't exist yet), creating a chicken-and-egg problem.
    #
    # dracut (Ubuntu 25.04+): 89keystore module with pre-mount hook
    # initramfs-tools (Ubuntu 24.04): hook + local-premount script

    if Path(f"{RECOVER_MNT}/usr/bin/dracut").exists():
        _install_keystore_dracut(RECOVER_MNT, ubuntu_name, log)
        # Enable extended bpool features (safe for GRUB on 25.04+)
        for feat in BPOOL_FEATURES_EXTENDED.split():
            _ = sh.run(f"zpool set feature@{feat}=enabled bpool")
        log.ok(f"bpool extended features enabled ({len(BPOOL_FEATURES_EXTENDED.split())})")
    else:
        _install_keystore_initramfs(RECOVER_MNT, ubuntu_name, log)

    # Install grub guard — prevents update-grub from running with external
    # ZFS pools connected (which corrupts grub.cfg with no kernel entries)
    grub_guard.install(target_root=RECOVER_MNT, log=log)

    # Regenerate initrd
    if Path(f"{RECOVER_MNT}/usr/bin/dracut").exists():
        log.info("Regenerating initrd (dracut)...")
        machine_id = sh.run(f"cat {RECOVER_MNT}/etc/machine-id").output.strip()
        if machine_id:
            for kver in sh.run(f"ls {RECOVER_MNT}/lib/modules/").lines:
                _ = sh.run(f"mkdir -p {RECOVER_MNT}/boot/efi/{machine_id}/{kver.strip()}")
        _ = sh.run(f"chroot {RECOVER_MNT} dracut --force --regenerate-all", log=log)
        log.ok("initrd regenerated (dracut) ✓")
    elif Path(f"{RECOVER_MNT}/usr/sbin/update-initramfs").exists():
        log.info("Regenerating initrd (update-initramfs)...")
        _ = sh.run(f"chroot {RECOVER_MNT} update-initramfs -u -k all", log=log)
        log.ok("initrd regenerated ✓")
    else:
        log.warn("No initrd generator found — boot may fail")

    # ── 16. Cleanup and summary ──────────────────────────────────────────
    log.step(16, TOTAL_STEPS, "Cleanup...")

    if Path(tmp_key).exists():
        os.remove(tmp_key)

    cleanup.run()

    restore_end = time.time()
    restore_mins, restore_secs = divmod(int(restore_end - restore_start), 60)
    total_mins, total_secs = divmod(int(restore_end - recover_start), 60)

    # Surface any dataset-layout drift from step 11 in a single block, right
    # before the success banner so it isn't lost in the middle of the run.
    _emit_dataset_layout_warnings(ubuntu_name, layout_missing, layout_extra, log)

    log.banner_ok(
        "RECOVERY COMPLETE",
        [
            f"Script version:  {log.W}zark v{VERSION}{log.N}",
            f"Restored from:   {log.W}{pool_name}{log.N}",
            f"Restore point:   {log.W}{chosen_snap}{log.N}",
            f"Internal disk:   {log.W}{internal_disk}{log.N}",
            f"Dataset restore: {log.W}{restore_mins}m {restore_secs}s{log.N}",
            f"Total time:      {log.W}{total_mins}m {total_secs}s{log.N}",
            "",
            f"{log.W}Next steps:{log.N}",
            "  1. Remove the live USB",
            f"  2. {log.Y}Disconnect the backup drive{log.N}",
            "  3. Reboot — enter your rpool passphrase at the prompt",
            f"  {log.Y}⚠  If it drops to emergency shell on first boot:{log.N}",
            f"     {log.W}zpool import rpool && exit{log.N}",
            f"  4. Run: {log.W}sudo update-grub{log.N}  (regenerates grub.cfg)",
            f"  5. Run: {log.W}sudo ./zark finish{log.N}",
            "",
            f"  {log.Y}If boot fails:{log.N} boot from live USB and run:",
            f"     {log.W}sudo ./zark repair-boot{log.N}",
        ],
    )
