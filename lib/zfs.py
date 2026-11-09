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
ZFS pool and dataset operations.

Wraps zfs/zpool commands with structured output and error handling.
"""

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from lib import dpkg_lock, forensics, scrub
from lib.config import now_utc_iso, zark_command
from lib.identity import BY_ID_DIR
from lib.log import Log
from lib.sh import RunResult, run
from lib.zfs_features import BEYOND_2_2_STREAM


class PoolTracker(Protocol):
    """What :meth:`ZFS.verify_exported_pool_readback` needs of a Cleanup."""

    def track_pool(self, name: str) -> None:
        """Export ``name`` on exit."""

    def untrack_pool(self, name: str) -> None:
        """No longer export ``name`` on exit."""


class KeyStatusError(Exception):
    """``zfs get keystatus`` failed: which keys are loaded is unknown."""


@dataclass
class PoolInfo:  # pylint: disable=too-many-instance-attributes
    """Information about a ZFS pool."""

    name: str
    guid: str = ""
    health: str = ""
    size: str = ""
    used: str = ""
    avail: str = ""
    used_bytes: int = 0
    avail_bytes: int = 0
    size_bytes: int = 0
    pct_used: int = 0


@dataclass(frozen=True)
class Space:  # pylint: disable=too-many-instance-attributes
    """A whole pool's space, ``zfs`` and ``zpool`` figures read in one pass
    (docs/STATUS-FILES.md, "Space object"). The two tools account
    differently (``used_bytes`` can exceed ``pool_alloc_bytes``), so neither
    set is derived from the other."""

    used_bytes: int
    available_bytes: int
    pool_size_bytes: int
    pool_alloc_bytes: int
    pool_free_bytes: int
    capacity_pct: int
    measured_at: str

    def to_json(self) -> dict[str, int | str]:
        """The Space object of the status files."""
        return {
            "used_bytes": self.used_bytes,
            "available_bytes": self.available_bytes,
            "pool_size_bytes": self.pool_size_bytes,
            "pool_alloc_bytes": self.pool_alloc_bytes,
            "pool_free_bytes": self.pool_free_bytes,
            "capacity_pct": self.capacity_pct,
            "measured_at": self.measured_at,
        }


def _ints(text: str, n: int) -> list[int] | None:
    """``n`` tab-separated non-negative integers on one line, or None."""
    fields = text.strip().split("\t")
    if len(fields) != n or not all(f.isdigit() for f in fields):
        return None
    return [int(f) for f in fields]


def read_space(pool: str) -> Space | None:
    """The Space object of ``pool``, None when it is not imported or a
    figure does not parse as an integer."""
    measured_at = now_utc_iso()
    zf = run(f"zfs list -Hp -o used,avail {pool}")
    zp = run(f"zpool list -Hp -o size,alloc,free,capacity {pool}")
    used = _ints(zf.output, 2) if zf.ok else None
    pool_ = _ints(zp.output, 4) if zp.ok else None
    if used is None or pool_ is None:
        return None
    # The status files' percentages are 0-100; zpool can report more for a
    # pool that went past its size.
    capacity = min(pool_[3], 100)
    return Space(used[0], used[1], pool_[0], pool_[1], pool_[2], capacity, measured_at)


DATASET_USAGE = "name,type,used,usedbydataset,usedbysnapshots,usedbychildren,usedbyrefreservation"
# Exports the read-back and prepare's first export try, each after udevadm
# settle, before -f (eli A5, M13R-15).
READBACK_EXPORT_TRIES = 3
# How long the read-back waits for an apt or dpkg run, from the first wait,
# never extended (M18-11 11a, M18-14 14a): then it is reported as not done.
READBACK_DPKG_WAIT_S = 15 * 60


@dataclass(frozen=True)
class DatasetUsage:
    """One dataset's space, as ``zfs list -p`` gives it (R2). Its own share,
    ``usedbydataset + usedbysnapshots + usedbyrefreservation``, overlaps no
    other dataset's, and the shares add up to the pool root's ``used``."""

    name: str
    type: str  # "filesystem" or "volume"
    used: int
    usedbydataset: int
    usedbysnapshots: int
    usedbychildren: int
    usedbyrefreservation: int

    def to_json(self) -> dict[str, int | str]:
        """A ``datasets`` entry of the status files."""
        return {
            "name": self.name,
            "type": self.type,
            "used_bytes": self.used,
            "usedbydataset_bytes": self.usedbydataset,
            "usedbysnapshots_bytes": self.usedbysnapshots,
            "usedbychildren_bytes": self.usedbychildren,
            "usedbyrefreservation_bytes": self.usedbyrefreservation,
        }


def read_datasets(pool: str) -> list[DatasetUsage] | None:
    """Every filesystem and volume of ``pool``, sorted by name; None when
    the listing fails or a line does not parse (never a partial list)."""
    r = run(f"zfs list -Hp -t filesystem,volume -r -o {DATASET_USAGE} {pool}")
    if not r.ok:
        return None
    out: list[DatasetUsage] = []
    for line in r.output.splitlines():
        if not line.strip():
            continue
        name, _, rest = line.partition("\t")
        kind, _, numbers = rest.partition("\t")
        values = _ints(numbers, 5)
        if not name or kind not in ("filesystem", "volume") or values is None:
            return None
        out.append(DatasetUsage(name, kind, *values))
    return sorted(out, key=lambda d: d.name) if out else None


@dataclass
class Damage:
    """What ``zpool status -vp`` reports wrong with a pool (I8)."""

    counters: list[str] = field(default_factory=list)  # vdev lines with READ/WRITE/CKSUM > 0
    errors: list[str] = field(default_factory=list)  # the "Permanent errors" list
    # M19E-1: a status with no errors: line, zpool having refused the list (on
    # stderr: "List of errors unavailable: permission denied", rc 0), as it does
    # while a damaged block's dataset is encrypted and its key is not loaded.
    unlisted: bool = False

    @property
    def any(self) -> bool:
        """True when a counter is non-zero or an object is listed."""
        return bool(self.counters or self.errors)


def parse_damage(text: str) -> Damage:
    """Read the non-zero vdev counters and the permanent errors from ``zpool status -vp``."""
    damage = Damage()
    in_config = in_errors = False
    seen_config = seen_errors = False
    for line in text.splitlines():
        words = line.split()
        if words[:5] == ["NAME", "STATE", "READ", "WRITE", "CKSUM"]:
            in_config = seen_config = True
            continue
        if line.startswith("errors:"):
            seen_errors = True
            in_config = False
            in_errors = "Permanent errors" in line
            continue
        if in_config:
            if not words:
                in_config = False
            elif len(words) >= 5 and all(w.isdigit() for w in words[2:5]):
                if any(int(w) for w in words[2:5]):
                    damage.counters.append(" ".join(words))
        elif in_errors and words:
            damage.errors.append(line.strip())
    damage.unlisted = seen_config and not seen_errors
    return damage


@dataclass
class Readback:  # pylint: disable=too-many-instance-attributes
    """Outcome of :meth:`ZFS.verify_exported_pool_readback`."""

    imported: bool
    health: str = ""
    status: str = ""  # `zpool status -v` when health is not ONLINE
    reexported: bool = False
    error: str = ""  # the import's stderr when it failed
    # Not done because apt or dpkg was running (M18-11): "operator" when
    # skipped at the question, "timeout" after the wait; who held the lock.
    skipped: str = ""
    holders: list[dpkg_lock.Holder] = field(default_factory=list)
    pool: str = ""
    # The GUID found at the device when it was not the one expected, or
    # "unreadable" (M20R-1): another pool of that name is on the disk.
    foreign: str = ""

    @property
    def ok(self) -> bool:
        """Re-imported, ONLINE and exported again."""
        return self.imported and self.health == "ONLINE" and self.reexported

    def summary(self, pool: str) -> str:
        """One line for a notification and status.json: a read-back that was
        not run is not one that failed (M18R-14)."""
        if self.skipped:
            return f"{pool} was not read back (apt was running)"
        if self.foreign:
            return f"{pool} is another pool (GUID {self.foreign})"
        return f"{pool} failed its read-back"

    def describe(self, *, again: bool = True) -> list[str]:
        """What happened, without guessing a cause (I6). ``again``: say that
        the next backup reads a skipped read-back's drive back."""
        if self.skipped:
            why = (
                "skipped by the operator"
                if self.skipped == "operator"
                else f"still running after {READBACK_DPKG_WAIT_S // 60} minutes"
            )
            return [
                f"The read-back of {self.pool} was not done: apt or dpkg was running "
                + f"({dpkg_lock.describe(self.holders)}); {why}.",
                "The backup's data is on the drive; it has not been read back.",
                *(
                    [
                        f"Run {zark_command()} backup again once apt has ended: "
                        + "the next run reads the drive back."
                    ]
                    if again
                    else []
                ),
            ]
        if self.foreign:
            return [
                f"The disk holds another pool named {self.pool} (GUID {self.foreign}), "
                + "not the one expected: nothing was registered.",
                *(
                    []
                    if self.reexported
                    else ["The read-back import could not be exported again: it is still imported."]
                ),
            ]
        if not self.imported:
            return [
                "The pool exported but could NOT be re-imported:",
                f"  {self.error or 'no error text'}",
                "One known cause is a USB-SATA bridge that acknowledges cache",
                "flushes it never performs (docs/HARDWARE.md).",
            ]
        lines: list[str] = []
        if self.health != "ONLINE":
            lines.append(f"The pool re-imported, but its health is {self.health or 'unknown'}:")
            lines += [f"  {line}" for line in self.status.splitlines() if line.strip()][:20]
        if not self.reexported:
            lines.append("The read-back import could not be exported again: it is still imported.")
        return lines


