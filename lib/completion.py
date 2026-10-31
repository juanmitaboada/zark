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
The shell completion's engine: from the command line typed so far, what may
come next, as ``lib/cli_spec.py`` declares it (M14-8, M14-10, M14-11).

Run as a script by the bash function in ``completion/zark``, as the user who
presses TAB, never through zark's entry point::

    python3 -I <zark root>/lib/completion.py "<the line up to the cursor>"

It prints one line saying how to complete (``words``, ``files``, ``dirs``),
then the candidates, one per line. It imports the standard library and
``lib.cli_spec`` only, and reads only what any user may: the spec,
``/dev/disk/by-id``, the drive registry the command would read, and
``zfs list`` / ``zfs get`` (2 s at most). It never imports a pool, writes,
takes a lock or logs; a listing that fails or is slow offers nothing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: lib.cli_spec from its own tree
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import cli_spec  # noqa: E402 # pylint: disable=wrong-import-position

BY_ID = Path("/dev/disk/by-id")
ZFS_TIMEOUT_S = 2
_PROPS = "org.zark:replicate,org.zark:snapshot"


def registry_path(root: Path) -> Path:
    """The ``known_drives.json`` ``zark registry`` would read, by
    ``Config.default_config_dir``'s rule (a test pins the two equal)."""
    env = os.environ.get("ZARK_CONFIG_DIR")
    if env and Path(env).is_dir():
        return Path(env) / "known_drives.json"
    portable = root / "etc" / "known_drives.json"
    return portable if portable.exists() else Path("/etc/zark/known_drives.json")


def drives(root: Path) -> list[str]:
    """The registered drives' names; none when the registry cannot be read."""
    try:
        data = json.loads(registry_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted(k for k in data if isinstance(k, str)) if isinstance(data, dict) else []


def devices(by_id: Path | None = None) -> list[str]:
    """Whole disks under ``by_id`` (:data:`BY_ID`), as full paths (zark
    refuses partitions)."""
    by_id = by_id or BY_ID
    try:
        names = os.listdir(by_id)
    except OSError:
        return []
    return sorted(str(by_id / n) for n in names if "-part" not in n)


def _zfs(argv: list[str]) -> list[str]:
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=ZFS_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln.split("\t", 1)[0] for ln in r.stdout.splitlines() if ln] if r.returncode == 0 else []


def datasets() -> list[str]:
    """rpool's filesystems, the ones ``exclude add`` takes."""
    return _zfs(["zfs", "list", "-H", "-o", "name", "-t", "filesystem", "-r", "rpool"])


def excluded() -> list[str]:
    """rpool's datasets that carry an exclusion, the ones ``exclude remove`` takes."""
    names = _zfs(["zfs", "get", "-H", "-s", "local", "-o", "name", _PROPS, "-r", "rpool"])
    return sorted(set(names))


def _values(arg: cli_spec.Arg, root: Path) -> tuple[str, list[str]]:
    """How to complete a value of ``arg``'s kind, and its candidates."""
    if arg.kind in ("file", "dir"):
        return ("files" if arg.kind == "file" else "dirs"), []
    found = {
        "device": devices,
        "drive": lambda: drives(root),
        "dataset": datasets,
        "excluded": excluded,
    }.get(arg.kind)
    if found is not None:
        return "words", found()
    if arg.kind == "words":
        # A setting typed as name=value (registry retention) is offered with its "=".
        return "words", [f"{w}=" if arg.repeat else w for w in arg.words]
    return "words", []


def _option(spec: cli_spec.Spec, name: str) -> cli_spec.Opt | None:
    every = (*spec.options, *cli_spec.GLOBAL_OPTIONS)
    return next((o for o in every if o.name == name), None)


def _walk(cmd: str, done: list[str]) -> tuple[cli_spec.Spec, list[str], bool]:
    """The (sub)command spec the line is in, the words typed after it, and
    whether a subcommand may still be typed."""
    spec = cli_spec.command(cmd)
    if not spec.subcommands:
        return spec, done, False
    if done and done[0] in {s.name for s in spec.subcommands}:
        return spec.sub(done[0]), done[1:], False
    default = spec.sub(spec.default_sub) if spec.default_sub else spec
    return default, done, not done


def complete(line: str, root: Path) -> tuple[str, list[str]]:
    """How to complete ``line`` (up to the cursor) and the candidates that
    start with what is being typed."""
    words = line.split()
    cur = "" if not words or line[-1:].isspace() else words.pop()
    # Whatever came before zark on the line (sudo, its options) is not zark's.
    while words and os.path.basename(words[0]) != "zark":
        words.pop(0)
    done = words[1:]
    how, out = _candidates(done, cur, root)
    return how, [c for c in out if c.startswith(cur)]


def _candidates(done: list[str], cur: str, root: Path) -> tuple[str, list[str]]:
    if not done:
        return "words", [*cli_spec.command_names(), *cli_spec.HELP_WORDS, *cli_spec.VERSION_WORDS]
    cmd = done[0][2:] if done[0].startswith("--") else done[0]
    if cmd not in cli_spec.SPECS:
        return "words", []
    spec, rest, sub_open = _walk(cmd, done[1:])
    if rest:
        opt = _option(spec, rest[-1])
        if opt is not None and opt.value is not None:
            return _values(opt.value, root)
    if cur.startswith("-"):
        names = [o.name for o in (*spec.options, *cli_spec.GLOBAL_OPTIONS)]
        return "words", sorted({n for n in names if n not in rest})
    if sub_open:
        return "words", [s.name for s in cli_spec.command(cmd).subcommands]
    return _positional(spec, rest, root)


def _positional(spec: cli_spec.Spec, rest: list[str], root: Path) -> tuple[str, list[str]]:
    """The next positional argument's values: options and their values are
    not positional; a repeated last argument takes any number."""
    positional: list[str] = []
    skip = False
    for w in rest:
        if skip:
            skip = False
        elif w.startswith("-"):
            opt = _option(spec, w)
            skip = opt is not None and opt.value is not None
        else:
            positional.append(w)
    if len(positional) < len(spec.args):
        return _values(spec.args[len(positional)], root)
    if spec.args and spec.args[-1].repeat:
        return _values(spec.args[-1], root)
    return "words", []


def main(argv: list[str]) -> int:
    """Print the completion of ``argv[0]``, the line up to the cursor."""
    root = Path(__file__).resolve().parent.parent
    how, out = complete(argv[0] if argv else "", root)
    sys.stdout.write("\n".join([how, *out]) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
