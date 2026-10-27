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
zark recover - Full bare-metal system recovery from backup drive.

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
           12a. Restore bpool content  (NO zvols on rpool yet - safe)
           12b. Restore keystore zvol  (adds zvol - LAST zfs operation)
         Export backup pool, verified.
  13.    Mount system, write EFI/crypttab/fstab
  14.    Restore hostid to target
  15.    Chroot: cachefile, grub.cfg UUID fix, grub-install, initrd
  16.    Cleanup and summary
"""

import os
import re
import shlex
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, NoReturn

from lib import (
    certificates,
    drive_marks,
    exclusions,
    platform_facts,
    pool_record,
    replication,
    sh,
    survey,
    zfs_features,
)
from lib.backup_layout import find_be as _find_be
from lib.boot_chain import RECOVER_MNT, install_boot_chain, mount_restored_system
from lib.cleanup import Cleanup, prompt_eject_or_attach
from lib.config import VERSION, Config, zark_command
from lib.drives import backup_device, scan_connected_drives, select_drive
from lib.identity import BY_ID_DIR, by_id_names, preferred_by_id, protected_disks, whole_disk
from lib.initrd import installed_kernels, kernels_without_vmlinuz, regenerate_initrd
from lib.keystore import SYSTEM_KEY_PATH, Keystore, open_keystore
from lib.log import Log
from lib.restore_plan import (
    RestorePlan,
    RestoreRow,
    build_plan,
    choose_point,
    is_incomplete,
    list_snapshots,
    partly_removed,
    show_plan,
)
from lib.restore_points import (
    Snap,
    complete_points,
    point_name,
    restore_points,
    run_complete_points,
)
from lib.zfs import ZFS

# The oldest live recover is made for (M6-8): older ones get a WARN.
OLDEST_LIVE = (26, 4)

# bpool features safe for GRUB 2.12 (Ubuntu 24.04 / initramfs-tools)
BPOOL_FEATURES_BASE = (
    "async_destroy bookmarks embedded_data empty_bpobj enabled_txg "
    "extensible_dataset filesystem_limits hole_birth large_blocks "
    "lz4_compress spacemap_histogram"
)


# The installer's root properties (subiquity manipulator.py:242-265), M6R-1;
# relatime is on by default since 2.2, local here as the installer sets it.
ROOT_PROPS = {
    "bpool": (
        "devices=off",
        "acltype=posixacl",
        "xattr=sa",
        "compression=lz4",
        "normalization=formD",
        "relatime=on",
    ),
    "rpool": (
        "acltype=posixacl",
        "xattr=sa",
        "dnodesize=auto",
        "normalization=formD",
        "relatime=on",
        "compression=lz4",
        "devices=off",
    ),
}

TOTAL_STEPS = 16

# Partition layout written in step 6, in MiB before the rpool partition:
# 1 MiB alignment + 1 GiB ESP + 2 GiB bpool + 8 GiB swap.
_LAYOUT_BEFORE_RPOOL_MIB = 1 + 1024 + 2048 + 8192
_BPOOL_PART_BYTES = 2 * 1024**3

# Headroom on top of the restored data: ZFS metadata and slop space.
_RECOVER_TARGET_OVERHEAD_PCT = 5


# The tools recover runs on the live, by the package that has them (M2P.3):
# no syncoid, pv, mbuffer, lzop, bc or sanoid, which recover never runs.
RECOVER_TOOLS = {
    "zfs": "zfsutils-linux",
    "zgenhostid": "zfsutils-linux",
    "sgdisk": "gdisk",
    "cryptsetup": "cryptsetup-bin",
    "mkfs.vfat": "dosfstools",
    "partprobe": "parted",
}


def _install_missing_tools(log: Log) -> None:
    """Install the packages of the tools the live lacks, only those."""
    missing = sorted({pkg for t, pkg in RECOVER_TOOLS.items() if not sh.run(f"which {t}").ok})
    if missing:
        log.info(f"Installing required packages: {' '.join(missing)}")
        _ = sh.run(f"apt-get install -y {' '.join(missing)}", log=log)


def _is_live_usb() -> bool:
    # Thin wrapper kept for call-site readability; the detection logic now
    # lives in lib.sh as the single source of truth (also used by repair-boot).
    return sh.is_live_usb()


def _load_keys_from_file(keyfile: str, pool_root: str, log: Log) -> int:
    """Load the keys of the encryption roots under pool_root with a key file;
    the number loaded."""
    zfs = ZFS(log)
    count = 0
    for ds in zfs.encryption_roots_needing_key(pool_root):
        r = sh.run(f"zfs load-key -L file://{keyfile} {ds}")
        if r.ok:
            count += 1
            log.dbg(f"Key loaded: {ds}")
        else:  # a root that fails is a real failure now, not inheritance
            log.dbg(f"Key load failed: {ds}: {r.stderr.strip()}")
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
          Trivial to implement but degrades the security model - the raw key
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
            "This is unusual - `zark prepare` always creates an initial snapshot.",
        ],
        "send_failed": [
            f"`zfs send {pool_name}/keystore@... | zfs receive rpool/keystore` failed.",
            "Possible causes: USB I/O error, corrupted snapshot, ZFS version",
            "mismatch between source and target.",
        ],
    }
    causes = causes_by_reason.get(reason, [f"Unknown failure mode: {reason}"])

    log.banner_error(
        "RECOVERY ABORTED - KEYSTORE MISSING FROM BACKUP",
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
            f"     → Boot the original system and re-run `{zark_command()} prepare` on the",
            f"       backup drive. This recreates {pool_name}/keystore properly.",
            f"     → Then re-run `{zark_command()} backup` to refresh data.",
            f"     → Then retry `{zark_command()} recover` from the live USB.",
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


# ── Restore plan ─────────────────────────────────────────────────────────


def _pools_from_record(
    pool: str, point: str, snaps: list[Snap], log: Log
) -> dict[str, pool_record.Create]:
    """How rpool and bpool are created from the point's record of origin's
    pools (M6-6), decided before anything is erased: a feature origin had
    active that this ZFS lacks is a FATAL here. Empty without a usable
    record: the pools are created as before 2.0.0-rc11, with a WARN (M6-7)."""
    try:
        rec = pool_record.read(pool, point, snaps)
    except survey.ReadError as e:
        log.fatal(
            f"Cannot read how origin's pools were at this point on {pool}: {e}",
            causes=["The drive did not answer a zfs get; nothing was written"],
            solutions=[f"Check the drive: zpool status {pool}", "Run recover again"],
        )
    except ValueError as e:
        log.warn(str(e))
        rec = None
    known = set(zfs_features.parse_upgrade_v(sh.run("zpool upgrade -v").stdout))
    if rec is None or not known or not all(p in rec.pools for p in ("rpool", "bpool")):
        live = ", ".join(ln.strip() for ln in sh.run("zfs version").lines) or "unknown"
        # Both lines: without -d, zpool create enables what the module supports
        # (fi_zfs_mod_supported, cmd/zpool/zpool_main.c:2240-2286 at OpenZFS 2.4.1).
        log.warn(
            f"{_no_record_why(rec, known)}: rpool gets every feature "
            + f"of this live's ZFS ({live}). If the restored "
            + "system runs an older OpenZFS, it may not import rpool once one of them is active."
        )
        return {}
    specs = {p: pool_record.create_spec(rec.pools[p], known) for p in ("rpool", "bpool")}
    lacking = [f"{p}: {', '.join(s.lacking_active)}" for p, s in specs.items() if s.lacking_active]
    if lacking:
        names = sorted({f for s in specs.values() for f in s.lacking_active})
        guids = tuple(f.guid for f in zfs_features.FEATURES if f.name in names)
        log.fatal(
            "This live's ZFS lacks features origin's pools had active at this point",
            causes=[
                *lacking,
                f"This live runs {(sh.run('zfs version').lines or ['?'])[0].strip()}",
            ],
            solutions=[
                f"Boot a live session with {zfs_features.needed_release(guids)} or later and "
                + "run recover from it",
                "Nothing was erased",
            ],
        )
    if "encryption" not in specs["rpool"].features:
        log.fatal(
            "Origin's rpool did not have the encryption feature at this point",
            causes=[
                "recover creates rpool encrypted, and zpool create refuses an encrypted "
                + "root without that feature"
            ],
            solutions=["Restore a point of an encrypted rpool", "Nothing was erased"],
        )
    for p, s in specs.items():
        log.info(pool_record.describe(rec, p))
        for prop in pool_record.root_options(s, ROOT_PROPS[p])[1]:
            log.info(
                f"  {p}: {prop} left to the default, origin's pool did not have "
                + pool_record.NEEDS_FEATURE[prop]
            )
        if s.lacking_enabled:
            log.info(f"  {p}: not enabled, this ZFS lacks them: {', '.join(s.lacking_enabled)}")
        if s.compat_missing:
            log.info(f"  {p}: compatibility={s.compat_missing} left off (not on this live)")
    return specs


def _no_record_why(rec: pool_record.PoolRecord | None, known: set[str]) -> str:
    """Why the pools are created as before 2.0.0-rc11, for recover's WARN (M6-7)."""
    # A whole record on a live that lists no feature is not an old point (M6R-4).
    if rec is not None and not known and all(p in rec.pools for p in ("rpool", "bpool")):
        return (
            "This live's zpool upgrade -v lists no feature, so origin's recorded pool "
            + "features cannot be checked against it"
        )
    return (
        "This point does not record origin's pool features (written before zark "
        + f"2.0.0-rc11{'' if rec is None else ', or incomplete'})"
    )


