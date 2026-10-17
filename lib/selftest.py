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
The platform self-test (M4s): checks, the bench they run on, the runner.

A check certifies one behaviour of ZFS, util-linux, systemd, the kernel or
CPython that zark relies on, on the machine it runs on. It has an ID and a
revision (``POOL-01.1``): a check whose meaning changes gets a new revision,
so a certificate never covers a check it did not run. Its function raises
:class:`Fail` (expected, came) or :class:`Skip` (reason), or returns for a
PASS; any other exception is a FAIL with its text.

Level 1 runs on file-backed pools (:class:`Bench`): sparse files in
``/dev/shm`` (tmpfs: ZFS opens a file vdev with a plain ``filp_open``,
OpenZFS 2.4.1 module/zfs/vdev_file.c:83-97, zfs_file_os.c:44-65), pools with
random names that nothing else has, imported under ``/run/zark/altroot``
(``-R`` implies ``cachefile=none``, cmd/zpool/zpool_main.c:2000-2008). The
teardown destroys a pool only after ``zpool get guid`` returned the GUID it
had at creation, and removes the directory only when no imported pool has a
vdev in it. Level 2 (:mod:`lib.selftest_l2`) runs on a scratch stick.

Nothing here runs ``zfs set mountpoint`` or ``zfs set canmount`` (the
chase.c:648 rule): mount properties are given at creation or receive time.
Mounts happen inside ``unshare -m`` and are unmounted before it ends, since
the kernel tears an emptied namespace down lazily (Linux 7.0
fs/namespace.c:6238-6245).
"""

import importlib
import json
import os
import secrets
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lib import sh
from lib.cleanup import Cleanup
from lib.log import Log
from lib.zfs import BACKUP_ALTROOT_BASE

# Changes when a check is added, removed or revised; the report carries it
# as information only (coverage is per check ID and revision).
CHECKLIST = 1

SHM = "/dev/shm"
VDEV_BYTES = 256 * 1024 * 1024  # SPA_MINDEVSIZE is 64 MiB (include/sys/fs/zfs.h:990)
MIN_SHM_FREE = 1024**3
MIN_MEM_AVAILABLE = 1536 * 1024**2
DIR_PREFIX = "zark-selftest-"


class Fail(Exception):
    """A check saw something other than what zark relies on."""

    def __init__(self, expected: str, came: str):
        super().__init__(f"expected {expected}; came {came}")
        self.expected = expected
        self.came = came


class Skip(Exception):
    """A check that cannot run here (a missing tool); the text says why."""


class SetupError(Exception):
    """The bench could not be built; nothing was checked."""


@dataclass(frozen=True)
class Check:  # pylint: disable=too-many-instance-attributes
    """One check: what it certifies is in its function's docstring."""

    cid: str
    rev: int
    level: int
    title: str
    depends: tuple[str, ...]
    fake: bool
    fn: Callable[["Bench"], None]

    @property
    def key(self) -> str:
        """``<ID>.<revision>``, what certificates cover."""
        return f"{self.cid}.{self.rev}"


@dataclass
class Result:
    """What one check gave."""

    key: str
    result: str  # PASS, FAIL or SKIP
    reason: str = ""
    expected: str = ""
    came: str = ""
    depends: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """The report's form: only the fields that say something."""
        out: dict[str, Any] = {"id": self.key, "result": self.result}
        if self.reason:
            out["reason"] = self.reason
        if self.result == "FAIL":
            out["expected"] = self.expected
            out["came"] = self.came
            out["depends"] = list(self.depends)
        return out


CHECKS: list[Check] = []


def check(  # pylint: disable=too-many-arguments
    cid: str,
    rev: int,
    level: int,
    title: str,
    *,
    depends: tuple[str, ...],
    fake: bool = False,
) -> Callable[[Callable[["Bench"], None]], Callable[["Bench"], None]]:
    """Register a check function under ``cid`` and ``rev``."""

    def wrap(fn: Callable[["Bench"], None]) -> Callable[["Bench"], None]:
        if any(c.cid == cid for c in CHECKS):
            raise ValueError(f"check {cid} registered twice")
        CHECKS.append(Check(cid, rev, level, title, depends, fake, fn))
        return fn

    return wrap


# The modules whose checks register themselves when imported, in run order.
CHECK_MODULES = ("lib.selftest_l1", "lib.selftest_host", "lib.selftest_l2")


def all_checks() -> list[Check]:
    """Every check, level 1 first, in the order they run."""
    # By name: the check modules import this one for the decorator, and
    # lib.selftest_host imports lib.alerts, which reaches lib.certificates.
    for name in CHECK_MODULES:
        _ = importlib.import_module(name)
    return sorted(CHECKS, key=lambda c: c.level)


