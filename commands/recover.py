# Copyright 2026 Juanmi Taboada # pylint: disable=too-many-lines
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
zark recover — Full bare-metal system recovery from backup drive.

Must run from Ubuntu live USB with backup drive connected.

CRITICAL RULES:
  - ZERO 'zfs set mountpoint/canmount' while ANY pool with zvols is imported:
    every restored dataset gets its canmount/mountpoint at receive time
    (``zfs receive -u -o ...``), which sets them in the kernel with no
    mount or unmount (lib/mount_props.py says where the values come from).
  - ZERO rapid pool export/import cycles (corrupts live USB overlay).
  - Keystore zvol (rpool/keystore) is restored LAST to avoid the kernel
    udev/systemd crash (chase.c:648).
  - The backup pool is only ever imported read-only, device-exact, under
    a private altroot (I-G).
  - Nothing on the internal disk is touched before the full pre-flight
    has passed and the operator typed YES.

Recovery order:
  1-2.   Find drive; import read-only by exact device; unlock keystore
  3-4.   Choose restore point; resolve every dataset (never forward);
         mount properties; hostid of the point; restore table
  5.     Choose internal disk; pre-flight (sizes, keystore, bpool); YES
  6-7.   Partition disk, create bpool + rpool
  8.     Raw send every rpool dataset of the point (no keystore = no zvols)
  9.     Export backup pool, verified (removes its zvols)
  10.    Load keys from saved system.key; restore encryptionroot
  11.    bootfs
  12.    Reimport backup pool (read-only):
           12a. Restore bpool content  (NO zvols on rpool yet — safe)
           12b. Restore keystore zvol  (adds zvol — LAST zfs operation)
         Export backup pool, verified.
  13.    Mount system, write EFI/crypttab/fstab
  14.    Restore hostid to target
  15.    Chroot: cachefile, grub.cfg UUID fix, grub-install, initrd
  16.    Cleanup and summary
"""

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from lib import apt_guard, grub_guard, sh
from lib.backup_layout import (
    find_be as _find_be,
    list_datasets as _list_datasets,
    root_is_empty,
    zark_props as _zark_props,
)
from lib.cleanup import Cleanup, prompt_eject_or_attach
from lib.config import VERSION, Config
from lib.drives import backup_device, scan_connected_drives, select_drive
from lib.identity import BY_ID_DIR, by_id_names, preferred_by_id, protected_disks, whole_disk
from lib.initrd import regenerate_initrd
from lib.keystore import SYSTEM_KEY_PATH, Keystore, open_keystore
from lib.log import Log
from lib.mount_props import (
    BPOOL_CONTAINERS,
    CREATED_CONTAINERS,
    MountProps,
    choose,
    effective_mountpoint,
    parse_list_cache,
    receive_options,
    rpool_root_mountpoint,
)
from lib.restore_points import Point, Snap, parse_snapshots, resolve, restore_points
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
PROBE_MNT = "/run/zark/probe"
TOTAL_STEPS = 16

# Partition layout written in step 6, in MiB before the rpool partition:
# 1 MiB alignment + 1 GiB ESP + 2 GiB bpool + 8 GiB swap.
_LAYOUT_BEFORE_RPOOL_MIB = 1 + 1024 + 2048 + 8192
_BPOOL_PART_BYTES = 2 * 1024**3

# Headroom on top of the restored data: ZFS metadata and slop space.
_RECOVER_TARGET_OVERHEAD_PCT = 5


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
    # Thin wrapper kept for call-site readability; the detection logic now
    # lives in lib.sh as the single source of truth (also used by repair-boot).
    return sh.is_live_usb()


def _load_keys_from_file(keyfile: str, pool_root: str, log: Log) -> int:
    """Load encryption keys for all datasets using a key file."""
    zfs = ZFS(log)
    count = 0
    for ds in zfs.datasets_needing_key(pool_root):
        if sh.run(f"zfs load-key -L file://{keyfile} {ds}").ok:
            count += 1
            log.dbg(f"Key loaded: {ds}")
    return count


def _abort_missing_keystore(reason: str, pool_name: str, log: Log) -> NoReturn:
    """
    Abort the recovery when the keystore zvol cannot be restored from backup.

    Reaching this point means the security model of the recovered system would
    be silently degraded: the rpool encryption depends on `system.key`, which
    normally lives inside the LUKS-encrypted keystore zvol. Without that zvol,
    the recovered system has no way to load its keys at boot.

    Two unsafe fallbacks were considered and deliberately rejected:

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


# ── Restore plan ─────────────────────────────────────────────────────────


@dataclass
class RestoreRow:  # pylint: disable=too-many-instance-attributes
    """One dataset of the restore table."""

    rel: str  # dataset relative to the backup pool (rpool/var, bpool/BOOT/x)
    snap: Snap | None  # resolved snapshot; None = not restored
    referenced: int = 0
    props: MountProps | None = None
    options: list[str] = field(default_factory=list)
    effective: str = ""  # effective mountpoint after receive
    note: str = ""  # why the dataset is not restored

    @property
    def restored(self) -> bool:
        """True when this dataset will be received."""
        return self.snap is not None and not self.note


