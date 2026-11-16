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
What zark learns about an imported pool without waiting on its drive (A1E-7).

Once a backup drive is gone, a ``zfs`` or ``zpool`` call on its pool can wait
in state D for as long as the pool is suspended, and nothing ends that wait
but the drive's return and a ``zpool clear``, or a reboot. Two reads never
wait:

  * the pool's state, from ``/proc/spl/kstat/zfs/<pool>/state``: the
    module prints ``spa_state_to_name`` there, a lock-less read that answers
    ``SUSPENDED`` before it looks at the vdevs (OpenZFS 2.2.2
    module/zfs/spa_stats.c:782-816, spa_misc.c:2745-2784; 2.4.1 and 2.4.4
    spa_stats.c:783-816). The file exists while the pool is imported;
  * whether a device link exists.

Anything else zark asks of such a pool goes through :func:`run_bounded`: the
child runs in a session of its own and is polled until a deadline; one still
running then is abandoned, never waited for, never killed and waited for
(a child in state D does not die), and never registered as the main
thread's running child (``lib.signals``).
"""

import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal

from lib import dpkg_lock, signals
from lib.config import zark_command
from lib.sh import CHILD_CWD

KSTAT_DIR = Path("/proc/spl/kstat/zfs")
MOUNTS = Path("/proc/self/mounts")
HEALTHY = frozenset({"ONLINE", "DEGRADED"})
# States that say at once that the drive is not usable (spa_state_to_name).
LOST = frozenset({"SUSPENDED", "REMOVED", "UNAVAIL", "FAULTED", "OFFLINE"})
# What state() returns when the file is not there (the pool is not
# imported) or cannot be read; the watch calls both "unknown".
MISSING = "(missing)"
UNREADABLE = "(unreadable)"


def _kstat(kstat_dir: Path | None) -> Path:
    return kstat_dir if kstat_dir is not None else KSTAT_DIR


def state(pool: str, kstat_dir: Path | None = None) -> str:
    """The pool's state word, :data:`MISSING` or :data:`UNREADABLE`."""
    kstat_dir = _kstat(kstat_dir)
    if not pool or "/" in pool or pool in (".", ".."):
        return UNREADABLE
    try:
        return (kstat_dir / pool / "state").read_text(encoding="ascii").strip() or UNREADABLE
    except FileNotFoundError:
        return MISSING
    except (OSError, UnicodeDecodeError):
        return UNREADABLE


def hold(pool: str, *, side: Literal["send", "receive"]) -> signals.Held:
    """``pool`` for an interrupt of a transfer (:func:`signals.stop_children`):
    suspended when its kstat state reads SUSPENDED at that moment; a state
    that cannot be read does not count (R5-1). ``side`` is the side of the
    pipe on ``pool``: only that one is left (M26R-57)."""
    return signals.Held(pool, lambda: state(pool) == "SUSPENDED", side)


def imported(pool: str, kstat_dir: Path | None = None) -> bool:
    """Whether the module holds ``pool`` (its kstat directory exists); with
    no kstat directory at all (no module loaded) nothing is."""
    return state(pool, kstat_dir) != MISSING


@dataclass
class Child:
    """A child of :func:`start`: polled, never waited for."""

    command: str
    proc: subprocess.Popen[bytes]
    out: IO[bytes]
    err: IO[bytes]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""

    def poll(self) -> int | None:
        """Its status once it ended (output read then), None while it runs."""
        if self.returncode is not None:
            return self.returncode
        rc = self.proc.poll()
        if rc is None:
            return None
        self.returncode = rc
        for stream, name in ((self.out, "stdout"), (self.err, "stderr")):
            _ = stream.seek(0)
            setattr(self, name, stream.read().decode(errors="replace"))
            stream.close()
        return rc


