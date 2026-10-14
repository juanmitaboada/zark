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
Run the real ``commands.clean.run`` on real mounts (H6).

Must run as root:

    python tests/clean_harness.py <workdir> <case>

It moves itself into a new mount namespace and checks that the namespace
changed before doing anything else, then makes every mount there private,
so nothing reaches the system's namespace. Everything is mounted below
``<workdir>``. clean's roots are patched to ``<workdir>/mnt/zark``;
``zpool``, ``zfs``, ``cryptsetup`` and ``systemctl`` are answered without
running (no pool, timer active); ``findmnt``, ``mount``, ``umount`` and the
filesystem are real.

A tmpfs (``<workdir>/data``, holding ``file``) is bound at
``<workdir>/mnt/zark/backup/home``, as a filesystem left mounted below one
of clean's roots.

Cases:

    busy     a process keeps its cwd inside the bind, so the unmount fails
    idle     nothing uses the bind
    otherfs  no clean run: ``_remove_empty_dirs`` is called directly while
             the bind, holding an empty directory, is still mounted

Prints one JSON object: ``rc`` (clean's exit status), ``file`` (the file in
the bound tmpfs still exists), ``mounted`` (the bind is still mounted),
``root`` (``<workdir>/mnt/zark`` still exists); for ``otherfs`` only
``inner`` (the empty directory inside the bind still exists).
"""

import json
import os
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isort: split

import commands.clean as clean_mod  # pylint: disable=wrong-import-position # noqa: E402
from lib import sh  # pylint: disable=wrong-import-position # noqa: E402
from lib.log import Log  # pylint: disable=wrong-import-position # noqa: E402
from lib.sh import RunResult  # pylint: disable=wrong-import-position # noqa: E402

_real_run = sh.run
_STUBBED = ("zpool", "zfs", "cryptsetup", "systemctl")


def _run(cmd: str | list[str], **kw: object) -> RunResult:
    text = cmd if isinstance(cmd, str) else " ".join(cmd)
    if text.split(" ", 1)[0] in _STUBBED:
        out = "active\n" if text.startswith("systemctl is-active") else ""
        return RunResult(returncode=0, stdout=out, stderr="", command=text)
    return _real_run(cmd, **kw)  # type: ignore[arg-type]


def _check(args: list[str]) -> None:
    _ = subprocess.run(args, check=True, capture_output=True)


def main() -> int:
    """Stage the case, run clean, print the result."""
    workdir, case = sys.argv[1], sys.argv[2]
    before = os.readlink("/proc/self/ns/mnt")
    os.unshare(os.CLONE_NEWNS)
    if os.readlink("/proc/self/ns/mnt") == before:
        print("not in a new mount namespace: refusing to run", file=sys.stderr)
        return 1
    _check(["mount", "--make-rprivate", "/"])
    _check(["mount", "-t", "tmpfs", "zarktest", workdir])

    root = os.path.join(workdir, "mnt", "zark")
    home = os.path.join(root, "backup", "home")
    data = os.path.join(workdir, "data")
    os.makedirs(home)
    os.makedirs(data)
    _check(["mount", "-t", "tmpfs", "zarkdata", data])
    with open(os.path.join(data, "file"), "w", encoding="ascii") as f:
        _ = f.write("precious\n")
    _check(["mount", "--bind", data, home])

    if case == "otherfs":
        inner = os.path.join(data, "empty")
        os.makedirs(inner)
        with patch.object(clean_mod, "ROOTS", (root,)):
            _ = clean_mod._remove_empty_dirs(Log(log_file="/dev/null"))  # pylint: disable=protected-access
        print(json.dumps({"inner": os.path.isdir(inner)}))
        return 0

    busy = None
    if case == "busy":
        busy = subprocess.Popen(["sleep", "60"], cwd=home)  # pylint: disable=consider-using-with

    rc = 0
    with (
        patch.object(clean_mod, "ROOTS", (root,)),
        patch.object(clean_mod, "MAPPER_DIR", os.path.join(workdir, "mapper")),
        patch.object(clean_mod, "ZVOL_INHIBIT", os.path.join(workdir, "zvol_inhibit_dev")),
        patch.object(clean_mod.engine, "SANOID_MARKER", os.path.join(workdir, "marker")),
        patch.object(clean_mod, "Log", lambda: Log(log_file="/dev/null")),
        patch("lib.sh.run", side_effect=_run),
        patch("builtins.input", return_value="y"),
    ):
        stdout = sys.stdout
        try:
            with open(os.devnull, "w", encoding="utf-8") as sink:
                sys.stdout = sink
                clean_mod.run([])
        except SystemExit as e:
            rc = int(e.code) if isinstance(e.code, int) else 1
        finally:
            sys.stdout = stdout
    if busy is not None:
        busy.kill()
        _ = busy.wait()

    mounted = subprocess.run(["findmnt", home], capture_output=True, check=False).returncode == 0
    print(
        json.dumps(
            {
                "rc": rc,
                "file": os.path.exists(os.path.join(data, "file")),
                "mounted": mounted,
                "root": os.path.isdir(root),
            },
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