@dataclass
class DatasetInfo:  # pylint: disable=too-many-instance-attributes
    """Information about a ZFS dataset."""

    name: str
    mountpoint: str = ""
    used: str = ""
    refer: str = ""
    canmount: str = ""
    keystatus: str = ""
    encryption: str = ""
    type: str = ""  # "filesystem" or "volume"


# pylint and basedpyright disagree about the body of a Protocol stub:
# without the `...` basedpyright reads the docstring-only body as
# `return None`, which contradicts the declared return type; with it,
# pylint calls the ellipsis unnecessary. The ellipsis is the one the
# type checkers need, so W2301 is silenced here.
class ZFSQuery(Protocol):
    """The read-only slice of :class:`ZFS` that dataset discovery needs.

    ``commands.setup._discover_rules`` only ever calls these two methods.
    Asking for the whole ZFS class there would be over-specifying: it
    forces every caller to build a real one, and it is why the test stub
    that implements exactly these two methods did not type-check.
    """

    # pylint: disable=unnecessary-ellipsis
    def pool_exists(self, name: str) -> bool:
        """Check if a pool is currently imported."""
        ...

    def list_datasets(self, root: str, recursive: bool = True) -> list["DatasetInfo"]:
        """List datasets under a root."""
        ...


class ZFS:
    """ZFS operations wrapper."""

    def __init__(self, log: Log):
        self.log = log

    # ── Pool operations ──────────────────────────────────────────────────

    def pool_exists(self, name: str) -> bool:
        """Check if a pool is currently imported."""
        return run(f"zpool list {name}").ok

    def pool_info(self, name: str) -> PoolInfo | None:
        """Get pool info. Returns None if not imported."""
        r = run(f"zpool list -H -o name,guid,health,size,alloc,free {name}")
        if not r.ok:
            return None
        parts = r.output.split("\t")
        if len(parts) < 6:
            return None

        # Get byte-level usage for percentage
        rb = run(f"zfs list -H -o used,avail -p {name}")
        used_b = avail_b = 0
        if rb.ok:
            bp = rb.output.split("\t")
            if len(bp) >= 2:
                used_b = int(bp[0].strip())
                avail_b = int(bp[1].strip())

        # `total` is the sum of used + free as reported by `zfs list -p`.
        # On healthy pools this equals the pool's logical capacity, which
        # we expose as `size_bytes` (a numeric companion to the human-
        # formatted `size` field returned by `zpool list`). Used by
        # callers that need to compare pool capacities arithmetically
        # (see backup.py preventive ENOSPC guard).
        total = used_b + avail_b
        pct = (used_b * 100 // total) if total > 0 else 0

        return PoolInfo(
            name=parts[0],
            guid=parts[1],
            health=parts[2],
            size=parts[3],
            used=parts[4],
            avail=parts[5],
            used_bytes=used_b,
            avail_bytes=avail_b,
            size_bytes=total,
            pct_used=pct,
        )

    def pool_import(
        self,
        name: str,
        device: str | None = None,
        altroot: str | None = None,
        no_mount: bool = False,
        force: bool = False,
        readonly: bool = False,
    ) -> bool:
        """
        Import a system pool (rpool, bpool); ``readonly`` adds
        ``-o readonly=on`` (nothing is written to the pool, its labels
        included). Candidates, in order:
          1. -d /dev/disk/by-id: the pool, and the zpool.cache written from
             it, record stable names instead of /dev/sdX (eli 2026-09-30: a
             kernel-named vdev stopped the boot with a USB stick plugged in);
          2. the caller's device hint, then its directory (``zark chroot
             <device>``), when by-id finds nothing;
          3. auto-scan (no -d), the last resort.
          Each candidate is attempted without -f first, then with -f. A pool
          imported by anything but by-id is named in a warning, since its
          vdevs may now be recorded by kernel names.

        Backup pools go through :meth:`import_backup_pool`, which never scans.
        """
        del force  # currently unused, but could be added as a param to try force first if desired
        if self.pool_exists(name):
            self.log.dbg(f"Pool {name} already imported")
            return True

        def _build_cmd(dev_arg: str = "", use_force: bool = False) -> str:
            cmd = "zpool import"
            if use_force:
                cmd += " -f"
            if no_mount:
                cmd += " -N"
            if readonly:
                cmd += " -o readonly=on"
            if altroot:
                cmd += f" -R {altroot}"
            if dev_arg:
                cmd += f" -d {dev_arg}"
            cmd += f" {name}"
            return cmd

        dev_args: list[str] = [BY_ID_DIR]
        if device:
            for candidate in (device, os.path.dirname(device)):
                if candidate and candidate.rstrip("/") != BY_ID_DIR and candidate not in dev_args:
                    dev_args.append(candidate)
        dev_args.append("")  # auto-scan, last resort

        last_err = ""
        for darg in dev_args:
            for use_force in (False, True):
                cmd = _build_cmd(darg, use_force)
                r = run(cmd, log=self.log)
                if r.ok:
                    self.log.ok(f"Pool {name} imported")
                    if darg != BY_ID_DIR:
                        self.log.warn(
                            f"{name} was imported by {darg or 'a scan of every device'}, "
                            + f"not {BY_ID_DIR}: its vdevs may be recorded by kernel names",
                        )
                    return True
                last_err = r.stderr.strip()

        self.log.error(f"Cannot import pool {name}: {last_err}")
        return False

    def import_backup_pool(  # pylint: disable=too-many-locals
        self,
        name: str,
        device: str | None,
        *,
        altroot: str | None = None,
        readonly: bool = False,
        guid: str | None = None,
        pause_scrub: bool = True,
    ) -> bool:
        """Import a backup pool under invariant I-G.

        ``zpool import -N -R <altroot> [-o readonly=on] -d <device> <name>``:
        nothing mounts at import, every later mount lands under the altroot,
        ``-R`` implies ``cachefile=none`` so the pool never enters
        /etc/zfs/zpool.cache, and ``-d`` names the exact partition (never a
        directory scan: the Micron bridges share one bogus WWN). A clean
        import is tried first, then ``-f`` for a pool last used by another
        host (e.g. the live USB). There is no by-name fallback: without an
        exact device the import is refused.

        A pool that is already imported is accepted only if it was imported
        under an altroot, in the requested read-only/read-write mode, and
        (when ``guid`` is given) is the expected pool; otherwise the caller
        is told to export it. A pool imported from ``device`` is checked against
        ``guid`` too, and exported when it is another one.

        ``pause_scrub``: after a read-write import of the expected pool, a
        scrub that an interrupted ``zark fullcheck`` left running (it resumed
        with the import) is paused (M19-5 5a); ``fullcheck`` alone passes
        False (M19R-10). A read-only import never reads the scan state.
        """
        if self.pool_exists(name):
            current = run(f"zpool get -H -o value altroot {name}").output
            is_ro = run(f"zpool get -H -o value readonly {name}").output == "on"
            actual = self.pool_guid(name)
            problem = ""
            if current in ("", "-"):
                problem = "without an altroot"
            elif guid and actual != guid:
                problem = f"with GUID {actual}, not {guid} (another disk)"
            elif is_ro != readonly:
                problem = "read-only" if is_ro else "read-write"
            if problem:
                self.log.error(
                    f"Pool {name} is already imported {problem} - "
                    + f"export it first: sudo zpool export {name}",
                )
                return False
            self.log.dbg(f"Pool {name} already imported (altroot={current})")
            return True
        if not device:
            self.log.error(f"Cannot import {name}: no exact device path is known")
            return False

        root = altroot or backup_altroot(name)
        base = f"zpool import -N -R {root}"
        if readonly:
            base += " -o readonly=on"
        last_err = ""
        imported = False
        # No apt or dpkg run meets the import: refused (DpkgBusy) while one
        # runs, and none can start until it has ended (M18-9, M18-12).
        with dpkg_lock.hold(self.log):
            run(f"mkdir -p {root}")
            for force in ("", " -f"):
                r = run(f"{base}{force} -d {device} {name}", log=self.log)
                if r.ok:
                    imported = True
                    break
                last_err = r.stderr.strip()
        if not imported:
            self.log.error(f"Cannot import pool {name} from {device}: {last_err}")
            return False
        mode = "read-only" if readonly else "read-write"
        self.log.ok(f"Pool {name} imported ({mode}, altroot={root})")
        # After the release: it may export the pool, which is imported by now.
        if guid and not self._is_expected(name, guid, device, root):
            return False
        if pause_scrub and not readonly:
            scrub.pause_interrupted(name, self.log)
        return True

    def _is_expected(self, name: str, guid: str, device: str, root: str) -> bool:
        """After an import: the pool is the one expected (M16-7). A pool of
        that name on another disk is exported at once, before anything reads
        or writes it, after ``udevadm settle`` as every export after a zvol
        appears (M16R-9), and the altroot directory the import made goes with
        it; an unreadable GUID counts as a mismatch."""
        actual = self.pool_guid(name)
        if actual == guid:
            return True
        self.log.error(
            f"Pool {name} at {device} has GUID {actual or 'unreadable'}, not {guid}: "
            + "another disk; exporting it"
        )
        # Its zvols appeared with the import: udev's probes can hold them (M16R-9, M13R-15).
        if self._export_after_udev(name, "Refused import"):
            remove_altroot_dir(root)  # the empty directory the import made (M16R-5)
        else:
            self.log.error(f"Could not export {name}: sudo zpool export {name}")
        return False

    def pool_export(self, name: str, *, wait_udev: bool = False) -> bool:
        """Export a pool safely.

        ``wait_udev``: the pool's zvols appeared moments ago (prepare's
        keystore receive), so udev's and udisks' probes may still hold
        them: each try waits for ``udevadm settle`` as the read-back's do,
        not a fixed second (M13R-15)."""
        if not self.pool_exists(name):
            return True

        r = run(f"zpool set cachefile=none {name}")
        if wait_udev:
            if self._export_after_udev(name, "Export"):
                self.log.ok(f"Pool {name} exported")
                run("sync")
                remove_altroot_dir(backup_altroot(name))
                return True
            self.log.warn(f"Could not export {name}")
            return False
        run("sleep 1")
        r = run(f"zpool export {name}", log=self.log)
        if r.ok:
            self.log.ok(f"Pool {name} exported")
            run("sync")
            remove_altroot_dir(backup_altroot(name))
            return True

        # Force export
        r = run(f"zpool export -f {name}", log=self.log)
        if r.ok:
            self.log.ok(f"Pool {name} exported (forced)")
            run("sync")
            remove_altroot_dir(backup_altroot(name))
            return True

        self.log.warn(f"Could not export {name}")
        return False

    def verify_exported_pool_readback(
        self,
        name: str,
        device: str | None = None,
        cleanup: PoolTracker | None = None,
        *,
        skip_note: str = "It would be reported as not done.",
        guid: str = "",
    ) -> "Readback":
        """Confirm an already-exported pool can be re-imported and reads clean.

        ``guid``, when given, is the pool GUID expected at ``device``: read
        while the pool is imported, before its health; another GUID (or one
        that cannot be read) exports it again and is not ``ok``, with
        ``foreign`` set (M20R-1). The import is by name, so without it a
        pool of the same name on that disk would pass.

        ``cleanup`` tracks the pool from before its re-import until its
        export succeeded, so a run stopped in between (a signal, a fatal)
        still exports it on exit (M4bX.1, M4c-25).

        This is the only reliable guard against USB-SATA bridges that lie
        about FUA: a ``zpool export`` returning 0 does NOT guarantee the pool
        is reimportable, because the bridge can acknowledge a cache flush
        whose data never reached NAND. The symptom is a pool whose labels and
        uberblocks persisted but whose spacemaps did not, so a later import
        fails deep in ``vdev_load`` with ``metaslab_init failed [error=52]``
        (EBADE / "Invalid exchange") - discovered only when the backup is
        finally needed.

        The check, in order:
          1. ``sync`` then drop the kernel page cache, so the read-back comes
             from the device (NAND) and not from RAM still holding the data
             we just wrote - without this we would "verify" against cache and
             a silently-corrupt pool would pass.
          2. Re-import read-only, no-mount, by the exact device, under an
             altroot (I-G), holding dpkg's lock-frontend for the import
             alone. While apt or dpkg runs it first waits for it to end, up
             to :data:`READBACK_DPKG_WAIT_S`, holding nothing, and the
             operator may skip; a skipped or timed-out read-back imports
             nothing and is reported as not done (``skipped``, M18-11).
          3. Read its health; when it is not ONLINE, keep ``zpool status -v``.
          4. Re-export, checked and logged: a pool left imported is a failed
             verification too (I12). Each try waits for udev first: the
             import makes the drive's keystore zvol appear, and udev's and
             udisks' probes of its LUKS header hold it open for a moment,
             so an export right after the import is refused "pool is busy"
             (eli, 26.10, 6 of 10 tries; every one cleared by ``udevadm
             settle``). Up to :data:`READBACK_EXPORT_TRIES` tries a second
             apart, then ``-f``.

        The caller MUST treat a result that is not ``ok`` as "the backup is
        not trustworthy - do not tell the operator it is safe to disconnect".

        Note: this performs one extra import/export cycle. That is fine for
        backup targets; the "no rapid export/import cycles" rule applies only
        to the live-USB overlay during ``recover``, not to external backup
        pools.
        """
        run("sync")
        # Best-effort; if drop_caches is unavailable the verify still runs,
        # it just might read warm cache (weaker guarantee, never wrong-way).
        run("sh -c 'echo 3 > /proc/sys/vm/drop_caches'")

        root = backup_altroot(name)
        cmd = f"zpool import -o readonly=on -N -R {root}"
        if device:
            cmd += f" -d {device}"
        cmd += f" {name}"
        got = self._import_for_readback(name, root, cmd, cleanup, skip_note)
        if isinstance(got, Readback):
            return got  # not done: apt or dpkg was running
        r = got
        if not r.ok:
            if cleanup is not None:
                cleanup.untrack_pool(name)  # not imported: nothing to export
            self.log.error(f"Read-back of {name}: could not re-import: {r.stderr.strip()}")
            _ = forensics.capture(name, "read-back could not re-import the pool", self.log)
            return Readback(imported=False, error=r.stderr.strip())

        if guid and (actual := self.pool_guid(name)) != guid:
            found = actual or "unreadable"
            self.log.error(
                f"Read-back of {name}: the pool at {device} has GUID {found}, not {guid}"
            )
            reexported = self._reexport(name, root, cleanup)
            return Readback(
                imported=True,
                reexported=reexported,
                error=f"GUID {found}, not {guid}",
                pool=name,
                foreign=found,
            )

        health = self.pool_health(name)
        status = ""
        if health != "ONLINE":
            status = run(f"zpool status -v {name}").output
            self.log.error(f"Read-back of {name}: re-imported, health={health}")
            # While it is still imported: zpool status -v needs the pool.
            _ = forensics.capture(name, f"read-back health {health}", self.log)

        reexported = self._reexport(name, root, cleanup)
        return Readback(imported=True, health=health, status=status, reexported=reexported)

    def _reexport(self, name: str, root: str, cleanup: PoolTracker | None) -> bool:
        """The read-back's export, checked and logged (I12)."""
        reexported = self._export_after_udev(name)
        run("sync")
        if reexported and cleanup is not None:
            cleanup.untrack_pool(name)
        if reexported:
            # M7P.7: the import above made them again, after Cleanup removed them.
            remove_altroot_dir(root)
            remove_altroot_base()
            self.log.ok(f"Read-back of {name}: exported again")
        else:
            self.log.error(f"Read-back of {name}: could NOT export it again - still imported")
        return reexported

    def _import_for_readback(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        name: str,
        root: str,
        cmd: str,
        cleanup: PoolTracker | None,
        skip_note: str,
    ) -> RunResult | Readback:
        """The read-back's import under the hold, after waiting for apt or
        dpkg when one runs (M18-11): the import's result, or the Readback
        of a read-back that was skipped or timed out (nothing imported)."""
        deadline: float | None = None
        held = dpkg_lock.busy()
        while True:
            if held:
                # Wait holding nothing; the 15 minutes count from the first wait.
                if deadline is None:
                    deadline = time.monotonic() + READBACK_DPKG_WAIT_S
                outcome = self._wait_for_dpkg(name, held, deadline, skip_note)
                if outcome != "done":
                    return Readback(
                        imported=False,
                        skipped=outcome,
                        holders=dpkg_lock.holders() or held,
                        pool=name,
                    )
            if deadline is not None and time.monotonic() >= deadline:
                # Never past the deadline, whatever ended the wait (M18R-21).
                self.log.warn(
                    f"apt or dpkg is still running after {READBACK_DPKG_WAIT_S // 60} minutes: "
                    + f"the read-back of {name} is not done."
                )
                return Readback(imported=False, skipped="timeout", holders=held, pool=name)
            if cleanup is not None:
                cleanup.track_pool(name)
            try:
                # The import alone under the hold; health and export after it.
                with dpkg_lock.hold(self.log):
                    run(f"mkdir -p {root}")
                    r = run(cmd, log=self.log)
                return r
            except dpkg_lock.DpkgBusy as e:
                if cleanup is not None:
                    cleanup.untrack_pool(name)  # not imported: nothing to export
                if e.holders and all(h.error for h in e.holders):
                    raise  # the lock cannot be taken, nobody holds it: fail closed (M18R-21)
                held = e.holders or dpkg_lock.holders()  # taken in between: wait again
                time.sleep(1)

    def _wait_for_dpkg(
        self, name: str, held: list[dpkg_lock.Holder], deadline: float, skip_note: str
    ) -> str:
        """The read-back's wait for apt or dpkg (M18-11 11a): ``"done"``,
        ``"operator"`` (skipped) or ``"timeout"``; nothing is held."""
        self.log.warn(
            "apt or dpkg is running: "
            + "; ".join(h.is_held_by() for h in held)
            + f". The read-back of {name} waits for it to end, "
            + f"up to {READBACK_DPKG_WAIT_S // 60} minutes."
        )
        outcome = self.log.ask_wait(
            f"Skip the read-back of {name} now?",
            max(0.0, deadline - time.monotonic()),
            lambda: not dpkg_lock.holders(),  # read only: nothing is held while waiting
            skip_note,
        )
        if outcome == "done":
            self.log.info(f"apt or dpkg has ended: reading {name} back.")
            return "done"
        if outcome == "skip":
            self.log.warn(f"Read-back of {name} skipped: not done.")
            return "operator"
        self.log.warn(
            f"apt or dpkg is still running after {READBACK_DPKG_WAIT_S // 60} minutes: "
            + f"the read-back of {name} is not done."
        )
        return "timeout"

    def _export_after_udev(self, name: str, what: str = "Read-back") -> bool:
        """``zpool export`` of a pool whose zvols just appeared, each try
        after ``udevadm settle`` (the zvol probes, the read-back's step 4
        above), then ``-f``. ``what`` names the caller in the log."""
        for attempt in range(1, READBACK_EXPORT_TRIES + 1):
            run("udevadm settle --timeout=30", log=self.log)
            if run(f"zpool export {name}", log=self.log).ok:
                return True
            self.log.info(f"{what} of {name}: export try {attempt} refused (busy?)")
            if attempt < READBACK_EXPORT_TRIES:
                time.sleep(1)
        return run(f"zpool export -f {name}", log=self.log).ok

    def pool_guid(self, name: str) -> str:
        """Get pool GUID (decimal string)."""
        r = run(f"zpool get -H -o value guid {name}")
        return r.output if r.ok else ""

    def pool_health(self, name: str) -> str:
        """Get pool health status (e.g. "ONLINE", "DEGRADED")."""
        r = run(f"zpool list -H -o health {name}")
        return r.output if r.ok else "UNKNOWN"

    def pool_damage(self, name: str) -> Damage:
        """Non-zero counters and permanent errors of an imported pool (I8)."""
        # -p: exact counters; "1.2K" would not parse as a number.
        r = run(f"zpool status -vp {name}")
        return parse_damage(r.stdout) if r.ok else Damage()

    def unload_keys(self, name: str) -> bool:
        """``zfs unload-key -r``: every key loaded under pool ``name`` (M19E-1)."""
        return run(f"zfs unload-key -r {name}", log=self.log).ok

    def write_zpool_cache(self, cache_path: str, pools: list[str]) -> None:
        """
        Persist the given pools into a zpool.cache file at ``cache_path`` and
        then disconnect the pools from the cache so a subsequent export won't
        erase the saved entries.

        The dance is required because ``zpool export`` removes a pool from the
        active cachefile, but if we set ``cachefile=none`` first we lose the
        entries entirely. Workflow used by both ``recover`` and ``repair-boot`` when
        preparing a target system to boot:

        1. Point both pools at ``cache_path`` so their entries land in it.
        2. Copy ``cache_path`` aside.
        3. Set ``cachefile=none`` on both pools (so future export is a no-op
           on the cache).
        4. Restore the saved copy back to ``cache_path``.

        ``cache_path``'s parent directory is created if it does not exist.
        """
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        backup = f"{cache_path}.recover"
        for pool in pools:
            run(f"zpool set cachefile={cache_path} {pool}")
        run(f"cp {cache_path} {backup}")
        for pool in pools:
            run(f"zpool set cachefile=none {pool}")
        run(f"mv {backup} {cache_path}")

    # ── Dataset operations ───────────────────────────────────────────────

    def list_datasets(self, root: str, recursive: bool = True) -> list[DatasetInfo]:
        """List datasets under a root."""
        flag = "-r" if recursive else ""
        r = run(
            "zfs list -H -o name,mountpoint,used,refer,canmount,type "
            + f"-t filesystem,volume {flag} {root}",
        )
        if not r.ok:
            return []

        result = []
        for line in r.lines:
            parts = line.split("\t")
            if len(parts) >= 6 and "@" not in parts[0]:
                result.append(
                    DatasetInfo(
                        name=parts[0],
                        mountpoint=parts[1],
                        used=parts[2],
                        refer=parts[3],
                        canmount=parts[4],
                        type=parts[5],
                    ),
                )
        return result

    def list_snapshots(self, root: str, pattern: str = "") -> list[str]:
        """List snapshot names under a root, optionally filtered."""
        r = run(f"zfs list -H -o name -t snapshot -r {root}")
        if not r.ok:
            return []
        snaps = r.lines
        if pattern:
            snaps = [s for s in snaps if pattern in s]
        return snaps

    def dataset_exists(self, name: str) -> bool:
        """Check if a dataset exists. Note: this does not check if it's mounted."""
        return run(f"zfs list {name}").ok

    def dataset_used_bytes(self, name: str) -> int:
        """Return `used` for a dataset in raw bytes, or 0 on failure.

        Wraps `zfs list -H -p -o used <name>`. Used by recover's
        preventive disk-size check, which needs to compare a dataset's
        size in bytes against the physical capacity of the target disk.
        """
        r = run(f"zfs list -H -p -o used {name}")
        if not r.ok:
            return 0
        try:
            return int(r.output.strip())
        except (ValueError, AttributeError):
            return 0

    def get_property(self, dataset: str, prop: str) -> str:
        """Get a specific property value for a dataset. Returns empty string if not found."""
        r = run(f"zfs get -H -o value {prop} {dataset}")
        return r.output if r.ok else ""

    def set_property(self, dataset: str, prop: str, value: str) -> bool:
        """Set a specific property value for a dataset. Returns True if successful."""
        r = run(f"zfs set {prop}={value} {dataset}", log=self.log)
        return r.ok

    def datasets_needing_key(self, root: str, strict: bool = False) -> list[str]:
        """
        Return datasets under `root` whose key is not yet loaded.

        Reads `zfs get -H keystatus -r <root>` and selects every non-snapshot
        dataset (no '@' in the name) with keystatus == 'unavailable'. Used by
        every caller that needs to walk an encryption hierarchy and load keys.
        A failed read is no dataset; with ``strict`` it raises
        :class:`KeyStatusError` instead, since "nothing to load" would then
        skip the passphrase (M4c-19).
        """
        r = run(f"zfs get -H keystatus -r {root}")
        if not r.ok:
            if strict:
                raise KeyStatusError(r.stderr.strip() or f"zfs get keystatus -r {root} failed")
            return []
        out: list[str] = []
        for line in r.lines:
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].strip() == "unavailable" and "@" not in parts[0]:
                out.append(parts[0].strip())
        return out

    def encryption_roots_needing_key(self, root: str, strict: bool = False) -> list[str]:
        """The encryption roots of the datasets under ``root`` whose key is
        not loaded, in the order zfs lists them.

        Only a root takes a key: libzfs refuses ``load-key`` of a dataset that
        inherits one (lib/libzfs/libzfs_crypto.c:1316-1328), and loading the
        root makes every dataset under it available, a clone of one of them
        too (module/zfs/dsl_crypt.c:1166-1189, :1913-1931); ``zfs load-key
        -a`` does the same (libzfs_crypto.c:1209-1234). A raw-received
        dataset is its own root (dsl_crypt.c:2294-2329). ``strict`` as
        :meth:`datasets_needing_key`.
        """
        r = run(
            f"zfs get -H -o name,property,value -t filesystem,volume -r "
            f"encryptionroot,keystatus {root}"
        )
        if not r.ok:
            if strict:
                raise KeyStatusError(r.stderr.strip() or f"zfs get keystatus -r {root} failed")
            return []
        props: dict[str, dict[str, str]] = {}
        for line in r.lines:
            parts = line.split("\t")
            if len(parts) >= 3:
                props.setdefault(parts[0].strip(), {})[parts[1].strip()] = parts[2].strip()
        roots: list[str] = []
        for p in props.values():
            enc = p.get("encryptionroot", "-")
            if p.get("keystatus") == "unavailable" and enc not in ("-", "") and enc not in roots:
                roots.append(enc)
        return roots

    def mount_dataset(self, dataset: str, mountpoint: str) -> bool:
        """Mount a specific dataset at a specific path (safe, explicit)."""
        run(f"mkdir -p {mountpoint}")
        r = run(f"mount -t zfs {dataset} {mountpoint}", log=self.log)
        return r.ok

    def mount_all(self) -> bool:
        """Mount all mountable datasets. Use with care."""
        r = run("zfs mount -a", log=self.log)
        return r.ok

    def unmount_all(self) -> bool:
        """Unmount all datasets. Use with care (e.g. before exporting a pool)."""
        r = run("zfs unmount -a", log=self.log)
        return r.ok

    # ── Discovery (no pool import needed) ────────────────────────────────

    def scan_zfs_members(self) -> list[dict[str, str]]:
        """
        Scan all ZFS member partitions via blkid.
        Returns list of {devname, uuid, label} dicts.
        """
        r = run("blkid -t TYPE=zfs_member -o export")
        if not r.ok:
            return []

        results: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                if current:
                    results.append(current)
                    current = {}
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                current[key.lower()] = val
        if current:
            results.append(current)

        return results

    def importable_pools(self) -> list[dict[str, str]]:
        """List pools available for import."""
        r = run("zpool import")
        if not r.ok:
            return []
        # Parse output - simplistic but functional
        pools: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("pool:"):
                if current:
                    pools.append(current)
                current = {"name": line.split(":", 1)[1].strip()}
            elif line.startswith("id:"):
                current["guid"] = line.split(":", 1)[1].strip()
            elif line.startswith("state:"):
                current["state"] = line.split(":", 1)[1].strip()
        if current:
            pools.append(current)
        return pools


