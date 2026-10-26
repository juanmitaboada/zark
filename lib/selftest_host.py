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
Level-1 self-test checks on the host's own tools: mounts and namespaces,
process signals and locks, systemd, sanoid (M4s-2).

Like :mod:`lib.selftest_l1`, each docstring names the source of the
behaviour at the version eli runs.
"""

import fcntl
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from lib import alerts, live_pools, progress, statusdirs
from lib.selftest import Bench, Fail, Skip, check, exit_status, expect, text_of
from lib.selftest_l1 import reimport
from lib.zfs import BACKUP_ALTROOT_BASE


def _q(s: str) -> str:
    return shlex.quote(s)


SANOID_DEFAULTS = ("/usr/share/sanoid/sanoid.defaults.conf", "/etc/sanoid/sanoid.defaults.conf")


# ── mounts ───────────────────────────────────────────────────────────────


@check(
    "MNT-01",
    1,
    1,
    "explicit ZFS mounts: snapshots anywhere, zfsutil, read-only pools",
    depends=("zark mount (commands/mount.py:121)", "backup_layout.root_is_empty"),
)
def mnt_01(b: Bench) -> None:
    """In its own namespace: ``mount -t zfs -o ro <ds>@<snap> <dir>`` mounts a
    snapshot anywhere; ``mount -t zfs -o zfsutil <ds> <dir>`` mounts a
    non-legacy dataset; on a read-only pool the mount is read-only. OpenZFS
    cmd/mount_zfs.c:264-281, :300-330; module/os/linux/zfs/zfs_vfsops.c:461-464,
    :1568-1572."""
    c = b.make_pool("c")
    try:
        x = b.fs(f"{c}/x")
        b.write(x, 16)
        _ = b.ok(f"zfs snapshot {x}@s")
        _ = b.ok(f"zpool export {c}")
        reimport(b, c, "-o readonly=on")
        m = b.mnt
        script = (
            f"mount -t zfs -o ro {x}@s {m} && ls {m} && findmnt -rn -o OPTIONS {m} && "
            + f"umount {m} && mount -t zfs -o zfsutil {x} {m} && "
            + f"findmnt -rn -o OPTIONS {m} && umount {m}"
        )
        r = b.in_namespace(script)
        expect(r.ok, "both mounts", exit_status(r), text_of(r))
        files = [ln for ln in r.lines if ln.startswith("f.")]
        expect(len(files) == 1, "the file in the snapshot", f"{len(files)} file(s)", text_of(r))
        opts = [ln for ln in r.lines if "," in ln or ln in ("ro", "rw")]
        ro = [o.split(",")[0] for o in opts]
        expect(ro == ["ro", "ro"], "both mounts read-only", " | ".join(opts))
    finally:
        b.drop_pool(c)


_READONLY_MOUNTS = r"""
A={a}
zfs mount {p} && zfs mount {k1} || exit 90
echo "OPTS $(findmnt -rn -o OPTIONS $A/p | cut -d, -f1)"
echo "K1 $(findmnt -rn -o TARGET $A/p/k1 | wc -l)"
zfs mount {k2} 2>&1
echo "RC $?"
echo "K2 $(findmnt -rn -o TARGET $A/p/k2 | wc -l)"
umount -R $A/p
"""


@check(
    "MNT-04",
    1,
    1,
    "a read-only import mounts with zfs mount, and cannot create a mountpoint",
    depends=("mount local read-only (M4c-21)", "lib/mount.py: mount_system_pools"),
)
def mnt_04(b: Bench) -> None:
    """In its own namespace: on a pool imported ``readonly=on``, ``zfs mount``
    mounts a dataset read-only, and fails for one whose mountpoint directory
    does not exist in its read-only parent (it cannot be created). OpenZFS
    lib/libzfs/libzfs_mount.c:383-388, :464-474; module/os/linux/zfs/
    zfs_vfsops.c:461-463, :936-945."""
    c = b.make_pool("c")
    try:
        p = b.fs(f"{c}/p", "mountpoint=/p")
        # Created -u: k2's mountpoint directory is never made.
        k1, k2 = b.fs(f"{p}/k1"), b.fs(f"{p}/k2")
        a = f"{BACKUP_ALTROOT_BASE}/{c}"
        # Read-write once, in a namespace: p and its k1 directory exist after.
        rw = b.in_namespace(f"zfs mount {p} && zfs mount {k1} && umount {a}/p/k1 && umount {a}/p")
        expect(rw.ok, "the read-write mounts", exit_status(rw), text_of(rw))
        _ = b.ok(f"zpool export {c}")
        reimport(b, c, "-o readonly=on")
        r = b.in_namespace(_READONLY_MOUNTS.format(a=_q(a), p=p, k1=k1, k2=k2))
        vals = dict(ln.split(" ", 1) for ln in r.lines if ln.startswith(("OPTS", "K1", "RC", "K2")))
        expect(
            vals.get("OPTS") == "ro", "the read-only pool mounted read-only", str(vals), text_of(r)
        )
        expect(vals.get("K1") == "1", f"{k1} mounted", str(vals), text_of(r))
        expect(vals.get("RC", "0") != "0" and vals.get("K2") == "0", f"{k2} refused", str(vals))
    finally:
        b.drop_pool(c)


_PROPAGATION = r"""
W={w}
mount -t tmpfs zarkhost $W || exit 90
mount --make-shared $W
mkdir -p $W/tree $W/host/run
unshare --mount --propagation slave sleep 120 &
P=$!
i=0
while [ "$(readlink /proc/$P/ns/mnt)" = "$(readlink /proc/$$/ns/mnt)" ]; do
  i=$((i+1)); [ $i -lt 500 ] || exit 91; sleep 0.01