def _warn_old_live(log: Log) -> None:
    """A WARN on a live older than 26.04 (M6-8 8a), and recover goes on: the
    drive's feature check and the pre-flight stop what cannot work there."""
    rel = platform_facts.ubuntu_release()
    found = platform_facts.ubuntu_tuple(rel)
    if found is None or found >= OLDEST_LIVE:
        return
    zv = sh.run("zfs version")
    zfs = ", ".join(ln.strip() for ln in zv.lines) if zv.ok and zv.lines else "unknown"
    log.warn(
        f"This live runs Ubuntu {rel} ({zfs}); zark recovers from Ubuntu "
        + f"{OLDEST_LIVE[0]}.{OLDEST_LIVE[1]:02d} or newer. Going on: what this live "
        + "cannot do stops recover before anything is erased."
    )


def needs_userdata(plan: RestorePlan) -> bool:
    """Whether step 8 creates the structural ``rpool/USERDATA``: a dataset
    the point restores below it, or one it recreates empty (M5b-13 13a)."""
    return any(r.rel.startswith("rpool/USERDATA/") for r in [*plan.rpool_rows, *plan.empty_rows])


def _rpool_create_cmd(
    mountpoint: str, key_file: str, vdev: str, spec: pool_record.Create | None = None
) -> str:
    """``zpool create`` for the encrypted rpool, root mountpoint as in origin;
    ``spec`` the record's (M6-6), None for every feature of this ZFS."""
    return (
        "zpool create -f -o ashift=12 -o autotrim=on "
        + (f"{spec.options()} " if spec else "")
        + f"{pool_record.root_options(spec, ROOT_PROPS['rpool'])[0]} "
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
    log.warn(f"No /dev/disk/by-id name for {sh.part(disk, n)} - the pool will record a kernel name")
    return sh.part(disk, n)


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
            "Cannot measure the restore size - refusing to erase the disk",
            causes=[
                *(f"referenced unknown for {u}" for u in unknown),
                *([f"size of {target_disk} unknown"] if disk_bytes <= 0 else []),
            ],
        )
    rpool_part = disk_bytes - _LAYOUT_BEFORE_RPOOL_MIB * 1024**2
    need_r = plan.rpool_bytes * (100 + _RECOVER_TARGET_OVERHEAD_PCT) // 100
    need_b = plan.bpool_bytes * (100 + _RECOVER_TARGET_OVERHEAD_PCT) // 100
    if need_r > rpool_part or need_b > _BPOOL_PART_BYTES:
        # The monthly bases are a convenience (M4b-6): the point alone may fit.
        dropped = plan.drop_bases()
        if dropped:
            log.warn(
                f"With their monthly bases the datasets do not fit on {target_disk}: "
                + f"{dropped} dataset(s) restored from the point only"
            )
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
    # Checked here, not after the wipe: zpool create refuses a non-empty
    # <altroot><mountpoint>, and step 6's rm stays off mounted subtrees.
    # The whole table, filtered: `findmnt -R <dir>` lists nothing when <dir>
    # itself is not a mountpoint, however much is mounted below it.
    table = sh.run("findmnt -rn -o TARGET")
    if not table.ok:
        log.fatal(f"Cannot list the mounts to check {RECOVER_MNT}: {table.stderr.strip()}")
    held = [t for t in table.lines if t == RECOVER_MNT or t.startswith(f"{RECOVER_MNT}/")]
    if held:
        tops = [t for t in held if not any(t.startswith(f"{o}/") for o in held)]
        log.fatal(
            f"Something is still mounted under {RECOVER_MNT}",
            causes=held,
            solutions=[*(f"sudo umount -R {t}" for t in tops), "Or reboot the live USB"],
        )
    missing = [t for t in RECOVER_TOOLS if not sh.run(f"which {t}").ok]
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
        log.warn("No bpool snapshot at this point - kernels will need reinstallation")
    _check_sizes(plan, target_disk, log)