def expect(ok: bool, expected: str, came: str) -> None:
    """Raise :class:`Fail` unless ``ok``."""
    if not ok:
        raise Fail(expected, came)


def _short(r: sh.RunResult) -> str:
    text = (r.stderr.strip() or r.stdout.strip()).replace("\n", " / ")
    return f"exit {r.returncode}" + (f": {text[:300]}" if text else "")


class Bench:  # pylint: disable=too-many-public-methods,too-many-instance-attributes
    """Where the level-1 checks run: a directory, its pools, a command runner.

    Pool ``a`` stands for origin (encrypted at its root, as Ubuntu's rpool),
    pool ``b`` for a backup drive (unencrypted, ``-m none``, as prepare
    makes it); both live for the whole run. A check that needs a pool of its
    own (an export, a read-only import, a destroy) makes ``c`` and drops it.
    Each check works under datasets named after its ID, so checks do not see
    each other's state.
    """

    def __init__(self, log: Log, cleanup: Cleanup, root: str = SHM):
        self.log = log
        self.cleanup = cleanup
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.dir = f"{root}/{DIR_PREFIX}{stamp}-{secrets.token_hex(2)}"
        self.tag = secrets.token_hex(4)
        self.guids: dict[str, str] = {}  # pool -> GUID read right after its creation
        self.key = f"{self.dir}/key"
        self.mnt = f"{self.dir}/mnt"
        self._torn_down = False

    # ── names ────────────────────────────────────────────────────────────

    def pool(self, letter: str) -> str:
        """The run's pool for ``letter`` (``zst<8 hex><letter>``)."""
        return f"zst{self.tag}{letter}"

    @property
    def a(self) -> str:  # pylint: disable=invalid-name
        """The origin pool."""
        return self.pool("a")

    @property
    def b(self) -> str:  # pylint: disable=invalid-name
        """The drive pool."""
        return self.pool("b")

    def vdev(self, name: str) -> str:
        """The file backing pool ``name``."""
        return f"{self.dir}/{name}.img"

    def masks(self) -> dict[str, str]:
        """What the report writes instead of this run's names."""
        out = {self.dir: "<run>", self.pool("a"): "<pool-a>", self.pool("b"): "<pool-b>"}
        out[self.pool("c")] = "<pool-c>"
        return out

    # ── commands ─────────────────────────────────────────────────────────

    def run(self, cmd: str, stdin: str | None = None) -> sh.RunResult:
        """Run a command, logged."""
        return sh.run(cmd, log=self.log, input=stdin)

    def ok(self, cmd: str, stdin: str | None = None) -> sh.RunResult:
        """Run a command that must succeed."""
        r = self.run(cmd, stdin)
        expect(r.ok, f"`{cmd}` to succeed", _short(r))
        return r

    def refused(self, cmd: str, *texts: str) -> sh.RunResult:
        """Run a command that must fail, saying each of ``texts``."""
        r = self.run(cmd)
        said = r.stdout + r.stderr
        expect(not r.ok, f"`{cmd}` to be refused", "it succeeded")
        missing = [t for t in texts if t not in said]
        expect(not missing, f"`{cmd}` refused saying {missing!r}", _short(r))
        return r

    def pipe(self, send: str, recv: str) -> sh.RunResult:
        """``send | recv``, logged."""
        return sh.run_pipe(send, recv, log=self.log)

    def pipe_ok(self, send: str, recv: str) -> sh.RunResult:
        """A transfer that must succeed."""
        r = self.pipe(send, recv)
        expect(r.ok, f"`{send} | {recv}` to succeed", _short(r))
        return r

    def pipe_refused(self, send: str, recv: str, *texts: str) -> sh.RunResult:
        """A transfer that must fail, saying each of ``texts``."""
        r = self.pipe(send, recv)
        said = r.stdout + r.stderr
        expect(not r.ok, f"`{send} | {recv}` to be refused", "it succeeded")
        missing = [t for t in texts if t not in said]
        expect(not missing, f"`{send} | {recv}` refused saying {missing!r}", _short(r))
        return r

    def value(self, prop: str, target: str) -> str:
        """``zfs get -Hp -o value <prop> <target>``."""
        return self.ok(f"zfs get -Hp -o value {prop} {shlex.quote(target)}").output

    def source(self, prop: str, target: str) -> str:
        """Where ``prop`` of ``target`` comes from (``local``, ``default``...)."""
        return self.ok(f"zfs get -H -o source {prop} {shlex.quote(target)}").output

    def pool_value(self, prop: str, pool: str) -> str:
        """``zpool get -Hp -o value <prop> <pool>``."""
        return self.ok(f"zpool get -Hp -o value {prop} {pool}").output

    def snapshots(self, ds: str) -> list[tuple[str, str, str]]:
        """``(name, guid, createtxg)`` of ``ds``'s own snapshots, oldest first."""
        r = self.ok(f"zfs list -Hp -t snapshot -o name,guid,createtxg -r {ds}")
        rows = [line.split("\t")[:3] for line in r.lines]
        own = [(n.split("@", 1)[1], g, t) for n, g, t in rows if n.split("@", 1)[0] == ds]
        return sorted(own, key=lambda row: int(row[2]))

    def snapshot_names(self, ds: str) -> list[str]:
        """Names of ``ds``'s own snapshots, oldest first."""
        return [n for n, _, _ in self.snapshots(ds)]

    def fs(self, name: str, *props: str) -> str:
        """``zfs create -u [-o p=v]... name``; returns ``name``."""
        opts = "".join(f" -o {p}" for p in props)
        _ = self.ok(f"zfs create -u{opts} {name}")
        return name

    def write(self, ds: str, kib: int = 256) -> None:
        """Write ``kib`` KiB of random data to ``ds`` (a filesystem), so its
        ``written`` grows; mounted in a namespace of its own and unmounted
        before it ends, then the pool synced."""
        mnt = shlex.quote(self.mnt)
        script = (
            f"mount -t zfs -o zfsutil {shlex.quote(ds)} {mnt} && "
            + f"head -c {kib * 1024} /dev/urandom > {mnt}/f.$$ ; rc=$? ; "
            + f"umount {mnt} && exit $rc"
        )
        _ = self.ok(f"unshare -m --propagation private sh -c {shlex.quote(script)}")
        _ = self.ok(f"zpool sync {ds.split('/')[0]}")

    def cut(self, send: str, recv: str, keep: int) -> sh.RunResult:
        """``send`` cut after ``keep`` bytes into ``recv`` (a ``receive -s``):
        the receive fails and keeps its partial state."""
        r = self.pipe(f"{send} | head -c {keep}", recv)
        expect(not r.ok, f"the stream cut at {keep} bytes to fail", "it was received whole")
        return r

    def in_namespace(self, script: str) -> sh.RunResult:
        """Run ``script`` in a mount namespace of its own (private propagation)."""
        return self.run(f"unshare -m --propagation private sh -c {shlex.quote(script)}")

    # ── pools ────────────────────────────────────────────────────────────

    def setup(self) -> None:
        """Make the directory, its key and pools a and b."""
        self._space()
        if not sh.run("zfs version").ok:
            raise SetupError("ZFS is not usable here: `zfs version` failed")
        taken = set(sh.run("zpool list -H -o name").lines)
        if {self.a, self.b, self.pool("c")} & taken:
            raise SetupError("a pool with this run's random name exists: run again")
        self.cleanup.track_action(self.teardown)
        os.makedirs(self.dir, mode=0o700)
        os.makedirs(self.mnt, mode=0o700)
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _ = os.write(fd, secrets.token_bytes(32))
        finally:
            os.close(fd)
        _ = self.make_pool(
            "a",
            "-O encryption=aes-256-gcm -O keyformat=raw "
            + f"-O keylocation=file://{self.key} -O dnodesize=auto",
        )
        _ = self.make_pool("b")

    def _space(self) -> None:
        st = os.statvfs(SHM)
        free = st.f_bavail * st.f_frsize
        if free < MIN_SHM_FREE:
            raise SetupError(f"{SHM} has {free // 1024**2} MiB free; the self-test needs 1024")
        avail = _mem_available()
        if avail is not None and avail < MIN_MEM_AVAILABLE:
            raise SetupError(
                f"{avail // 1024**2} MiB of memory available; the self-test needs 1536"
            )

    def make_pool(self, letter: str, options: str = "", altroot: bool = True) -> str:
        """Create pool ``letter`` on its own sparse file, as prepare creates a
        drive (``-o ashift=12 ... -m none -R``), plus ``options``."""
        name = self.pool(letter)
        vdev = self.vdev(name)
        with open(vdev, "wb") as f:
            f.truncate(VDEV_BYTES)
        root = f"-R {BACKUP_ALTROOT_BASE}/{name} " if altroot else ""
        cmd = (
            "zpool create -o ashift=12 -O atime=off -O xattr=sa -O normalization=formD "
            + f"{options} -m none {root}{name} {vdev}"
        )
        _ = self.ok(" ".join(cmd.split()))
        self.guids[name] = self.pool_value("guid", name)
        return name

    def drop_pool(self, name: str) -> None:
        """Destroy one of the run's pools and remove its file."""
        if name in self.guids:
            self._destroy_own(name, self.guids.pop(name))

    def _destroy_own(self, name: str, guid: str) -> None:
        """Destroy ``name`` if it is the pool this run created (same GUID),
        importing it read-write from the run's directory if needed."""
        vdev = self.vdev(name)
        imported = sh.run(f"zpool get -Hp -o value guid {name}", log=self.log)
        if imported.ok and imported.output != guid:
            self.log.warn(f"Pool {name} is not the one this run created: left alone")
            return
        here = imported.ok
        if here and sh.run(f"zpool get -H -o value readonly {name}").output == "on":
            # A read-only destroy writes nothing to the labels (M4aR-1).
            here = not sh.run(f"zpool export {name}", log=self.log).ok
        if not here and Path(vdev).exists():
            _ = sh.run(
                f"zpool import -d {vdev} -N -R {BACKUP_ALTROOT_BASE}/{name} {guid}", log=self.log
            )
        if sh.run(f"zpool get -Hp -o value guid {name}").output == guid:
            if not sh.run(f"zpool destroy {name}", log=self.log).ok:
                _ = sh.run(f"zpool destroy -f {name}", log=self.log)
        if sh.run(f"zpool list -H -o name {name}").ok:
            self.log.warn(f"Pool {name} could not be destroyed: export it, then remove {vdev}")
            return
        Path(vdev).unlink(missing_ok=True)
        remove_empty_dirs(f"{BACKUP_ALTROOT_BASE}/{name}")

    def teardown(self) -> None:
        """Destroy every pool this run created, then its directory."""
        if self._torn_down:
            return
        self._torn_down = True
        for name, guid in list(self.guids.items()):
            self._destroy_own(name, guid)
            self.guids.pop(name, None)
        if not os.path.isdir(self.dir):
            return
        listing = sh.run("zpool list -vHP")
        if self.dir in listing.stdout:
            self.log.warn(f"An imported pool still uses {self.dir}: left in place")
            return
        _ = sh.run(f"rm -rf {shlex.quote(self.dir)}", log=self.log)
        self.cleanup.untrack_action(self.teardown)