done
mount -t tmpfs zarkds $W/tree
mkdir $W/tree/run
mount --bind $W/host/run $W/tree/run
mkdir $W/host/run/user
mount -t tmpfs hostuser $W/host/run/user
echo "SEEN $(findmnt --task $P -rn -o TARGET | grep -c "^$W/tree")"
mount --make-rprivate $W/tree/run
umount -R $W/tree/run
umount -R $W/tree
echo "LEFT $(findmnt --task $P -rn -o TARGET | grep -c "^$W/tree")"
nsenter --mount=/proc/$P/ns/mnt umount -R $W/tree 2>/dev/null
echo "AFTER $(findmnt --task $P -rn -o TARGET | grep -c "^$W/tree")"
echo "HOST $(findmnt -rn -o TARGET | grep -c "^$W/host/run/user$")"
kill $P
umount -R $W
"""


@check(
    "MNT-02",
    1,
    1,
    "copies in another namespace: findmnt --task, nsenter umount -R",
    depends=("release_elsewhere and _namespaces (commands/umount.py:162, :125)", "clean (M4b-3)"),
)
def mnt_02(b: Bench) -> None:
    """Two namespaces sharing propagation: a copy with a mount below it
    survives the propagated unmount; ``findmnt --task <pid>`` lists it;
    ``nsenter --mount=/proc/<pid>/ns/mnt umount -R <top>`` releases it; the
    host's own mounts stay. Linux fs/pnode.c:542-595, :658-690,
    fs/namespace.c:6425-6467; util-linux misc-utils/findmnt.c:1033-1039,
    :1974-1979, sys-utils/nsenter.c:157-205, :251-285, :570-571."""
    r = b.in_namespace(_PROPAGATION.format(w=_q(b.mnt)))
    seen = dict(ln.split(" ", 1) for ln in r.lines if " " in ln)
    marks = f"SEEN {seen.get('SEEN', '?')}, AFTER {seen.get('AFTER', '?')}"
    marks += f", HOST {seen.get('HOST', '?')}"
    out = text_of(r)
    expect(seen.get("SEEN", "0") != "0", "the tree listed by findmnt --task", marks, out)
    expect(seen.get("AFTER") == "0", "no copy left after nsenter umount -R", marks, out)
    expect(seen.get("HOST") == "1", "the host's own mount kept", marks, out)


_UMOUNT_TREE = r"""
M={m}
mount -t tmpfs zarkt $M || exit 90
mkdir -p $M/a "$M/sp ace" $M/src/sub $M/b
mount -t tmpfs zarka $M/a
mount -t tmpfs zarks "$M/sp ace"
mount --bind $M/src/sub $M/b
echo "LIST"
findmnt -rn -o TARGET,FSTYPE,SOURCE
echo "END"
(cd $M/a && sleep 30) &
P=$!
sleep 0.3
umount -R $M 2>&1
echo "RC $?"
echo "ROOT $(findmnt -rn -o TARGET | grep -c "^$M$")"
echo "BUSY $(findmnt -rn -o TARGET | grep -c "^$M/a$")"
kill $P
wait $P 2>/dev/null
umount -R $M
"""


@check(
    "MNT-03",
    1,
    1,
    "umount -R stops at a busy child; findmnt -r escapes and bind roots",
    depends=("_unmount_tree (commands/umount.py:85)", "clean", "W-2's tell by SOURCE"),
)
def mnt_03(b: Bench) -> None:
    """``umount -R`` unmounts children first and stops at a busy child,
    leaving the parent (exit 32; its message is not read: util-linux
    translates it, po/es.po:8303-8304); ``findmnt -rn -o
    TARGET,FSTYPE,SOURCE`` prints a bind of a subdirectory as
    ``<source>[/<sub>]`` and a space as ``\\x20``. util-linux
    sys-utils/umount.c:304-344, :158-190; misc-utils/findmnt.c:642-662;
    libsmartcols/src/print.c:618-622; include/carefulputc.h:106-121."""
    m = b.mnt
    r = b.in_namespace(_UMOUNT_TREE.format(m=_q(m)))
    out = r.stdout
    listing = out.split("LIST\n", 1)[-1].split("END\n", 1)[0].splitlines()
    rows = {ln.split(" ")[0]: ln.split(" ") for ln in listing}
    expect(
        f"{m}/sp\\x20ace" in rows,
        f"{m}/sp\\x20ace listed",
        f"not among {len(listing)} line(s)",
        text_of(r),
    )
    bind = rows.get(f"{m}/b", [])
    expect(
        len(bind) == 3 and bind[2] == "zarkt[/src/sub]",
        "the bind's SOURCE zarkt[/src/sub]",
        " ".join(bind) or "not listed",
    )
    marks = ("RC ", "ROOT ", "BUSY ")
    vals = dict(ln.split(" ", 1) for ln in out.splitlines() if ln.startswith(marks))
    expect(vals.get("RC") == "32", "umount -R exit 32", vals.get("RC", "?"))
    expect(
        vals.get("ROOT") == "1" and vals.get("BUSY") == "1", "parent and busy child kept", str(vals)
    )


# ── processes ────────────────────────────────────────────────────────────

_IGNORE_CHILD = r"""
import signal, subprocess, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
out = subprocess.run(["grep", "^SigIgn", "/proc/self/status"], capture_output=True, text=True)
out = out.stdout
print(out.split()[1])
p = subprocess.Popen(["sh", "-c", "kill -TERM $$; echo alive"], stdout=subprocess.PIPE, text=True)
print(p.communicate()[0].strip())
"""


@check(
    "PRC-01",
    1,
    1,
    "an ignored signal stays ignored across exec; SIGPIPE back to default",
    depends=("every cleanup runs its zpool export with the exit signals ignored (M4b-1, M4bF-5)",),
)
def prc_01(_b: Bench) -> None:
    """A signal set to ``SIG_IGN`` stays ignored in a child started by
    ``subprocess`` after ``exec``; the child has SIGPIPE back at ``SIG_DFL``;
    a signal sent to a process that ignores it is discarded. Linux
    kernel/signal.c:519-534, :77-121, :935; CPython 3.14.4
    Modules/_posixsubprocess.c:757-759, Modules/signalmodule.c:1954-1966,
    :518-538."""
    r = subprocess.run(
        [sys.executable, "-I", "-c", _IGNORE_CHILD], capture_output=True, text=True, check=False
    )
    lines = r.stdout.split()
    expect(
        len(lines) == 2,
        "two lines from the child",
        f"{len(lines)}",
        (r.stdout + r.stderr).strip(),
    )
    mask = int(lines[0], 16)
    expect(bool(mask & (1 << (signal.SIGTERM - 1))), "SIGTERM ignored after exec", lines[0])
    expect(not mask & (1 << (signal.SIGPIPE - 1)), "SIGPIPE not ignored after exec", lines[0])
    expect(lines[1] == "alive", "a SIGTERM to the ignoring child discarded", lines[1])


@check(
    "PRC-02",
    1,
    1,
    "a process group of its own reaches the command behind sh -c",
    depends=("signals.stop_children (lib/signals.py:242)", "sh.run and sh.run_pipe"),
)
def prc_02(_b: Bench) -> None:
    """``Popen(process_group=0)`` puts ``sh -c "sleep 'N'"`` in its own
    group with the command it runs; ``killpg`` stops every member. CPython
    3.14.4 Lib/subprocess.py:825, :908-909, Modules/_posixsubprocess.c:775-780;
    Linux kernel/signal.c:1429-1446, :1572-1601."""
    p = subprocess.Popen(["sh", "-c", "sleep '30'; :"], process_group=0)  # pylint: disable=consider-using-with
    try:
        members: list[str] = []
        for _ in range(200):
            members = _group_members(p.pid)
            if "sleep" in members:
                break
            time.sleep(0.01)
        expect("sleep" in members, "sleep in the shell's group", ", ".join(members))
        os.killpg(p.pid, signal.SIGTERM)
        _ = p.wait(timeout=10)
        deadline = time.monotonic() + 5
        while _group_members(p.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        left = _group_members(p.pid)
        expect(not left, "the whole group gone", ", ".join(left))
    finally:
        if p.poll() is None:
            p.kill()
            _ = p.wait()


def _group_members(pgid: int) -> list[str]:
    out: list[str] = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        comm = stat[stat.index("(") + 1 : stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2 :].split()
        if len(fields) > 2 and fields[0] != "Z" and int(fields[2]) == pgid:
            out.append(comm)
    return out


@check(
    "PRC-03",
    1,
    1,
    "with SIGPIPE ignored a dead reader raises BrokenPipeError",
    depends=("a dead stdout stops the command with its cleanup (M4b-2, signals.guard_output)",),
)
def prc_03(_b: Bench) -> None:
    """With SIGPIPE ignored, a write to a pipe whose reader is gone raises
    ``BrokenPipeError`` (EPIPE). Linux fs/pipe.c:460-465; CPython 3.14.4
    Modules/signalmodule.c:1922-1939, Objects/exceptions.c:4479."""
    expect(
        signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN,
        "SIGPIPE ignored in zark",
        str(signal.getsignal(signal.SIGPIPE)),
    )
    rfd, wfd = os.pipe()
    os.close(rfd)
    try:
        _ = os.write(wfd, b"x")
    except BrokenPipeError:
        return
    finally:
        os.close(wfd)
    raise Fail("BrokenPipeError", "the write succeeded")


_LOCK_HOLDER = r"""
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
time.sleep(60)
"""


@check(
    "PRC-04",
    1,
    1,
    "flock: exclusive, gone with a killed holder, not kept by children",
    depends=(
        "zark's lock (lib/lock.py:66)",
        "purge's disk lock (_held_from_udev)",
        "status.json's status.lock (lib/status_file.py)",
    ),
)
def prc_04(b: Bench) -> None:
    """``flock(LOCK_EX|LOCK_NB)`` from a second open of the same file fails
    with EWOULDBLOCK; the lock goes when its holder is SIGKILLed; a child
    started by ``subprocess`` does not keep it. Linux fs/locks.c:2198-2244,
    :1176-1183, :2722-2740, :2767-2789, fs/file_table.c:461; CPython 3.14.4
    Modules/posixmodule.c:11179, Modules/_posixsubprocess.c:817-821."""
    path = f"{b.dir}/lockfile"
    Path(path).touch(mode=0o600)
    fd1, fd2 = os.open(path, os.O_RDWR), os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise Fail("EWOULDBLOCK from a second open", "it locked")
        except BlockingIOError:
            pass
        child = subprocess.Popen(["sleep", "30"])  # pylint: disable=consider-using-with
        try:
            os.close(fd1)
            fd1 = -1
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Fail("the lock free once its holder closed it", "a child kept it") from e
        finally:
            child.kill()
            _ = child.wait()
        fcntl.flock(fd2, fcntl.LOCK_UN)
        holder = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-I", "-c", _LOCK_HOLDER, path], stdout=subprocess.PIPE, text=True
        )
        assert holder.stdout is not None
        line = holder.stdout.readline().strip()
        expect(line == "held", "the holder to take the lock", line)
        holder.kill()
        _ = holder.wait()
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Fail("the lock free after SIGKILL", "still held") from e
    finally:
        for fd in (fd1, fd2):
            if fd >= 0:
                os.close(fd)


@check(
    "PRC-05",
    1,
    1,
    "/proc/<pid>/stat field 22 after the last ) tells a process apart; boot_id's form",
    depends=(
        "progress.json's pid_start_ticks and boot_id (lib/progress.py start_ticks)",
        "the orphan rules readers apply (docs/STATUS-FILES.md, rules 3 to 5)",
    ),
)
def prc_05(b: Bench) -> None:
    """A child whose name holds ``) (``: zark's reader of field 22
    (``starttime``, counted after the last ``)``) gives the same value twice,
    and that value, in clock ticks, is the child's start, ``/proc/uptime``
    minus its ``etimes`` from ``ps``, within a second and a half;
    ``/proc/sys/kernel/random/boot_id`` has the schema's form. Linux
    fs/proc/array.c:466, :580, :585 (the name, unescaped), :606."""
    sleep = shutil.which("sleep")
    if not sleep:
        raise Skip("sleep is not installed")
    named = Path(b.dir, "z) (x")
    named.symlink_to(sleep)  # the name a process gets is the one it was executed by
    child = subprocess.Popen([str(named), "30"])  # pylint: disable=consider-using-with
    try:
        time.sleep(0.3)
        line = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii", errors="replace")
        expect(") (" in line[: line.rindex(")") + 1], "a name holding ') ('", "it does not")
        first, second = progress.start_ticks(child.pid), progress.start_ticks(child.pid)
        expect(first == second, "the same starttime twice", f"{first} then {second}")
        r = b.ok(f"ps -o etimes= -p {child.pid}")
        uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        started = uptime - int(r.output.strip())
        hz = os.sysconf("SC_CLK_TCK")
        expect(
            abs(first / hz - started) <= 1.5,
            f"starttime/{hz} within 1.5 s of uptime - etimes",
            f"{first / hz:.1f} against {started:.1f}",
        )
    finally:
        child.kill()
        _ = child.wait()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    expect(
        re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", boot)
        is not None,
        "boot_id in the schema's form",
        f"{len(boot)} characters",
    )


# ── readers ──────────────────────────────────────────────────────────────

# setpriv(1) from util-linux: the commands below run as nobody, with no
# supplementary group, as a user who runs zark monitor would.
_NOBODY = "setpriv --reuid=65534 --regid=65534 --clear-groups --"


def _rows(text: str, pools: tuple[str, ...]) -> list[str]:
    return sorted(li for li in text.splitlines() if li.split("\t", 1)[0] in pools)


@check(
    "RDR-01",
    1,
    1,
    "a user without privileges reads what zark monitor reads",
    depends=(
        "zark monitor runs as any user (commands/monitor.py, M8-3)",
        "lib.live_pools listings",
        "lib.status_reader.process_alive",
    ),
)
def rdr_01(b: Bench) -> None:
    """As nobody: ``zpool list`` and ``zfs list`` with the monitor's options
    give root's rows for the bench's pools (``/dev/zfs`` 0666, OpenZFS 2.4.1
    udev/rules.d/90-zfs.rules.in:10; list ioctls ``zfs_secpolicy_none`` and
    ``zfs_secpolicy_read``, module/zfs/zfs_ioctl.c:379-398, :7645-7646,
    :7687-7688, :7708-7709, :7734-7742); ``boot_id`` is readable (Linux 7.0
    drivers/char/random.c:1694-1696); ``/proc/1/stat``'s ``starttime``, a
    root process's, equals root's read (fs/proc/base.c:3338,
    fs/proc/array.c:580, :606); ``/dev/disk/by-id`` lists."""
    if not shutil.which("setpriv"):
        raise Skip("setpriv (util-linux) is not installed")
    pools = (b.a, b.b)
    zpool = shlex.join(live_pools.zpool_argv())
    zfs = shlex.join([*live_pools.zfs_head(), b.a])
    for what, cmd, pick in (
        ("zpool list", zpool, lambda t: _rows(t, pools)),
        ("zfs list", zfs, lambda t: t.splitlines()),
    ):
        mine, theirs = b.ok(cmd), b.run(f"{_NOBODY} {cmd}")
        expect(theirs.ok, f"{what} to work as nobody", exit_status(theirs), text_of(theirs))
        expect(
            bool(pick(mine.stdout)) and pick(theirs.stdout) == pick(mine.stdout),
            f"{what} as nobody to give root's rows",
            f"{len(pick(theirs.stdout))} rows, {len(pick(mine.stdout))} as root",
            text_of(theirs),
        )
    for what, path in (("boot_id", "/proc/sys/kernel/random/boot_id"), ("stat", "/proc/1/stat")):
        mine, theirs = b.ok(f"cat {path}"), b.run(f"{_NOBODY} cat {path}")
        expect(theirs.ok, f"{what} readable as nobody", exit_status(theirs), text_of(theirs))
        if what == "stat":
            got = theirs.stdout[theirs.stdout.rfind(")") + 1 :].split()[19:20]
            want = mine.stdout[mine.stdout.rfind(")") + 1 :].split()[19:20]
        else:
            got, want = [theirs.stdout.strip()], [mine.stdout.strip()]
        expect(bool(want) and got == want, f"{what} as nobody equal to root's", "different")
    if Path("/dev/disk/by-id").is_dir():
        r = b.run(f"{_NOBODY} ls /dev/disk/by-id")
        expect(r.ok, "/dev/disk/by-id listable as nobody", exit_status(r), text_of(r))


# ── systemd, sanoid ──────────────────────────────────────────────────────


@check(
    "SYS-01",
    1,
    1,
    "systemd refuses a quote in zark's ExecStart path, accepts a space",
    depends=("alerts.unsafe_char and zark alerts install's refusal (lib/alerts.py:58, M3F-9)",),
)
def sys_01(b: Bench) -> None:
    """``systemd-analyze verify`` of the unit ``zark alerts install`` writes
    (``alerts.service_unit``): with a quote in zark's path it fails
    ("Executable path contains special characters"); with a space it passes.
    systemd src/core/load-fragment.c:987-992; src/basic/string-util.c:1089-1104;
    src/analyze/analyze-verify-util.c:276-375."""
    if not shutil.which("systemd-analyze"):
        raise Skip("systemd-analyze is not installed")
    units: dict[str, str] = {}
    for tag, sub in (("good", "a b"), ("bad", "it's")):
        exe = Path(b.dir, "units", sub, "zark")
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\n", encoding="ascii")
        exe.chmod(0o700)
        unit = Path(b.dir, "units", f"zark-st-{tag}.service")
        unit.write_text(alerts.service_unit(str(exe)), encoding="utf-8")
        units[tag] = str(unit)
    r = b.run(f"systemd-analyze verify {_q(units['good'])}")
    expect(r.ok, "the unit with a space in the path to verify", exit_status(r), text_of(r))
    r = b.run(f"systemd-analyze verify {_q(units['bad'])}")
    expect(
        not r.ok and "special characters" in r.stdout + r.stderr,
        "the quoted path refused for special characters",
        exit_status(r),
        text_of(r),
    )


@check(
    "SYS-02",
    1,
    1,
    "a tmpfiles.d d line makes the directory 0755 under any umask, and fixes it",
    depends=("/run/zark at boot from the package's tmpfiles.d entry (M7-11)",),
)
def sys_02(b: Bench) -> None:
    """``systemd-tmpfiles --create`` of a ``d <path> 0755 root root -``
    line, the form of the package's ``/run/zark`` entry, run under umask
    077: the directory is created 0755, and a 0700 one is set back to 0755.
    systemd v259.5 man/tmpfiles.d.xml:197-201; src/tmpfiles/tmpfiles.c:2089-2114
    (create_directory, then fd_set_perms whether it was made or found),
    :964-1060 (fd_set_perms, fchmod to the line's mode)."""
    exe = shutil.which("systemd-tmpfiles")
    if not exe:
        raise Skip("systemd-tmpfiles is not installed")
    d = Path(b.dir, "tmpd")
    conf = Path(b.dir, "zark-st.conf")
    conf.write_text(f"d {d} {statusdirs.MODE:04o} root root -\n", encoding="ascii")
    cmd = f"sh -c {_q(f'umask 077; {exe} --create {conf}')}"
    for state in ("absent", "0700"):
        if state == "0700":
            d.chmod(0o700)
        _ = b.ok(cmd)
        mode = os.lstat(d).st_mode & 0o7777 if d.is_dir() else -1
        expect(mode == 0o755, f"0755 when {state}", f"{mode:o}" if mode >= 0 else "no directory")


@check(
    "SAN-01",
    1,
    1,
    "sanoid prunes autosnap_* only",
    depends=("zark's points never pruned by sanoid (D14)", "zark's own residue pruning"),
)
def san_01(b: Bench) -> None:
    """``sanoid --prune-snapshots --configdir --cache-dir --run-dir`` with
    ``hourly = 0`` removes a dataset's ``autosnap_*_hourly`` snapshot and
    leaves its ``zark_*`` point, writing only under the given directories.
    sanoid sanoid:21-31, :47-54, :318-372, :886, :907-918, :932-953."""
    exe = shutil.which("sanoid") or ""
    if not exe:
        raise Skip("sanoid is not installed")
    defaults = next((p for p in SANOID_DEFAULTS if Path(p).exists()), "")
    if not defaults:
        raise Skip("sanoid.defaults.conf is not installed")
    x = b.fs(f"{b.a}/san01")
    _ = b.ok(f"zfs snapshot {x}@autosnap_2026-01-01_00:00:00_hourly")
    _ = b.ok(f"zfs snapshot {x}@zark_selftest")
    conf = Path(b.dir, "sanoid")
    conf.mkdir()
    (conf / "sanoid.defaults.conf").write_bytes(Path(defaults).read_bytes())
    (conf / "sanoid.conf").write_text(
        f"[{x}]\n\tuse_template = st\n\n[template_st]\n\tfrequently = 0\n\thourly = 0\n"
        + "\tdaily = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n\tautosnap = no\n"
        + "\tautoprune = yes\n",
        encoding="utf-8",
    )
    time.sleep(1.1)  # sanoid prunes only snapshots older than now (strict <, sanoid:364)
    d = str(conf)
    _ = b.ok(f"{exe} --prune-snapshots --configdir {d} --cache-dir {d}/cache --run-dir {d}/run")
    names = b.snapshot_names(x)
    expect(names == ["zark_selftest"], "only zark_selftest left", ", ".join(names))
