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

from pathlib import Path

from lib import sh
from lib.config import Config
from lib.keystore import Keystore
from lib.log import Log

MNT_BASE = "/mnt/zark"


def run(args: list[str]):  # pylint: disable=too-many-branches
    """
    Unmount a previously mounted backup pool.
    """
    del args  # Unused
    log = Log()
    cfg = Config.load()

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

    # Unmount all ZFS datasets for this pool
    log.info("Unmounting datasets...")
    _ = sh.run("zfs unmount -a")
    _ = sh.run(f"umount -R {MNT_BASE}/{selected}")

    # Close keystore
    log.info("Closing keystore...")
    ks = Keystore(log)
    ks.attach_to_pool(selected)
    ks.umount()

    # Export pool
    log.info(f"Exporting pool {selected}...")
    r = sh.run(f"zpool export {selected}")
    if r.ok:
        log.ok(f"Pool {selected} exported ✓")
        _ = sh.run("sync")
        log.ok("Safe to disconnect")
    else:
        r2 = sh.run(f"zpool export -f {selected}")
        if r2.ok:
            log.ok(f"Pool {selected} exported (forced) ✓")
        else:
            log.warn(f"Cannot export — run: zpool export {selected}")

    # Clean up mount directories
    mnt_dir = Path(f"{MNT_BASE}/{selected}")
    if mnt_dir.exists():
        _ = sh.run(f"find {mnt_dir} -depth -type d -empty -delete")
        if mnt_dir.exists():
            _ = sh.run(f"rmdir {mnt_dir}")

    log.blank()