def _receive(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    src: str, dst: str, options: list[str], raw: bool, log: Log, base: str = ""
) -> bool:
    """``zfs send [-w] [-i base] src | zfs receive -u <options> dst``; ENOSPC
    is fatal."""
    send = "zfs send " + ("-w " if raw else "") + (f"-i {base} " if base else "") + src
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


class Received(NamedTuple):
    """Where a dataset's receive left it: the row as restored (its snapshot
    the point, or the monthly base when only that arrived; None when nothing
    did), and the error line for the summary, "" when the point arrived."""

    row: RestoreRow | None
    error: str


def _receive_row(pool: str, row: RestoreRow, raw: bool, log: Log) -> Received:
    """One dataset: its monthly base in full, then the point with ``-i``
    from it (M4b-6); the point in full when it has no base.

    A base that arrived leaves the dataset at that older monthly, which is
    not "receive failed" (M4bR-6), and is what gets anchored (M4cP.2).
    """
    assert row.snap is not None
    point = f"{pool}/{row.snap.stored}@{row.snap.name}"
    if row.base is None:
        if _receive(point, row.rel, row.options, raw=raw, log=log):
            return Received(row, "")
        return Received(None, f"{row.rel}: receive failed")
    base = f"{pool}/{row.base.stored}@{row.base.name}"
    if not _receive(base, row.rel, row.options, raw=raw, log=log):
        return Received(None, f"{row.rel}: receive failed")
    if _receive(point, row.rel, row.options, raw=raw, log=log, base=base):
        return Received(row, "")
    return Received(
        replace(row, snap=row.base),
        f"{row.rel}: left at @{row.base.name}, older than the point (the point's receive failed)",
    )