@dataclass
class RestorePlan:  # pylint: disable=too-many-instance-attributes
    """Everything decided before the internal disk is touched."""

    pool: str
    device: str
    be: str
    point: Point
    rows: list[RestoreRow]
    keystore_snap: str
    keystore_bytes: int
    hostid: str = ""  # 8 hex digits, big-endian as zgenhostid expects
    rpool_mountpoint: str = "/"
    rpool_mountpoint_source: str = "ubuntu"

    def row(self, rel: str) -> RestoreRow | None:
        """Row of ``rel``, if any."""
        return next((r for r in self.rows if r.rel == rel), None)

    @property
    def rpool_rows(self) -> list[RestoreRow]:
        """Restored rows under rpool, parents first."""
        return [r for r in self.rows if r.rel.startswith("rpool/") and r.restored]

    @property
    def skipped(self) -> list[RestoreRow]:
        """Rows that will not be restored."""
        return [r for r in self.rows if not r.restored]

    @property
    def rpool_bytes(self) -> int:
        """Bytes received into rpool (datasets + keystore)."""
        return sum(r.referenced for r in self.rpool_rows) + self.keystore_bytes

    @property
    def bpool_bytes(self) -> int:
        """Bytes received into bpool."""
        return sum(r.referenced for r in self.rows if r.rel.startswith("bpool/") and r.restored)


def _list_snapshots(pool: str) -> list[Snap]:
    snaps: list[Snap] = []
    for root in (f"{pool}/rpool", f"{pool}/bpool"):
        r = sh.run(f"zfs list -Hp -t snapshot -o name,guid,createtxg,creation -r {root}")
        if r.ok:
            snaps += parse_snapshots(r.lines, pool)
    return snaps


def _referenced(snapshots: list[str]) -> dict[str, int]:
    """``referenced`` in bytes of each full snapshot name."""
    if not snapshots:
        return {}
    r = sh.run("zfs get -Hp -o name,value referenced " + " ".join(snapshots))
    out: dict[str, int] = {}
    for line in r.lines if r.ok else []:
        fields = line.split("\t")
        if len(fields) == 2 and fields[1].isdigit():
            out[fields[0]] = int(fields[1])
    return out


def _mount_snapshot(full_snap: str) -> bool:
    """Mount a snapshot read-only at PROBE_MNT (snapshots mount as legacy)."""
    _ = sh.run(f"mkdir -p {PROBE_MNT}")
    return sh.run(f"mount -t zfs -o ro {full_snap} {PROBE_MNT}").ok


def _umount_probe() -> None:
    _ = sh.run(f"umount {PROBE_MNT}")


def _probe_be(full_snap: str, log: Log) -> tuple[str, str, str]:
    """Read origin's zfs-list.cache files and hostid from the BE snapshot.

    Returns ``(rpool cache text, bpool cache text, hostid hex)``, each
    empty when absent. The snapshot is mounted read-only in a private
    directory, never over ``/`` (hallazgo 9).
    """
    if not _mount_snapshot(full_snap):
        log.warn(f"Could not mount {full_snap} to read origin metadata")
        return "", "", ""
    try:
        root = Path(PROBE_MNT)
        caches: list[str] = []
        for pool in ("rpool", "bpool"):
            f = root / "etc/zfs/zfs-list.cache" / pool
            caches.append(f.read_text(encoding="utf-8") if f.is_file() else "")
        hostid = ""
        hf = root / "etc/hostid"
        if hf.is_file():
            raw = hf.read_bytes()
            if len(raw) >= 4:
                hostid = f"{int.from_bytes(raw[:4], 'little'):08x}"
        return caches[0], caches[1], hostid
    finally:
        _umount_probe()


def _empty_root(full_snap: str) -> bool | None:
    """True when the snapshot's root directory has no entries; None if unreadable."""
    return root_is_empty(full_snap, PROBE_MNT, zfsutil=False)


def _choose_point(points: list[Point], log: Log) -> Point:
    options = []
    for p in points:
        kinds = sorted({s.name.split("_")[-1] for m in p.members.values() for s in m})
        detail = "/".join(kinds) if p.family == "autosnap_" else f"{len(p.members)} datasets"
        options.append(f"{p.label_utc()}  {p.family.rstrip('_')}  ({detail})")
    idx = log.ask_choice("Restore point (newest is the default):", options, len(points) - 1)
    return points[idx]


