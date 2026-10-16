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
zark clean: undo what a zark run that did not end left behind.

Works from an inventory of what zark itself owns, shows it, asks, and acts
only on that:

  * mounts below zark's mount roots (``/mnt/zark``, ``/mnt/recover``,
    ``/mnt/repair``, ``/run/zark/altroot``, ``/run/zark/probe``);
  * pools imported with an altroot at or below one of those roots. A pool
    without an altroot is never touched: on an installed system it is the
    running rpool/bpool, on a live session a pool imported by hand;
  * keystore mappings ``/dev/mapper/zark_ks_*``; ``/run/keystore/rpool`` is
    unmounted only while the mount on top of it comes from one of them;
  * ``sanoid.timer`` left stopped by an interrupted backup (its marker in
    ``/run/zark``) and ``zvol_inhibit_dev=1`` left by fix-rpool-mountpoint.

The same rules apply on an installed system and on a live session: the
altroot tells zark's imports apart, not the environment. Pools are exported
without ``-f``. Only directories left empty are removed, after the mounts
below them are gone; anything else is listed, never deleted.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from commands.umount import Mount, _unmount_tree, release_elsewhere
from lib import engine, sh
from lib.keystore import KEYSTORE_MOUNT
from lib.log import Log
from lib.zfs import BACKUP_ALTROOT_BASE

ROOTS: tuple[str, ...] = (
    "/mnt/zark",
    "/mnt/recover",
    "/mnt/repair",
    BACKUP_ALTROOT_BASE,
    "/run/zark/probe",
)
MAPPER_DIR = "/dev/mapper"
KEYSTORE_PREFIX = "zark_ks_"
ZVOL_INHIBIT = "/sys/module/zfs/parameters/zvol_inhibit_dev"


@dataclass
class Inventory:
    """What zark owns right now, and what it found and leaves alone."""

    mounts: list[str] = field(default_factory=list)  # under ROOTS, parents first
    pools: list[str] = field(default_factory=list)  # imported under ROOTS
    foreign: list[str] = field(default_factory=list)  # imported, not zark's (shown only)
    keystores: list[str] = field(default_factory=list)  # zark_ks_* mapper names
    keystore_mounted: bool = False  # top mount at KEYSTORE_MOUNT is a zark mapper
    sanoid_left: bool = False  # marker present and sanoid.timer not active
    zvol_inhibited: bool = False

    @property
    def empty(self) -> bool:
        """True when there is nothing for clean to act on."""
        return not (
            self.mounts
            or self.pools
            or self.keystores
            or self.keystore_mounted
            or self.sanoid_left
            or self.zvol_inhibited
        )


def _under_roots(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(f"{r}/") for r in ROOTS)


def _mount_table() -> list[tuple[str, str]]:
    """(target, source) of every mount, in mountinfo order (stacked: last on top)."""
    r = sh.run("findmnt -rn -o TARGET,SOURCE")
    out: list[tuple[str, str]] = []
    for line in r.lines if r.ok else []:
        target, _, source = line.partition(" ")
        if target:
            out.append((target, source.strip()))
    return out


def inventory() -> Inventory:
    """Read what zark owns; read-only."""
    inv = Inventory()
    table = _mount_table()
    under = [t for t, _ in table if any(t == r or t.startswith(f"{r}/") for r in ROOTS)]
    inv.mounts = sorted(dict.fromkeys(under), key=lambda t: Path(t).parts)

    r = sh.run("zpool list -H -o name,altroot")
    for line in r.lines if r.ok else []:
        name, _, altroot = line.partition("\t")
        name, altroot = name.strip(), altroot.strip()
        if not name:
            continue
        if altroot.startswith("/") and _under_roots(altroot):
            inv.pools.append(name)
        elif name not in ("rpool", "bpool") or altroot.startswith("/"):
            inv.foreign.append(f"{name} (altroot {altroot or '-'})")
    # bpool sits under rpool's tree (/boot): export it first.
    inv.pools.sort(key=lambda p: (p != "bpool", p))

    mapper = Path(MAPPER_DIR)
    if mapper.is_dir():
        inv.keystores = sorted(
            p.name for p in mapper.iterdir() if p.name.startswith(KEYSTORE_PREFIX)
        )
    tops = [s for t, s in table if t == KEYSTORE_MOUNT]
    inv.keystore_mounted = bool(tops) and Path(tops[-1]).name.startswith(KEYSTORE_PREFIX)

    timer = sh.run("systemctl is-active sanoid.timer").output.strip()
    inv.sanoid_left = Path(engine.SANOID_MARKER).exists() and timer != "active"
    try:
        inv.zvol_inhibited = Path(ZVOL_INHIBIT).read_text(encoding="ascii").strip() == "1"
    except OSError:
        inv.zvol_inhibited = False
    return inv


def _show(inv: Inventory, log: Log) -> None:
    for t in inv.mounts:
        log.info(f"  mount:    {t}")
    for p in inv.pools:
        log.info(f"  pool:     {p} (export, without -f)")
    for k in inv.keystores:
        log.info(f"  keystore: /dev/mapper/{k} (close)")
    if inv.keystore_mounted:
        log.info(f"  keystore: {KEYSTORE_MOUNT} (unmount zark's mount on top)")
    if inv.sanoid_left:
        log.info("  sanoid.timer left stopped by an interrupted backup (start it)")
    if inv.zvol_inhibited:
        log.info("  zvol_inhibit_dev=1 left by fix-rpool-mountpoint (restore 0)")
    for f in inv.foreign:
        log.info(f"  left alone, not imported by zark: {f}")