def restore_rpool_rows(
    pool: str, rows: list[RestoreRow], be_ds: str, disk_guid: str, log: Log
) -> list[str]:
    """Step 8: raw-receive every rpool dataset, then anchor what arrived;
    the error lines. A boot environment that did not reach its point is fatal."""
    reached: list[RestoreRow] = []  # at the point
    at_base: list[RestoreRow] = []  # left at their monthly
    errors: list[str] = []
    for row in rows:
        got = _receive_row(pool, row, raw=True, log=log)
        if got.error:
            if row.rel == be_ds:
                log.fatal("Failed to raw send root dataset")
            errors.append(got.error)
        if got.row is not None:
            (at_base if got.error else reached).append(got.row)
    if len(reached) == len(rows):
        log.ok(f"{len(rows)} of {len(rows)} rpool dataset(s) restored ✓")
    else:
        log.warn(f"{len(reached)} of {len(rows)} rpool dataset(s) restored")
    # Before the backup pool goes: the anchors are for the drive just read.
    # A monthly that arrived is a base sanoid must not prune either (M4cP.2).
    return errors + anchor_restored(reached + at_base, pool, disk_guid, log)


def restore_bpool(
    pool: str, row: RestoreRow | None, disk_guid: str, log: Log
) -> tuple[str | None, list[str]]:
    """Step 12a: receive bpool's boot environment and anchor what arrived.

    Returns where it was left ("" at the point, the monthly's name when
    only that arrived, None when nothing was restored) and the error lines.
    A bpool at its monthly is still mounted and anchored (M4cP.2): GRUB
    reads it at boot whether zark mounts it or not.
    """
    if row is None or not row.restored or row.snap is None:
        return None, ["bpool: not in backup at this point - kernels need reinstallation"]
    _ = sh.run("zfs create -o canmount=off -o mountpoint=none bpool/BOOT", log=log)
    got = _receive_row(pool, row, raw=False, log=log)
    if got.row is None or got.row.snap is None:
        return None, ["bpool: receive failed - kernels need reinstallation"]
    errors = [got.error] if got.error else []
    # Before the keystore: the keystore stays the last zfs operation.
    if failure := anchor_bpool(got.row, pool, disk_guid, log):
        errors.append(failure)
    return (got.row.snap.name if got.error else ""), errors


def kernel_gaps(root: str, monthly: str) -> list[str]:
    """With /boot at bpool's monthly and the root at the point: each kernel
    the root has and /boot lacks, and whether any kernel is in both."""
    errors = [
        f"kernel {v}: in the restored root but not in /boot (bpool at @{monthly}): "
        + f"reinstall it from zark chroot: apt install --reinstall linux-image-{v}"
        for v in kernels_without_vmlinuz(root)
    ]
    if not installed_kernels(root):
        errors.append(
            "no kernel in /boot matches the restored root: it will not boot until one "
            + "is reinstalled from zark chroot"
        )
    return errors