def _plan(  # pylint: disable=too-many-locals,too-many-branches
    pool: str,
    device: str,
    be: str,
    point: Point,
    snaps: list[Snap],
    log: Log,
) -> RestorePlan:
    """Resolve every dataset for ``point`` and decide its mount properties."""
    types = _list_datasets(pool)
    wanted = [
        ds
        for ds in sorted(types)
        if (ds.startswith("rpool/") and ds not in CREATED_CONTAINERS) or ds == f"bpool/BOOT/{be}"
    ]
    resolved = resolve(point, [s for s in snaps if s.dataset in wanted], wanted)

    be_row_snap = resolved.get(f"rpool/ROOT/{be}")
    cache_r, cache_b, hostid = ("", "", "")
    if be_row_snap:
        cache_r, cache_b, hostid = _probe_be(f"{pool}/rpool/ROOT/{be}@{be_row_snap.name}", log)
    cache = {**parse_list_cache(cache_r), **parse_list_cache(cache_b)}
    zark = _zark_props(pool)
    children = {ds for ds in types for other in types if other.startswith(f"{ds}/")}

    root_mp, root_src = rpool_root_mountpoint(cache)
    effective: dict[str, str] = {"rpool": root_mp, **CREATED_CONTAINERS, **BPOOL_CONTAINERS}
    claimed: dict[str, str] = {}  # guessed mountpoint → dataset that got it
    rows: list[RestoreRow] = []
    for ds in wanted:
        snap = resolved[ds]
        row = RestoreRow(rel=ds, snap=snap)
        parent = ds.rsplit("/", 1)[0]
        leaf = ds.rsplit("/", 1)[-1]
        if types[ds] == "volume":
            row.note = "zvol (only the keystore zvol is restored)"
        elif snap is None:
            row.note = "no snapshot at or before this point"
        elif parent not in effective:
            row.note = f"parent {parent} is not restored"
        if row.note:
            rows.append(row)
            continue
        assert snap is not None
        full = f"{pool}/{ds}@{snap.name}"
        needs_probe = ds not in zark and ds not in cache and ds in children
        empty = bool(needs_probe and _empty_root(full))
        row.props = choose(ds, be, zark=zark, cache=cache, empty_with_children=empty)
        target = effective_mountpoint(row.props, effective[parent], leaf)
        if row.props.source in ("ubuntu", "inferred") and row.props.canmount == "on":
            if target in claimed:
                # e.g. a second home_* from a reinstall: never stack two
                # datasets on one mountpoint by guesswork.
                log.warn(f"{ds}: {target} already taken by {claimed[target]} — noauto")
                row.props = MountProps("noauto", row.props.mountpoint, row.props.source)
            else:
                claimed[target] = ds
        row.options = receive_options(row.props, effective[parent], leaf)
        row.effective = effective_mountpoint(row.props, effective[parent], leaf)
        effective[ds] = row.effective
        rows.append(row)

    refs = _referenced([f"{pool}/{r.rel}@{r.snap.name}" for r in rows if r.restored and r.snap])
    for r in rows:
        if r.restored and r.snap:
            r.referenced = refs.get(f"{pool}/{r.rel}@{r.snap.name}", -1)

    ks = sh.run(f"zfs list -Hp -t snapshot -o name,createtxg -s createtxg {pool}/keystore")
    ks_snap = ks.lines[-1].split("\t")[0] if ks.ok and ks.lines else ""
    ks_bytes = _referenced([ks_snap]).get(ks_snap, -1) if ks_snap else -1
    return RestorePlan(
        pool,
        device,
        be,
        point,
        rows,
        ks_snap,
        ks_bytes,
        hostid,
        rpool_mountpoint=root_mp,
        rpool_mountpoint_source=root_src,
    )


def _rpool_create_cmd(mountpoint: str, key_file: str, vdev: str) -> str:
    """``zpool create`` for the encrypted rpool, root mountpoint as in origin."""
    return (
        "zpool create -f -o ashift=12 -o autotrim=on "
        + "-O acltype=posixacl -O xattr=sa -O dnodesize=auto "
        + "-O normalization=formD -O relatime=on "
        + f"-O canmount=off -O mountpoint={mountpoint} "
        + "-O encryption=aes-256-gcm -O keyformat=raw "
        + f"-O keylocation=file://{key_file} "
        + f"-R {RECOVER_MNT} rpool {vdev}"
    )


def _stable_vdev(disk: str, n: int, log: Log) -> str:
    """Partition ``n`` of ``disk`` by its /dev/disk/by-id name, for the pool vdev.

    The vdev path is what zpool.cache and the labels record. A kernel name
    (/dev/sda2) is taken by whatever enumerates first: a USB stick plugged
    in at boot then gets it, and libzfs aborts the import on that device
    (eli, 2026-09-30). Falls back to the kernel name, with a warning, when
    the disk has no by-id name (e.g. a virtio disk without a serial).
    """
    by_id = preferred_by_id(by_id_names(disk))
    path = f"{BY_ID_DIR}/{by_id}-part{n}" if by_id else ""
    if path and Path(path).exists():
        return path
    log.warn(f"No /dev/disk/by-id name for {sh.part(disk, n)} — the pool will record a kernel name")
    return sh.part(disk, n)


