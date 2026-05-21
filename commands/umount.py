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
from lib.cleanup import flush_device_cache, prompt_eject_or_attach
from lib.config import Config
from lib.keystore import Keystore
from lib.log import Log

MNT_BASE = "/mnt/zark"


def run(  # pylint: disable=too-many-branches,too-many-locals,too-many-statements # noqa: E501
    args: list[str],
):
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

    # Clean up mount directories
    mnt_dir = Path(f"{MNT_BASE}/{selected}")
    if mnt_dir.exists():
        _ = sh.run(f"find {mnt_dir} -depth -type d -empty -delete")
        if mnt_dir.exists():
            _ = sh.run(f"rmdir {mnt_dir}")

    if exported_ok:
        # Default to ejecting: the operator asked to unmount, which
        # strongly suggests intent to physically disconnect. The "n"
        # branch covers operators who only wanted to release the mount
        # for a different reason (e.g. about to re-mount under another
        # path).
        prompt_eject_or_attach(device_for_eject, selected, log, default_eject=True)
    else:
        log.blank()
