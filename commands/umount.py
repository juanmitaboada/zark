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
import re
import shlex
from collections.abc import Collection
from pathlib import Path

from lib import sh
from lib.cleanup import flush_device_cache, prompt_eject_or_attach
from lib.config import Config
from lib.keystore import Keystore
from lib.log import Log

MNT_BASE = "/mnt/zark"
SYSTEM_TARGETS = ("local", "system", "rpool")
# Why an export can still fail once zark released what it could, here and in
# the other mount namespaces it could enter (W-1, X-5).
BUSY_HINT = [
    "A dataset may still be in use (fuser -vm <path>), or held",
    "in a mount namespace zark could not enter. Reboot to",
    "release it (the live session, or the installed system).",
]
# The live's reboot leaves rpool/bpool marked as its own (seen on eli).
SYSTEM_REBOOT_HINT = [
    "The installed system may then stop at an emergency",
    "shell; there run: zpool import -N -f rpool; exit",
]
SNAPD_NS_DIR = "/run/snapd/ns"


def _pool_of(source: str) -> str:
    """Pool name of a ZFS mount's source (``pool/ds`` or ``pool/ds@snap``)."""
    return re.split(r"[/@]", source, maxsplit=1)[0]


def _below(targets: list[str], root: str) -> list[str]:
    return [t for t in targets if t == root or t.startswith(f"{root}/")]


def _tops(targets: list[str]) -> list[str]:
    return [t for t in targets if not any(t.startswith(f"{o}/") for o in targets if o != t)]


# A mount as findmnt prints it: (FSTYPE, SOURCE), the source with the bind's
# root in brackets when it is a subdirectory (util-linux 2.41.3
# misc-utils/findmnt.c:642-659).
Mount = tuple[str, str]


def _table(cmd: str) -> list[tuple[str, Mount]] | None:
    """``findmnt -rn -o TARGET,FSTYPE,SOURCE`` (prefixed by ``cmd``) as
    ``(target, (fstype, source))`` rows; None when it cannot be read."""
    table = sh.run(f"{cmd} findmnt -rn -o TARGET,FSTYPE,SOURCE".strip())
    if not table.ok:
        return None
    rows: list[tuple[str, Mount]] = []
    for line in table.lines:
        rest, _, source = line.rpartition(" ")
        target, _, fstype = rest.rpartition(" ")
        rows.append((target, (fstype, source)))
    return rows


def _is_copy(mount: Mount, pools: Collection[str], had: Collection[Mount]) -> bool:
    """Whether a mount in another namespace is a copy of zark's tree: a ZFS
    mount from one of the pools being released, or the same filesystem and
    source as a mount the tree had here (a chroot's binds) (M4bR-3,
    M4bF-6 6a). The path alone names nothing: in a namespace with a root of
    its own, the same path can hold an unrelated mount."""
    fstype, source = mount
    return (fstype == "zfs" and _pool_of(source) in pools) or mount in had


def _unmount_tree(root: str, pools: Collection[str]) -> tuple[list[str], frozenset[Mount]]:
    """``umount -R root``: the ZFS unmounts propagate, the other mounts do not.

    Under systemd mounts are shared. Every dataset mounted here has copies in
    other mount namespaces (systemd services, snaps, the namespaces snapd pins
    in /run/snapd/ns) that only a propagated unmount releases; made private,
    they stay and keep the pool busy (eli, K6). A bind of a host directory
    under ``root`` (a chroot's /run, /dev…) would instead carry the unmount to
    whatever the host mounted there since, so each non-dataset mount is made
    private and unmounted first. A dataset is a ZFS mount whose source is in
    one of ``pools``, the pools being released; a bound host directory that is
    itself ZFS is a bind like any other (W-2).

    Returns what is still mounted at or below ``root`` here afterwards (a
    process using it), empty when that cannot be read (R2§11.4a), and the
    mounts the tree had, by which :func:`release_elsewhere` tells their
    copies in other namespaces.
    """
    below: list[tuple[str, bool]] = []
    had: set[Mount] = set()
    for target, mount in _table("") or []:
        if target == root or target.startswith(f"{root}/"):
            below.append((target, mount[0] == "zfs" and _pool_of(mount[1]) in pools))
            had.add(mount)
    others = [t for t, dataset in below if not dataset and t != root]
    for top in _tops(others):
        _ = sh.run(f"mount --make-rprivate {top}")
        _ = sh.run(f"umount -R {top}")
    _ = sh.run(f"umount -R {root}")
    after = sh.run("findmnt -rn -o TARGET")
    return (_below(after.lines, root) if after.ok else []), frozenset(had)


def _namespaces(proc: str = "/proc", snapd_ns: str = SNAPD_NS_DIR) -> list[str]:
    """One path per mount namespace other than this one: a process's
    ``<proc>/<pid>/ns/mnt``, or a namespace snapd pins in ``snapd_ns`` with
    no process in it (its id is the file's inode)."""
    seen = {os.readlink("/proc/self/ns/mnt")}
    out: list[str] = []
    # In pid order, so the process named for a namespace is its oldest (a
    # service's main process rather than one of its workers).
    pids = [d for d in Path(proc).iterdir() if d.name.isdigit()]
    for d in sorted(pids, key=lambda p: int(p.name)):
        try:
            ns = os.readlink(d / "ns" / "mnt")
        except OSError:
            continue
        if ns not in seen:
            seen.add(ns)
            out.append(str(d / "ns" / "mnt"))
    snapd = Path(snapd_ns)
    for f in sorted(snapd.glob("*.mnt")) if snapd.is_dir() else []:
        try:
            ns = f"mnt:[{os.stat(f).st_ino}]"
        except OSError:
            continue
        if ns not in seen:
            seen.add(ns)
            out.append(str(f))
    return out