# Private altroot for backup-pool imports that mount nothing on purpose.
BACKUP_ALTROOT_BASE = "/run/zark/altroot"


def backup_altroot(pool: str) -> str:
    """Altroot used for a backup pool when the command does not mount it."""
    return f"{BACKUP_ALTROOT_BASE}/{pool}"


def remove_altroot_dir(path: str) -> None:
    """Remove the empty directory an import used as its altroot, once the pool
    was exported: a successful export means no mount below it in this
    namespace, and a busy copy in another one makes the export fail first
    (M5a-15). Only an empty directory goes; any error is ignored."""
    try:
        os.rmdir(path)
    except OSError:
        pass


def remove_altroot_base() -> None:
    """:func:`remove_altroot_dir` of :data:`BACKUP_ALTROOT_BASE` itself, once
    its pools' directories went (M7P.7): a pool still imported under it, by
    this run or another, keeps it."""
    remove_altroot_dir(BACKUP_ALTROOT_BASE)


# Where `zpool create -o compatibility=<name>` looks, in order (OpenZFS 2.4.1
# lib/libzfs/libzfs_pool.c:5170-5375).
COMPAT_DIRS = ("/etc/zfs/compatibility.d", "/usr/share/zfs/compatibility.d")
DRIVE_COMPATIBILITY = "openzfs-2.2"


