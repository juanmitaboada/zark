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
zark mount — Mount a backup pool for inspection.

Imports the pool once under the altroot /mnt/zark/<pool> (``-N -R``, plus
``readonly=on`` in read-only mode), unlocks its keystore and loads the keys.

Read-only mode rebuilds origin's tree under /mnt/zark/<pool> the way recover
restores it: the boot environment's root first, then every other dataset at
origin's mountpoint, taken from ``org.zark:*`` → origin's zfs-list.cache in
the boot environment → the Ubuntu layout → structure (lib/mount_props). It
never writes to the backup: the pool is imported read-only and a dataset
whose mountpoint directory does not exist in its parent is skipped.
Read-write mode mounts each dataset with ``zfs mount`` at its stored
mountpoint under the altroot.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from lib import engine, sh
from lib.backup_layout import find_be, list_datasets, root_is_empty, zark_props
from lib.cleanup import Cleanup
from lib.config import Config
from lib.drives import backup_device, scan_connected_drives, select_drive
from lib.keystore import Keystore, open_keystore
from lib.log import Log
from lib.mount import mount_system_pools
from lib.mount_props import (
    BPOOL_CONTAINERS,
    CREATED_CONTAINERS,
    choose,
    effective_mountpoint,
    parse_list_cache,
    rpool_root_mountpoint,
)
from lib.zfs import ZFS

MNT_BASE = "/mnt/zark"
PROBE_DIR = "/run/zark/probe"
SYSTEM_MNT = "/mnt/zark/system"
SYSTEM_TARGETS = ("local", "system", "rpool")


@dataclass(frozen=True)
class LayoutMount:
    """One dataset of the read-only tree: where it goes, or why it does not."""

    rel: str  # relative to the backup pool (rpool/var/lib/docker)
    target: str  # absolute directory; "" when skipped
    reason: str = ""  # why it is not mounted


def _plan_origin_layout(  # pylint: disable=too-many-locals
    pool: str,
    be: str,
    mnt: str,
    cache: dict[str, tuple[str, str]],
) -> list[LayoutMount]:
    """Where every filesystem of ``pool`` goes under ``mnt``, as on origin.

    The boot environment's root (already mounted at ``mnt``) is not listed.
    Mount order is the order of the returned targets sorted by path.
    """
    types = list_datasets(pool)
    zark = zark_props(pool)
    children = {ds for ds in types for other in types if other.startswith(f"{ds}/")}
    be_root = f"rpool/ROOT/{be}"
    effective: dict[str, str] = {
        "rpool": rpool_root_mountpoint(cache)[0],
        **CREATED_CONTAINERS,
        **BPOOL_CONTAINERS,
        be_root: "/",
    }
    plan: list[LayoutMount] = []
    for rel in sorted(types):
        if ".archived-" in rel:
            plan.append(LayoutMount(rel, "", "archived lineage (listed apart)"))
            continue
        if types[rel] != "filesystem" or rel in effective:
            continue
        parent, leaf = rel.rsplit("/", 1)
        other_be = rel.startswith(("rpool/ROOT/", "bpool/BOOT/")) and not (
            rel == f"bpool/BOOT/{be}" or rel.startswith(f"{be_root}/")
        )
        if other_be:
            plan.append(LayoutMount(rel, "", "another boot environment"))
            continue
        if parent not in effective:
            plan.append(LayoutMount(rel, "", f"parent {parent} is not part of the tree"))
            continue
        needs_probe = rel not in zark and rel not in cache and rel in children
        empty = bool(
            needs_probe and root_is_empty(f"{pool}/{rel}", PROBE_DIR, zfsutil=True),
        )
        props = choose(rel, be, zark=zark, cache=cache, empty_with_children=empty)
        eff = effective_mountpoint(props, effective[parent], leaf)
        effective[rel] = eff
        if props.canmount != "on":
            plan.append(LayoutMount(rel, "", f"canmount={props.canmount} [{props.source}]"))
        elif not eff.startswith("/"):
            plan.append(LayoutMount(rel, "", f"mountpoint={eff or 'unknown'} [{props.source}]"))
        else:
            plan.append(LayoutMount(rel, f"{mnt.rstrip('/')}{eff}"))
    return plan


