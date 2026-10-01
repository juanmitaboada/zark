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
Run the real ``commands.umount._unmount_tree`` on real mounts (review 2, §11.3).

Must run as root:

    python tests/umount_propagation_harness.py <workdir> <case>

Before mounting anything it moves itself into a new mount namespace and makes
every mount there private, so nothing it does reaches the system's namespace;
it stops if the namespace did not change. Everything is mounted below
``<workdir>`` and disappears with the namespace. tmpfs stands in for
ZFS: only the FSTYPE column of ``findmnt`` is rewritten for the mounts whose
source is ``zarkds``; every mount and unmount is real.

``<workdir>`` (a shared tmpfs) plays the host. A second process, unshared with
slave propagation before the tree is mounted, plays a systemd service or a
snap. The tree is ``<workdir>/zark/system``.

Cases:

    datasets        two datasets
    bind            datasets + a bind of an empty host directory
    bind-hostmount  datasets + that bind, then a host mount below the host directory
    nested-bind     datasets + /dev and /dev/pts binds, as ``zark chroot`` does

Prints one JSON object: ``other_left`` (mounts under the tree still present
in the second namespace), ``tree_left`` (the same, here), ``host_lost`` (host
mounts outside the tree that existed before the unmount and are gone).
"""

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

import commands.umount as umount_mod  # pylint: disable=wrong-import-position # noqa: E402
from lib import sh  # pylint: disable=wrong-import-position # noqa: E402
from lib.sh import RunResult  # pylint: disable=wrong-import-position # noqa: E402

DATASET_SOURCE = "zarkds"
CASES = ("datasets", "bind", "bind-hostmount", "nested-bind")


def _run(*args: str) -> None:
    _ = subprocess.run(args, check=True, capture_output=True)


def _mount_targets(mountinfo: str) -> list[str]:
    """Mount points listed in a mountinfo file (field 5, octal escapes decoded)."""
    with open(mountinfo, encoding="utf-8") as f:
        return [line.split()[4].encode().decode("unicode_escape") for line in f if line.strip()]


def _below(targets: list[str], root: str) -> list[str]:
    return [t for t in targets if t == root or t.startswith(f"{root}/")]


def _findmnt_as_zfs(real_run: Callable[..., RunResult] = sh.run) -> Callable[..., RunResult]:
    """sh.run with the tree's tmpfs datasets reported as zfs by findmnt."""

    def fake(cmd: str, *args: object, **kwargs: object) -> RunResult:
        if cmd != "findmnt -rn -o TARGET,FSTYPE":
            return real_run(cmd, *args, **kwargs)
        r = real_run("findmnt -rn -o TARGET,FSTYPE,SOURCE")
        rows = []
        for line in r.lines:
            rest, _, source = line.rpartition(" ")
            target, _, fstype = rest.rpartition(" ")
            rows.append(f"{target} {'zfs' if source == DATASET_SOURCE else fstype}")
        return replace(r, stdout="\n".join(rows) + "\n", command=cmd)

    return fake


@contextlib.contextmanager
def _other_namespace() -> Iterator[int]:
    """A process in its own mount namespace with slave propagation; yields its pid."""
    own = os.readlink("/proc/self/ns/mnt")
    # Without --fork, unshare execs sleep in the new namespace: the pid is sleep's.
    p = subprocess.Popen(["unshare", "--mount", "--propagation", "slave", "sleep", "300"])
    try:
        for _ in range(200):
            if os.readlink(f"/proc/{p.pid}/ns/mnt") != own:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("the second namespace was not created")
        yield p.pid
    finally:
        p.kill()
        _ = p.wait()


def main() -> None:
    """Build one case, run _unmount_tree, print what is left where."""
    work, case = os.path.realpath(sys.argv[1]), sys.argv[2]
    if case not in CASES:
        raise SystemExit(f"unknown case {case!r}")
    system_ns = os.readlink("/proc/self/ns/mnt")
    os.unshare(os.CLONE_NEWNS)
    _run("mount", "--make-rprivate", "/")
    if os.readlink("/proc/self/ns/mnt") == system_ns:
        raise SystemExit("refusing to run: no mount namespace of its own")

    _run("mount", "-t", "tmpfs", "zarkhost", work)
    _run("mount", "--make-shared", work)  # what systemd does for / on the host
    root = f"{work}/zark/system"
    for d in (root, f"{work}/host/run", f"{work}/host/dev/pts"):
        os.makedirs(d)
    _run("mount", "-t", "tmpfs", "devpts", f"{work}/host/dev/pts")  # the host's own /dev/pts

    with _other_namespace() as other:
        _run("mount", "-t", "tmpfs", DATASET_SOURCE, root)
        os.makedirs(f"{root}/home")
        _run("mount", "-t", "tmpfs", DATASET_SOURCE, f"{root}/home")
        if case in ("bind", "bind-hostmount"):
            os.makedirs(f"{root}/run")
            _run("mount", "--bind", f"{work}/host/run", f"{root}/run")
        if case == "bind-hostmount":
            os.makedirs(f"{work}/host/run/user")
            _run("mount", "-t", "tmpfs", "hostuser", f"{work}/host/run/user")
        if case == "nested-bind":
            os.makedirs(f"{root}/dev")
            _run("mount", "--bind", f"{work}/host/dev", f"{root}/dev")
            _run("mount", "--bind", f"{work}/host/dev/pts", f"{root}/dev/pts")

        here = _mount_targets("/proc/self/mountinfo")
        host_before = [t for t in here if t.startswith(f"{work}/host/")]
        with patch.object(umount_mod.sh, "run", side_effect=_findmnt_as_zfs()):
            umount_mod._unmount_tree(root)  # pylint: disable=protected-access

        here = _mount_targets("/proc/self/mountinfo")
        result = {
            "other_left": _below(_mount_targets(f"/proc/{other}/mountinfo"), root),
            "tree_left": _below(here, root),
            "host_lost": [t for t in host_before if t not in here],
        }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
