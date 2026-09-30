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
zark umount — Unmount a previously mounted backup pool.

Safely unmounts all datasets, closes keystore, exports pool.
"""

import os
from pathlib import Path

from lib import sh
from lib.cleanup import flush_device_cache, prompt_eject_or_attach
from lib.config import Config
from lib.keystore import Keystore
from lib.log import Log

MNT_BASE = "/mnt/zark"
SYSTEM_TARGETS = ("local", "system", "rpool")


def _unmount_tree(root: str) -> None:
    """``umount -R root`` without reaching the live system through shared binds.

    Under systemd mounts are shared: a bind of a host directory left under
    ``root`` (a chroot's /run, /dev…) propagates the unmount of anything
    mounted there on the host since. Private first, then unmount.
    """
    _ = sh.run(f"mount --make-rprivate {root}")
    _ = sh.run(f"umount -R {root}")


def _umount_local_system(log: Log) -> None:
    """Export the installed system's rpool/bpool mounted by 'zark mount local'.

    Safety discriminator: zark always imports the system under an altroot
    beneath ``/mnt/zark`` (see mount_system_pools). The *running* system's
    rpool has no altroot (``altroot = -``). We refuse to export anything
    whose altroot is not under ``/mnt/zark`` — that is the guard against
    pulling the live root out from under a running machine.
    """
    if not sh.run("zpool list rpool").ok:
        log.warn("rpool is not imported — nothing to unmount.")
        return

    stored = sh.run("zpool get -H -o value altroot rpool").output.strip()
    # Resolved: the value goes to umount -R, and "/mnt/zark/../.." is a prefix match.
    altroot = os.path.realpath(stored) if stored.startswith("/") else ""
    if not altroot.startswith(f"{MNT_BASE}/"):
        log.fatal(
            "rpool was not imported by zark under an altroot "
            f"(altroot={stored or '-'}).\n"
            "  Refusing to export — this looks like the running system, not a\n"
            "  live-USB inspection mount.",
        )

    # Only the system's tree: `zfs unmount -a` would also unmount a backup
    # mounted with `zark mount` in the same session. zpool export unmounts the rest.
    log.info("Unmounting system datasets...")
    _unmount_tree(altroot)

    # Close the rpool keystore (LUKS over the keystore zvol) BEFORE exporting.
    # Unmounting and `unload-key` do not close the cryptsetup mapping, so
    # without this the zvol keeps a device-mapper holder and `zpool export
    # rpool` hangs in taskq_wait waiting for the zvol to be released. This is
    # the same teardown the chroot path performs on exit.
    if sh.run("zpool list rpool").ok:
        log.info("Closing keystore...")
        ks = Keystore(log)
        ks.attach_to_pool("rpool")
        ks.umount()

    for pool in ("bpool", "rpool"):  # bpool first (it sits under /boot)
        if not sh.run(f"zpool list {pool}").ok:
            continue
        sh.run(f"zfs unload-key -r {pool}")
        if sh.run(f"zpool export {pool}").ok:
            log.ok(f"Pool {pool} exported ✓")
        elif sh.run(f"zpool export -f {pool}").ok:
            log.ok(f"Pool {pool} exported (forced) ✓")
        else:
            log.warn(f"Could not export {pool} — run: zpool export {pool}")
    flush_device_cache(log)
    log.banner_ok("SYSTEM UNMOUNTED", ["rpool/bpool exported cleanly ✓"])


def run(  # pylint: disable=too-many-branches,too-many-locals,too-many-statements # noqa: E501
    args: list[str],
):
    """
    Unmount a previously mounted backup pool.
    """
    log = Log()
    cfg = Config.load()
    cfg.check_registry(log, fatal=False)

    # Local/system target: export the installed system's pools (the
    # counterpart to 'zark mount local'). Explicit keyword so the default
    # backup-drive behaviour is unchanged.
    target = next((a for a in args if not a.startswith("-")), None)
    if target in SYSTEM_TARGETS:
        log.banner("UNMOUNT SYSTEM")
        _umount_local_system(log)
        return

    log.banner("UNMOUNT BACKUP POOL")

    # Find imported known pools
    mounted: list[str] = []
    for pool_name in list(cfg.known_drives.keys()):
        r = sh.run(f"zpool list {pool_name}")
        if r.ok:
            mounted.append(pool_name)

    # Also check unknown pools under MNT_BASE
    mnt_base = Path(MNT_BASE)
    if mnt_base.exists():
        for d in mnt_base.iterdir():
            if d.is_dir() and d.name not in mounted:
                r = sh.run(f"zpool list {d.name}")
                if r.ok:
                    mounted.append(d.name)

    if not mounted:
        log.warn("No backup pools currently imported")
        return

    # Select pool
    if len(mounted) == 1:
        selected = mounted[0]
        mnt = f"{MNT_BASE}/{selected}"
        log.info(f"Pool mounted: {log.W}{selected}{log.N} at {mnt}")
        if not log.ask(f"Unmount {selected}?", default=True):
            log.info("Aborted")
            return
    else:
        idx = log.ask_choice(
            "Select pool to unmount:",
            [f"{p}  (at {MNT_BASE}/{p})" for p in mounted],
        )
        selected = mounted[idx]

    # Only this pool's tree: `zfs unmount -a` would also try every dataset
    # of the running system. zpool export unmounts anything left.
    log.info("Unmounting datasets...")
    _unmount_tree(f"{MNT_BASE}/{selected}")

    # Close keystore
    log.info("Closing keystore...")
    ks = Keystore(log)
    ks.attach_to_pool(selected)
    ks.umount()

    # Resolve the underlying device path BEFORE exporting — once the
    # pool is gone we can no longer ask zpool which device backed it.
    # known_drives.json drives a stable by-id path; pools imported via
    # `zark mount` without registration fall back to parsing
    # `zpool status` for the first /dev/ entry. Either way this is
    # best-effort: if no device is resolved, flush_and_eject still
    # runs sync but skips eject (no power-down) — durable but the
    # operator should pause a couple of seconds before unplugging.
    device_for_eject: str | None = None
    info = cfg.known_drives.get(selected)
    if info is not None and info.drive_id and info.drive_id != "<unknown>":
        by_id = Path(f"/dev/disk/by-id/{info.drive_id}")
        if by_id.exists():
            device_for_eject = str(by_id)
    if device_for_eject is None:
        status = sh.run(f"zpool status -P {selected}")
        for line in status.lines:
            for token in line.split():
                if token.startswith("/dev/"):
                    device_for_eject = token
                    break
            if device_for_eject:
                break

    # Export pool
    log.info(f"Exporting pool {selected}...")
    exported_ok = False
    r = sh.run(f"zpool export {selected}")
    if r.ok:
        log.ok(f"Pool {selected} exported ✓")
        exported_ok = True
    else:
        r2 = sh.run(f"zpool export -f {selected}")
        if r2.ok:
            log.ok(f"Pool {selected} exported (forced) ✓")
            exported_ok = True
        else:
            log.warn(f"Cannot export — run: zpool export {selected}")

    if exported_ok:
        flush_device_cache(log)

    # Clean up mount directories — only once the pool is gone: with it still
    # imported, find would delete empty directories inside the backup itself.
    mnt_dir = Path(f"{MNT_BASE}/{selected}")
    if not exported_ok:
        log.warn(f"{mnt_dir} left as is (the pool may still be mounted there)")
    elif mnt_dir.exists():
        _ = sh.run(f"find {mnt_dir} -xdev -depth -type d -empty -delete")
        if mnt_dir.exists():
            _ = sh.run(f"rmdir {mnt_dir}")

    if exported_ok:
        # Default to ejecting: the operator asked to unmount, which
        # strongly suggests intent to physically disconnect. The "n"
        # branch covers operators who only wanted to release the mount
        # for a different reason (e.g. about to re-mount under another
        # path).
        prompt_eject_or_attach(
            device_for_eject,
            selected,
            log,
            default_eject=True,
            autoeject=cfg.drive_autoeject(selected),
        )
    else:
        log.blank()