def _anchor_for(disk_guid: str, row: RestoreRow) -> str:
    """The drive's anchor name for the row's restored snapshot: the name
    ``anchor_held`` gives a held snapshot, the run's own anchor name when
    the snapshot is the point (M4c-12)."""
    assert row.snap is not None
    stamp = replication.point_name(datetime.fromtimestamp(row.snap.creation, UTC))
    return replication.anchor_name(disk_guid, stamp)


def _not_anchored(rel: str, pool: str) -> str:
    return f"{rel}: not anchored for {pool}: its next backup of it resends it in full"


def anchor_restored(rows: list[RestoreRow], pool: str, disk_guid: str, log: Log) -> list[str]:
    """M4c-12 (M4bF-2): bookmark every restored rpool dataset's snapshot as
    the anchor of the drive it came from, so that drive's next backup
    continues from it instead of resending in full. No key is needed; a
    failure is an error line, never fatal."""
    errors: list[str] = []
    for row in rows:
        assert row.snap is not None
        name = _anchor_for(disk_guid, row)
        r = sh.run(f"zfs bookmark {row.rel}@{row.snap.name} {row.rel}#{name}", log=log)
        if r.ok:
            log.dbg(f"  {row.rel}: anchored {row.snap.name} for {pool}")
        else:
            log.warn(f"  {row.rel}: could not anchor {row.snap.name}: {r.stderr.strip()}")
            errors.append(
                f"{row.rel}: not anchored for {pool}: its next backup of it resends it in full"
            )
    if rows and not errors:
        log.ok(f"{len(rows)} rpool dataset(s) anchored for {pool} ✓")
    return errors


def anchor_bpool(row: RestoreRow, pool: str, disk_guid: str, log: Log) -> str:
    """M4c-12: the restored bpool snapshot renamed to the drive's bpool
    anchor (I-B; I-F: no bookmark on bpool). "" or the error line."""
    assert row.snap is not None
    name = _anchor_for(disk_guid, row)
    r = sh.run(f"zfs rename {row.rel}@{row.snap.name} {row.rel}@{name}", log=log)
    if r.ok:
        log.ok(f"{row.rel}: {row.snap.name} kept as the anchor of {pool} ({name}) ✓")
        return ""
    log.warn(f"  {row.rel}: could not anchor {row.snap.name}: {r.stderr.strip()}")
    return _not_anchored(row.rel, pool)


def drop_prepare_snapshot(keystore_snap: str, log: Log) -> None:
    """M4c-14 (M4bP.8): the keystore arrives with the ``@prepare_<stamp>``
    snapshot prepare takes on the drive, which prepare destroys in origin
    (I-B); left here, backup lists it as a residue. A zvol snapshot has no
    device node (snapdev=hidden), so its destroy touches no device; it runs
    after the backup pool's final export. A failure is a warning."""
    name = keystore_snap.split("@", 1)[-1]
    if not name.startswith("prepare_"):
        return
    r = sh.run(f"zfs destroy rpool/keystore@{name}", log=log)
    if r.ok:
        log.ok(f"rpool/keystore@{name} removed (prepare's snapshot, not origin's)")
    else:
        log.warn(f"Could not remove rpool/keystore@{name}: {r.stderr.strip()}")


def _export_verified(pool: str, log: Log) -> None:
    """Export the backup pool and prove its zvols are gone (hallazgo 9)."""
    _ = sh.run(f"zfs unload-key -r {pool}")
    if not sh.run(f"zpool export {pool}", log=log).ok:
        _ = sh.run(f"zpool export -f {pool}", log=log)
    _ = sh.run("udevadm settle --timeout=10")  # /dev/zvol symlinks go away via udev
    still = sh.run(f"zpool list {pool}").ok or sh.run(f"test -e /dev/zvol/{pool}").ok
    if still:
        log.fatal(
            f"{pool} could not be exported - refusing to continue",
            causes=["Its keystore zvol would stay present during the next steps"],
            solutions=[f"Check: zpool status {pool}; fuser -vm /dev/zvol/{pool}/keystore"],
        )
    log.ok(f"{pool} exported - its zvols are gone")