def _fmt_delta(seconds: int) -> str:
    """Signed offset from the point, e.g. ``-36m30s`` or ``0``."""
    if seconds == 0:
        return "0"
    sign = "-" if seconds < 0 else "+"
    m, sec = divmod(abs(seconds), 60)
    h, m = divmod(m, 60)
    return f"{sign}{h}h{m:02d}m" if h else f"{sign}{m}m{sec:02d}s"


def _show_plan(plan: RestorePlan, log: Log) -> None:
    """The restore table (hallazgo 3): what each dataset will be restored from."""
    log.info(f"Restore point: {plan.point.label_utc()} ({plan.point.family.rstrip('_')})")
    log.raw(f"  {'DATASET':44} {'SNAPSHOT':44} {'Δ POINT':>9} {'SIZE':>7}  MOUNT")
    log.raw(
        f"  {'rpool (pool root)':44} {'(created)':44} {'':>9} {'':>7}  "
        + f"off {plan.rpool_mountpoint} [{plan.rpool_mountpoint_source}]",
    )
    for r in plan.rows:
        if not r.restored:
            log.raw(f"  {log.Y}{r.rel:44} NOT RESTORED — {r.note}{log.N}")
            continue
        assert r.snap is not None and r.props is not None
        delta = _fmt_delta(r.snap.creation - plan.point.label)
        size = sh.humanize_bytes(r.referenced) if r.referenced >= 0 else "?"
        mark = log.Y if r.props.source == "inferred" else ""
        log.raw(
            f"  {r.rel:44} {r.snap.name:44} {delta:>9} {size:>7}  "
            + f"{mark}{r.props.canmount} {r.effective} [{r.props.source}]{log.N if mark else ''}",
        )
    log.raw(f"  {'keystore':44} {plan.keystore_snap.split('@')[-1]:44} (newest, independent)")


def _disk_size_bytes(disk: str) -> int:
    """Size of a block device in bytes, or 0 on failure."""
    r = sh.run(f"lsblk -bdn -o SIZE {disk}")
    try:
        return int(r.output.strip()) if r.ok else 0
    except ValueError:
        return 0


def _check_sizes(plan: RestorePlan, target_disk: str, log: Log) -> None:
    """Hallazgo 14: size against the chosen point, fail-closed on any unknown."""
    unknown = [r.rel for r in plan.rows if r.restored and r.referenced < 0]
    if plan.keystore_bytes < 0:
        unknown.append("keystore")
    disk_bytes = _disk_size_bytes(target_disk)
    if unknown or disk_bytes <= 0:
        log.fatal(
            "Cannot measure the restore size — refusing to erase the disk",
            causes=[
                *(f"referenced unknown for {u}" for u in unknown),
                *([f"size of {target_disk} unknown"] if disk_bytes <= 0 else []),
            ],
        )
    rpool_part = disk_bytes - _LAYOUT_BEFORE_RPOOL_MIB * 1024**2
    need_r = plan.rpool_bytes * (100 + _RECOVER_TARGET_OVERHEAD_PCT) // 100
    need_b = plan.bpool_bytes * (100 + _RECOVER_TARGET_OVERHEAD_PCT) // 100
    log.info(
        f"Restore size: rpool {sh.humanize_bytes(plan.rpool_bytes)} "
        + f"(partition {sh.humanize_bytes(max(rpool_part, 0))}), "
        + f"bpool {sh.humanize_bytes(plan.bpool_bytes)} (partition 2G)",
    )
    if need_r > rpool_part or need_b > _BPOOL_PART_BYTES:
        log.fatal(
            f"Target disk {target_disk} too small for this restore point",
            causes=[
                f"rpool data at this point: {sh.humanize_bytes(plan.rpool_bytes)} "
                + f"(+{_RECOVER_TARGET_OVERHEAD_PCT}%)",
                f"rpool partition: {sh.humanize_bytes(max(rpool_part, 0))}",
                f"bpool data: {sh.humanize_bytes(plan.bpool_bytes)} vs 2G partition",
            ],
            solutions=["Recover to a larger disk"],
        )


def _target_candidates(backup_disk: str) -> list[tuple[str, str]]:
    """(disk, label) of every disk recover may erase (hallazgo 15).

    Excluded: the backup drive, any disk with a mounted filesystem, active
    swap or an imported pool's vdev (the live USB mounts /cdrom), and
    virtual devices.
    """
    protected = protected_disks()
    r = sh.run("lsblk -dn -P -o NAME,TYPE,SIZE,MODEL,SERIAL,TRAN")
    out: list[tuple[str, str]] = []
    for line in r.lines if r.ok else []:
        f = dict(re.findall(r'(\w+)="([^"]*)"', line))
        name = f.get("NAME", "")
        dev = f"/dev/{name}"
        if f.get("TYPE") != "disk" or name.startswith(("zd", "loop", "sr", "zram", "ram")):
            continue
        if dev in (backup_disk, *protected):
            continue
        tran = f.get("TRAN", "")
        warn = "  ⚠ USB" if tran == "usb" else ""
        label = (
            f"{dev}  {f.get('SIZE', '?')}  {f.get('MODEL', '').strip()}  "
            + f"serial {f.get('SERIAL', '?')}  {tran}{warn}"
        )
        out.append((dev, label))
    return out