def create_command(
    pool: str, device: str, altroot: str, compatibility: str, marks: dict[str, str] | None = None
) -> str:
    """``zpool create`` of a backup drive (prepare; the self-test's level 2).

    -R: the pool never enters zpool.cache and nothing it will hold can mount
    over the running system (I-G). The vdev is named by its by-id so later
    imports never depend on /dev/sdX (I16). ``compatibility`` keeps the
    drive readable by older lives (M4s-10); "" leaves every feature on.
    ``marks`` are ``org.zark:<key>=<value>`` set on the root dataset by the
    create itself, so the pool never exists without them (M13F-6 6a).
    """
    compat = f"-o compatibility={compatibility} " if compatibility else ""
    props = "".join(f"-O org.zark:{k}={v} " for k, v in sorted((marks or {}).items()))
    return (
        f"zpool create -f -o ashift=12 {compat}-O atime=off -O xattr=sa "
        + f"-O dnodesize=auto -O normalization=formD {props}-m none -R {altroot} "
        + f"{pool} {device}"
    )


def drive_compatibility(origin_pools: tuple[str, ...], log: Log) -> str:
    """The ``compatibility`` a new drive gets: openzfs-2.2, so that Ubuntu
    24.04's live (OpenZFS 2.2) and later can read it and dynamic_gang_header
    (2.4) never activates on it; "" when that set is not installed here, or
    when origin has a feature active that it lacks and zark's streams would
    carry (M4s-10)."""
    if not any(Path(d, DRIVE_COMPATIBILITY).is_file() for d in COMPAT_DIRS):
        log.warn(f"No {DRIVE_COMPATIBILITY} feature set installed: the drive gets every feature")
        return ""
    for pool in origin_pools:
        for feature in BEYOND_2_2_STREAM:
            if run(f"zpool get -H -o value feature@{feature} {pool}").output == "active":
                log.warn(
                    f"{pool} uses {feature}, which {DRIVE_COMPATIBILITY} lacks: the drive "
                    + "gets every feature and needs a live with this OpenZFS or later to read it"
                )
                return ""
    return DRIVE_COMPATIBILITY