def _restore_encryption_root(log: Log) -> list[str]:
    """``zfs change-key -i`` every received dataset back under rpool's key.

    Returns one error per dataset left as its own encryptionroot, for the
    verdict (M2R-16, M4a-14).
    """
    errors: list[str] = []
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
            # In the verdict (M2R-16, M4a-14): a dataset left as its own
            # encryptionroot is not the layout the installer made.
            log.warn(f"  change-key -i failed: {ds}: {r.stderr.strip()}")
            errors.append(f"{ds}: change-key -i failed (still its own encryptionroot)")
    if changed:
        log.ok(f"Encryption hierarchy restored ({changed} dataset(s) → encryptionroot=rpool) ✓")
    elif not errors:
        log.ok("Encryption hierarchy already correct ✓")
    return errors


def create_excluded(rows: list[RestoreRow], log: Log) -> list[str]:
    """Each dataset the point left out, parents first, as an empty dataset
    with the canmount and mountpoint origin had (the mountpoint only when
    it would not inherit it) and, on each exclusion's top, the exclusion
    itself, so the restored system keeps it. ``-u``: nothing is mounted
    here; encryption is inherited from the parent (rpool's key). A failure
    is an error line, never fatal: the restored data is intact."""
    errors: list[str] = []
    for row in rows:
        props = list(row.options)
        if row.flags:
            props.append(f"-o {exclusions.REPLICATE}=no")
        if row.flags == exclusions.BOTH:
            props.append(f"-o {exclusions.SNAPSHOT}=no")
        r = sh.run(f"zfs create -u {' '.join(props)} {shlex.quote(row.rel)}", log=log)
        if r.ok:
            log.ok(f"{row.rel}: created empty (excluded from backups at this point)")
        else:
            log.error(f"{row.rel}: could not create it empty: {r.stderr.strip()}")
            errors.append(f"{row.rel}: excluded at this point, not created: {r.stderr.strip()}")
    return errors