def _mount_origin_layout(  # pylint: disable=too-many-locals
    pool: str,
    mnt: str,
    log: Log,
    readonly: bool = True,
) -> tuple[int, list[LayoutMount]]:
    """Mount the backup with origin's tree; (mounted count, skipped).

    Explicit ``mount -t zfs -o [ro,]zfsutil`` of each dataset: the backup's
    own mount properties are ``canmount=noauto`` and an inherited
    ``mountpoint=none`` (M2 decision 1), and mount.zfs with ``zfsutil``
    checks neither (cmd/mount_zfs.c:300-326).
    """
    opts = "ro,zfsutil" if readonly else "zfsutil"
    be = find_be(pool)
    if not be:
        log.fatal(f"No boot environment under {pool}/rpool/ROOT")
    _ = sh.run(f"mkdir -p {mnt}")
    if not sh.run(f"mount -t zfs -o {opts} {pool}/rpool/ROOT/{be} {mnt}", log=log).ok:
        log.fatal(f"Cannot mount the boot environment {pool}/rpool/ROOT/{be}")
    cache_dir = Path(mnt) / "etc/zfs/zfs-list.cache"
    cache: dict[str, tuple[str, str]] = {}
    for name in ("rpool", "bpool"):
        f = cache_dir / name
        if f.is_file():
            cache.update(parse_list_cache(f.read_text(encoding="utf-8")))
    if not cache:
        log.warn("No zfs-list.cache in the backup — mountpoints come from the Ubuntu layout")

    mounted = 1
    skipped: list[LayoutMount] = []
    plan = _plan_origin_layout(pool, be, mnt, cache)
    root = os.path.realpath(mnt)
    # Parents before children: compare path components, not strings.
    for item in sorted((m for m in plan if m.target), key=lambda m: Path(m.target).parts):
        # Resolved as mount(8) will: a symlink or .. in the backup must not lead
        # out of the tree onto the running system. The resolved path is mounted.
        target = os.path.realpath(item.target)
        if not target.startswith(f"{root}/"):
            skipped.append(LayoutMount(item.rel, "", f"{item.target} resolves to {target}"))
            continue
        if not Path(target).is_dir():
            # Creating it would write to the backup (impossible read-only).
            skipped.append(LayoutMount(item.rel, "", f"no directory {item.target}"))
            continue
        r = sh.run(f"mount -t zfs -o {opts} {pool}/{item.rel} {target}")
        if r.ok:
            mounted += 1
        else:
            skipped.append(LayoutMount(item.rel, "", r.stderr.strip() or "mount failed"))
    skipped += [m for m in plan if not m.target]
    return mounted, skipped


def _mount_local_system(log: Log, zfs: ZFS, cleanup: Cleanup) -> None:
    """Mount the installed system's rpool/bpool for inspection (live USB).

    The local-disk counterpart to the backup-pool flow below: from a live
    session it imports the top-level rpool/bpool under an altroot and mounts
    the boot environment, leaving it mounted for the operator. Read-only by
    default. Use 'zark umount local' (or 'zark chroot' for a working shell).
    """
    if zfs.pool_exists("rpool"):
        log.fatal(
            "rpool is already imported — this looks like the running system.\n"
            "  Boot a live USB to inspect the installed system's pools.",
        )
    if not sh.is_live_usb():
        log.warn("This does not look like a live USB session.")
        if not log.ask("Continue anyway?", default=False):
            log.fatal("Aborted — boot a live USB and retry.")

    mode_idx = log.ask_choice(
        "Mount mode:",
        [
            f"Read-only  {log.G}(recommended — safe){log.N}",
            f"Read-write {log.R}(can modify the system){log.N}",
        ],
    )
    readonly = mode_idx == 0
    result = mount_system_pools(
        SYSTEM_MNT,
        None,  # prompt inside, re-asking on a typo (I9)
        log,
        zfs,
        Keystore(log),
        cleanup,
        readonly=readonly,
    )
    if result is None:
        log.fatal("Could not mount the system — see messages above.")
    root_path, ubuntu_name = result

    log.banner_ok(
        f"SYSTEM MOUNTED ({ubuntu_name})",
        [
            f"Mount point: {log.W}{root_path}{log.N}",
            f"Mode:        {log.W}{'read-only' if readonly else 'read-write'}{log.N}",
            "",
            f"{log.Y}Browse:{log.N}  ls {root_path}/",
            f"{log.Y}Chroot:{log.N}  sudo ./zark chroot",
            "",
            f"{log.Y}To unmount:{log.N}  sudo ./zark umount local",
        ],
    )
    # Leave it mounted for the operator.
    cleanup.disable()


