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
zark fix-rpool-mountpoint — give a restored rpool back the installer's mountpoint=/.

zark recover up to 1.0.12 created rpool with mountpoint=none, where the
Ubuntu installer uses / (with canmount=off). Datasets created later directly
under rpool then inherit none and never mount, and ``prepare`` copies none
to new backup drives, so ``zark mount`` finds nothing to mount.

Changing a mountpoint while a zvol of the pool exists is forbidden
(chase.c:648), and rpool always holds the keystore zvol. So this runs from a
live USB only: with the ``zvol_inhibit_dev`` module parameter set before the
import, no zvol device is created; rpool is imported with ``-N`` under an
altroot and no key is loaded, so nothing is mounted.
"""

import glob
import signal
from pathlib import Path
from types import FrameType
from typing import NoReturn

from lib import sh
from lib.log import Log
from lib.mount_props import UBUNTU_RPOOL_MOUNTPOINT, inherited
from lib.zfs import ZFS

ZVOL_INHIBIT = Path("/sys/module/zfs/parameters/zvol_inhibit_dev")
ALTROOT = "/run/zark/altroot/rpool"
EXIT_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
TEARDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _exit_on_signal(signum: int, _frame: FrameType | None) -> NoReturn:
    """Turn a kill or a closed terminal into SystemExit, so run()'s teardown runs."""
    raise SystemExit(128 + signum)


def _pools_with_zvols() -> list[str]:
    """Imported pools that hold at least one zvol."""
    r = sh.run("zfs list -H -t volume -o name")
    return sorted({line.split("/", 1)[0] for line in (r.lines if r.ok else []) if line})


def _without_altroot(value: str) -> str:
    """Stored mountpoint from the value zfs shows under ALTROOT (/ shows as ALTROOT)."""
    if value == ALTROOT:
        return "/"
    if value.startswith(f"{ALTROOT}/"):
        return value[len(ALTROOT) :]
    return value


def _props(dataset: str) -> dict[str, tuple[str, str]]:
    """mountpoint/canmount of ``dataset`` as {property: (stored value, source)}."""
    r = sh.run(f"zfs get -H -o property,value,source mountpoint,canmount {dataset}")
    props = {
        f[0]: (f[1], f[2])
        for f in (line.split("\t") for line in (r.lines if r.ok else []))
        if len(f) == 3
    }
    if "mountpoint" in props:
        value, source = props["mountpoint"]
        props["mountpoint"] = (_without_altroot(value), source)
    return props


def _inheriting_from_rpool() -> list[tuple[str, str]]:
    """(dataset, canmount) of the filesystems whose mountpoint is inherited from rpool."""
    r = sh.run(
        "zfs get -H -r -t filesystem -o name,property,value,source mountpoint,canmount rpool"
    )
    rows = [f for f in (line.split("\t") for line in (r.lines if r.ok else [])) if len(f) == 4]
    canmount = {f[0]: f[2] for f in rows if f[1] == "canmount"}
    return [
        (f[0], canmount.get(f[0], "?"))
        for f in rows
        if f[1] == "mountpoint" and f[3] == "inherited from rpool"
    ]


def _decide_canmount_on(log: Log, dataset: str, target: str) -> bool | None:
    """Keep (True) or turn off (False) a dataset that will start mounting; None aborts."""
    log.warn(f"{dataset} has canmount=on: from the next boot it mounts at {target},")
    log.warn(f"  over whatever the boot environment keeps in {target}.")
    choice = log.ask_choice(
        f"What should happen to {dataset}?",
        [
            f"Keep canmount=on — it mounts at {target}",
            "Set canmount=off — it never mounts",
            "Abort — change nothing",
        ],
        default=2,
    )
    if choice == 1:
        return False
    if choice == 2:
        return None
    typed = log.ask_text(
        f"    Type {dataset} to confirm it mounts at {target}: ",
        accept=(dataset,),
        label=f"Type {dataset} to keep it mounting at {target}",
    )
    return True if typed == dataset else None


def _review_inheritors(log: Log) -> list[str] | None:
    """List what inherits from rpool; the canmount=on ones to turn off, None to abort."""
    affected = _inheriting_from_rpool()
    targets = {ds: inherited(UBUNTU_RPOOL_MOUNTPOINT, ds[len("rpool/") :]) for ds, _ in affected}
    if affected:
        log.warn("These datasets inherit their mountpoint from rpool and will change:")
        for ds, canmount in affected:
            log.raw(f"    {ds:44} none → {targets[ds]:24} canmount={canmount}")
    else:
        log.info("No other dataset inherits its mountpoint from rpool")

    turn_off: list[str] = []
    for ds, canmount in affected:
        if canmount != "on":
            continue
        keep = _decide_canmount_on(log, ds, targets[ds])
        if keep is None:
            return None
        if not keep:
            turn_off.append(ds)
    return turn_off


