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
An in-memory model of the zfs/zpool commands the replication engine runs.

Unlike :class:`tests.mock_sh.MockShell`, which replays scripted answers,
this interprets each command against a model of two sides (origin pools and
a backup pool), so tests check outcomes — which snapshots exist where —
rather than command strings. It keeps the kernel rules the engine relies on
(OpenZFS 2.4.1, M2 plan §2):

  * one ``zfs snapshot`` call per pool (EXDEV otherwise);
  * ``-I`` never from a bookmark;
  * an incremental receive without ``-F`` needs its base to be the
    destination's newest snapshot (ETXTBSY otherwise); ``-F`` is refused
    outright here, because the engine must never pass it;
  * a raw incremental from a bookmark needs the IV-set GUID, stored only
    when ``feature@bookmark_v2`` was enabled at bookmark creation;
  * partial receive state (``-s``) blocks a new stream until resumed
    (``send -t``) or aborted (``receive -A``).

Failures can be injected per destination dataset: ``interrupt`` (after N
snapshots, leaving a resume token) or ``enospc``.
"""

import shlex
from dataclasses import dataclass, field

from lib.sh import RunResult
from tests.mock_sh import MockShell


@dataclass
class FSnap:
    """A snapshot or bookmark in the model."""

    name: str
    guid: str
    txg: int
    creation: int
    ivset: bool = True


@dataclass
class FDataset:
    """A dataset in the model."""

    kind: str = "filesystem"
    props: dict[str, str] = field(default_factory=dict)
    snaps: list[FSnap] = field(default_factory=list)
    bookmarks: list[FSnap] = field(default_factory=list)
    token: str = ""


def _ok(out: str = "") -> RunResult:
    return RunResult(returncode=0, stdout=out, stderr="", command="")


def _err(msg: str, rc: int = 1) -> RunResult:
    return RunResult(returncode=rc, stdout="", stderr=msg + "\n", command="")


class FakeZfs(MockShell):  # pylint: disable=too-many-public-methods,too-many-instance-attributes
    """Model of origin pools plus one backup pool."""

    def __init__(self) -> None:
        super().__init__()
        self.ds: dict[str, FDataset] = {}
        self.pools: set[str] = set()
        self.features: dict[str, str] = {}
        self.units: dict[str, str] = {
            "sanoid.timer": "active",
            "sanoid.service": "inactive",
            "sanoid-prune.service": "inactive",
        }
        self.txg = 100
        self.guid = 1000
        self.clock = 1_700_000_000
        self.inject: dict[str, tuple[str, int]] = {}  # dest dataset → (kind, after)
        self.fail_cmds: set[str] = set()  # a command containing one of these fails

    # ── building the model ───────────────────────────────────────────────

    def pool(self, name: str, bookmark_v2: str = "enabled") -> None:
        """Create a pool (its root dataset included)."""
        self.pools.add(name)
        self.features[name] = bookmark_v2
        self.ds[name] = FDataset(props={"mountpoint": "none", "canmount": "on"})

    def dataset(self, name: str, kind: str = "filesystem", **props: str) -> None:
        """Create a dataset."""
        base = {"canmount": "on", "mountpoint": "/" + name.split("/", 1)[-1]}
        base.update(props)
        self.ds[name] = FDataset(kind=kind, props=base)

    def snap(self, ds: str, name: str) -> FSnap:
        """Take one snapshot (new guid)."""
        self.txg += 1
        self.guid += 1
        self.clock += 60
        s = FSnap(name, str(self.guid), self.txg, self.clock)
        self.ds[ds].snaps.append(s)
        return s

    def names(self, ds: str) -> list[str]:
        """Snapshot names of a dataset, oldest first."""
        return [s.name for s in self.ds[ds].snaps] if ds in self.ds else []

    def bookmarks(self, ds: str) -> list[str]:
        """Bookmark names of a dataset."""
        return [b.name for b in self.ds[ds].bookmarks] if ds in self.ds else []

    # ── dispatch ─────────────────────────────────────────────────────────

    def mock_run(  # pylint: disable=unused-argument,redefined-builtin
        self,
        cmd,
        check=False,
        input=None,
        log=None,
        timeout=None,
    ) -> RunResult:
        """Interpret one command."""
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        self._calls.append(cmd_str)
        if any(f in cmd_str for f in self.fail_cmds):
            return _err(f"fake: injected failure: {cmd_str}")
        argv = shlex.split(cmd_str)
        handler = {
            ("zpool", "list"): self._zpool_list,
            ("zpool", "get"): self._zpool_get,
            ("zfs", "list"): self._list,
            ("zfs", "snapshot"): self._snapshot,
            ("zfs", "send"): self._send_dry,
            ("zfs", "receive"): self._receive_abort,
            ("zfs", "bookmark"): self._bookmark,
            ("zfs", "destroy"): self._destroy,
            ("zfs", "rename"): self._rename,
            ("zfs", "rollback"): self._rollback,
            ("zfs", "set"): self._set,
            ("zfs", "inherit"): self._inherit,
            ("zfs", "create"): self._create,
            ("zfs", "get"): self._get,
            ("zpool", "export"): lambda _a: _ok(),
            ("zpool", "create"): self._zpool_create,
            ("zfs", "unload-key"): lambda _a: _ok(),
            ("systemctl", "is-active"): self._is_active,
            ("systemctl", "stop"): self._unit_set("inactive"),
            ("systemctl", "start"): self._unit_set("active"),
        }.get((argv[0], argv[1]) if len(argv) > 1 else ("", ""))
        if handler is None:
            return _err(f"fake: unsupported: {cmd_str}", 127)
        return handler(argv[2:])

    def mock_run_pipe(self, cmd1, cmd2, log=None) -> RunResult:  # pylint: disable=unused-argument
        """Interpret ``zfs send … | zfs receive …``."""
        self._calls.append(f"{cmd1} | {cmd2}")
        return self._pipe(shlex.split(cmd1)[2:], shlex.split(cmd2)[2:])

    # ── zpool / systemctl ────────────────────────────────────────────────

    def _zpool_create(self, args: list[str]) -> RunResult:
        name = args[-2]
        if name in self.pools:
            return _err(f"pool '{name}' already exists")
        self.pool(name)
        return _ok()

    def _zpool_list(self, args: list[str]) -> RunResult:
        return _ok(args[-1] + "\n") if args[-1] in self.pools else _err("no such pool")

    def _zpool_get(self, args: list[str]) -> RunResult:
        prop, pool = args[-2], args[-1]
        if pool not in self.pools:
            return _err("no such pool")
        if prop == "guid":
            return _ok(f"9{abs(hash(pool)) % 10**8}\n")
        return _ok(self.features[pool] + "\n") if prop == "feature@bookmark_v2" else _err("?")

    def _get(self, args: list[str]) -> RunResult:
        """``zfs get -H [-s local] -o value|property,value <prop|all> <ds>``."""
        ds, prop = args[-1], args[-2]
        if ds not in self.ds:
            return _err("does not exist")
        props = self.ds[ds].props
        if prop == "all":
            return _ok("".join(f"{k}\t{v}\n" for k, v in sorted(props.items()) if ":" in k))
        return _ok(props.get(prop, "-") + "\n")

    def _is_active(self, args: list[str]) -> RunResult:
        state = self.units.get(args[0], "inactive")
        return RunResult(0 if state == "active" else 3, state + "\n", "", "")

    def _unit_set(self, state: str):
        def _do(args: list[str]) -> RunResult:
            self.units[args[0]] = state
            return _ok()

        return _do

    # ── zfs list ─────────────────────────────────────────────────────────

    def _list(self, args: list[str]) -> RunResult:  # pylint: disable=too-many-branches
        types, fields, root, recursive = ["filesystem", "volume"], ["name"], "", False
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-t":
                types, i = args[i + 1].split(","), i + 2
            elif a == "-o":
                fields, i = args[i + 1].split(","), i + 2
            elif a == "-r":
                recursive, i = True, i + 1
            elif a.startswith("-"):
                i += 1
            else:
                root, i = a, i + 1
        if root not in self.ds:
            return _err(f"cannot open '{root}': dataset does not exist")
        names = [
            n for n in sorted(self.ds) if n == root or (recursive and n.startswith(root + "/"))
        ]
        rows: list[str] = []
        for n in names:
            d = self.ds[n]
            if d.kind in types:
                rows.append(self._row(n, d, None, fields))
            if "snapshot" in types:
                rows += [self._row(f"{n}@{s.name}", d, s, fields) for s in d.snaps]
            if "bookmark" in types:
                rows += [self._row(f"{n}#{b.name}", d, b, fields) for b in d.bookmarks]
        return _ok("".join(r + "\n" for r in rows))

    def _row(self, name: str, d: FDataset, s: FSnap | None, fields: list[str]) -> str:
        vals: list[str] = []
        for f in fields:
            if f == "name":
                vals.append(name)
            elif f == "type":
                vals.append(d.kind)
            elif f == "guid" and s:
                vals.append(s.guid)
            elif f == "createtxg" and s:
                vals.append(str(s.txg))
            elif f == "creation" and s:
                vals.append(str(s.creation))
            elif f == "receive_resume_token":
                vals.append(d.token or "-")
            elif f == "used":
                vals.append(str(1024 * (1 + len(d.snaps))))
            else:
                vals.append(d.props.get(f, "-"))
        return "\t".join(vals)

    # ── snapshots, bookmarks, destroy, rename, rollback, set ─────────────

    def _snapshot(self, args: list[str]) -> RunResult:
        names = [a for a in args if not a.startswith("-")]
        pools = {n.split("/")[0].split("@")[0] for n in names}
        if len(pools) != 1:
            return _err("cannot create snapshots: cross-device link (EXDEV)")
        self.txg += 1
        self.clock += 1
        for n in names:
            ds, snap = n.split("@")
            if ds not in self.ds or snap in self.names(ds):
                return _err(f"cannot create snapshot '{n}'")
        for n in names:
            ds, snap = n.split("@")
            self.guid += 1
            self.ds[ds].snaps.append(FSnap(snap, str(self.guid), self.txg, self.clock))
        return _ok()

    def _bookmark(self, args: list[str]) -> RunResult:
        src, dst = args[-2], args[-1]
        ds, snap = src.split("@")
        bm = dst.split("#")[1]
        s = next((x for x in self.ds[ds].snaps if x.name == snap), None)
        if s is None:
            return _err(f"cannot bookmark '{src}': does not exist")
        ivset = self.features[ds.split("/")[0]] in ("enabled", "active")
        if ivset:
            self.features[ds.split("/")[0]] = "active"
        self.ds[ds].bookmarks.append(FSnap(bm, s.guid, s.txg, s.creation, ivset))
        return _ok()

    def _destroy(self, args: list[str]) -> RunResult:
        recursive = "-r" in args
        target = args[-1]
        if "@" in target or "#" in target:
            sep = "@" if "@" in target else "#"
            ds, name = target.split(sep)
            if ds not in self.ds:
                return _err("does not exist")
            lst = self.ds[ds].snaps if sep == "@" else self.ds[ds].bookmarks
            hit = [x for x in lst if x.name == name]
            if not hit:
                return _err(f"could not find any snapshots to destroy: {target}")
            lst.remove(hit[0])
            return _ok()
        kids = [n for n in self.ds if n.startswith(target + "/")]
        if kids and not recursive:
            return _err("filesystem has children")
        for n in [*kids, target]:
            self.ds.pop(n, None)
        return _ok()

    def _rename(self, args: list[str]) -> RunResult:
        src, dst = args[-2], args[-1]
        if "@" in src:
            ds, a = src.split("@")
            b = dst.split("@")[1]
            for s in self.ds[ds].snaps:
                if s.name == a:
                    s.name = b
                    return _ok()
            return _err("does not exist")
        if dst in self.ds:
            return _err("dataset already exists")
        if dst.rsplit("/", 1)[0] not in self.ds:
            return _err("parent does not exist")
        for n in sorted(n for n in list(self.ds) if n == src or n.startswith(src + "/")):
            self.ds[dst + n[len(src) :]] = self.ds.pop(n)
        return _ok()

    def _rollback(self, args: list[str]) -> RunResult:
        ds, name = args[-1].split("@")
        names = self.names(ds)
        if name not in names:
            return _err("does not exist")
        later = names[names.index(name) + 1 :]
        if later and "-r" not in args:
            return _err("more recent snapshots exist")
        self.ds[ds].snaps = self.ds[ds].snaps[: names.index(name) + 1]
        return _ok()

    def _create(self, args: list[str]) -> RunResult:
        ds = args[-1]
        if ds in self.ds:
            return _err(f"cannot create '{ds}': dataset already exists")
        if ds.rsplit("/", 1)[0] not in self.ds:
            return _err(f"cannot create '{ds}': parent does not exist")
        props = {}
        for j, a in enumerate(args):
            if a == "-o":
                k, v = args[j + 1].split("=", 1)
                props[k] = v
        self.ds[ds] = FDataset(props=props)
        return _ok()

    def _set(self, args: list[str]) -> RunResult:
        ds = args[-1]
        if ds not in self.ds:
            return _err("does not exist")
        for kv in args[:-1]:
            k, v = kv.split("=", 1)
            self.ds[ds].props[k] = v
        return _ok()

    def _inherit(self, args: list[str]) -> RunResult:
        ds = args[-1]
        if ds not in self.ds:
            return _err("does not exist")
        for k in args[:-1]:
            _ = self.ds[ds].props.pop(k, None)
        return _ok()

    # ── send / receive ───────────────────────────────────────────────────

    def _send_dry(self, args: list[str]) -> RunResult:
        if "-nvP" in args:
            incremental = any(a in args for a in ("-i", "-I", "-t"))
            return _ok(f"size\t{4096 if incremental else 10**9}\n")
        return _err("fake: send outside a pipe")

    def _receive_abort(self, args: list[str]) -> RunResult:
        if "-A" not in args:
            return _err("fake: receive outside a pipe")
        d = self.ds.get(args[-1])
        if d is None or not d.token:
            return _err("no partially-received state")
        d.token = ""
        if not d.snaps:
            del self.ds[args[-1]]
        return _ok()

    @staticmethod
    def _parse_send(args: list[str]) -> tuple[bool, str, str, str, str]:
        """(raw, mode, from, to, token) with mode in full|I|i|t."""
        raw = "-w" in args
        if "-t" in args:
            return raw, "t", "", "", args[args.index("-t") + 1]
        if "-I" in args:
            j = args.index("-I")
            return raw, "I", args[j + 1], args[j + 2], ""
        if "-i" in args:
            j = args.index("-i")
            return raw, "i", args[j + 1], args[j + 2], ""
        return raw, "full", "", args[-1], ""

    def _pipe(self, send: list[str], recv: list[str]) -> RunResult:  # noqa: C901 # pylint: disable=too-many-return-statements,too-many-branches,too-many-locals
        if "-F" in recv:
            raise AssertionError("the engine must never receive with -F")
        dst = recv[-1]
        raw, mode, frm, to, token = self._parse_send(send)
        if mode == "t":
            src_ds, snapname = token.split(":", 2)[1:]
            d = self.ds.get(dst)
            if d is None or not d.token:
                return _err("no partial state")
            have = self.ds.get(src_ds, FDataset()).snaps
            src = next((s for s in have if s.name == snapname), None)
            if src is None:
                return _err(f"cannot resume send: '{src_ds}@{snapname}' does not exist")
            d.snaps.append(FSnap(src.name, src.guid, self._dtxg(), src.creation))
            d.token = ""
            return _ok()
        src_ds = to.split("@")[0]
        origin = self.ds[src_ds]
        if src_ds.startswith("rpool") and origin.props.get("encryption") != "off" and not raw:
            return _err("encrypted dataset: raw send (-w) required")
        to_snap = next(s for s in origin.snaps if s.name == to.split("@")[1])
        d = self.ds.get(dst)
        if d is not None and d.token:
            return _err("destination contains partially-complete state (ZFS_ERR_RESUME_EXISTS)")
        if mode == "full":
            if d is not None:
                return _err(f"destination '{dst}' exists")
            if dst.rsplit("/", 1)[0] not in self.ds:
                return _err("parent does not exist")
            stream = [to_snap]
        else:
            if d is None:
                return _err(f"destination '{dst}' does not exist")
            sep = "#" if "#" in frm else "@"
            if mode == "I" and sep == "#":
                return _err("multiple snapshots cannot be sent from a bookmark")
            fname = frm.split(sep)[1]
            pool = origin.bookmarks if sep == "#" else origin.snaps
            base = next((x for x in pool if x.name == fname), None)
            if base is None:
                return _err(f"incremental source {frm} does not exist")
            if sep == "#" and raw and not base.ivset:
                return _err("ZFS_ERR_FROM_IVSET_GUID_MISSING")
            if not d.snaps or d.snaps[-1].guid != base.guid:
                return _err("destination has been modified since most recent snapshot (ETXTBSY)")
            if mode == "I":
                stream = [s for s in origin.snaps if base.txg < s.txg <= to_snap.txg]
            else:
                stream = [to_snap]
        kind, after = self.inject.pop(dst, ("", 0))
        if kind == "interrupt" and after >= len(stream):
            self.inject[dst] = (kind, after - len(stream))  # cut a later stream
            kind = ""
        if kind == "enospc":
            return _err("cannot receive incremental stream: out of space")
        if d is None:
            d = FDataset(kind=origin.kind)
            self.ds[dst] = d
        for k, s in enumerate(stream):
            if kind == "interrupt" and k == after:
                d.token = f"tok:{src_ds}:{s.name}"
                return _err("cannot receive: connection reset (stream truncated)")
            d.snaps.append(FSnap(s.name, s.guid, self._dtxg(), s.creation))
        self._apply_props(d, recv)
        return _ok()

    def _apply_props(self, d: FDataset, recv: list[str]) -> None:
        for j, a in enumerate(recv):
            if a == "-o":
                k, v = recv[j + 1].split("=", 1)
                d.props[k] = v
            elif a == "-x":
                d.props.pop(recv[j + 1], None)

    def _dtxg(self) -> int:
        self.txg += 1
        return self.txg