# ── grub.cfg helpers ─────────────────────────────────────────────────────

# In Ubuntu ZFS systems, every `search --no-floppy --fs-uuid --set=NAME HEX`
# line in /boot/grub/grub.cfg points to the bpool: rpool is referenced via
# the `root=ZFS=...` kernel param, ESP via /etc/fstab - neither uses fs-uuid
# in grub. So replacing all such hex values with the new bpool GUID is safe.
#
# `update-grub` (10_linux_zfs) emits `--set=root` on real Ubuntu hardware.
# Manual / test configs may use `--set=boot_fs`. The regex matches any
# `--fs-uuid` followed by zero or more options (such as `--set=<name>`,
# `--hint-bios=...`, `--hint-efi=...`, `--hint-baremetal=...`) and finally
# the 16-hex-char UUID. The earlier version of this regex required only
# whitespace between `--set=<name>` and the UUID, which silently failed on
# the standard Ubuntu grub.cfg lines that include hints (the only ones that
# actually run on grub 2.12+ when `feature_platform_search_hint` is set).
# This caused the recovered system to keep stale (source) UUIDs in the
# `if`-branch of every menu entry, producing `error: no such device:
# <old-uuid>` followed by `disk 'hdN,gptN' not found` and
# `you need to load the kernel first` at boot - but only on machines
# where the source disk's BIOS index didn't happen to match the new
# machine's, so the bug stayed hidden until a recovery between hosts
# with different drive enumeration.
_GRUB_FS_UUID_RE = re.compile(r"(--fs-uuid[^\n]*?\s+)([a-f0-9]{16})\b")