def restore_snapshot_exclusions(
    plan: RestorePlan, excluded: exclusions.PointMark | None, log: Log
) -> list[str]:
    """``org.zark:snapshot=no`` again on each restored dataset the point
    recorded as excluded from snapshots only: a raw send carries no user
    property, and without it the restored system's next setup would let
    sanoid snapshot the subtree again. A user property mounts nothing."""
    errors: list[str] = []
    for top in excluded.no_snapshot if excluded else []:
        row = plan.row(top)
        if row is None or not row.restored:
            continue
        r = sh.run(f"zfs set {exclusions.SNAPSHOT}=no {shlex.quote(top)}", log=log)
        if r.ok:
            log.ok(f"{top}: excluded from snapshots again, as at this point")
        else:
            errors.append(f"{top}: {exclusions.SNAPSHOT}=no not set again: {r.stderr.strip()}")
    return errors


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
    certificates.show_warnings(log)

    uptime = sh.run("uptime -p").output or sh.run("cat /proc/uptime").output
    log.info(f"System uptime: {uptime}")

    # ── Verify live USB ──────────────────────────────────────────────────
    if not _is_live_usb():
        log.warn("NOT running from a live USB environment")
        confirm = log.ask_text("  Type IUNDERSTAND to continue anyway: ", accept=("IUNDERSTAND",))
        if confirm != "IUNDERSTAND":
            return

    _warn_old_live(log)
    _install_missing_tools(log)

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
    # A drive using features this live's ZFS lacks is named before any import (M4s-11).
    zfs_features.check_drive(pool_name, device, log)
    if not zfs.import_backup_pool(pool_name, device, readonly=True, guid=drive.guid):
        log.fatal(f"Cannot import pool {pool_name}")
    cleanup.track_pool(pool_name)
    # Strict (M3X.1, M4c-18): an unread last-point would make the drive a
    # legacy one and change which points count as complete.
    try:
        meta = drive_marks.log_metadata(pool_name, log, strict=True)
    except survey.ReadError as e:
        log.fatal(
            f"Cannot read the metadata of {pool_name}: {e}",
            causes=["The drive did not answer a zfs get; nothing was written"],
            solutions=[f"Check the drive: zpool status {pool_name}", "Run recover again"],
        )
    backup_disk = whole_disk(device)

    ks = Keystore(log)
    if not open_keystore(ks, pool_name, log, readonly=True):
        log.fatal("Cannot open keystore", causes=["Wrong passphrase (3 attempts)"])
    cleanup.track_keystore(ks)
    _ = ks.load_pool_keys(f"{pool_name}/rpool")
    log.ok("Encryption key loaded ✓")

    # Save system.key to temp - survives pool exports, used throughout recovery.
    # Private from creation, in a directory Cleanup removes on every exit it handles.
    key_dir = tempfile.mkdtemp(prefix="zark_syskey_")
    cleanup.track_dir(key_dir)
    tmp_key = os.path.join(key_dir, "system.key")
    fd = os.open(tmp_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        _ = f.write(Path(SYSTEM_KEY_PATH).read_bytes())
    log.dbg(f"Saved system.key to {tmp_key}")

    # ── 3. Select restore point ──────────────────────────────────────────
    log.step(3, TOTAL_STEPS, "Available restore points...")
    ubuntu_name = _find_be(pool_name, log)
    if not ubuntu_name:
        log.fatal("Cannot find root dataset in backup")
    log.ok(f"Root dataset: {ubuntu_name}")
    snaps = [s for s in list_snapshots(pool_name, log) if s.dataset != "keystore"]
    points = restore_points(snaps, f"rpool/ROOT/{ubuntu_name}")
    if not points:
        log.fatal("No restore point found on backup (no snapshot of the boot environment)")
    complete = complete_points(snaps, meta.get("last-point", ""))
    run_complete = run_complete_points(snaps, meta.get("last-point", ""))
    point = choose_point(points, log, complete, run_complete)

    # ── 4. Restore plan: datasets, mount properties, hostid ──────────────
    log.step(4, TOTAL_STEPS, "Resolving every dataset for this point...")
    try:
        excluded = exclusions.read_point_mark(pool_name, point_name(point), snaps)
    except survey.ReadError as e:
        log.fatal(
            f"Cannot read what this point left out on {pool_name}: {e}",
            causes=["The drive did not answer a zfs get; nothing was written"],
            solutions=[f"Check the drive: zpool status {pool_name}", "Run recover again"],
        )
    except ValueError as e:
        log.warn(f"{e}: every dataset is restored as if nothing were excluded")
        excluded = None
    plan = build_plan(pool_name, device, ubuntu_name, point, snaps, log, complete, excluded)
    show_plan(plan, log, {i.guid: n for n, i in cfg.known_drives.items()})
    pools_spec = _pools_from_record(pool_name, point_name(point), snaps, log)
    if plan.hostid:
        _ = sh.run(f"zgenhostid -f 0x{plan.hostid}", log=log)
        log.ok(f"Hostid set to: {sh.run('hostid').output}")
    else:
        log.warn("No /etc/hostid in the backup - generating a new one")
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
    if plan.empty_rows:
        log.info(
            f"{len(plan.empty_rows)} dataset(s) excluded from backups at this point are "
            + "created empty (see table above)"
        )
    if is_incomplete(point, complete):
        earlier = [
            r for r in plan.rows if r.restored and r.snap and r.snap.name != point_name(point)
        ]
        if partly_removed(point, complete, run_complete):
            log.warn(
                f"This backup point lost {len(earlier)} of its datasets on the drive since "
                + "its run (a prune, a rollback or a destroy): they come from earlier "
                + "snapshots (Δ column)"
            )
        else:
            log.warn(
                "This backup point is from an incomplete run: "
                + f"{len(earlier)} dataset(s) come from earlier snapshots (Δ column)"
            )
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
    _ = sh.run(f"rm -rf --one-file-system {RECOVER_MNT}")
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

    # bpool as origin had it (M6-6); without a record, only GRUB-safe features.
    bpool_spec = pools_spec.get("bpool")
    features = (
        bpool_spec.options()
        if bpool_spec
        else "-d " + " ".join(f"-o feature@{f}=enabled" for f in BPOOL_FEATURES_BASE.split())
    )
    r = sh.run(
        f"zpool create -f -o ashift=12 -o autotrim=on {features} "
        + f"{pool_record.root_options(bpool_spec, ROOT_PROPS['bpool'])[0]} "
        # The root at /boot, as the installer has it (M6X.1); canmount=off keeps
        # zpool create from mounting it (lib/libzfs/libzfs_mount.c:241-262).
        + "-O mountpoint=/boot -O canmount=off "
        + f"-R {RECOVER_MNT} bpool {bpool_vdev}",
        log=log,
    )
    if not r.ok:
        log.fatal("Failed to create bpool")
    cleanup.track_pool("bpool")
    log.ok("bpool created")

    # Set at create time: the backup pool (with its keystore zvol) is still
    # imported, so a later `zfs set mountpoint` is forbidden (chase.c:648).
    r = sh.run(
        _rpool_create_cmd(plan.rpool_mountpoint, tmp_key, rpool_vdev, pools_spec.get("rpool")),
        log=log,
    )
    if not r.ok:
        log.fatal("Failed to create rpool container")
    cleanup.track_pool("rpool")
    # Set keylocation to standard Ubuntu path (used by dracut keystore module)
    _ = sh.run("zfs set keylocation=file:///run/keystore/rpool/system.key rpool")
    log.ok("rpool container created (encrypted, matching Ubuntu installer)")

    # ── 8. Raw send every rpool dataset (NO keystore = no zvols = safe) ──
    log.step(8, TOTAL_STEPS, "Restoring datasets via raw send (keystore deferred)...")
    _ = sh.run("zfs create -o canmount=off -o mountpoint=none rpool/ROOT", log=log)
    if needs_userdata(plan):
        _ = sh.run("zfs create -o canmount=off -o mountpoint=none rpool/USERDATA", log=log)

    errors += restore_rpool_rows(
        pool_name, plan.rpool_rows, f"rpool/ROOT/{ubuntu_name}", drive.guid, log
    )

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
    errors += _restore_encryption_root(log)

    # ── 10c. What the point left out, created empty (M5b-13 13a) ────────
    # The backup pool is exported (no zvol anywhere: the chase.c:648 rule
    # has nothing to apply to) and rpool's keys are loaded.
    errors += create_excluded(plan.empty_rows, log)
    errors += restore_snapshot_exclusions(plan, excluded, log)

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
    bpool_left_at, bpool_errors = restore_bpool(
        pool_name, plan.row(f"bpool/BOOT/{ubuntu_name}"), drive.guid, log
    )
    errors += bpool_errors
    bpool_received = bpool_left_at is not None

    # ── 12b. Keystore zvol (LAST zfs op - adds zvol to rpool)
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
    drop_prepare_snapshot(plan.keystore_snap, log)

    # ── 13. Mount system ─────────────────────────────────────────────────
    log.step(13, TOTAL_STEPS, "Mounting system...")
    mount_restored_system(ubuntu_name, bpool_received, log)
    if bpool_left_at:
        errors += kernel_gaps(RECOVER_MNT, bpool_left_at)

    # Format EFI partition
    _ = sh.run(f"mkfs.vfat -F32 {sh.part(internal_disk, 1)}", log=log)
    _ = sh.run(f"mkdir -p {RECOVER_MNT}/boot/efi")
    _ = sh.run(f"mount {sh.part(internal_disk, 1)} {RECOVER_MNT}/boot/efi")
    cleanup.track_mount(f"{RECOVER_MNT}/boot/efi")

    # crypttab - preserve existing lines, update swap only.
    # NOTE: keystore-rpool is NOT in crypttab - it's handled by the dracut
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
            else "the zfs initramfs script"
        )
        log.ok(f"crypttab written (swap only - keystore handled by {ks_handler})")

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
    errors += install_boot_chain(
        internal_disk, ubuntu_name, zfs, cleanup, log, bpool_as_recorded=bool(pools_spec)
    )
    errors += regenerate_initrd(RECOVER_MNT, log)

    # ── 16. Cleanup and summary ──────────────────────────────────────────
    log.step(16, TOTAL_STEPS, "Cleanup...")

    cleanup.run()  # also removes the system.key copy

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
        summary += [f"  • {r.rel} - {r.note}" for r in plan.skipped]
    if plan.empty_rows:
        summary += ["", "Created empty (excluded from backups at this point):"]
        summary += [
            f"  • {r.rel}" + (f" ({r.below} dataset(s) below it not created)" if r.below else "")
            for r in plan.empty_rows
        ]
    if errors:
        summary += ["", f"{log.R}Errors:{log.N}"]
        summary += [f"  • {e}" for e in errors]
    summary += [
        "",
        f"{log.W}Next steps:{log.N}",
        "  1. Remove the live USB",
        f"  2. {log.Y}Disconnect the backup drive{log.N}",
        "  3. Reboot - enter your rpool passphrase at the prompt",
        f"  {log.Y}⚠  If it drops to emergency shell on first boot:{log.N}",
        f"     {log.W}zpool import rpool && exit{log.N}",
        f"  4. Run: {log.W}{zark_command()} finish{log.N}  (it also regenerates grub.cfg)",
        "",
        f"  {log.Y}If boot fails:{log.N} boot from live USB and run:",
        f"     {log.W}{zark_command()} repair-boot{log.N}",
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