def _select_target(backup_disk: str, log: Log) -> str:
    candidates = _target_candidates(backup_disk)
    if not candidates:
        log.fatal(
            "No internal disk available to restore to",
            causes=["Every disk is the backup drive, the live USB, or in use"],
        )
    idx = log.ask_choice("Internal disk to ERASE and restore to:", [c[1] for c in candidates])
    return candidates[idx][0]


def _refuse_imported_system_pools(log: Log) -> None:
    """An imported rpool/bpool lives on a disk that is never a candidate (its
    vdevs protect it), so it is another system's pool: refuse, never destroy."""
    imported = [p for p in ("rpool", "bpool") if sh.run(f"zpool list {p}").ok]
    if imported:
        log.fatal(
            f"{' and '.join(imported)} already imported in this live session",
            causes=["They belong to a disk that is not the restore target"],
            solutions=[f"Export them first: sudo zpool export {' '.join(imported)}"],
        )


def _preflight(plan: RestorePlan, target_disk: str, log: Log) -> None:
    """Everything that can fail without the disk being erased (hallazgo 15)."""
    _refuse_imported_system_pools(log)
    tools = ("sgdisk", "mkfs.vfat", "cryptsetup", "zgenhostid", "partprobe")
    missing = [t for t in tools if not sh.run(f"which {t}").ok]
    if missing:
        log.fatal(f"Missing tools in the live session: {' '.join(missing)}")
    be_row = plan.row(f"rpool/ROOT/{plan.be}")
    if be_row is None or not be_row.restored:
        log.fatal("The boot environment has no snapshot at this point")
    if not sh.run(f"zfs list {plan.pool}/keystore").ok:
        _abort_missing_keystore("no_dataset", plan.pool, log)
    if not plan.keystore_snap:
        _abort_missing_keystore("no_snapshot", plan.pool, log)
    bpool_row = plan.row(f"bpool/BOOT/{plan.be}")
    if bpool_row is None or not bpool_row.restored:
        log.warn("No bpool snapshot at this point — kernels will need reinstallation")
    _check_sizes(plan, target_disk, log)


def _receive(src: str, dst: str, options: list[str], raw: bool, log: Log) -> bool:
    """``zfs send [-w] src | zfs receive -u <options> dst``; ENOSPC is fatal."""
    send = f"zfs send -w {src}" if raw else f"zfs send {src}"
    recv = f"zfs receive -u {' '.join(options)} {dst}".replace("  ", " ")
    log.info(f"  {dst} ← @{src.split('@', 1)[1]}")
    r = sh.run_pipe(send, recv)
    if r.ok:
        return True
    if sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout):
        log.fatal(
            f"Recovery ran out of space while restoring {dst}",
            causes=["Target disk filled up during the receive"],
            solutions=["Recover to a larger disk"],
        )
    log.error(f"  receive failed: {dst}: {r.stderr.strip()}")
    return False


def _export_verified(pool: str, log: Log) -> None:
    """Export the backup pool and prove its zvols are gone (hallazgo 9)."""
    _ = sh.run(f"zfs unload-key -r {pool}")
    if not sh.run(f"zpool export {pool}", log=log).ok:
        _ = sh.run(f"zpool export -f {pool}", log=log)
    _ = sh.run("udevadm settle --timeout=10")  # /dev/zvol symlinks go away via udev
    still = sh.run(f"zpool list {pool}").ok or sh.run(f"test -e /dev/zvol/{pool}").ok
    if still:
        log.fatal(
            f"{pool} could not be exported — refusing to continue",
            causes=["Its keystore zvol would stay present during the next steps"],
            solutions=[f"Check: zpool status {pool}; fuser -vm /dev/zvol/{pool}/keystore"],
        )
    log.ok(f"{pool} exported — its zvols are gone")


