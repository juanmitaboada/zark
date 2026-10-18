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
``zark selftest run --only POOL-01`` through ``zark``, stopped (M4sR-6):

    python tests/selftest_stop_harness.py <workdir> <case>

Must run as root. It moves itself into a new mount namespace and checks that
the namespace changed before doing anything else, then makes every mount
there private and mounts a tmpfs over ``/var/log``, ``/var/lib/zark``,
``/run/zark`` and ``/dev/shm``, so the run writes nothing of the machine's.
A mount point that is missing is made first and named in the result, for
the caller to remove.

``zpool`` and ``zfs`` are scripts in ``<workdir>/fake``, first in PATH: they
keep each pool as a file and append every call to a list. ``zpool create``
of the pool POOL-01 makes itself (``zst<hex>c``) is the running child: it
records its pid and sleeps. Cases:

    sig:INT, sig:TERM, sig:HUP   the signal once that create is running
    deadpipe:                    the reader of zark's output leaves once the
                                 bench is built (both its pools created)

Prints one JSON object: ``rc``, ``created`` (pools whose create finished),
``destroyed``, ``left`` (pools still there), ``benches`` (bench directories
left in /dev/shm), ``child_alive``, ``log`` (zark.log's lines about the stop
and the cleanup), ``rm_after_destroys``, ``report`` (the record's
``partial``, ``stopped`` and ``checks``) and ``made``.
"""

import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

_CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOUNTS = ("/var/log", "/var/lib/zark", "/run/zark", "/dev/shm")
_WAIT_SEC = 30  # for the run to reach the moment of the stop
_END_SEC = 60  # for the stopped run to end

_ZPOOL = r"""#!/bin/bash
S=${STATE:?}
echo "zpool $*" >>"$S/calls"
case "$1 $2" in
  "list -H")
    if [ "$3" = "-o" ] && [ "$4" = "name" ] && [ -n "${5:-}" ]; then
      [ -f "$S/pool.$5" ] && { echo "$5"; exit 0; }
      exit 1
    fi
    for f in "$S"/pool.*; do [ -f "$f" ] && echo "${f##*/pool.}"; done; exit 0;;
  "list -vHP")
    for f in "$S"/pool.*; do
      [ -f "$f" ] || continue
      echo "${f##*/pool.}"; printf '\t%s\n' "$(cut -d' ' -f2 "$f")"
    done; exit 0;;
  "create "*)
    name=${@: -2:1}; vdev=${@: -1}
    case "$name" in
      # POOL-01's own pool: the child running when the stop comes.
      *c) echo $$ >"$S/child.pid"; sleep "${FAKE_SLEEP:?}";;
    esac
    echo "$RANDOM$RANDOM$RANDOM $vdev" >"$S/pool.$name"
    echo "$name" >>"$S/created"; exit 0;;
  "get "*)
    name=${@: -1}; prop=${@: -2:1}
    [ -f "$S/pool.$name" ] || { echo "cannot open '$name': no such pool" >&2; exit 1; }
    case "$prop" in
      guid) cut -d' ' -f1 "$S/pool.$name";; readonly) echo off;; cachefile) echo none;;
      *) echo "-";;
    esac; exit 0;;
  "destroy "*) rm -f "$S/pool.${@: -1}"; exit 0;;
  "export "*|"sync "*|"import "*) exit 0;;
esac
echo "fake zpool: unhandled: $*" >&2; exit 1
"""

_ZFS = r"""#!/bin/bash
S=${STATE:?}
echo "zfs $*" >>"$S/calls"
case "$1" in
  version) printf 'zfs-2.4.1-1ubuntu5.1\nzfs-kmod-2.4.1-1ubuntu5.1\n';;