def remove_empty_dirs(top: str) -> None:
    """Remove ``top`` and the directories below it that are empty (mount
    points a namespace mount made); a file or a mount anywhere keeps its
    directory."""
    for root, _dirs, _files in os.walk(top, topdown=False):
        try:
            os.rmdir(root)
        except OSError:
            pass


def _mem_available() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


@dataclass
class RunState:
    """What a run has done so far, for the record when a signal stops it."""

    results: list[Result] = field(default_factory=list)
    current: str = ""


def run_checks(
    checks: list[Check], bench: "Bench", log: Log, state: RunState | None = None
) -> list[Result]:
    """Run ``checks`` on ``bench`` in order; one line each."""
    state = state if state is not None else RunState()
    total = len(checks)
    for i, c in enumerate(checks, 1):
        state.current = c.key
        log.step(i, total, f"{c.key} {c.title}")
        res = run_one(c, bench)
        state.results.append(res)
        if res.result == "PASS":
            log.ok(f"{c.key} PASS")
        elif res.result == "SKIP":
            log.warn(f"{c.key} SKIP: {res.reason}")
        else:
            log.error(f"{c.key} FAIL")
            log.error(f"  expected: {res.expected}")
            log.error(f"  came:     {res.came}")
            log.error(f"  zark relies on it for: {'; '.join(c.depends)}")
    state.current = ""
    return state.results


def run_one(c: Check, bench: "Bench") -> Result:
    """One check's result; an unexpected exception is a FAIL with its text."""
    try:
        c.fn(bench)
    except Skip as e:
        return Result(c.key, "SKIP", reason=str(e))
    except Fail as e:
        return Result(c.key, "FAIL", expected=e.expected, came=e.came, depends=c.depends)
    except Exception as e:  # pylint: disable=broad-exception-caught
        return Result(
            c.key,
            "FAIL",
            expected="the check to run",
            came=f"{type(e).__name__}: {e}",
            depends=c.depends,
        )
    return Result(c.key, "PASS")


def masked(text: str, masks: dict[str, str]) -> str:
    """``text`` with each key of ``masks`` replaced by its value."""
    for k, v in sorted(masks.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(k, v)
    return text


def dumps(obj: object) -> str:
    """The JSON zark writes for reports and records (stable, ASCII)."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