def _mount_restored_system(ubuntu_name: str, bpool_received: bool, log: Log) -> None:
    """Mount the restored system under RECOVER_MNT, root first.

    Receives were ``-u``, so nothing is mounted yet. The root is mounted
    explicitly (``zfs mount -a`` skips a ``canmount=noauto`` boot
    environment), then bpool on its ``/boot``, then everything else. A
    bpool mounted before the root would be shadowed by it.
    """
    root_ok = sh.run(f"zfs mount rpool/ROOT/{ubuntu_name}", log=log).ok
    if not root_ok or not Path(f"{RECOVER_MNT}/usr/bin/bash").exists():
        log.fatal(
            f"Cannot mount the restored root at {RECOVER_MNT}",
            solutions=["Boot the live USB again and run: sudo ./zark repair-boot"],
        )
    if bpool_received:
        boot_ok = sh.run(f"zfs mount bpool/BOOT/{ubuntu_name}", log=log).ok
        if not boot_ok or not sh.run(f"findmnt -n {RECOVER_MNT}/boot").ok:
            log.fatal(
                f"Cannot mount the restored bpool at {RECOVER_MNT}/boot",
                solutions=["Boot the live USB again and run: sudo ./zark repair-boot"],
            )
        kernel_count = len(list(Path(f"{RECOVER_MNT}/boot").glob("vmlinuz*")))
        log.ok(f"bpool mounted at {RECOVER_MNT}/boot ✓  ({kernel_count} kernel(s))")
    _ = sh.run("zfs mount -a")
    mounted = sh.run(f"zfs mount | grep -c {RECOVER_MNT}").output
    log.ok(f"System mounted at {RECOVER_MNT} ({mounted} datasets)")


def _install_boot_chain(  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    internal_disk: str,
    ubuntu_name: str,
    zfs: ZFS,
    cleanup: Cleanup,
    log: Log,
) -> None:
    """Chroot binds, zpool.cache, grub.cfg fix, signed GRUB/shim, keystore hook, guards."""
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

    # Install the apt guard — stops a later kernel/GRUB/ZFS upgrade on the
    # recovered system from half-applying while a backup drive is connected.
    apt_guard.install(target_root=RECOVER_MNT, log=log)