def fix_grub_bpool_uuid(grub_cfg: Path, new_bpool_hex: str, log: Log) -> bool:
    """
    Rewrite every `fs-uuid --set=<name> <hex>` line in grub_cfg so the hex
    matches the freshly-recreated bpool's GUID.

    The bpool is created from scratch during recovery, so it gets a new GUID,
    but the grub.cfg restored via bpool send/receive still references the
    OLD source bpool GUID. This helper rewrites every stale fs-uuid reference
    in the file to point at the new bpool.

    Returns True if grub.cfg existed and was processed (modified or already
    correct); False if grub.cfg was missing.
    """
    if not grub_cfg.exists():
        log.warn(f"{grub_cfg} not found - bpool content was not restored")
        return False

    # surrogateescape: a title that is not UTF-8 is written back as it was read.
    content = grub_cfg.read_text(encoding="utf-8", errors="surrogateescape")
    matches = _GRUB_FS_UUID_RE.findall(content)
    if not matches:
        log.warn(f"No fs-uuid --set= references in {grub_cfg.name} - bpool UUID not updated")
        return True

    old_uuids = {hex_ for _, hex_ in matches if hex_ != new_bpool_hex}
    if not old_uuids:
        log.dbg(f"grub.cfg bpool UUID already correct: {new_bpool_hex}")
        return True

    # Substitute only hex values that follow `fs-uuid --set=...`. Using
    # re.sub instead of str.replace keeps us from touching any unrelated
    # 16-hex tokens that might appear elsewhere in the file.
    def _sub(match: re.Match[str]) -> str:
        prefix, old_hex = match.group(1), match.group(2)
        if old_hex == new_bpool_hex:
            return match.group(0)
        return f"{prefix}{new_bpool_hex}"

    _ = grub_cfg.write_text(
        _GRUB_FS_UUID_RE.sub(_sub, content),
        encoding="utf-8",
        errors="surrogateescape",
    )

    old_list = ", ".join(sorted(old_uuids))
    log.ok(f"Fixed bpool UUID in grub.cfg: {old_list} → {new_bpool_hex}")
    return True
