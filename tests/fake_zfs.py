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
    destination's newest snapshot and the destination unwritten since it
    (``written``; ETXTBSY otherwise, with libzfs's two-line text); ``-F``
    is refused outright here, because the engine must never pass it;
  * a raw incremental from a bookmark needs the IV-set GUID, stored only
    when ``feature@bookmark_v2`` was enabled at bookmark creation;
  * partial receive state (``-s``) blocks a new stream until resumed
    (``send -t``) or aborted (``receive -A``); a resume onto a destination
    written since its newest snapshot is refused at its end and the state
    kept (eli R10, M2V-1).

Failures can be injected per destination dataset: ``interrupt`` (after N
snapshots, leaving a resume token) or ``enospc``.
"""

import shlex
from dataclasses import dataclass, field

from lib.sh import RunResult
from tests.mock_sh import MockShell


@dataclass
class FSnap:  # pylint: disable=too-many-instance-attributes
    """A snapshot or bookmark in the model."""

    name: str
    guid: str
    txg: int
    creation: int
    ivset: bool = True
    props: dict[str, str] = field(default_factory=dict)  # user properties set on a snapshot
    used: int = 1 << 20  # what destroying it alone would free
    clones: str = ""  # datasets cloned from it (destroy refused, EEXIST)
    userrefs: int = 0  # holds (destroy refused, EBUSY)


@dataclass
class FDataset:
    """A dataset in the model."""

    kind: str = "filesystem"
    props: dict[str, str] = field(default_factory=dict)
    snaps: list[FSnap] = field(default_factory=list)
    bookmarks: list[FSnap] = field(default_factory=list)
    token: str = ""
    written: int = 0  # bytes written since the newest snapshot (a read-write mount)
    origin: str = ""  # a clone's origin snapshot


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
        self.importable: set[str] = set()  # pools `zpool import` lists (exported here)
        self.capacity: dict[str, int] = {}  # pool → `zpool list -o capacity` (50 when unset)

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
        if argv and argv[0] == "env":  # `env LC_ALL=C <command>`
            argv = [a for a in argv[1:] if "=" not in a]
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
            ("zfs", "clone"): self._clone,
            ("zfs", "promote"): self._promote,
            ("zfs", "rollback"): self._rollback,
            ("zfs", "set"): self._set,
            ("zfs", "inherit"): self._inherit,
            ("zfs", "create"): self._create,
            ("zfs", "get"): self._get,
            ("zpool", "export"): lambda _a: _ok(),
            ("zpool", "import"): self._zpool_import_list,
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

    def _zpool_import_list(self, args: list[str]) -> RunResult:
        """``zpool import`` without a pool name: list the importable pools."""
        if args:
            return _err("fake: only the listing form of zpool import")
        if not self.importable:
            return _err("no pools available to import")
        return _ok(
            "".join(
                f"   pool: {p}\n     id: 4242\n  state: ONLINE\n\n" for p in sorted(self.importable)
            )
        )

    def _zpool_list(self, args: list[str]) -> RunResult:
        if args[-1] not in self.pools:
            return _err("no such pool")
        if "capacity" in args:
            return _ok(f"{self.capacity.get(args[-1], 50)}\n")
        return _ok(args[-1] + "\n")

    def _zpool_get(self, args: list[str]) -> RunResult:
        prop, pool = args[-2], args[-1]
        if pool not in self.pools:
            return _err("no such pool")
        if prop == "guid":
            return _ok(f"9{abs(hash(pool)) % 10**8}\n")
        return _ok(self.features[pool] + "\n") if prop == "feature@bookmark_v2" else _err("?")

    def _get(self, args: list[str]) -> RunResult:
        """``zfs get -H [-s local] -o value|property,value <prop|all> <ds>``, and
        ``zfs get -H -p -r -s local -t snapshot -o name,value <prop> <root>``."""
        named = [a for a in args if "@" in a or "#" in a]
        if named and "-r" not in args:
            return self._get_named(args[args.index(named[0]) - 1], named)
        ds, prop = args[-1], args[-2]
        if ds not in self.ds:
            return _err("does not exist")
        if "-r" in args and "snapshot" in args:
            return self._get_snapshots(ds, prop, local="local" in args)
        props = self.ds[ds].props
        if prop == "all":
            return _ok("".join(f"{k}\t{v}\n" for k, v in sorted(props.items()) if ":" in k))
        return _ok(props.get(prop, "-") + "\n")

    def _get_named(self, prop: str, names: list[str]) -> RunResult:
        """``zfs get -Hp -o value guid <ds@snap|ds#bookmark>...``: one line each."""
        if prop != "guid":
            return _err(f"fake zfs get: {prop} of a snapshot or bookmark is not modelled")
        out: list[str] = []
        for n in names:
            sep = "@" if "@" in n else "#"
            ds, _, short = n.partition(sep)
            d = self.ds.get(ds)
            lst = (d.snaps if sep == "@" else d.bookmarks) if d else []
            s = next((x for x in lst if x.name == short), None)
            if s is None:
                return _err(f"cannot open '{n}': dataset does not exist")
            out.append(f"{s.guid}\n")
        return _ok("".join(out))

    def _get_snapshots(self, root: str, prop: str, local: bool) -> RunResult:
        rows: list[str] = []
        for n in sorted(self.ds):
            if n != root and not n.startswith(root + "/"):
                continue
            for s in self.ds[n].snaps:
                value = s.props.get(prop, "") if local else self._inherited(n, s, prop)
                if value and value != "-":
                    rows.append(f"{n}@{s.name}\t{value}\n")
        return _ok("".join(rows))

    def _inherited(self, name: str, s: FSnap | None, prop: str) -> str:
        """A user property as ZFS shows it: the snapshot's own value, else its
        filesystem's, else the nearest ancestor's (dsl_prop.c:1224-1245)."""
        if s is not None and prop in s.props:
            return s.props[prop]
        parts = name.split("/")
        for i in range(len(parts), 0, -1):
            d = self.ds.get("/".join(parts[:i]))
            if d is not None and prop in d.props:
                return d.props[prop]
        return "-"

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

    def _row(  # pylint: disable=too-many-branches
        self, name: str, d: FDataset, s: FSnap | None, fields: list[str]
    ) -> str:
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
            elif f == "used" and s is None:
                vals.append(str(1024 * (1 + len(d.snaps))))
            elif f == "clones" and s is not None:
                vals.append(s.clones or "")
            elif f == "userrefs" and s is not None:
                vals.append(str(s.userrefs))
            elif f == "used" and s is not None:
                vals.append(str(s.used))
            elif f == "written" and s is None:
                vals.append(str(d.written) if d.snaps else "-")
            elif ":" in f:
                vals.append(self._inherited(name.split("@")[0].split("#")[0], s, f))
            else:
                vals.append(d.props.get(f, "-"))
        return "\t".join(vals)

    # ── snapshots, bookmarks, destroy, rename, rollback, set ─────────────

    def _snapshot(self, args: list[str]) -> RunResult:
        # `-o <user property>=<value>` is set on every snapshot of the call, in
        # the same sync task (module/zfs/zfs_ioctl.c:3815-3870).
        props = dict(args[i + 1].split("=", 1) for i, a in enumerate(args[:-1]) if a == "-o")
        names = [
            a
            for i, a in enumerate(args)
            if not a.startswith("-") and (i == 0 or args[i - 1] != "-o")
        ]
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
            self.ds[ds].snaps.append(
                FSnap(snap, str(self.guid), self.txg, self.clock, props=dict(props))
            )
        return _ok()

    def _bookmark(self, args: list[str]) -> RunResult:
        src, dst = args[-2], args[-1]
        ds, snap = src.split("@")
        bm = dst.split("#")[1]
        s = next((x for x in self.ds[ds].snaps if x.name == snap), None)
        if s is None:
            return _err(f"cannot bookmark '{src}': does not exist")
        if bm in self.bookmarks(ds):
            return _err(f"cannot create bookmark '{dst}': bookmark exists")
        ivset = self.features[ds.split("/")[0]] in ("enabled", "active")
        if ivset:
            self.features[ds.split("/")[0]] = "active"
        self.ds[ds].bookmarks.append(FSnap(bm, s.guid, s.txg, s.creation, ivset))
        return _ok()

    def _destroy(self, args: list[str]) -> RunResult:  # pylint: disable=too-many-return-statements
        recursive = "-r" in args
        target = args[-1]
        if "%" in target:
            return _err("fake: zark never destroys a % range")
        if any(a.startswith("-") and "n" in a for a in args[:-1]):
            return self._destroy_dry(target)
        if "@" in target and "," in target:
            return self._destroy_group(target)
        if "@" in target or "#" in target:
            sep = "@" if "@" in target else "#"
            ds, name = target.split(sep)
            if ds not in self.ds:
                return _err("does not exist")
            lst = self.ds[ds].snaps if sep == "@" else self.ds[ds].bookmarks
            hit = [x for x in lst if x.name == name]
            if not hit:
                return _err(f"could not find any snapshots to destroy: {target}")
            if hit[0].clones:
                return _err(f"cannot destroy '{target}': snapshot has dependent clones")
            if hit[0].userrefs:
                return _err(f"cannot destroy snapshot {target}: dataset is busy")
            lst.remove(hit[0])
            return _ok()
        kids = [n for n in self.ds if n.startswith(target + "/")]
        if kids and not recursive:
            return _err("filesystem has children")
        for n in [*kids, target]:
            self.ds.pop(n, None)
        return _ok()

    def _destroy_group(self, target: str) -> RunResult:
        """``zfs destroy ds@a,b,c``: a missing name is skipped, and one that
        cannot go keeps them all (dsl_destroy.c:589-601)."""
        ds, _, spec = target.partition("@")
        if ds not in self.ds:
            return _err("does not exist")
        names = spec.split(",")
        hit = [x for x in self.ds[ds].snaps if x.name in names]
        if not hit:
            return _err("could not find any snapshots to destroy; check snapshot names.")
        for x in hit:
            if x.clones:
                return _err(f"cannot destroy '{ds}@{x.name}': snapshot has dependent clones")
            if x.userrefs or f"{ds}@{x.name}" in self.fail_cmds:
                return _err(f"cannot destroy snapshot {ds}@{x.name}: dataset is busy")
        self.ds[ds].snaps = [x for x in self.ds[ds].snaps if x not in hit]
        return _ok()

    def _destroy_dry(self, target: str) -> RunResult:
        """``zfs destroy -nvp ds@a,b,…``: what it would free
        (cmd/zfs/zfs_main.c:1756-1779), the list split on commas."""
        ds, _, spec = target.partition("@")
        if ds not in self.ds or not spec:
            return _err("does not exist")
        names = spec.split(",")
        hit = [x for x in self.ds[ds].snaps if x.name in names]
        if len(hit) != len(set(names)):
            return _err("could not find any snapshots to destroy; check snapshot names.")
        lines = [f"destroy\t{ds}@{x.name}\n" for x in hit]
        return _ok("".join(lines) + f"reclaim\t{sum(x.used for x in hit)}\n")

    def _clone(self, args: list[str]) -> RunResult:
        """``zfs clone [-u] ds@snap new``: an empty head on that snapshot."""
        src, dst = args[-2], args[-1]
        ds, _, name = src.partition("@")
        s = next((x for x in self.ds[ds].snaps if x.name == name), None) if ds in self.ds else None
        if s is None:
            return _err(f"cannot open '{src}': dataset does not exist")
        if dst in self.ds:
            return _err(f"cannot create '{dst}': dataset already exists")
        old = self.ds[ds]
        self.ds[dst] = FDataset(kind=old.kind, props=dict(old.props), origin=src)
        s.clones = dst
        return _ok()

    def _promote(self, args: list[str]) -> RunResult:
        """``zfs promote clone``: the origin's snapshots up to the clone's
        origin move to the clone, which the old lineage is then a clone of."""
        ds = args[-1]
        d = self.ds.get(ds)
        if d is None or not d.origin:
            return _err(f"cannot promote '{ds}': not a cloned filesystem")
        src, _, name = d.origin.partition("@")
        old = self.ds[src]
        cut = [x.name for x in old.snaps].index(name) + 1
        d.snaps, old.snaps = old.snaps[:cut] + d.snaps, old.snaps[cut:]
        d.snaps[cut - 1].clones = src
        d.origin, old.origin = "", f"{ds}@{name}"
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
        self.ds[ds].written = 0
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
        """``zfs set k=v [k=v]… <dataset|snapshot>…``."""
        kvs = [a.split("=", 1) for a in args if "=" in a]
        targets = [a for a in args if "=" not in a]
        if not targets:
            return _err("missing dataset name(s)")
        for t in targets:
            ds, _, snap = t.partition("@")
            if ds not in self.ds or (snap and snap not in self.names(ds)):
                return _err(f"cannot open '{t}': dataset does not exist")
        for t in targets:
            ds, _, snap = t.partition("@")
            if snap:
                props = next(x for x in self.ds[ds].snaps if x.name == snap).props
            else:
                props = self.ds[ds].props
            for k, v in kvs:
                props[k] = v
        return _ok()

    def _inherit(self, args: list[str]) -> RunResult:
        ds = args[-1]
        if ds not in self.ds:
            return _err("does not exist")
        for k in args[:-1]:
            _ = self.ds[ds].props.pop(k, None)
        return _ok()

    # ── send / receive ───────────────────────────────────────────────────

    def _resume_check(self, token: str) -> str:
        """libzfs's checks before a resumed stream starts, dry run included
        (lib/libzfs/libzfs_sendrecv.c:1857-1890): "" when it can resume."""
        src_ds, snapname, frm = token.split("|")[1:]
        d = self.ds.get(src_ds, FDataset())
        if snapname not in self.names(src_ds):
            return (
                "cannot resume send: "
                + f"'{src_ds}@{snapname}' used in the initial send no longer exists"
            )
        if frm.startswith("#"):
            gone = frm[1:] not in [b.name for b in d.bookmarks]
        else:
            gone = bool(frm) and frm not in self.names(src_ds)
        if gone:
            return "cannot resume send: incremental source 0x1234 no longer exists"
        return ""

    def _send_dry(self, args: list[str]) -> RunResult:
        if "-t" in args:
            err = self._resume_check(args[args.index("-t") + 1])
            if err:
                return _err(err)
        if "-nvP" in args:
            incremental = any(a in args for a in ("-i", "-I", "-t"))
            if "-I" in args and args[args.index("-I") + 1].split("@")[0] != args[-1].split("@")[0]:
                # OpenZFS 2.4.1 estimates a clone sent with -I from its origin
                # as a full stream (libzfs_sendrecv.c:1237-1255).
                incremental = False
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

    def _pipe(self, send: list[str], recv: list[str]) -> RunResult:  # noqa: C901 # pylint: disable=too-many-return-statements,too-many-branches,too-many-locals,too-many-statements
        if "-F" in recv:
            raise AssertionError("the engine must never receive with -F")
        dst = recv[-1]
        raw, mode, frm, to, token = self._parse_send(send)
        if mode == "t":
            d = self.ds.get(dst)
            if d is None or not d.token:
                return _err("no partial state")
            err = self._resume_check(token)
            if err:
                return _err(err)
            if d.written:
                # The resumed stream is received in full, then refused at its
                # end and the state kept (module/zfs/dmu_recv.c:3632,
                # dsl_dataset.c:4101-4104; dmu_recv.c:2762-2765).
                return _err(
                    f"cannot receive resume stream: destination {dst} has been modified\n"
                    "since most recent snapshot"
                )
            src_ds, snapname, _frm = token.split("|")[1:]
            src = next(s for s in self.ds[src_ds].snaps if s.name == snapname)
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
        clone_src = frm.split("@")[0] if mode == "I" and "@" in frm else src_ds
        if clone_src != src_ds:
            # A clone sent from its origin (fromorigin) into a new dataset,
            # received as a clone of `-o origin=` (dmu_recv.c:778-800).
            got = self._clone_stream(src_ds, frm, to_snap, dst, recv)
            if isinstance(got, RunResult):
                return got
            stream, d = got, self.ds[dst]
        elif mode == "full":
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
            if d.written:
                # lib/libzfs/libzfs_sendrecv.c:5159-5174, as `<action>: <aux>`.
                return _err(
                    f"cannot receive incremental stream: destination {dst} has been modified\n"
                    "since most recent snapshot"
                )
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
                # The token names the stream's snapshot and its incremental
                # source, as the kernel's does (toguid, fromguid).
                if k:
                    frm = stream[k - 1].name
                else:
                    frm = "" if mode == "full" else frm.split("@")[-1] if "@" in frm else frm
                d.token = f"tok|{src_ds}|{s.name}|{frm}"
                return _err("cannot receive: connection reset (stream truncated)")
            d.snaps.append(FSnap(s.name, s.guid, self._dtxg(), s.creation))
        self._apply_props(d, recv)
        return _ok()

    def _clone_stream(
        self, src_ds: str, frm: str, to_snap: FSnap, dst: str, recv: list[str]
    ) -> list[FSnap] | RunResult:
        if dst in self.ds:
            return _err(f"destination '{dst}' exists")
        if self.ds[src_ds].props.get("origin") != frm:
            return _err("incremental source must be in same filesystem")
        target = next((a.split("=", 1)[1] for a in recv if a.startswith("origin=")), "")
        fds, fname = frm.split("@")
        base = next((x for x in self.ds[fds].snaps if x.name == fname), None)
        tds, _, tname = target.partition("@")
        held = next((x for x in self.ds.get(tds, FDataset()).snaps if x.name == tname), None)
        if base is None or held is None:
            return _err(f"cannot receive: local origin for clone {dst} does not exist")
        if held.guid != base.guid:
            return _err("cannot receive: most recent snapshot does not match incremental source")
        held.clones = dst
        self.ds[dst] = FDataset(kind=self.ds[src_ds].kind, props={"origin": target})
        return [x for x in self.ds[src_ds].snaps if x.txg <= to_snap.txg]

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