def run(
    args: list[str],
):  # pylint: disable= too-many-statements, too-many-branches, too-many-locals
    """Main entry point for 'zark recover'. See module docstring for details."""
    del args  # no CLI args supported (yet)
    log = Log()
    cfg = Config.load()
    cfg.check_registry(log, fatal=False)
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
        confirm = log.ask_text("  Type IUNDERSTAND to continue anyway: ", accept=("IUNDERSTAND",))
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
    device = backup_device(drive)
    if not device:
        log.fatal(f"Cannot locate the device of {pool_name} for a device-exact import")

    # ── 2. Import (read-only) and unlock backup pool ─────────────────────
    log.step(2, TOTAL_STEPS, f"Importing pool {pool_name} read-only...")
    if not zfs.import_backup_pool(pool_name, device, readonly=True, guid=drive.guid):
        log.fatal(f"Cannot import pool {pool_name}")
    cleanup.track_pool(pool_name)
    backup_disk = whole_disk(device)

    ks = Keystore(log)
    if not open_keystore(ks, pool_name, log, readonly=True):
        log.fatal("Cannot open keystore", causes=["Wrong passphrase (3 attempts)"])
    cleanup.track_keystore(ks)
    _ = ks.load_pool_keys(f"{pool_name}/rpool")
    log.ok("Encryption key loaded ✓")

    # Save system.key to temp — survives pool exports, used throughout recovery
    tmp_key = f"/tmp/zark_syskey_{os.getpid()}"
    _ = shutil.copy2(SYSTEM_KEY_PATH, tmp_key)
    os.chmod(tmp_key, 0o600)
    log.dbg(f"Saved system.key to {tmp_key}")

    # ── 3. Select restore point ──────────────────────────────────────────
    log.step(3, TOTAL_STEPS, "Available restore points...")
    ubuntu_name = _find_be(pool_name)
    if not ubuntu_name:
        log.fatal("Cannot find root dataset in backup")
    log.ok(f"Root dataset: {ubuntu_name}")
    snaps = [s for s in _list_snapshots(pool_name) if s.dataset != "keystore"]
    points = restore_points(snaps, f"rpool/ROOT/{ubuntu_name}")
    if not points:
        log.fatal("No restore point found on backup (no snapshot of the boot environment)")
    point = _choose_point(points, log)

    # ── 4. Restore plan: datasets, mount properties, hostid ──────────────
    log.step(4, TOTAL_STEPS, "Resolving every dataset for this point...")
    plan = _plan(pool_name, device, ubuntu_name, point, snaps, log)
    _show_plan(plan, log)
    if plan.hostid:
        _ = sh.run(f"zgenhostid -f 0x{plan.hostid}", log=log)
        log.ok(f"Hostid set to: {sh.run('hostid').output}")
    else:
        log.warn("No /etc/hostid in the backup — generating a new one")
        _ = sh.run("zgenhostid -f")

    # ── 5. Select internal disk + pre-flight ─────────────────────────────
    log.step(5, TOTAL_STEPS, "Selecting internal disk...")
    _refuse_imported_system_pools(log)
    internal_disk = _select_target(backup_disk, log)
    _preflight(plan, internal_disk, log)

    log.blank()
    log.warn(f"This will COMPLETELY ERASE {internal_disk}")
    log.info(f"Restore point: {point.label_utc()}")
    log.info(f"Source pool:   {pool_name}")
    if plan.skipped:
        log.warn(f"{len(plan.skipped)} dataset(s) will NOT be restored (see table above)")
    inferred = [r.rel for r in plan.rows if r.props and r.props.source == "inferred"]
    if inferred:
        log.warn(f"Mount properties inferred for: {', '.join(inferred)}")
    confirm = log.ask_text(
        "    Type YES to proceed: ",
        accept=("YES",),
        label=f"Type YES to erase {internal_disk}",
    )
    if confirm != "YES":
        log.fatal("Aborted by user")

    restore_start = time.time()
    errors: list[str] = []

    # ── 6. Cleanup + partition ───────────────────────────────────────────
    log.step(6, TOTAL_STEPS, "Cleaning up and partitioning...")
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
    _ = sh.run("udevadm settle --timeout=10")  # by-id -partN links come from udev

    for i in range(1, 5):
        if not Path(sh.part(internal_disk, i)).exists():
            log.fatal(f"Partition {sh.part(internal_disk, i)} not created")
        _ = sh.run(f"zpool labelclear -f {sh.part(internal_disk, i)}")
    log.ok("Partitions created")

    # ── 7. Create pools ──────────────────────────────────────────────────
    log.step(7, TOTAL_STEPS, "Creating bpool + rpool...")

    bpool_vdev = _stable_vdev(internal_disk, 2, log)
    rpool_vdev = _stable_vdev(internal_disk, 4, log)
    log.info(f"Pool devices: bpool {bpool_vdev}, rpool {rpool_vdev}")

    # Use only GRUB-safe features for bpool (compatible with GRUB 2.12+)
    features = " ".join(f"-o feature@{f}=enabled" for f in BPOOL_FEATURES_BASE.split())
    r = sh.run(
        f"zpool create -f -o ashift=12 -o autotrim=on -d {features} "
        + "-O devices=off -O mountpoint=none -O canmount=off "
        + "-O acltype=posixacl -O xattr=sa -O compression=lz4 -O normalization=formD "
        + f"-R {RECOVER_MNT} bpool {bpool_vdev}",
        log=log,
    )
    if not r.ok:
        log.fatal("Failed to create bpool")
    cleanup.track_pool("bpool")
    log.ok("bpool created")

    # Set at create time: the backup pool (with its keystore zvol) is still
    # imported, so a later `zfs set mountpoint` is forbidden (chase.c:648).
    # zpool create refuses a non-empty <altroot><mountpoint>, even with -f.
    root_dir = Path(f"{RECOVER_MNT}{plan.rpool_mountpoint}")
    if root_dir.is_dir() and any(root_dir.iterdir()):
        log.fatal(f"{root_dir} is not empty — zpool create would refuse rpool's mountpoint")
    r = sh.run(_rpool_create_cmd(plan.rpool_mountpoint, tmp_key, rpool_vdev), log=log)
    if not r.ok:
        log.fatal("Failed to create rpool container")
    cleanup.track_pool("rpool")
    # Set keylocation to standard Ubuntu path (used by dracut keystore module)
    _ = sh.run("zfs set keylocation=file:///run/keystore/rpool/system.key rpool")
    log.ok("rpool container created (encrypted, matching Ubuntu installer)")

    # ── 8. Raw send every rpool dataset (NO keystore = no zvols = safe) ──
    log.step(8, TOTAL_STEPS, "Restoring datasets via raw send (keystore deferred)...")
    _ = sh.run("zfs create -o canmount=off -o mountpoint=none rpool/ROOT", log=log)
    if any(r.rel.startswith("rpool/USERDATA/") for r in plan.rpool_rows):
        _ = sh.run("zfs create -o canmount=off -o mountpoint=none rpool/USERDATA", log=log)

    be_ds = f"rpool/ROOT/{ubuntu_name}"
    for row in plan.rpool_rows:
        assert row.snap is not None
        ok = _receive(
            f"{pool_name}/{row.rel}@{row.snap.name}",
            row.rel,
            row.options,
            raw=True,
            log=log,
        )
        if not ok:
            if row.rel == be_ds:
                log.fatal("Failed to raw send root dataset")
            errors.append(f"{row.rel}: receive failed")
    log.ok(f"{len(plan.rpool_rows)} rpool dataset(s) restored ✓")

    # ── 9. Export backup pool (removes its zvols) ────────────────────────
    log.step(9, TOTAL_STEPS, "Exporting backup pool (prevents kernel crash)...")
    ks.umount()
    _export_verified(pool_name, log)
    cleanup.untrack_pool(pool_name)

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

    # ── 11. Boot filesystem (mount properties were set at receive) ───────
    log.step(11, TOTAL_STEPS, "Setting bootfs...")
    _ = sh.run(f"zpool set bootfs=rpool/ROOT/{ubuntu_name} rpool")
    log.ok("bootfs set ✓")

    # ── 12. Reimport backup pool → bpool → keystore → export ─────────────
    # Neither bpool nor the keystore dataset is encrypted: no key is loaded.
    log.step(12, TOTAL_STEPS, "Reimporting backup pool for bpool + keystore restore...")
    if not zfs.import_backup_pool(pool_name, device, readonly=True, guid=drive.guid):
        log.fatal(
            f"Cannot reimport {pool_name} to restore bpool and the keystore",
            causes=["Without the keystore the recovered system cannot unlock rpool"],
            solutions=["Reconnect the backup drive and run recover again"],
        )
    cleanup.track_pool(pool_name)

    # ── 12a. bpool (no zvol on rpool yet; properties set at receive)
    bpool_received = False
    bpool_row = plan.row(f"bpool/BOOT/{ubuntu_name}")
    if bpool_row is not None and bpool_row.restored and bpool_row.snap is not None:
        _ = sh.run("zfs create -o canmount=off -o mountpoint=none bpool/BOOT", log=log)
        bpool_received = _receive(
            f"{pool_name}/{bpool_row.rel}@{bpool_row.snap.name}",
            bpool_row.rel,
            bpool_row.options,
            raw=False,
            log=log,
        )
        if not bpool_received:
            errors.append("bpool: receive failed — kernels need reinstallation")
    else:
        errors.append("bpool: not in backup at this point — kernels need reinstallation")

    # ── 12b. Keystore zvol (LAST zfs op — adds zvol to rpool)
    #
    # If anything goes wrong here the recovered system would boot without
    # a way to load its encryption key. Continuing silently in that state
    # is unsafe, so we abort with a detailed explanation and let the user
    # fix the backup before retrying. See _abort_missing_keystore() for
    # the full rationale and the rejected fallback options.
    log.info("Restoring keystore zvol...")
    r = sh.run_pipe(
        f"zfs send {plan.keystore_snap}",
        "zfs receive -u -o encryption=off rpool/keystore",
    )
    if not r.ok:
        log.error(f"Keystore restore failed: {r.stderr.strip()}")
        if sh.is_enospc(r.stderr) or sh.is_enospc(r.stdout):
            log.fatal(
                "Recovery ran out of space while restoring keystore",
                causes=["rpool data + keystore zvol exceed target disk capacity"],
                solutions=["Recover to a larger disk"],
            )
        _abort_missing_keystore("send_failed", pool_name, log)
    log.ok("rpool/keystore restored ✓")

    log.info(f"Exporting backup pool {pool_name} (final)...")
    _export_verified(pool_name, log)
    cleanup.untrack_pool(pool_name)

    # ── 13. Mount system ─────────────────────────────────────────────────
    log.step(13, TOTAL_STEPS, "Mounting system...")
    _mount_restored_system(ubuntu_name, bpool_received, log)

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
    _install_boot_chain(internal_disk, ubuntu_name, zfs, cleanup, log)
    errors += regenerate_initrd(RECOVER_MNT, log)

    # ── 16. Cleanup and summary ──────────────────────────────────────────
    log.step(16, TOTAL_STEPS, "Cleanup...")

    if Path(tmp_key).exists():
        os.remove(tmp_key)

    cleanup.run()

    restore_end = time.time()
    restore_mins, restore_secs = divmod(int(restore_end - restore_start), 60)
    total_mins, total_secs = divmod(int(restore_end - recover_start), 60)

    summary = [
        f"Script version:  {log.W}zark v{VERSION}{log.N}",
        f"Restored from:   {log.W}{pool_name}{log.N}",
        f"Restore point:   {log.W}{point.label_utc()}{log.N}",
        f"Internal disk:   {log.W}{internal_disk}{log.N}",
        f"Dataset restore: {log.W}{restore_mins}m {restore_secs}s{log.N}",
        f"Total time:      {log.W}{total_mins}m {total_secs}s{log.N}",
    ]
    if plan.skipped:
        summary += ["", f"{log.Y}Not restored:{log.N}"]
        summary += [f"  • {r.rel} — {r.note}" for r in plan.skipped]
    if errors:
        summary += ["", f"{log.R}Errors:{log.N}"]
        summary += [f"  • {e}" for e in errors]
    summary += [
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
    ]
    if errors:
        log.banner_error("RECOVERY COMPLETED WITH ERRORS", summary)
    else:
        log.banner_ok("RECOVERY COMPLETE", summary)

    # By this point the backup pool has been exported (steps 9 and 12)
    # and any cleanup.run() exports have issued the kernel-side flush.
    # Offer the eject before the final visible signal. Default "yes":
    # the next step in the printed instructions above is "remove live
    # USB and reboot", so the typical operator path is to disconnect.
    prompt_eject_or_attach(
        device,
        pool_name,
        log,
        default_eject=True,
        autoeject=cfg.drive_autoeject(pool_name),
    )