def _fix(zfs: ZFS, log: Log) -> bool:
    """Import, check, confirm and set. True when rpool ends with mountpoint=/."""
    if not zfs.pool_import("rpool", altroot=ALTROOT, no_mount=True):
        log.fatal("Cannot import rpool — see messages above")
    zvols = sorted(glob.glob("/dev/zd*"))
    if zvols:
        log.fatal(
            "zvol devices exist although zvol_inhibit_dev=1 — refusing to change a mountpoint",
            causes=[f"{', '.join(zvols)} (chase.c:648 risk)"],
            solutions=["Reboot the live USB and run this command before anything else"],
        )
    log.ok("rpool imported with no zvol devices")

    props = _props("rpool")
    mountpoint, source = props.get("mountpoint", ("", ""))
    if (mountpoint, source) != ("none", "local"):
        log.ok(f"Nothing to fix: rpool mountpoint={mountpoint} ({source})")
        return mountpoint == UBUNTU_RPOOL_MOUNTPOINT
    if props.get("canmount", ("", ""))[0] != "off" or not zfs.dataset_exists("rpool/ROOT"):
        log.fatal(
            "rpool does not have the Ubuntu layout (canmount=off, rpool/ROOT) — not touching it",
        )

    log.info(f"rpool: mountpoint none → {UBUNTU_RPOOL_MOUNTPOINT} (canmount stays off)")
    turn_off = _review_inheritors(log)
    if turn_off is None:
        log.info("Aborted — nothing changed")
        return False

    answer = log.ask_text(
        f"    Type YES to set mountpoint={UBUNTU_RPOOL_MOUNTPOINT} on rpool: ",
        accept=("YES",),
        label=f"Type YES to set rpool mountpoint={UBUNTU_RPOOL_MOUNTPOINT}",
    )
    if answer != "YES":
        log.info("Aborted — nothing changed")
        return False

    # Before the mountpoint: once it is set, libzfs tries to mount canmount=on inheritors.
    for ds in turn_off:
        _ = sh.run(f"zfs set canmount=off {ds}", log=log)
        if _props(ds).get("canmount", ("", ""))[0] != "off":
            log.error(f"{ds} is not canmount=off — rpool's mountpoint left unchanged")
            return False
        log.ok(f"{ds} canmount=off ✓")

    r = sh.run(f"zfs set mountpoint={UBUNTU_RPOOL_MOUNTPOINT} rpool", log=log)
    # Judge by the property, not the exit code: with no key loaded the
    # remount of children can fail after the property itself was stored.
    after = _props("rpool").get("mountpoint", ("", ""))
    if after != (UBUNTU_RPOOL_MOUNTPOINT, "local"):
        log.error(f"rpool mountpoint is {after[0]} ({after[1]}): {r.stderr.strip()}")
        return False
    if not r.ok:
        log.warn(f"zfs set reported: {r.stderr.strip()} — the property was stored anyway")
    log.ok(f"rpool mountpoint={UBUNTU_RPOOL_MOUNTPOINT} ✓")
    return True


def run(args: list[str]) -> None:
    """Main entry point for zark fix-rpool-mountpoint."""
    del args
    log = Log()
    zfs = ZFS(log)
    log.banner("FIX RPOOL MOUNTPOINT", "Restore the Ubuntu installer's mountpoint=/ on rpool")

    if not sh.is_live_usb():
        log.fatal(
            "Run this from a live USB",
            causes=["On the running system rpool's keystore zvol exists (chase.c:648)"],
        )
    if zfs.pool_exists("rpool"):
        log.fatal("rpool is already imported — export it first (sudo zpool export rpool)")
    _ = sh.run("modprobe zfs")
    if not ZVOL_INHIBIT.exists():
        log.fatal(f"{ZVOL_INHIBIT} not found — is the zfs module loaded?")
    zvols = sorted(glob.glob("/dev/zd*"))
    if zvols:
        pools = _pools_with_zvols()
        log.fatal(
            "zvol devices exist before rpool is imported — refusing to start",
            causes=[f"{', '.join(zvols)} of imported pool(s): {', '.join(pools) or 'unknown'}"],
            solutions=[
                f"Export them first: sudo zpool export {' '.join(pools)}"
                if pools
                else "Reboot the live USB and run this command before anything else",
            ],
        )

    previous = ZVOL_INHIBIT.read_text(encoding="utf-8").strip()
    fixed = False
    exported = False
    handlers = {sig: signal.getsignal(sig) for sig in TEARDOWN_SIGNALS}
    for sig in EXIT_SIGNALS:
        _ = signal.signal(sig, _exit_on_signal)
    try:
        ZVOL_INHIBIT.write_text("1", encoding="utf-8")
        log.ok("zvol device creation inhibited (zvol_inhibit_dev=1)")
        fixed = _fix(zfs, log)
    finally:
        # Nothing may cut the teardown short: a second Ctrl-C, a kill or a
        # closed terminal. Ignored signals stay ignored in the zpool child too.
        for sig in TEARDOWN_SIGNALS:
            _ = signal.signal(sig, signal.SIG_IGN)
        try:
            exported = zfs.pool_export("rpool")
        finally:
            ZVOL_INHIBIT.write_text(previous, encoding="utf-8")
            log.info(f"zvol_inhibit_dev restored to {previous}")
            for sig, handler in handlers.items():
                if handler is not None:
                    _ = signal.signal(sig, handler)

    mp_line = (
        f"rpool mountpoint={UBUNTU_RPOOL_MOUNTPOINT} ✓" if fixed else "✗ rpool mountpoint unchanged"
    )
    export_line = (
        "rpool exported ✓"
        if exported
        else "✗ rpool is still imported — run: sudo zpool export rpool"
    )
    lines = [
        mp_line,
        export_line,
        "",
        "Next: remove the live USB and boot.",
    ]
    if not (fixed and exported):
        log.banner_error("RPOOL MOUNTPOINT NOT FIXED", lines)
        raise SystemExit(1)
    log.banner_ok("RPOOL MOUNTPOINT FIXED", lines)