def _tops(mounts: list[str]) -> list[str]:
    """Mounts with no other listed mount above them."""
    return [m for m in mounts if not any(m.startswith(f"{o}/") for o in mounts if o != m)]


def _remove_empty_dirs(log: Log) -> list[str]:
    """rmdir every empty directory under ROOTS, deepest first, on one filesystem.

    Called only when no mount is left under ROOTS and no zark pool is still
    imported. Returns what stays (files, or directories that could not be
    removed), never deleting a file.
    """
    left: list[str] = []
    for root in ROOTS:
        base = Path(root)
        if not base.is_dir() or base.is_symlink():
            continue
        dev = base.stat().st_dev
        for dirpath, _, filenames in os.walk(root, topdown=False):
            here = Path(dirpath)
            if here.stat().st_dev != dev:  # another filesystem: never walk into it
                left.append(dirpath)
                continue
            left += [str(here / f) for f in filenames]
            try:
                here.rmdir()
                log.dbg(f"Removed empty directory {dirpath}")
            except OSError:
                if not filenames:
                    left.append(dirpath)
    return [p for p in left if not any(p.startswith(f"{q}/") for q in left if q != p)]


def _export(pool: str, log: Log) -> bool:
    """``zpool export`` without ``-f``."""
    if sh.run(f"zpool export {pool}", log=log).ok:
        log.ok(f"Exported {pool}")
        return True
    return False


def _export_pools(pools: list[str], trees: dict[str, frozenset[Mount]], log: Log) -> None:
    """Export each pool; when one is busy, release the copies other mount
    namespaces keep below the unmounted trees (W-1; ``trees`` maps each top
    to the mounts it had, by which the copies are told) and try again."""
    busy = [pool for pool in pools if not _export(pool, log)]
    if busy:
        for top, had in trees.items():
            _ = release_elsewhere(top, log, pools, had)
        for pool in busy:
            _ = _export(pool, log)


def run(args: list[str]) -> None:
    """Show what zark left behind, ask, and undo only that."""
    del args  # Unused
    log = Log()
    log.banner("CLEANUP", "Undo what an interrupted zark run left")

    inv = inventory()
    if inv.empty:
        _show(inv, log)
        for p in _remove_empty_dirs(log):
            log.info(f"  kept (not empty): {p}")
        log.ok("Nothing of zark's is mounted, imported or open")
        return
    _show(inv, log)
    if not log.ask("Unmount, close and export the items above?"):
        log.info("Aborted: nothing changed")
        return

    trees: dict[str, frozenset[Mount]] = {}
    for top in _tops(inv.mounts):
        log.info(f"Unmounting {top}...")
        _, trees[top] = _unmount_tree(top, inv.pools)
    if inv.keystore_mounted:
        _ = sh.run(f"umount {KEYSTORE_MOUNT}", log=log)
    for name in inv.keystores:
        _ = sh.run(f"cryptsetup close {name}", log=log)
    _export_pools(inv.pools, trees, log)
    if inv.zvol_inhibited and not inventory().pools:
        try:
            _ = Path(ZVOL_INHIBIT).write_text("0\n", encoding="ascii")
            log.ok("zvol_inhibit_dev restored to 0")
        except OSError as e:
            log.warn(f"Could not restore zvol_inhibit_dev: {e}")
    if inv.sanoid_left and sh.run("systemctl start sanoid.timer", log=log).ok:
        Path(engine.SANOID_MARKER).unlink(missing_ok=True)
        log.ok("sanoid.timer started")

    after = inventory()
    # rmdir of an empty directory detaches a copy mounted there in another
    # mount namespace (the copies that keep a pool's export busy): no
    # removal while a mount or a zark pool is left.
    leftovers = _remove_empty_dirs(log) if not after.mounts and not after.pools else []
    remaining = [
        *(f"✗ still mounted: {t}" for t in after.mounts),
        *(f"✗ still imported: {p}" for p in after.pools),
        *(f"✗ still open: /dev/mapper/{k}" for k in after.keystores),
        *([f"✗ still mounted: {KEYSTORE_MOUNT} (zark keystore)"] if after.keystore_mounted else []),
        *(["✗ sanoid.timer still stopped"] if after.sanoid_left else []),
        *(["✗ zvol_inhibit_dev still 1"] if after.zvol_inhibited else []),
    ]
    kept = [f"  kept (not empty): {p}" for p in leftovers]
    if remaining:
        log.banner_error(
            "CLEANUP INCOMPLETE",
            [
                *remaining,
                *kept,
                "",
                *(
                    ["No directory was removed while a zark pool is still imported."]
                    if after.pools
                    else []
                ),
                "Nothing below a mount that is still there was removed.",
                "A process may be using it (fuser -vm <path>), or a copy may",
                "be held in a mount namespace zark could not enter: reboot.",
            ],
        )
        raise SystemExit(1)
    log.banner_ok("CLEANUP COMPLETE", ["Everything zark had left is released ✓", *kept])