def run(
    args: list[str],
):  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
    """Mount a backup pool for inspection / chroot / recovery."""
    log = Log()
    cfg = Config.load()
    cfg.check_registry(log, fatal=False)
    zfs = ZFS(log)
    cleanup = Cleanup(log)
    cleanup.register()

    # Local/system target: mount the installed system's pools (live USB),
    # not a removable backup drive. Kept as an explicit keyword so the
    # default (no-arg) behaviour — scan backup drives — is unchanged.
    target = next((a for a in args if not a.startswith("-")), None)
    if target in SYSTEM_TARGETS:
        log.banner("MOUNT SYSTEM", "Mount the installed ZFS system (live USB)")
        _mount_local_system(log, zfs, cleanup)
        return

    log.banner("MOUNT BACKUP POOL", "Mount for inspection / chroot / recovery")

    # ── Find drives ──────────────────────────────────────────────────────
    log.info("Scanning for backup drives...")
    drives = scan_connected_drives(cfg, log)

    if not drives:
        log.warn("No backup drives connected")
        return

    drive = select_drive(drives, log, known_only=False)
    if not drive:
        return

    pool_name = drive.name
    mnt_point = f"{MNT_BASE}/{pool_name}"

    if zfs.pool_exists(pool_name):
        log.warn(f"Pool {pool_name} is already imported")
        log.info("To unmount: sudo ./zark umount")
        cleanup.disable()
        return

    # ── Mount mode ───────────────────────────────────────────────────────
    mode_idx = log.ask_choice(
        "Mount mode:",
        [
            f"Read-only  {log.G}(recommended — safe){log.N}",
            f"Read-write {log.R}(can modify backup data){log.N}",
        ],
    )
    readonly = mode_idx == 0

    # ── Import once, under the user-visible altroot (I-G) ────────────────
    # Read-only mode imports the pool readonly=on: nothing on the backup is
    # written, not even the keystore's ext4 journal replay.
    log.info(f"Importing pool {pool_name} (altroot={mnt_point})...")
    if not zfs.import_backup_pool(
        pool_name,
        backup_device(drive),
        altroot=mnt_point,
        readonly=readonly,
        guid=drive.guid,
    ):
        log.fatal(f"Cannot import pool {pool_name}")
    cleanup.track_pool(pool_name)
    _ = engine.log_metadata(pool_name, log)

    ks = Keystore(log)
    if not open_keystore(ks, pool_name, log, readonly=readonly):
        log.fatal("Cannot open keystore", causes=["Wrong passphrase (3 attempts)"])
    loaded = ks.load_pool_keys(f"{pool_name}/rpool")
    ks.umount()
    log.ok(f"Loaded {loaded} encryption key(s)")

    # ── Mount datasets ───────────────────────────────────────────────────
    log.info("Mounting datasets...")
    mounted, skipped = _mount_origin_layout(pool_name, mnt_point, log, readonly=readonly)
    archived = [item.rel for item in skipped if item.reason.startswith("archived lineage")]
    for item in skipped:
        log.dbg(f"Not mounted: {item.rel} — {item.reason}")

    log.ok(f"Mounted {mounted} datasets")

    if mounted == 0:
        log.fatal(
            "No datasets mounted",
            solutions=[f"Check: zfs get mountpoint,canmount -r {pool_name}/rpool"],
        )

    root_path = mnt_point  # _mount_origin_layout mounted the boot environment there

    # ── Show results ─────────────────────────────────────────────────────
    result_lines = [
        f"Mount point: {log.W}{mnt_point}{log.N}",
        f"Mode:        {log.W}{'read-only' if readonly else 'read-write'}{log.N}",
        f"Datasets:    {log.W}{mounted}{log.N}",
        "",
        f"{log.Y}Browse data:{log.N}",
        f"  ls {mnt_point}/",
        "",
        f"{log.Y}Access old snapshots:{log.N}",
        (f"  ls {root_path}/.zfs/snapshot/" if root_path else f"  ls {mnt_point}/.zfs/snapshot/"),
        "",
    ]

    if root_path:
        result_lines += [
            f"{log.Y}Chroot into the backup system:{log.N}",
            f"  sudo mount --bind /proc {root_path}/proc",
            f"  sudo mount --bind /sys  {root_path}/sys",
            f"  sudo mount --bind /dev  {root_path}/dev",
            f"  sudo chroot {root_path}",
            "",
        ]

    if archived:
        result_lines += [f"{log.Y}Archived lineages (not mounted; browse one with):{log.N}"]
        result_lines += [f"  {pool_name}/{rel}" for rel in archived]
        result_lines += [
            f"  sudo mount -t zfs -o ro,zfsutil {pool_name}/<lineage> <directory>",
            "",
        ]

    result_lines += [
        f"{log.Y}To unmount:{log.N}",
        "  sudo ./zark umount",
    ]

    log.banner_ok(f"POOL {pool_name} MOUNTED", result_lines)

    # Leave mounted for the user
    cleanup.disable()