def _ns_label(ns: str) -> str:
    """A mount namespace and what holds it, for the operator: the oldest
    process in it (its name from ``/proc/<pid>/comm``), or the snap whose
    namespace snapd pins (M4bR-8, M4bF-8 8a)."""
    path = Path(ns)
    if path.suffix == ".mnt" and str(path.parent) == SNAPD_NS_DIR:
        return f"mount namespace pinned by snapd for snap {path.stem}"
    pid = path.parent.parent.name
    try:
        comm = Path("/proc", pid, "comm").read_text(encoding="utf-8").strip()
    except OSError:
        comm = ""
    return f"mount namespace {ns}" + (f", process {comm}" if comm else "")


def release_elsewhere(
    root: str,
    log: Log,
    pools: Collection[str],
    had: Collection[Mount],
    namespaces: list[str] | None = None,
) -> list[str]:
    """Unmount the copies other mount namespaces still hold of zark's tree
    at or below ``root`` (W-1, M4b-3 3b); return what stays, one line per
    namespace, naming its process.

    A non-dataset mount under the tree with a mount of its own below it is
    left out of the propagated unmount, and so are its parents, the dataset
    included (Linux 7.0 fs/pnode.c:565-595). Entered with
    ``nsenter --mount`` (util-linux 2.41.3), the copies there are slaves of
    this namespace: unmounting them propagates nothing back. Only copies of
    the tree are touched (:func:`_is_copy`: ``pools`` being released, ``had``
    the mounts the tree had here); anything else at those paths is left
    alone and does not count as holding the pool.
    """

    def copies(enter: str) -> list[str] | None:
        rows = _table(enter)
        if rows is None:
            return None
        out: list[str] = []
        for target, mount in rows:
            if target != root and not target.startswith(f"{root}/"):
                continue
            if _is_copy(mount, pools, had):
                out.append(target)
            else:
                log.dbg(f"  left alone in {enter}: {target} ({' '.join(mount)}), not zark's")
        return out

    left: list[str] = []
    for ns in _namespaces() if namespaces is None else namespaces:
        enter = f"nsenter --mount={shlex.quote(ns)}"
        held = copies(enter) or []
        for top in _tops(held):
            r = sh.run(f"{enter} umount -R {shlex.quote(top)}", log=log)
            if r.ok:
                log.info(f"  released {top} in another mount namespace ({ns})")
        still = (copies(enter) or []) if held else []
        if still:
            left.append(f"{', '.join(still)} ({_ns_label(ns)})")
    return left


def _still_held(here: list[str], elsewhere: list[str]) -> list[str]:
    """Banner lines for what keeps a pool busy: mounts still here (a process
    in them), and what other namespaces still hold."""
    return [
        *(f"✗ still mounted here: {t} (in use? fuser -vm {t})" for t in _tops(here)),
        *(f"✗ still held: {x}" for x in elsewhere),
    ]


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
    here, had = _unmount_tree(altroot, ("rpool", "bpool"))

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

    left: list[str] = []
    for pool in ("bpool", "rpool"):  # bpool first (it sits under /boot)
        if not sh.run(f"zpool list {pool}").ok:
            continue
        sh.run(f"zfs unload-key -r {pool}")
        if sh.run(f"zpool export {pool}").ok:
            log.ok(f"Pool {pool} exported ✓")
        elif sh.run(f"zpool export -f {pool}").ok:
            log.ok(f"Pool {pool} exported (forced) ✓")
        else:
            log.warn(f"Could not export {pool}: busy")
            left.append(pool)
    elsewhere: list[str] = []
    if left:
        # Copies the propagated unmount could not reach (W-1): released where
        # they are, then the export is tried again.
        elsewhere = release_elsewhere(altroot, log, ("rpool", "bpool"), had)
        for pool in list(left):
            if sh.run(f"zpool export {pool}").ok:
                log.ok(f"Pool {pool} exported ✓")
                left.remove(pool)
    flush_device_cache(log)
    if left:
        log.banner_error(
            "SYSTEM NOT UNMOUNTED",
            [
                f"✗ still imported: {', '.join(left)}",
                *_still_held(here, elsewhere),
                "",
                *BUSY_HINT,
                *SYSTEM_REBOOT_HINT,
            ],
        )
        raise SystemExit(1)
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
    here, had = _unmount_tree(f"{MNT_BASE}/{selected}", (selected,))

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
            log.warn(f"Could not export {selected}: busy")
    elsewhere: list[str] = []
    if not exported_ok:
        # Copies the propagated unmount could not reach (W-1).
        elsewhere = release_elsewhere(f"{MNT_BASE}/{selected}", log, (selected,), had)
        if sh.run(f"zpool export {selected}").ok:
            log.ok(f"Pool {selected} exported ✓")
            exported_ok = True

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
        log.banner_error(
            "BACKUP NOT UNMOUNTED",
            [f"✗ {selected} is still imported", *_still_held(here, elsewhere), "", *BUSY_HINT],
        )
        raise SystemExit(1)