esac
exit 0
"""


def _check(args: list[str]) -> None:
    _ = subprocess.run(args, check=True, capture_output=True)


def _private_tree() -> list[str]:
    """The namespace and its tmpfs mounts; the mount points it had to make."""
    made = [d for d in _MOUNTS if not os.path.isdir(d)]
    for d in made:
        os.makedirs(d)
    _check(["mount", "--make-rprivate", "/"])
    for d in _MOUNTS:
        _check(["mount", "-t", "tmpfs", "-o", "size=2g", "zarkstop", d])
    return made


def _fakes(work: str) -> tuple[str, str]:
    fake, state = os.path.join(work, "fake"), os.path.join(work, "state")
    os.makedirs(fake)
    os.makedirs(state)
    for name, text in (("zpool", _ZPOOL), ("zfs", _ZFS)):
        path = os.path.join(fake, name)
        with open(path, "w", encoding="ascii") as f:
            _ = f.write(text)
        os.chmod(path, 0o755)
    return fake, state


def _log() -> str:
    try:
        with open("/var/log/zark.log", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _child_running() -> bool:
    """Whether zark.log shows the create of POOL-01's pool, within _WAIT_SEC."""
    end = time.monotonic() + _WAIT_SEC
    while time.monotonic() < end:
        if re.search(r"\$ zpool create .* zst[0-9a-f]{8}c ", _log()):
            return True
        time.sleep(0.1)
    return False


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _alive(pid_file: str) -> bool:
    pids = _read(pid_file).split()
    if not pids:
        return False
    try:
        os.kill(int(pids[0]), 0)
    except ProcessLookupError:
        return False
    return True


def _report() -> dict[str, object] | None:
    files = sorted(glob.glob("/var/lib/zark/selftest/report-*.json"))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        cert = json.load(f)["certificate"]
    return {k: cert.get(k) for k in ("partial", "stopped", "checks")}


def main() -> int:  # pylint: disable=too-many-locals
    """Stop the run as the case says; print what it left."""
    work, case = sys.argv[1], sys.argv[2]
    kind, _, name = case.partition(":")
    before = os.readlink("/proc/self/ns/mnt")
    os.unshare(os.CLONE_NEWNS)
    if os.readlink("/proc/self/ns/mnt") == before:
        print("not in a new mount namespace: refusing to run", file=sys.stderr)
        return 1
    made = _private_tree()
    fake, state = _fakes(work)
    env = dict(os.environ, PATH=f"{fake}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    # The dead pipe is seen at zark's next write, after the child ends.
    env.update(STATE=state, FAKE_SLEEP="2" if kind == "deadpipe" else "20")
    zark = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, os.path.join(_CHECKOUT, "zark"), "selftest", "run", "--only", "POOL-01"],
        cwd=_CHECKOUT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # only what the harness sends reaches it
    )
    assert zark.stdout is not None
    reached = _child_running()  # the bench's two pools are made before it
    if kind == "deadpipe":
        zark.stdout.close()
    else:
        time.sleep(1)
        zark.send_signal(getattr(signal, f"SIG{name}"))
    try:
        if kind != "deadpipe":
            _ = zark.stdout.read()
        rc = zark.wait(timeout=_END_SEC)
    except subprocess.TimeoutExpired:
        zark.kill()
        rc = zark.wait()
    log = _log().splitlines()
    marks = ("Interrupted by", "Output closed", "Cleaning up", "$ rm -rf", "$ zpool destroy")
    kept = [ln for ln in log if any(m in ln for m in marks) or "Traceback" in ln]
    destroys = [i for i, ln in enumerate(log) if "$ zpool destroy" in ln]
    removes = [i for i, ln in enumerate(log) if "$ rm -rf /dev/shm/zark-selftest-" in ln]
    calls = [ln.split() for ln in _read(os.path.join(state, "calls")).splitlines()]
    print(
        json.dumps(
            {
                "rc": rc,
                "reached": reached,
                "created": _read(os.path.join(state, "created")).split(),
                "destroyed": [c[-1] for c in calls if c[:2] == ["zpool", "destroy"]],
                "left": sorted(p.split("pool.", 1)[1] for p in glob.glob(f"{state}/pool.*")),
                "benches": glob.glob("/dev/shm/zark-selftest-*"),
                "child_alive": _alive(os.path.join(state, "child.pid")),
                "log": kept,
                "rm_after_destroys": bool(removes and destroys and removes[0] > destroys[-1]),
                "report": _report(),
                "made": made,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