def start(argv: list[str]) -> Child:
    """Start ``argv`` in a session of its own, its output to unnamed files
    (a pipe left unread could block it), with the C locale for parsing."""
    out = tempfile.TemporaryFile()  # pylint: disable=consider-using-with
    err = tempfile.TemporaryFile()  # pylint: disable=consider-using-with
    env = {**os.environ, "LC_ALL": "C"}
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        argv,
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=err,
        env=env,
        start_new_session=True,
        cwd=CHILD_CWD,
    )
    return Child(" ".join(argv), proc, out, err)


@dataclass
class Bounded:
    """What :func:`run_bounded` saw: ``rc`` None when the child was still
    running at the deadline (``child`` then keeps it, to poll later)."""

    rc: int | None
    stdout: str = ""
    stderr: str = ""
    child: Child | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        """Ended with status 0."""
        return self.rc == 0


def run_bounded(
    argv: list[str],
    deadline_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Bounded:
    """Run ``argv`` and wait for it at most ``deadline_s`` seconds."""
    try:
        child = start(argv)
    except OSError as e:
        return Bounded(126, "", str(e))
    end = clock() + deadline_s
    while (rc := child.poll()) is None:
        if clock() >= end:
            return Bounded(None, "", "", child)
        sleep(0.05)
    return Bounded(rc, child.stdout, child.stderr, child)


# ── what an export needs (A1E-7, M26R-19, M26R-26, M26R-28) ──────────────
#
# zpool export of a read-write pool waits for a transaction group with the
# pool namespace lock held, before any branch that looks at -f or -F
# (spa_export_common, OpenZFS 2.4.1 spa.c:7703-7705), and zfs unload-key
# waits for one too (dsl_crypt.c:936-937). On a pool whose drive is gone but
# which has not suspended yet, the forced write fails, the pool suspends
# under them and they wait: zpool clear and every pool command by name wait
# behind the export, and only a reboot ends it. A read-only pool has no sync
# thread: neither waits (spa_sync_on is set by a writeable import only).
#
# So an export of a read-write backup pool needs rules 1 and 2: its state is
# ONLINE or DEGRADED, and every leaf device path it was imported from
# exists. A run whose drive was seen gone needs rule 3 too: a probe, zpool
# sync -f, that returned 0. -f dirties the vdev configuration, so the
# labels and uberblocks are written (zfs_ioc_pool_sync; without it a pool
# with nothing dirty writes nothing, vdev_label.c vdev_config_sync); it is
# refused at once on a suspended pool and otherwise waits without the
# namespace lock, so a pool it makes suspend leaves the clear free.

BOUNDED_READ_S = 10.0
PROBE_DEADLINE_S = 30.0
# How long a pool reads ONLINE or DEGRADED with its devices there before a
# probe starts: a zpool sync -f admitted while a transaction group is being
# written queues as a writer of the pool's config lock, and if that write
# then fails the pool suspends with the lock held, so every later reader
# (zpool clear's own stats read among them) waits behind it until a reboot
# (M26R-32; the window is there on 2.2.2, 2.4.1 and 2.4.4). Right after a
# resume the sync thread is finishing the group it was stuck in.
SETTLE_S = 10.0


@dataclass(frozen=True)
class Kept:
    """How zark imported a pool: its mode and its leaf device paths."""

    readonly: bool
    paths: tuple[str, ...]


_KEPT: dict[str, Kept] = {}


def remember(pool: str, readonly: bool, paths: list[str]) -> None:
    """Keep ``pool``'s mode and leaf paths, read right after its import."""
    _KEPT[pool] = Kept(readonly, tuple(paths))


def kept(pool: str) -> Kept | None:
    """What :func:`remember` kept for ``pool``, None when zark did not import it."""
    return _KEPT.get(pool)


def leaf_paths(text: str) -> list[str]:
    """The leaf paths of ``zpool list -vHP``'s vdev lines: devices and, for
    the self-test's pools, files (M26R-31). Group lines (mirror-0, logs,
    cache) are not paths."""
    out: list[str] = []
    for line in text.splitlines()[1:]:
        path = line.strip().split("\t")[0]
        if path.startswith("/"):
            out.append(path)
    return out


def read_kept(pool: str) -> Kept | None:
    """Mode and paths of a pool zark did not import in this process, through
    the bounded runner; None when either cannot be read in time."""
    mode = run_bounded(["zpool", "get", "-H", "-o", "value", "readonly", pool], BOUNDED_READ_S)
    if not mode.ok or mode.stdout.strip() not in ("on", "off"):
        return None
    listing = run_bounded(["zpool", "list", "-vHP", pool], BOUNDED_READ_S)
    paths = leaf_paths(listing.stdout) if listing.ok else []
    return Kept(mode.stdout.strip() == "on", tuple(paths)) if paths else None


def export_refusal(pool: str, kstat_dir: Path | None = None, *, read: bool = True) -> str:
    """Why ``pool`` must not be exported now (rules 1 and 2), "" when it may.

    A read-only pool is refused only when suspended (both calls would be
    refused anyway). ``read``: a pool zark did not import in this process
    has its mode and paths read through the bounded runner; when they cannot
    be read it is treated as read-write with unknown devices, and refused.
    """
    if not _kstat(kstat_dir).is_dir():
        # No ZFS module here to hold the pool (a unit test, a host without
        # zfs loaded): nothing these rules could judge.
        return ""
    word = state(pool, kstat_dir)
    info = kept(pool)
    if info is None and read and word != "SUSPENDED":
        # The state first: a suspended pool is refused in either mode, so its
        # two zpool reads would be waited for and abandoned for nothing (M26R-36).
        info = read_kept(pool)
    return refusal_for(info, word)


def refusal_for(info: Kept | None, word: str) -> str:
    """Rules 1 and 2 for a pool whose mode and paths are ``info`` (None:
    unknown) and whose kstat state is ``word``."""
    if info is not None and info.readonly:
        return "its pool is suspended" if word == "SUSPENDED" else ""
    if word not in HEALTHY:
        what = "cannot be read" if word in (MISSING, UNREADABLE) else f"is {word}"
        return f"its state {what}"
    if info is None:
        return "its devices could not be read"
    gone = [p for p in info.paths if not os.path.lexists(p)]
    if gone:
        return f"its device {gone[0]} is gone"
    return ""


def settle() -> None:
    """The pause before a probe of a pool that may have just been resumed."""
    time.sleep(SETTLE_S)


def probe(pool: str, deadline_s: float = PROBE_DEADLINE_S) -> Bounded:
    """``zpool sync -f <pool>``: 0 means labels and uberblocks were written
    through the vdev as it is. Read-write pools only (refused read-only)."""
    return run_bounded(["zpool", "sync", "-f", pool], deadline_s)


def probe_line(pool: str, rc: int | None) -> str:
    """The probe as zark.log records it (the bounded runner logs nothing of
    its own; M26 eli revision 4, B5b)."""
    got = "has not returned" if rc is None else f"rc {rc}"
    return f"$ zpool sync -f {pool} (the probe): {got}"


# Why every message offers the reboot first and the clear second: on eli
# (2.4.4) the module panicked in zio_encrypt 3.5 s after the user's clear
# resumed a raw receive, as OpenZFS #18394 reports on 2.4.1 (M26 eli, B1d).
CLEAR_RISK = (
    "On OpenZFS 2.4 a clear of a pool whose drive went away with writes in flight "
    + "can crash the ZFS module (OpenZFS #18394): writes to the pool then hang, and "
    + "the reboot itself may need a power-off."
)


# Why the reboot zark offers is not a plain one: on eli the orderly shutdown
# with backup2 suspended did not end in 10 minutes (zark's zfs receive in
# state D, blkdeactivate's dmsetup on the keystore's mapping; M26 eli,
# revision 3's B1d).
REBOOT_WHY = (
    "A plain reboot is not offered: with a pool suspended the shutdown can hang "
    + "on processes it cannot end."
)

# What the forced reboot skips that an orderly shutdown does (M26R-49).
REBOOT_SKIPS = (
    "It closes nothing: save open work first, and do not run it while apt or dpkg is running."
)

# A step that never returns (a sync waiting behind the lost pool's namespace
# lock, M26R-48's fourth case) still leaves the reboot alone, which takes no
# pool lock (systemd's halt_now: sync(2), then reboot(2)).
REBOOT_FORCED = "sudo systemctl reboot --force --force"
REBOOT_STUCK = f"If a step does not return, run `{REBOOT_FORCED}` alone from another terminal."


def _ext_boot(mounts: Path) -> bool:
    """Whether /boot is an ext2/3/4 file system of its own (zark-boot's layout)."""
    try:
        lines = mounts.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any(
        len(f) > 2 and f[1] == "/boot" and f[2] in ("ext2", "ext3", "ext4")
        for f in (ln.split() for ln in lines)
    )


def reboot_command(pool: str, kstat_dir: Path | None = None, mounts: Path | None = None) -> str:
    """The reboot zark offers first for ``pool``, whose drive went away.

    The system's own pools are synced by name, only those the kstat reads
    ONLINE or DEGRADED (``zpool sync`` with names opens and syncs those pools
    alone; with none it would reach the suspended one too); an ext4 /boot is
    synced with ``sync -f``; then ``systemctl reboot --force --force``, which
    stops no service and unmounts nothing (systemd's ``halt_now``: sync(2),
    then reboot(2)). All reads here are files, never a ``zpool`` call.

    The syncs are best effort and the reboot is the point, so the steps are
    joined with ``;``: a step that fails (a pool imported read-only, one that
    went away since the line was printed) does not keep the reboot from
    running (M26R-48). Each name is quoted; a name that begins with ``$`` is
    a scan's or an import's transient pool (``$import-...``), never synced."""
    kstat_dir = _kstat(kstat_dir)
    try:
        names = sorted(
            d.name
            for d in kstat_dir.iterdir()
            if d.name != pool
            and not d.name.startswith("$")
            and d.is_dir()
            and state(d.name, kstat_dir) in HEALTHY
        )
    except OSError:
        names = []
    steps = [f"sudo zpool sync {' '.join(shlex.quote(n) for n in names)}"] if names else []
    if _ext_boot(mounts if mounts is not None else MOUNTS):
        steps.append("sudo sync -f /boot")
    steps.append(REBOOT_FORCED)
    return "; ".join(steps)


def _apt_running() -> str:
    """Who holds a dpkg lock now, "" when nobody does. Read without taking
    the lock; while zark itself holds lock-frontend only ``lock`` is read,
    since opening and closing lock-frontend would drop zark's own lock."""
    try:
        held = dpkg_lock.holders(skip_frontend=dpkg_lock.is_held())
    except OSError:
        return ""
    return dpkg_lock.describe(held) if held else ""


# The suite points this at nothing: a test never reads the host's dpkg locks.
apt_running: Callable[[], str] = _apt_running


def reboot_lines(pool: str, indent: str = "    ") -> list[str]:
    """The first way out, as every message gives it: the drive unplugged, the
    command for this system on a line of its own (it is pasted whole, so no
    caller puts it in a box that wraps), why not a plain reboot, what it
    skips, and what to do when a step does not return."""
    lines = [
        f"{indent}{reboot_command(pool)}",
        f"{indent}{REBOOT_WHY} Nothing imports the drive at boot, and its next import "
        + "starts from the last state it saved.",
        f"{indent}{REBOOT_SKIPS} {REBOOT_STUCK}",
    ]
    if apt := apt_running():
        lines.append(f"{indent}apt or dpkg is running now ({apt}): let it end first.")
    return lines


def left_imported_lines(pool: str, why: str) -> list[str]:
    """What the user does with a pool zark leaves imported (plan 4.8)."""
    return [
        f"{pool} is left imported: {why}. Either:",
        "  - unplug its drive if it is not, then reboot at once with:",
        *reboot_lines(pool, "      "),
        f"  - or plug its drive back in; if `zpool status {pool}` says it is suspended:",
        f"      sudo zpool clear {pool}",
        "    then, to close its keystore and export it:",
        f"      {zark_command()} clean",
        f"    {CLEAR_RISK}",
    ]
