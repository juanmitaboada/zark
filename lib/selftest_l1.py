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
Level-1 self-test checks on ZFS: file-backed pools, no disk (M4s-2).

Each docstring says what the check certifies and where that behaviour is
in the source of the version eli runs (OpenZFS 2.4.1, util-linux 2.41.3,
systemd 259.5, Linux 7.0, CPython 3.14.4, sanoid 2.3.0); the plan's
section 4 has the same table. The command shapes are zark's own.
"""

# One check per function, each with its source citations: long by design.
# pylint: disable=too-many-lines

import os
import shlex
import shutil
from pathlib import Path

from lib import identity, pool_record, sh, zfs_features
from lib.keystore import ZVOL_ID, Keystore
from lib.selftest import (
    Bench,
    Fail,
    Skip,
    check,
    exit_status,
    expect,
    observed,
    remove_empty_dirs,
    text_of,
)
from lib.zfs import BACKUP_ALTROOT_BASE, COMPAT_DIRS, DRIVE_COMPATIBILITY

ZPOOL_CACHE = "/etc/zfs/zpool.cache"
# A raw incremental of this many KiB is cut at half its size to leave partial state.
STREAM_KIB = 2048


def _q(s: str) -> str:
    return shlex.quote(s)


def _altroot(pool: str) -> str:
    return f"{BACKUP_ALTROOT_BASE}/{pool}"


def reimport(b: Bench, pool: str, extra: str = "") -> None:
    """Import one of the run's exported pools from its file, as zark imports a
    drive (``-N -R <altroot>``)."""
    _ = b.ok(f"zpool import -d {b.vdev(pool)} -N -R {_altroot(pool)} {extra} {pool}".strip())


def _origin(b: Bench, cid: str, kib: int = 256) -> str:
    """``<a>/<cid>``: an encrypted dataset of the origin pool with some data."""
    ds = b.fs(f"{b.a}/{cid}")
    b.write(ds, kib)
    return ds


def _send_full(b: Bench, src: str, dst: str) -> None:
    """The engine's NEW transfer (lib/engine.py:444-465)."""
    _ = b.pipe_ok(f"zfs send -w {_q(src)}", f"zfs receive -s -u {_recv_props()} {_q(dst)}")


def _recv_props() -> str:
    return (
        "-o canmount=noauto -x mountpoint "
        + "-o org.zark:canmount=on -o org.zark:mountpoint=/selftest"
    )


def _load_key(b: Bench, ds: str) -> None:
    """Load the run's key into a raw-received encryption root (its keylocation
    is ``prompt``)."""
    _ = b.ok(f"zfs load-key -L file://{b.key} {_q(ds)}")


# ── pools ────────────────────────────────────────────────────────────────


@check(
    "POOL-01",
    1,
    1,
    "create and import with -R leave zpool.cache alone",
    depends=(
        "I-G: ZFS.import_backup_pool (lib/zfs.py:293)",
        "prepare's zpool create -R",
        "health's test pool",
    ),
)
def pool_01(b: Bench) -> None:
    """``zpool create -R`` and ``zpool import -R`` default ``cachefile=none``;
    the pool never enters /etc/zfs/zpool.cache. OpenZFS
    cmd/zpool/zpool_main.c:2000-2008, :4416-4423; lib/libzfs/libzfs_pool.c:2068-2084;
    module/zfs/spa.c:916-938; module/zfs/spa_config.c:175-179, :193-205."""
    c = b.make_pool("c")
    try:
        got = b.pool_value("cachefile", c)
        expect(got in ("none", "-"), "cachefile none after zpool create -R", got)
        _ = b.ok(f"zpool export {c}")
        reimport(b, c)
        got = b.pool_value("cachefile", c)
        expect(got in ("none", "-"), "cachefile none after zpool import -R", got)
        cache = Path(ZPOOL_CACHE)
        if cache.exists():
            expect(c.encode() not in cache.read_bytes(), f"{c} not in {ZPOOL_CACHE}", "it is")
    finally:
        b.drop_pool(c)


@check(
    "POOL-02",
    1,
    1,
    "import -N mounts nothing; mounts land under the altroot",
    depends=("I-G", "zark mount and recover's explicit mounts", "recover's mount values (M1)"),
)
def pool_02(b: Bench) -> None:
    """``zpool import -N`` mounts nothing; with an altroot a later mount lands
    under it and ``zfs get mountpoint`` shows the prefixed value. OpenZFS
    cmd/zpool/zpool_main.c:3905-3915, :4402-4403; lib/libzfs/libzfs_dataset.c:2697-2764;
    lib/libzfs/libzfs_mount.c:241-269."""
    c = b.make_pool("c")
    try:
        _ = b.fs(f"{c}/x", "canmount=on", "mountpoint=/x")
        _ = b.ok(f"zpool export {c}")
        reimport(b, c)
        root = _altroot(c)
        mounted = [t for t in b.ok("findmnt -rn -o TARGET").lines if t.startswith(root)]
        expect(not mounted, f"nothing mounted under {root} after import -N", ", ".join(mounted))
        got = b.value("mountpoint", f"{c}/x")
        expect(got == f"{root}/x", f"mountpoint {root}/x", got)
        r = b.in_namespace(f"zfs mount {c}/x && findmnt -rn -o TARGET; zfs umount {c}/x")
        expect(f"{root}/x" in r.lines, f"zfs mount on {root}/x", "not mounted there", text_of(r))
    finally:
        b.drop_pool(c)


@check(
    "POOL-03",
    1,
    1,
    "import without -N mounts canmount=on only",
    depends=("the at-rest protection: drive filesystems canmount=noauto (redesign §9, F10)",),
)
def pool_03(b: Bench) -> None:
    """An import without ``-N`` mounts only ``canmount=on``, never ``noauto``,
    and nothing below a ``noauto`` dataset. OpenZFS
    lib/libzfs/libzfs_mount.c:1280-1307, :911-947 (:920-923), :258-259."""
    c = b.make_pool("c")
    try:
        _ = b.fs(f"{c}/on", "canmount=on", "mountpoint=/on")
        _ = b.fs(f"{c}/na", "canmount=noauto", "mountpoint=/na")
        _ = b.fs(f"{c}/na/kid", "canmount=on")
        _ = b.ok(f"zpool export {c}")
        root = _altroot(c)
        imp = f"zpool import -d {b.vdev(c)} -R {root} {c}"
        r = b.in_namespace(f"{imp} && findmnt -rn -o TARGET; zpool export {c}")
        expect(r.ok, "the import and export in a namespace to succeed", exit_status(r), text_of(r))
        mounted = [t for t in r.lines if t.startswith(root)]
        expect(mounted == [f"{root}/on"], f"only {root}/on mounted", ", ".join(mounted) or "none")
        reimport(b, c)
    finally:
        b.drop_pool(c)


@check(
    "POOL-04",
    1,
    1,
    "read-only import: no writes, keys load",
    depends=(
        "recover and zark mount read-only",
        "purge's and health's read-only test before a destroy",
        "backup's read-back",
    ),
)
def pool_04(b: Bench) -> None:
    """``readonly=on`` import: ``zpool get readonly`` is ``on``; ``zfs
    snapshot`` is refused with "pool is read-only"; ``zfs load-key`` works.
    OpenZFS module/zcommon/zpool_prop.c:160-162; module/zfs/spa.c:478-479;
    module/zfs/zfs_ioctl.c:7869-7890, :7459-7462, :7549-7552;
    lib/libzfs/libzfs_util.c:268-269."""
    c = b.make_pool(
        "c", f"-O encryption=aes-256-gcm -O keyformat=raw -O keylocation=file://{b.key}"
    )
    try:
        _ = b.fs(f"{c}/x")
        _ = b.ok(f"zpool export {c}")
        reimport(b, c, "-o readonly=on")
        got = b.pool_value("readonly", c)
        expect(got == "on", "readonly on", got)
        _ = b.refused(f"zfs snapshot {c}/x@s", "pool is read-only")
        _ = b.ok(f"zfs load-key {c}")
        got = b.value("keystatus", f"{c}/x")
        expect(got == "available", "keystatus available after load-key", got)
        _ = b.ok(f"zfs unload-key {c}")
    finally:
        b.drop_pool(c)


@check(
    "POOL-05",
    1,
    1,
    "a read-write destroy marks the labels DESTROYED",
    depends=("purge's and health's pool destroy, counted only when the pool is gone (M4aR-1)",),
)
def pool_05(b: Bench) -> None:
    """``zpool destroy`` of a read-write import writes DESTROYED to the
    labels: ``zpool import -d`` no longer lists it, ``-D`` lists it
    ``(DESTROYED)``. OpenZFS module/zfs/spa.c:7851-7855, :7726, :7759-7764;
    cmd/zpool/zpool_main.c:4384-4385, :3422-3426."""
    c = b.make_pool("c")
    vdev = b.vdev(c)
    guid = b.guids.pop(c)
    try:
        _ = b.ok(f"zpool destroy {c}")
        r = b.run(f"zpool import -d {vdev}")
        expect(f"pool: {c}" not in r.stdout, f"{c} not listed without -D", "listed", text_of(r))
        r = b.ok(f"zpool import -D -d {vdev}")
        expect(
            f"pool: {c}" in r.stdout and "(DESTROYED)" in r.stdout,
            f"{c} listed (DESTROYED) with -D",
            "not listed so",
            text_of(r),
        )
        expect(guid in r.stdout, "its GUID in the -D listing", "not there", text_of(r))
    finally:
        if sh.run(f"zpool get -Hp -o value guid {c}").output == guid:
            _ = b.run(f"zpool destroy {c}")
        Path(vdev).unlink(missing_ok=True)
        remove_empty_dirs(_altroot(c))


@check(
    "POOL-06",
    1,
    1,
    "zpool import -d lists only, in zark's format",
    depends=(
        "the bpool refusal (survey.require_boot_pool)",
        "health's import state",
        "chroot's listing",
        "recover's drive check (M4s-11)",
    ),
)
def pool_06(b: Bench) -> None:
    """``zpool import -d <dir>`` without a name lists only (``  pool: ``,
    ``    id: ``, `` state: ``) and imports nothing; with nothing to list it
    says "no pools available to import" on stderr and exits 0. OpenZFS
    cmd/zpool/zpool_main.c:3987-4017, :3415-3426, :4077-4079."""
    c = b.make_pool("c")
    try:
        guid = b.guids[c]
        _ = b.ok(f"zpool export {c}")
        r = b.ok(f"zpool import -d {b.vdev(c)}")
        for line in (f"  pool: {c}", f"    id: {guid}", " state: ONLINE"):
            expect(line in r.stdout.splitlines(), f"a line {line!r}", "no such line", text_of(r))
        expect(not b.run(f"zpool list -H {c}").ok, f"{c} still exported", "it was imported")
        empty = f"{b.dir}/empty"
        os.makedirs(empty, exist_ok=True)
        r = b.run(f"zpool import -d {empty}")
        expect(
            r.ok and "no pools available to import" in r.stderr,
            "exit 0 and 'no pools available to import' on stderr",
            exit_status(r),
            text_of(r),
        )
        reimport(b, c)
    finally:
        b.drop_pool(c)


@check(
    "POOL-07",
    1,
    1,
    "zdb -l reads a label read-only",
    depends=("identity.read_pool_label", "the registry match by GUID", "purge's label marks"),
)
def pool_07(b: Bench) -> None:
    """``zdb -l <vdev>`` prints ``name``, ``pool_guid`` and ``state`` (0
    active, 1 exported, 2 destroyed) and exits 2 where there is no label;
    zark's parser reads it. OpenZFS cmd/zdb/zdb.c:5483, :5629-5630;
    module/zfs/spa_config.c:368-387; include/sys/fs/zfs.h:1069-1078."""
    c = b.make_pool("c")
    try:
        guid = b.guids[c]
        _ = b.ok(f"zpool export {c}")
        r = b.ok(f"zdb -l {b.vdev(c)}")
        for line in (f"name: '{c}'", f"pool_guid: {guid}", "state: 1"):
            expect(line in r.stdout, f"{line!r} in the label", "not there", text_of(r))
        label = identity.read_pool_label(b.vdev(c))
        expect(
            label is not None and label.name == c and label.guid == guid,
            f"read_pool_label: {c} {guid}",
            "no label" if label is None else f"{observed(label.name)} {observed(label.guid)}",
        )
        blank = f"{b.dir}/blank.img"
        with open(blank, "wb") as f:
            f.truncate(64 * 1024**2)
        r = b.run(f"zdb -l {blank}")
        expect(r.returncode == 2, "zdb -l on a blank file: exit 2", f"exit {r.returncode}")
        os.unlink(blank)
        reimport(b, c)
    finally:
        b.drop_pool(c)


@check(
    "POOL-08",
    1,
    1,
    "zpool list -vHP prints full vdev paths",
    depends=("identity.protected_disks", "registry list", "repair-boot's kernel-name check"),
)
def pool_08(b: Bench) -> None:
    """``zpool list -vHP`` prints each vdev's full path. OpenZFS
    cmd/zpool/zpool_main.c:7430-7432, :7192-7193; lib/libzfs/libzfs_pool.c:4504-4561."""
    r = b.ok(f"zpool list -vHP {b.b}")
    firsts = [line.strip().split("\t")[0] for line in r.lines[1:]]
    expect(
        b.vdev(b.b) in firsts,
        f"a vdev line {b.vdev(b.b)}",
        f"{len(firsts)} other vdev line(s)",
        text_of(r),
    )


@check(
    "POOL-09",
    1,
    1,
    "encryption enables bookmark_v2; a bookmark activates it",
    depends=("setup's ensure_bookmark_v2", "the engine's refusal without it"),
)
def pool_09(b: Bench) -> None:
    """On a pool with encryption enabled ``feature@bookmark_v2`` is enabled
    (a dependency) and becomes active at the first bookmark. OpenZFS
    module/zcommon/zfeature_common.c:640-652, :628-638; module/zfs/zfeature.c:357-358;
    module/zfs/dsl_bookmark.c:364-372, :418-425."""
    c = b.make_pool(
        "c", f"-O encryption=aes-256-gcm -O keyformat=raw -O keylocation=file://{b.key}"
    )
    try:
        got = b.pool_value("feature@bookmark_v2", c)
        expect(got == "enabled", "bookmark_v2 enabled on a new encrypted pool", got)
        _ = b.fs(f"{c}/x")
        _ = b.ok(f"zfs snapshot {c}/x@s")
        _ = b.ok(f"zfs bookmark {c}/x@s {c}/x#s")
        got = b.pool_value("feature@bookmark_v2", c)
        expect(got == "active", "bookmark_v2 active after a bookmark", got)
    finally:
        b.drop_pool(c)


@check(
    "POOL-10",
    1,
    1,
    "compatibility=openzfs-2.2 receives zark's streams",
    depends=("prepare's compatibility (M4s-10)",),
)
def pool_10(b: Bench) -> None:
    """A pool created with ``-o compatibility=openzfs-2.2`` has every feature
    outside that set disabled and receives zark's raw streams. OpenZFS
    lib/libzfs/libzfs_pool.c:5170-5375; cmd/zpool/zpool_main.c:2237-2288;
    module/zfs/dmu_recv.c:561-620; module/zfs/dmu_send.c:1953-2032."""
    if not any(Path(d, DRIVE_COMPATIBILITY).is_file() for d in COMPAT_DIRS):
        raise Fail(f"a {DRIVE_COMPATIBILITY} feature set", "none in " + " or ".join(COMPAT_DIRS))
    allowed = set(zfs_features.COMPAT_2_2)
    c = b.make_pool("c", f"-o compatibility={DRIVE_COMPATIBILITY}")
    try:
        r = b.ok(f"zpool get -H -o property,value all {c}")
        enabled = [
            line.split("\t")[0][len("feature@") :]
            for line in r.lines
            if line.startswith("feature@") and line.split("\t")[1] != "disabled"
        ]
        extra = sorted(set(enabled) - allowed)
        expect(not extra, "no feature outside openzfs-2.2 enabled", ", ".join(extra))
        src = _origin(b, "pool10")
        _ = b.ok(f"zfs snapshot {src}@1")
        b.write(src)
        _ = b.ok(f"zfs snapshot {src}@2")
        _send_full(b, f"{src}@1", f"{c}/x")
        _ = b.pipe_ok(f"zfs send -w -I {src}@1 {src}@2", f"zfs receive -s -u {c}/x")
    finally:
        b.drop_pool(c)


@check(
    "POOL-11",
    1,
    1,
    "zpool upgrade -v names this ZFS's features",
    depends=("recover's drive check (M4s-11): the release each feature needs",),
)
def pool_11(b: Bench) -> None:
    """``zpool upgrade -v`` lists the features this ZFS supports by short
    name; every feature zark's table gives to this release or an older one is
    there. OpenZFS cmd/zpool/zpool_main.c:11821-11826, :11835-11880."""
    r = b.ok("zpool upgrade -v")
    listed = zfs_features.parse_upgrade_v(r.stdout)
    expect(bool(listed), "a feature list", "none parsed", text_of(r))
    version = zfs_features.release_tuple(_zfs_userland(b))
    want = [f.name for f in zfs_features.FEATURES if f.since <= version]
    missing = sorted(set(want) - set(listed))
    expect(not missing, "every feature of this release listed", ", ".join(missing))


@check(
    "POOL-12",
    1,
    1,
    "a pool's label carries the hostid of the system that imported it",
    depends=("finish's hostid line (M4c-4)", "identity.read_pool_label"),
)
def pool_12(b: Bench) -> None:
    """The label of a pool this system created and imported carries its
    hostid, the 4 little-endian bytes of /etc/hostid, which ``zdb -l``
    prints in decimal; zark's parser reads it. OpenZFS
    module/zfs/spa_misc.c:792; module/os/linux/spl/spl-generic.c:449-463,
    :400-443; module/zfs/spa.c:5671-5700; cmd/zdb/zdb.c:5447;
    module/nvpair/nvpair.c:3804-3806."""
    try:
        raw = Path("/etc/hostid").read_bytes()
    except OSError as e:
        raise Skip("this system has no /etc/hostid") from e
    if len(raw) < 4:
        raise Skip("/etc/hostid is shorter than 4 bytes")
    want = str(int.from_bytes(raw[:4], "little"))
    c = b.make_pool("c")
    try:
        _ = b.ok(f"zpool export {c}")
        reimport(b, c)
        _ = b.ok(f"zpool export {c}")
        r = b.ok(f"zdb -l {b.vdev(c)}")
        expect(f"hostid: {want}" in r.stdout, f"hostid: {want} in the label", "not there")
        label = identity.read_pool_label(b.vdev(c))
        got = label.hostid if label is not None else "no label"
        expect(got == want, f"read_pool_label: hostid {want}", observed(got))
        reimport(b, c)
    finally:
        b.drop_pool(c)


@check(
    "POOL-13",
    1,
    1,
    "zpool create -d with a feature list enables only those, as the record reads them",
    depends=(
        "recover's pools as origin had them (M6-6): pool_record.create_spec and options",
        "prepare's and backup's record of origin's pools (M6-5): pool_record.parse_get",
    ),
)
def pool_13(b: Bench) -> None:
    """``zpool create -d -o feature@X=enabled ...`` enables only the listed
    features and the ones they depend on; every other is disabled. One that
    activates on enable is ``active``. ``zpool get -H -p all`` prints each
    with its state and zark's reader gives back the same set. OpenZFS
    cmd/zpool/zpool_main.c:1997-1998, :2246-2288; module/zfs/zfeature.c:340-358;
    module/zcommon/zfeature_common.c:387-390, :629-651;
    lib/libzfs/libzfs_pool.c:1174-1185."""
    known = set(zfs_features.parse_upgrade_v(b.ok("zpool upgrade -v").stdout))
    asked = {"async_destroy": "enabled", "encryption": "enabled", "lz4_compress": "active"}
    spec = pool_record.create_spec(pool_record.PoolState("off", asked), known)
    expect(
        sorted(spec.features) == sorted(asked),
        "every asked feature known to this ZFS",
        ", ".join(sorted(set(asked) - set(spec.features))),
    )
    want = {
        **asked,
        "bookmark_v2": "enabled",  # encryption's dependencies
        "bookmarks": "enabled",
        "extensible_dataset": "enabled",
    }
    c = b.make_pool("c", spec.options())
    try:
        r = b.ok(f"zpool get -H -p -o property,value all {c}")
        got = pool_record.parse_get(r.stdout)
        extra = sorted(set(got.features) - set(want))
        expect(not extra, "no feature beyond the asked and their dependencies", ", ".join(extra))
        wrong = sorted(
            f"{k}={got.features.get(k, 'disabled')}"
            for k, v in want.items()
            if got.features.get(k) != v
        )
        expect(not wrong, "each feature in its state", ", ".join(wrong), text_of(r))
        expect(got.compatibility == "off", "compatibility off", observed(got.compatibility))
    finally:
        b.drop_pool(c)


def _zfs_userland(b: Bench) -> str:
    out = b.ok("zfs version").lines
    return out[0].removeprefix("zfs-") if out else ""


# ── snapshots and properties ─────────────────────────────────────────────


@check(
    "SNP-01",
    1,
    1,
    "one zfs snapshot call is one txg, and one pool",
    depends=("I-C: Run.take_point (lib/engine.py:187)",),
    fake=True,
)
def snp_01(b: Bench) -> None:
    """One ``zfs snapshot`` of several datasets of one pool gives them one
    ``createtxg``; one that names two pools fails and creates nothing. OpenZFS
    module/zfs/zfs_ioctl.c:3842-3846, :3860-3866; module/zfs/dsl_dataset.c:1906-1937;
    lib/libzfs/libzfs_dataset.c:4266-4273."""
    x, y = b.fs(f"{b.a}/snp01x"), b.fs(f"{b.a}/snp01y")
    z = b.fs(f"{b.b}/snp01z")
    _ = b.ok(f"zfs snapshot {x}@p {y}@p")
    tx, ty = b.snapshots(x)[0][2], b.snapshots(y)[0][2]
    expect(tx == ty, "one createtxg for both", f"{tx} and {ty}")
    _ = b.refused(f"zfs snapshot {x}@q {z}@q")
    made = [n for ds in (x, z) for n in b.snapshot_names(ds) if n == "q"]
    expect(not made, "no @q created", f"{len(made)} created")


@check(
    "SNP-02",
    1,
    1,
    "zfs snapshot -o sets a user property on every snapshot",
    depends=("points that name their drive (M4b-4)", "release_point (lib/anchors.py:73)"),
    fake=True,
)
def snp_02(b: Bench) -> None:
    """``zfs snapshot -o org.zark:point-for=<x>`` sets it, source ``local``,
    on every snapshot of the call. OpenZFS cmd/zfs/zfs_main.c:4663-4667, :4711;
    module/zfs/zfs_ioctl.c:3812-3870; module/zfs/dsl_dataset.c:1924-1927."""
    x, y = b.fs(f"{b.a}/snp02x"), b.fs(f"{b.a}/snp02y")
    _ = b.ok(f"zfs snapshot -o org.zark:point-for=1234 {x}@p {y}@p")
    for s in (f"{x}@p", f"{y}@p"):
        got = b.value("org.zark:point-for", s)
        expect(got == "1234", f"{s} point-for 1234", got)
        got = b.source("org.zark:point-for", s)
        expect(got == "local", f"{s} point-for local", got)


@check(
    "SNP-03",
    1,
    1,
    "user properties on snapshots: multi-target set, local versus inherited",
    depends=("completion marks: record_complete, drive_snapshots, recover (M2V-3, M3-5)",),
    fake=True,
)
def snp_03(b: Bench) -> None:
    """A user property set on several snapshots in one ``zfs set``; with one
    target missing the others are set and the exit is non-zero; ``zfs get -s
    local -t snapshot`` returns only what was set on the snapshot, while a
    value set on an ancestor shows without ``-s local``. OpenZFS
    module/zfs/zfs_ioctl.c:4875-4881; cmd/zfs/zfs_main.c:4534-4603;
    cmd/zfs/zfs_iter.c:515-536; module/zfs/dsl_prop.c:1208-1246;
    cmd/zfs/zfs_main.c:2124-2146."""
    top = b.fs(f"{b.a}/snp03")
    x = b.fs(f"{top}/x")
    _ = b.ok(f"zfs snapshot {x}@1")
    _ = b.ok(f"zfs snapshot {x}@2")
    _ = b.ok(f"zfs set org.zark:t=1 {x}@1 {x}@2")
    r = b.run(f"zfs set org.zark:t=2 {x}@1 {x}@missing")
    expect(not r.ok, "a set naming a missing snapshot to exit non-zero", "exit 0")
    got = b.value("org.zark:t", f"{x}@1")
    expect(got == "2", f"{x}@1 set anyway (2)", got)
    _ = b.ok(f"zfs set org.zark:anc=v {top}")
    local = "zfs get -H -p -r -s local -t snapshot -o name,value"
    r = b.ok(f"{local} org.zark:t {top}")
    expect(
        sorted(r.lines) == [f"{x}@1\t2", f"{x}@2\t1"],
        "the two local values",
        f"{len(r.lines)} line(s)",
        text_of(r),
    )
    r = b.ok(f"{local} org.zark:anc {top}")
    expect(
        not r.lines, "no local org.zark:anc on a snapshot", f"{len(r.lines)} line(s)", text_of(r)
    )
    got = b.value("org.zark:anc", f"{x}@1")
    expect(got == "v", "the ancestor's value shown on the snapshot", got)


@check(
    "SNP-04",
    1,
    1,
    "written: numeric always, unchanged by a snapshot property",
    depends=("MODIFIED detection after the marks (lib/replication.py:235)",),
    fake=True,
)
def snp_04(b: Bench) -> None:
    """Setting a snapshot's user property leaves the head's ``written`` at 0;
    ``written`` is a number on every filesystem, also without a user
    snapshot. OpenZFS module/zfs/dsl_dataset.c:2940-2967, :2862-2867,
    :1186-1187, :1214-1218; module/zfs/dsl_prop.c:740-750."""
    fresh = b.fs(f"{b.a}/snp04n")
    got = b.value("written", fresh)
    expect(got.isdigit(), "written numeric with no snapshot", got)
    x = _origin(b, "snp04")
    _ = b.ok(f"zfs snapshot {x}@s")
    _ = b.ok(f"zfs set org.zark:complete=1 {x}@s")
    _ = b.ok(f"zpool sync {b.a}")
    got = b.value("written", x)
    expect(got == "0", "written 0 after a snapshot property", got)


@check(
    "SNP-05",
    1,
    1,
    "a received snapshot keeps its GUID and creation",
    depends=("the planner's GUID matching (lib/replication.py:262)", "restore points by creation"),
    fake=True,
)
def snp_05(b: Bench) -> None:
    """A received snapshot keeps the sender's GUID and ``creation``. OpenZFS
    module/zfs/dmu_recv.c:3716-3723, :3742-3749; module/zfs/dsl_dataset.c:1774."""
    x = _origin(b, "snp05")
    _ = b.ok(f"zfs snapshot {x}@p")
    _send_full(b, f"{x}@p", f"{b.b}/snp05")
    for prop in ("guid", "creation"):
        mine, theirs = b.value(prop, f"{x}@p"), b.value(prop, f"{b.b}/snp05@p")
        expect(mine == theirs, f"{prop} {mine} on the drive", theirs)


@check(
    "SNP-06",
    1,
    1,
    "partial receive state: hidden child, token on the parent",
    depends=(
        "RESUME and stale detection",
        "apply_prune naming what a -r takes (lib/reclaim.py:132)",
    ),
    fake=True,
)
def snp_06(b: Bench) -> None:
    """A partial receive's ``%recv`` child never appears in ``zfs list``; its
    parent's ``receive_resume_token`` shows it. OpenZFS
    module/zcommon/zfs_comutil.c:232-244; module/zfs/zfs_ioctl.c:2357-2363;
    module/zfs/dsl_dataset.c:2504-2529, :2870-2876."""
    x = _origin(b, "snp06")
    dst = f"{b.b}/snp06"
    _ = b.ok(f"zfs snapshot {x}@1")
    _send_full(b, f"{x}@1", dst)
    b.write(x, STREAM_KIB)
    _ = b.ok(f"zfs snapshot {x}@2")
    _ = b.cut(f"zfs send -w -i {x}@1 {x}@2", f"zfs receive -s -u {dst}", STREAM_KIB * 512)
    names = b.ok(f"zfs list -H -o name -t all -r {dst}").lines
    expect(not [n for n in names if "%" in n], "no % name listed", ", ".join(names))
    token = b.value("receive_resume_token", dst)
    expect(token not in ("", "-"), "a receive_resume_token", token or "none")
    _ = b.ok(f"zfs receive -A {dst}")


@check(
    "SNP-07",
    1,
    1,
    "a user property set on a dataset is local there, inherited below, listed with -s local",
    depends=("zark exclude and exclusions.read (lib/exclusions.py:195)", "D7, M5b-4 4a"),
    fake=True,
)
def snp_07(b: Bench) -> None:
    """``org.zark:replicate=no`` set on a dataset is ``local`` there and
    ``inherited from`` it on a child; ``zfs get -s local -r`` lists the
    dataset only, and ``zfs inherit`` removes it. OpenZFS
    cmd/zfs/zfs_main.c:2292-2312; man/man7/zfsprops.7:2279-2280;
    module/zfs/dsl_prop.c:105, :115-121."""
    top = b.fs(f"{b.a}/snp07")
    child = b.fs(f"{top}/c")
    prop = "org.zark:replicate"
    _ = b.ok(f"zfs set {prop}=no {top}")
    got = b.source(prop, top)
    expect(got == "local", f"{prop} local on {top}", got)
    got = b.source(prop, child)
    expect(got == f"inherited from {top}", f"{prop} inherited from {top} on {child}", got)
    listing = f"zfs get -H -s local -o name,property,value {prop},org.zark:snapshot -r {b.a}"
    got = ", ".join(b.ok(listing).lines)
    expect(got == f"{top}\t{prop}\tno", f"{top} alone with -s local", got)
    _ = b.ok(f"zfs inherit {prop} {top}")
    got = ", ".join(b.ok(listing).lines)
    expect(got == "", "nothing listed after zfs inherit", got)


@check(
    "SNP-08",
    1,
    1,
    "user properties of 8191 bytes, several in one zfs set, on snapshots",
    depends=("the point's record in parts: exclusions.record, point_records (M5bR-1)",),
    fake=True,
)
def snp_08(b: Bench) -> None:
    """One ``zfs set`` of four user properties of 8191 bytes each on two
    snapshots sets them all on both; ``zfs get -s local -t snapshot -r -o
    name,property,value`` with the four names returns each value whole; a
    value of 8192 bytes is refused and the snapshot stays without it.
    OpenZFS include/sys/fs/zfs.h:86; module/zfs/dsl_prop.c:963-970,
    :1033-1054; cmd/zfs/zfs_main.c:4574-4603, :2292-2316;
    lib/libzfs/libzfs_util.c:1943-1959."""
    top = b.fs(f"{b.a}/snp08")
    x = b.fs(f"{top}/x")
    for s in ("1", "2"):
        _ = b.ok(f"zfs snapshot {x}@{s}")
    # A different digit per property, so a value read under another name shows.
    values = {
        name: str(i) * 8191
        for i, name in enumerate(("org.zark:t", "org.zark:t.1", "org.zark:t.2", "org.zark:t.3"))
    }
    _ = b.ok(f"zfs set {' '.join(f'{k}={v}' for k, v in values.items())} {x}@1 {x}@2")
    r = b.ok(
        f"zfs get -H -p -r -s local -t snapshot -o name,property,value {','.join(values)} {top}"
    )
    want = sorted(f"{x}@{s}\t{k}\t{v}" for s in ("1", "2") for k, v in values.items())
    got = sorted(r.lines)
    expect(
        got == want,
        "the four values of 8191 bytes on each snapshot, whole",
        f"{len(got)} line(s), lengths {[len(line) for line in got]}",
    )
    r = b.run(f"zfs set org.zark:t.4={'9' * 8192} {x}@1")
    expect(not r.ok, "a value of 8192 bytes to be refused", "exit 0")
    got1 = b.value("org.zark:t.4", f"{x}@1")
    expect(got1 == "-", f"{x}@1 without org.zark:t.4", got1[:16])


# ── send and receive ─────────────────────────────────────────────────────


@check(
    "SND-01",
    1,
    1,
    "raw stream under an unencrypted parent: own encryption root, no key",
    depends=("the engine's NEW raw transfer", "prepare", "unencrypted containers (M2 decision 13)"),
)
def snd_01(b: Bench) -> None:
    """``zfs send -w`` of an encrypted dataset received below an unencrypted
    parent: no key needed, its own encryption root, ``keystatus``
    unavailable, ``keylocation`` prompt. OpenZFS module/zfs/dmu_recv.c:723-746,
    :653-664, :867-874, :3369-3392; module/zfs/dsl_crypt.c:1940-1941,
    :2295-2328, :1182-1190."""
    x = _origin(b, "snd01")
    _ = b.ok(f"zfs snapshot {x}@p")
    dst = f"{b.b}/snd01"
    _send_full(b, f"{x}@p", dst)
    for prop, want in (
        ("encryptionroot", dst),
        ("keystatus", "unavailable"),
        ("keylocation", "prompt"),
    ):
        got = b.value(prop, dst)
        expect(got == want, f"{prop} {want}", got)


@check(
    "SND-02",
    1,
    1,
    "receive -u -o/-x: local properties, nothing mounted",
    depends=("Run._recv (lib/engine.py:446)", "I-G", "the at-rest protection"),
    fake=True,
)
def snd_02(b: Bench) -> None:
    """``zfs receive -s -u -o canmount=noauto -x mountpoint -o org.zark:...``
    on a raw full and a raw incremental receive: the properties are
    ``local``, nothing is mounted; an encryption property with ``-o`` on a
    raw stream is refused. OpenZFS module/zfs/zfs_ioctl.c:5404-5430, :5560-5614;
    lib/libzfs/libzfs_sendrecv.c:4265-4272, :4296-4338, :5323-5327, :5611;
    cmd/zfs/zfs_main.c:5215-5216."""
    x = _origin(b, "snd02")
    dst = f"{b.b}/snd02"
    _ = b.ok(f"zfs snapshot {x}@1")
    _send_full(b, f"{x}@1", dst)
    b.write(x)
    _ = b.ok(f"zfs snapshot {x}@2")
    _ = b.pipe_ok(f"zfs send -w -i {x}@1 {x}@2", f"zfs receive -s -u {_recv_props()} {dst}")
    for prop, want in (
        ("canmount", "noauto"),
        ("org.zark:canmount", "on"),
        ("org.zark:mountpoint", "/selftest"),
    ):
        got = b.value(prop, dst)
        expect(got == want, f"{prop} {want}", got)
        got = b.source(prop, dst)
        expect(got == "local", f"{prop} local", got)
    got = b.value("mountpoint", dst)
    expect(got == "none", "mountpoint none (inherited from the drive's root)", got)
    got = b.value("mounted", dst)
    expect(got == "no", "not mounted", got)
    _ = b.pipe_refused(
        f"zfs send -w {x}@2",
        f"zfs receive -u -o encryption=off {b.b}/snd02r",
        "cannot be set or excluded for raw streams",
    )


@check(
    "SND-03",
    1,
    1,
    "-I carries intermediates; bookmarks as incremental sources",
    depends=("VIA_BOOKMARK's two steps (lib/engine.py:246-261)", "anchors (I-B, D16, F5)"),
    fake=True,
)
def snd_03(b: Bench) -> None:
    """``-I`` carries every intermediate; ``-I`` from a bookmark is refused;
    ``-w -i ds#bm ds@s`` is received after the bookmark's snapshot was
    destroyed, and the bookmark survives that destroy. OpenZFS
    cmd/zfs/zfs_main.c:4813-4818, :5024-5077; lib/libzfs/libzfs_sendrecv.c:1194-1215;
    module/zfs/dsl_bookmark.c:364-368, :1386-1395; module/zfs/dsl_crypt.c:2073-2086."""
    x = _origin(b, "snd03")
    dst = f"{b.b}/snd03"
    for n in ("1", "2", "3"):
        _ = b.ok(f"zfs snapshot {x}@{n}")
    _send_full(b, f"{x}@1", dst)
    _ = b.pipe_ok(f"zfs send -w -I {x}@1 {x}@3", f"zfs receive -s -u {dst}")
    names = b.snapshot_names(dst)
    expect(names == ["1", "2", "3"], "1, 2, 3 on the drive", ", ".join(names))
    _ = b.ok(f"zfs bookmark {x}@3 {x}#b3")
    _ = b.ok(f"zfs snapshot {x}@4")
    _ = b.pipe_refused(
        f"zfs send -w -I {x}#b3 {x}@4",
        f"zfs receive -s -u {dst}",
        "multiple snapshots cannot be sent from a bookmark",
    )
    _ = b.ok(f"zfs destroy {x}@3")
    names = b.ok(f"zfs list -H -o name -t bookmark -r {x}").lines
    expect(f"{x}#b3" in names, f"{x}#b3 kept", ", ".join(names))
    b.write(x)
    _ = b.ok(f"zfs snapshot {x}@5")
    _ = b.pipe_ok(f"zfs send -w -i {x}#b3 {x}@4", f"zfs receive -s -u {dst}")
    _ = b.pipe_ok(f"zfs send -w -I {x}@4 {x}@5", f"zfs receive -s -u {dst}")
    got = b.snapshot_names(dst)
    expect(got[-2:] == ["4", "5"], "4 and 5 received after the bookmark", ", ".join(got))


@check(
    "SND-04",
    1,
    1,
    "without -F: wrong base, a written destination and a full stream are refused",
    depends=(
        "I-A's second net",
        "the MODIFIED state",
        "Outcome.error (M2R-12)",
        "NEW only for missing datasets",
    ),
    fake=True,
)
def snd_04(b: Bench) -> None:
    """Without ``-F``: a raw incremental whose base is not the destination's
    newest is refused; an incremental onto a written destination is refused
    with "has been modified since most recent snapshot"; a full stream into
    an existing dataset is refused ("exists"). OpenZFS module/zfs/dmu_recv.c:463-500;
    module/zfs/dsl_dataset.c:2940-2967; lib/libzfs/libzfs_sendrecv.c:5161-5174,
    :4764-4772; lib/libzfs/libzfs_util.c:369."""
    x = _origin(b, "snd04")
    dst = f"{b.b}/snd04"
    for n in ("1", "2", "3"):
        _ = b.ok(f"zfs snapshot {x}@{n}")
    _send_full(b, f"{x}@1", dst)
    _ = b.pipe_ok(f"zfs send -w -i {x}@1 {x}@2", f"zfs receive -s -u {dst}")
    _ = b.pipe_refused(
        f"zfs send -w -i {x}@1 {x}@3",
        f"zfs receive -s -u {dst}",
        "has been modified",
        "since most recent snapshot",
    )
    _load_key(b, dst)
    b.write(dst, 64)
    _ = b.ok(f"zfs unload-key {dst}")
    _ = b.pipe_refused(
        f"zfs send -w -i {x}@2 {x}@3",
        f"zfs receive -s -u {dst}",
        "has been modified",
        "since most recent snapshot",
    )
    _ = b.pipe_refused(f"zfs send -w {x}@3", f"zfs receive -s -u {dst}", f"'{dst}' exists")
    names = b.snapshot_names(dst)
    expect(names == ["1", "2"], "the drive still at 1, 2", ", ".join(names))


@check(
    "SND-05",
    1,
    1,
    "a cut receive -s resumes with send -t",
    depends=("RESUME (lib/engine.py:238-239, :448-449)",),
    fake=True,
)
def snd_05(b: Bench) -> None:
    """A stream cut with ``receive -s`` leaves ``receive_resume_token``; a new
    stream into it is refused ("partially-complete state"); ``send -nvP -t``
    gives a ``size`` line; ``send -t | receive -s -u`` completes. OpenZFS
    module/zfs/dmu_recv.c:3425, :2762-2773, :374-383;
    lib/libzfs/libzfs_sendrecv.c:5303-5311, :1810-1811, :1666, :4148-4179."""
    x = _origin(b, "snd05")
    dst = f"{b.b}/snd05"
    _ = b.ok(f"zfs snapshot {x}@1")
    _send_full(b, f"{x}@1", dst)
    b.write(x, STREAM_KIB)
    _ = b.ok(f"zfs snapshot {x}@2")
    _ = b.cut(f"zfs send -w -i {x}@1 {x}@2", f"zfs receive -s -u {dst}", STREAM_KIB * 512)
    token = b.value("receive_resume_token", dst)
    expect(token not in ("", "-"), "a receive_resume_token", token or "none")
    _ = b.pipe_refused(
        f"zfs send -w -i {x}@1 {x}@2", f"zfs receive -s -u {dst}", "partially-complete state"
    )
    r = b.ok(f"zfs send -nvP -t {_q(token)}")
    sizes = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.startswith("size\t")]
    expect(bool(sizes), "a size line from send -nvP -t", "none", text_of(r))
    _ = b.pipe_ok(f"zfs send -t {_q(token)}", f"zfs receive -s -u {dst}")
    names = b.snapshot_names(dst)
    expect(names == ["1", "2"], "1, 2 after the resume", ", ".join(names))
    got = b.value("receive_resume_token", dst)
    expect(got in ("", "-"), "no token after the resume", got)


@check(
    "SND-06",
    1,
    1,
    "a stale token is refused by name; receive -A drops only the partial state",
    depends=("M2R-10's discard (lib/engine.py:497-502)", "abort_partial (lib/engine.py:523)"),
    fake=True,
)
def snd_06(b: Bench) -> None:
    """After the token's snapshot is destroyed ``send -nvP -t`` fails with
    "used in the initial send no longer exists"; for a same-name snapshot
    with another GUID, "is no longer the same snapshot"; ``zfs receive -A``
    removes only the partial state. OpenZFS lib/libzfs/libzfs_sendrecv.c:1856-1894,
    :1939; cmd/zfs/zfs_main.c:5262-5301."""
    x = _origin(b, "snd06")
    dst = f"{b.b}/snd06"
    _ = b.ok(f"zfs snapshot {x}@1")
    _send_full(b, f"{x}@1", dst)
    b.write(x, STREAM_KIB)
    _ = b.ok(f"zfs snapshot {x}@2")
    _ = b.cut(f"zfs send -w -i {x}@1 {x}@2", f"zfs receive -s -u {dst}", STREAM_KIB * 512)
    token = b.value("receive_resume_token", dst)
    _ = b.ok(f"zfs destroy {x}@2")
    _ = b.refused(f"zfs send -nvP -t {_q(token)}", "used in the initial send no longer exists")
    _ = b.ok(f"zfs snapshot {x}@2")
    _ = b.refused(f"zfs send -nvP -t {_q(token)}", "is no longer the same snapshot")
    _ = b.ok(f"zfs receive -A {dst}")
    got = b.value("receive_resume_token", dst)
    expect(got in ("", "-"), "no token after receive -A", got)
    names = b.snapshot_names(dst)
    expect(names == ["1"], "1 kept on the drive", ", ".join(names))


@check(
    "SND-07",
    1,
    1,
    "a resume onto a written head fails at its end; abort, roll back, resend",
    depends=("M2V-1's DISCARD (Run.discard_changes, lib/engine.py:295)",),
    fake=True,
)
def snd_07(b: Bench) -> None:
    """A resumed receive (with ``-s``) onto a head written since its newest
    snapshot fails at its end and keeps the token; ``receive -A``, then
    ``rollback`` without ``-r``, then the incremental, completes. OpenZFS
    module/zfs/dmu_recv.c:1071-1228, :3632-3637, :3845-3849, :2762-2765;
    module/zfs/dsl_dataset.c:4101-4104."""
    x = _origin(b, "snd07")
    dst = f"{b.b}/snd07"
    _ = b.ok(f"zfs snapshot {x}@1")
    _send_full(b, f"{x}@1", dst)
    b.write(x, STREAM_KIB)
    _ = b.ok(f"zfs snapshot {x}@2")
    _ = b.cut(f"zfs send -w -i {x}@1 {x}@2", f"zfs receive -s -u {dst}", STREAM_KIB * 512)
    token = b.value("receive_resume_token", dst)
    _load_key(b, dst)
    b.write(dst, 64)
    _ = b.ok(f"zfs unload-key {dst}")
    _ = b.pipe_refused(f"zfs send -t {_q(token)}", f"zfs receive -s -u {dst}", "has been modified")
    got = b.value("receive_resume_token", dst)
    expect(got not in ("", "-"), "the token kept after the refused resume", got or "none")
    _ = b.ok(f"zfs receive -A {dst}")
    _ = b.ok(f"zfs rollback {dst}@1")
    _ = b.pipe_ok(f"zfs send -w -i {x}@1 {x}@2", f"zfs receive -s -u {dst}")
    names = b.snapshot_names(dst)
    expect(names == ["1", "2"], "1, 2 after the discard", ", ".join(names))


@check(
    "SND-08",
    1,
    1,
    "rollback: to the newest without -r, older only with -r",
    depends=("DISCARD", "ROLLBACK's roll back option (lib/engine.py:289-293)"),
    fake=True,
)
def snd_08(b: Bench) -> None:
    """``zfs rollback <ds>@<newest>`` without ``-r`` clears what was written
    and destroys nothing; to an older snapshot it is refused ("more recent
    snapshots or bookmarks exist"); ``rollback -r`` destroys exactly the later
    snapshots. OpenZFS cmd/zfs/zfs_main.c:4355-4410, :4482-4489;
    module/zfs/dsl_dataset.c:3141-3229; man/man8/zfs-rollback.8:47-73."""
    x = _origin(b, "snd08")
    dst = f"{b.b}/snd08"
    for n in ("1", "2", "3"):
        _ = b.ok(f"zfs snapshot {x}@{n}")
    _send_full(b, f"{x}@1", dst)
    _ = b.pipe_ok(f"zfs send -w -I {x}@1 {x}@3", f"zfs receive -s -u {dst}")
    _load_key(b, dst)
    b.write(dst, 64)
    _ = b.ok(f"zfs unload-key {dst}")
    _ = b.ok(f"zfs rollback {dst}@3")
    _ = b.ok(f"zpool sync {b.b}")
    got = b.value("written", dst)
    expect(got == "0", "written 0 after the rollback", got)
    got = ", ".join(b.snapshot_names(dst))
    expect(got == "1, 2, 3", "1, 2, 3 kept", got)
    _ = b.refused(f"zfs rollback {dst}@1", "more recent snapshots")
    _ = b.ok(f"zfs rollback -r {dst}@2")
    got = ", ".join(b.snapshot_names(dst))
    expect(got == "1, 2", "1, 2 after rollback -r to 2", got)


@check(
    "SND-09",
    1,
    1,
    "send -nvP sizes; a clone estimated from its origin with -i",
    depends=("Run.estimate (lib/engine.py:263) and its -i for a clone (M3 7a)", "the space check"),
    fake=True,
)
def snd_09(b: Bench) -> None:
    """``zfs send -nvP`` prints ``size\\t<n>``; for a clone, ``-nvP -i
    <origin> <clone>@<s>`` reports the delta, not the clone's full size.
    OpenZFS lib/libzfs/libzfs_sendrecv.c:1079-1106, :1666, :2464-2469,
    :1237-1255, :1282-1283."""
    x = _origin(b, "snd09", STREAM_KIB)
    _ = b.ok(f"zfs snapshot {x}@1")
    clone = f"{b.a}/snd09c"
    _ = b.ok(f"zfs clone -u {x}@1 {clone}")
    b.write(clone, 64)
    _ = b.ok(f"zfs snapshot {clone}@c")
    full = _size(b, f"zfs send -nvP -w {clone}@c")
    delta = _size(b, f"zfs send -nvP -w -i {x}@1 {clone}@c")
    expect(delta < full, "the -i estimate smaller than the full one", f"{delta} and {full}")


def _size(b: Bench, cmd: str) -> int:
    r = b.ok(cmd)
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        f = line.split("\t")
        if len(f) == 2 and f[0] == "size" and f[1].isdigit():
            return int(f[1])
    raise Fail(f"a size line from `{cmd}`", "none", text_of(r))


@check(
    "SND-10",
    1,
    1,
    "a clone sent from its origin is attached to that origin by GUID",
    depends=("O3 (lib/engine.py:240-243, :451-453)",),
    fake=True,
)
def snd_10(b: Bench) -> None:
    """``zfs send -w -I <origin> <clone>@<p>`` is a compound stream: the
    receive finds the clone's origin on the destination by the stream's
    GUID, whatever ``-o origin=`` names, and refuses the stream when no
    snapshot there has it ("local origin for clone ... does not exist").
    OpenZFS cmd/zfs/zfs_main.c:5105-5116; lib/libzfs/libzfs_sendrecv.c:4615-4631,
    :5602-5606, :5467-5472."""
    x = _origin(b, "snd10")
    other = _origin(b, "snd10o")
    alone = _origin(b, "snd10z")
    for ds in (x, other, alone):
        _ = b.ok(f"zfs snapshot {ds}@1")
    clone, stray = f"{b.a}/snd10c", f"{b.a}/snd10s"
    _ = b.ok(f"zfs clone -u {x}@1 {clone}")
    _ = b.ok(f"zfs clone -u {alone}@1 {stray}")
    b.write(clone, 64)
    _ = b.ok(f"zfs snapshot {clone}@p")
    _ = b.ok(f"zfs snapshot {stray}@p")
    _send_full(b, f"{x}@1", f"{b.b}/snd10")
    _send_full(b, f"{other}@1", f"{b.b}/snd10o")
    _ = b.pipe_ok(
        f"zfs send -w -I {x}@1 {clone}@p",
        f"zfs receive -s -u -o origin={b.b}/snd10o@1 {_recv_props()} {b.b}/snd10c",
    )
    got = b.value("origin", f"{b.b}/snd10c")
    expect(got == f"{b.b}/snd10@1", f"origin {b.b}/snd10@1 (by GUID)", got)
    _ = b.pipe_refused(
        f"zfs send -w -I {alone}@1 {stray}@p",
        f"zfs receive -s -u -o origin={b.b}/snd10@1 {_recv_props()} {b.b}/snd10s",
        "local origin for clone",
    )


@check(
    "SND-11",
    1,
    1,
    "recover's receive shapes, the keystore's included",
    depends=(
        "recover's _receive and _receive_row (commands/recover.py:401)",
        "its keystore",
        "M4b-6",
    ),
    fake=True,
)
def snd_11(b: Bench) -> None:
    """recover's shapes: ``zfs send -w <base> | zfs receive -u -o canmount=<v>
    -o mountpoint=<p>``, then ``-w -i <base> <point>`` with the same options;
    a non-raw ``-o encryption=off`` receive of an unencrypted zvol under an
    encrypted parent. OpenZFS as SND-02; module/zfs/zfs_ioctl.c:5476-5490."""
    x = _origin(b, "snd11")
    _ = b.ok(f"zfs snapshot {x}@m")
    b.write(x, 64)
    _ = b.ok(f"zfs snapshot {x}@p")
    src = f"{b.b}/snd11"
    _send_full(b, f"{x}@m", src)
    _ = b.pipe_ok(f"zfs send -w -i {x}@m {x}@p", f"zfs receive -s -u {src}")
    opts = "-o canmount=noauto -o mountpoint=/srv/rec"
    dst = f"{b.a}/rec11"
    _ = b.pipe_ok(f"zfs send -w {src}@m", f"zfs receive -u {opts} {dst}")
    _ = b.pipe_ok(f"zfs send -w -i {src}@m {src}@p", f"zfs receive -u {opts} {dst}")
    got = ", ".join(b.snapshot_names(dst))
    expect(got == "m, p", "m, p restored", got)
    for prop, want in (("canmount", "noauto"), ("mountpoint", "/srv/rec")):
        got = b.value(prop, dst)
        expect(got.endswith(want), f"{prop} {want}", got)
        expect(b.source(prop, dst) == "local", f"{prop} local", b.source(prop, dst))
    zv = f"{b.b}/ks11"
    _ = b.ok(f"zfs create -V 16M {zv}")
    _ = b.ok(f"zfs snapshot {zv}@k")
    _ = b.pipe_ok(f"zfs send {zv}@k", f"zfs receive -u -o encryption=off {b.a}/ks11")
    got = b.value("encryption", f"{b.a}/ks11")
    expect(got == "off", "encryption off on the restored keystore", got)


@check(
    "SND-12",
    1,
    1,
    "a recovered snapshot's bookmark, and a renamed one, are the drive's bases",
    depends=(
        "recover's anchors (M4c-12): anchor_restored, anchor_bpool",
        "the drive's next backup after a recover",
    ),
)
def snd_12(b: Bench) -> None:
    """recover receives a drive's snapshot raw into a new origin, bookmarks
    it and ``zfs change-key -i`` puts it under origin's key: that bookmark
    is a base the drive accepts for a raw incremental (the snapshot keeps
    the stream's IV set and guid, the bookmark copies both, change-key keeps
    the master key). On bpool, unencrypted, the restored snapshot renamed to
    the anchor's name is a base for a non-raw incremental (a rename keeps
    the guid). OpenZFS module/zfs/dmu_recv.c:3720-3721, :3746-3747,
    :462-478, :484-497; module/zfs/dsl_bookmark.c:352, :364-369;
    module/zfs/dsl_crypt.c:1251, :1484-1494, :2073-2087, :2199-2210;
    module/zfs/dsl_dataset.c:3023-3060."""
    x = _origin(b, "snd12", 64)
    _ = b.ok(f"zfs snapshot {x}@p")
    drive = f"{b.b}/snd12"
    _send_full(b, f"{x}@p", drive)
    rec = f"{b.a}/rec12"
    _ = b.pipe_ok(f"zfs send -w {drive}@p", f"zfs receive -u {rec}")
    _ = b.ok(f"zfs bookmark {rec}@p {rec}#anchor")
    _load_key(b, rec)
    _ = b.ok(f"zfs change-key -i {rec}")
    root = b.value("encryptionroot", rec)
    expect(root == b.a, f"{rec} under {b.a}'s key", root)
    b.write(rec, 64)
    _ = b.ok(f"zfs snapshot {rec}@q")
    _ = b.ok(f"zfs destroy {rec}@p")  # origin pruned it: only the bookmark is left
    _ = b.pipe_ok(f"zfs send -w -i {rec}#anchor {rec}@q", f"zfs receive -s -u {drive}")
    got = ", ".join(b.snapshot_names(drive))
    expect(got == "p, q", "p, q on the drive", got)
    boot = b.fs(f"{b.b}/snd12boot")
    b.write(boot, 16)
    _ = b.ok(f"zfs snapshot {boot}@p")
    dboot = f"{b.b}/snd12dboot"
    _ = b.pipe_ok(f"zfs send {boot}@p", f"zfs receive -u {dboot}")
    restored = f"{b.b}/snd12rboot"
    _ = b.pipe_ok(f"zfs send {dboot}@p", f"zfs receive -u {restored}")
    _ = b.ok(f"zfs rename {restored}@p {restored}@zark_anchor")
    b.write(restored, 16)
    _ = b.ok(f"zfs snapshot {restored}@q")
    _ = b.pipe_ok(f"zfs send -i {restored}@zark_anchor {restored}@q", f"zfs receive -s -u {dboot}")
    got = ", ".join(b.snapshot_names(dboot))
    expect(got == "p, q", "p, q on the drive's bpool", got)


# ── destroy, clone, keys ─────────────────────────────────────────────────


@check(
    "DST-01",
    1,
    1,
    "destroy -nvp lists exact names and what it frees",
    depends=("the retention dry run (lib/reclaim.py:51-56)",),
    fake=True,
)
def dst_01(b: Bench) -> None:
    """``zfs destroy -nvp ds@a,b,c`` prints a ``destroy\\t<name>`` line per
    exact name and ``reclaim\\t<n>``; a missing name is skipped; nothing is
    destroyed. OpenZFS cmd/zfs/zfs_main.c:1539-1578, :1740-1777;
    lib/libzfs/libzfs_iter.c:419-485."""
    x = _origin(b, "dst01", 64)
    for n in ("1", "2", "3"):
        _ = b.ok(f"zfs snapshot {x}@{n}")
    r = b.ok(f"zfs destroy -nvp {x}@1,3,9")
    destroys = sorted(ln for ln in r.lines if ln.startswith("destroy\t"))
    expect(
        destroys == [f"destroy\t{x}@1", f"destroy\t{x}@3"],
        "destroy lines for 1 and 3",
        f"{len(destroys)} destroy line(s)",
        text_of(r),
    )
    reclaim = [ln for ln in r.lines if ln.startswith("reclaim\t")]
    expect(
        len(reclaim) == 1 and reclaim[0].split("\t")[1].isdigit(),
        "one reclaim line",
        f"{len(reclaim)}",
        text_of(r),
    )
    got = ", ".join(b.snapshot_names(x))
    expect(got == "1, 2, 3", "nothing destroyed", got)


@check(
    "DST-02",
    1,
    1,
    "grouped destroys are all or nothing; -r takes a name everywhere",
    depends=(
        "grouped destroys and their retry (M4b-10, lib/reclaim.py:71-114)",
        "retention's pins",
    ),
    fake=True,
)
def dst_02(b: Bench) -> None:
    """``zfs destroy ds@a,b`` with one held snapshot destroys none ("it's
    being held"); a clone origin is refused ("has dependent clones"); ``zfs
    destroy -r ds@s`` takes that name on every descendant. OpenZFS
    lib/libzfs/libzfs_dataset.c:3968-4030; module/zfs/dsl_destroy.c:603-690, :55-89;
    cmd/zfs/zfs_main.c:1604-1622."""
    x = _origin(b, "dst02", 64)
    for n in ("1", "2", "3"):
        _ = b.ok(f"zfs snapshot {x}@{n}")
    _ = b.ok(f"zfs hold zark-selftest {x}@2")
    _ = b.refused(f"zfs destroy {x}@1,2", "being held")
    got = ", ".join(b.snapshot_names(x))
    expect(got == "1, 2, 3", "nothing destroyed while 2 is held", got)
    _ = b.ok(f"zfs release zark-selftest {x}@2")
    _ = b.ok(f"zfs clone -u {x}@3 {b.a}/dst02c")
    _ = b.refused(f"zfs destroy {x}@3", "has dependent clones")
    top = b.fs(f"{b.a}/dst02r")
    kids = [b.fs(f"{top}/k1"), b.fs(f"{top}/k2")]
    _ = b.ok(f"zfs snapshot -r {top}@s")
    _ = b.ok(f"zfs destroy -r {top}@s")
    left = [ds for ds in (top, *kids) if "s" in b.snapshot_names(ds)]
    expect(not left, "@s gone everywhere", ", ".join(left))


@check(
    "DST-03",
    1,
    1,
    "snapshots and bookmarks are destroyed without the key",
    depends=("zark prune, retention, drop, undo_point",),
)
def dst_03(b: Bench) -> None:
    """A snapshot and a bookmark are destroyed with the encryption key
    unloaded. OpenZFS module/zfs/dsl_destroy.c:55-118, :308-560;
    module/zfs/dsl_bookmark.c:1091-1153."""
    c = b.make_pool(
        "c", f"-O encryption=aes-256-gcm -O keyformat=raw -O keylocation=file://{b.key}"
    )
    try:
        x = b.fs(f"{c}/x")
        _ = b.ok(f"zfs snapshot {x}@1")
        _ = b.ok(f"zfs snapshot {x}@2")
        _ = b.ok(f"zfs bookmark {x}@1 {x}#b1")
        _ = b.ok(f"zfs unload-key {c}")
        got = b.value("keystatus", x)
        expect(got == "unavailable", "keystatus unavailable", got)
        _ = b.ok(f"zfs destroy {x}@1")
        _ = b.ok(f"zfs destroy {x}#b1")
        got = ", ".join(b.snapshot_names(x))
        expect(got == "2", "only 2 left", got)
    finally:
        b.drop_pool(c)


@check(
    "CLN-01",
    1,
    1,
    "branch a raw lineage with its key unloaded",
    depends=("ROLLBACK's keep (Run.branch, lib/engine.py:326; M4b-5, M4bF-9)",),
    fake=True,
)
def cln_01(b: Bench) -> None:
    """On a raw-received lineage with its key unloaded: ``zfs rename``, ``zfs
    clone -u -o canmount=noauto -o org.zark:...``, ``zfs promote``, then a raw
    incremental from the common snapshot; the clone's properties are
    ``local`` and nothing is mounted. OpenZFS module/zfs/zfs_ioctl.c:3756-3783,
    :2748-2918; module/zcommon/zfs_prop.c:536-538; module/zfs/dsl_dataset.c:3326-3373,
    :3467-3475; module/zfs/dsl_crypt.c:1695-1742; cmd/zfs/zfs_main.c:834-935."""
    x = _origin(b, "cln01", 64)
    live, kept = f"{b.b}/cln01", f"{b.b}/cln01-kept"
    for n in ("1", "2", "3"):
        _ = b.ok(f"zfs snapshot {x}@{n}")
    _send_full(b, f"{x}@1", live)
    _ = b.pipe_ok(f"zfs send -w -I {x}@1 {x}@3", f"zfs receive -s -u {live}")
    _ = b.ok(f"zfs destroy {x}@3")
    b.write(x, 64)
    _ = b.ok(f"zfs snapshot {x}@4")
    _ = b.ok(f"zfs rename {live} {kept}")
    marks = "-o canmount=noauto -o org.zark:canmount=on -o org.zark:mountpoint=/selftest"
    _ = b.ok(f"zfs clone -u {marks} {kept}@2 {live}")
    _ = b.ok(f"zfs promote {live}")
    _ = b.pipe_ok(f"zfs send -w -i {x}@2 {x}@4", f"zfs receive -s -u {live}")
    got = ", ".join(b.snapshot_names(live))
    expect(got == "1, 2, 4", "1, 2, 4 on the live lineage", got)
    got = ", ".join(b.snapshot_names(kept))
    expect(got == "3", "3 kept in the archive", got)
    for prop, want in (("canmount", "noauto"), ("org.zark:mountpoint", "/selftest")):
        got = b.value(prop, live)
        expect(got == want, f"{prop} {want}", got)
        expect(b.source(prop, live) == "local", f"{prop} local", b.source(prop, live))
    got = b.value("mounted", live)
    expect(got == "no", "not mounted", got)


@check(
    "KEY-01",
    1,
    1,
    "a raw-received root takes its parent's key with change-key -i",
    depends=("recover's key load and _restore_encryption_root (commands/recover.py:599)",),
)
def key_01(b: Bench) -> None:
    """``zfs load-key -L file://`` on a raw-received encryption root, then
    ``zfs change-key -i`` with both keys loaded makes it inherit its
    parent's encryption root. OpenZFS module/zfs/zfs_ioctl.c:7549-7552,
    :7557-7561; module/zfs/dsl_crypt.c:772-870, :1262-1346, :1583-1597."""
    x = _origin(b, "key01", 64)
    _ = b.ok(f"zfs snapshot {x}@1")
    src = f"{b.b}/key01"
    _send_full(b, f"{x}@1", src)
    dst = f"{b.a}/key01r"
    _ = b.pipe_ok(f"zfs send -w {src}@1", f"zfs receive -u {dst}")
    got = b.value("encryptionroot", dst)
    expect(got == dst, f"encryptionroot {dst} after the raw receive", got)
    _load_key(b, dst)
    _ = b.ok(f"zfs change-key -i {dst}")
    got = b.value("encryptionroot", dst)
    expect(got == b.a, f"encryptionroot {b.a} after change-key -i", got)


@check(
    "KEY-02",
    1,
    1,
    "the keystore zvol: zvol_id, LUKS read-only, ext4 ro,noload",
    depends=("Keystore.find_zvol_for_pool (lib/keystore.py:113, H11)", "its read-only open"),
)
def key_02(b: Bench) -> None:
    """A zvol: ``zvol_id /dev/zdN`` prints its dataset name and udev makes
    ``/dev/zvol/<dataset>``; a LUKS container on it opens with ``cryptsetup
    open --readonly`` and its ext4 mounts ``-o ro,noload``. OpenZFS
    udev/zvol_id.c:52-91, udev/rules.d/60-zvol.rules.in:11; Linux
    fs/ext4/super.c:1768-1769, :5499-5528, fs/ext4/orphan.c:396-399."""
    for tool in ("cryptsetup", "mkfs.ext4", "udevadm"):
        if not shutil.which(tool):
            raise Skip(f"{tool} is not installed")
    if not Path(ZVOL_ID).exists():
        raise Skip(f"{ZVOL_ID} is not installed")
    c = b.make_pool("c")
    mapper = f"{c}-ks"
    try:
        _ = b.ok(f"zfs create -V 64M {c}/keystore")
        _ = b.ok("udevadm settle --timeout=30")
        zd = Keystore(b.log).find_zvol_for_pool(c)
        expect(zd is not None, f"the zvol of {c}/keystore found", "none")
        assert zd is not None
        got = b.ok(f"{ZVOL_ID} {zd}").output
        expect(got == f"{c}/keystore", f"zvol_id: {c}/keystore", got)
        link = os.path.realpath(f"/dev/zvol/{c}/keystore")
        expect(link == zd, f"/dev/zvol/{c}/keystore -> {zd}", link)
        word = "selftest\n"
        _ = b.ok(
            "cryptsetup luksFormat --batch-mode --pbkdf pbkdf2 --pbkdf-force-iterations 1000 "
            + f"--key-file=- {zd}",
            word,
        )
        _ = b.ok(f"cryptsetup open --key-file=- {zd} {mapper}", word)
        _ = b.ok(f"mkfs.ext4 -q /dev/mapper/{mapper}")
        r = b.in_namespace(
            f"mount /dev/mapper/{mapper} {b.mnt} && echo k > {b.mnt}/system.key; umount {b.mnt}"
        )
        expect(r.ok, "the rw mount to write system.key", exit_status(r), text_of(r))
        _ = b.ok(f"cryptsetup close {mapper}")
        _ = b.ok(f"cryptsetup open --readonly --key-file=- {zd} {mapper}", word)
        r = b.in_namespace(
            f"mount -o ro,noload /dev/mapper/{mapper} {b.mnt} && cat {b.mnt}/system.key; "
            + f"umount {b.mnt}"
        )
        expect(
            r.output == "k", "system.key read from a ro,noload mount", exit_status(r), text_of(r)
        )
    finally:
        if Path(f"/dev/mapper/{mapper}").exists():
            _ = b.run(f"cryptsetup close {mapper}")
        b.drop_pool(c)


@check(
    "KEY-03",
    1,
    1,
    "only an encryption root takes a key; loading it unlocks its children and clones",
    depends=(
        "ZFS.encryption_roots_needing_key (M4d-19)",
        "Keystore.load_pool_keys, recover's _load_keys_from_file",
    ),
)
def key_03(b: Bench) -> None:
    """An encryption root with an inheriting child and a clone of the
    child's snapshot elsewhere: with the key unloaded all three are
    ``unavailable`` and name the root as ``encryptionroot``; ``load-key``
    of the child is refused; loading the root alone makes all three
    ``available``. OpenZFS lib/libzfs/libzfs_crypto.c:1316-1328,
    :1209-1234; module/zfs/dsl_crypt.c:1166-1189, :436-456, :1913-1931."""
    root = b.fs(
        f"{b.a}/key03",
        "encryption=aes-256-gcm",
        "keyformat=raw",
        f"keylocation=file://{b.key}",
    )
    child = b.fs(f"{root}/c")
    b.write(child, 64)
    _ = b.ok(f"zfs snapshot {child}@s")
    clone = f"{b.a}/key03k"
    _ = b.ok(f"zfs clone -u {child}@s {clone}")
    three = (root, child, clone)
    for ds in three:
        got = b.value("encryptionroot", ds)
        expect(got == root, f"{ds}: encryptionroot {root}", got)
    _ = b.ok(f"zfs unload-key {root}")
    for ds in three:
        got = b.value("keystatus", ds)
        expect(got == "unavailable", f"{ds} unavailable with the key unloaded", got)
    r = b.run(f"zfs load-key -L file://{b.key} {child}")
    expect(not r.ok, f"load-key of {child} refused", exit_status(r), text_of(r))
    _ = b.ok(f"zfs load-key -L file://{b.key} {root}")
    for ds in three:
        got = b.value("keystatus", ds)
        expect(got == "available", f"{ds} available once {root}'s key is loaded", got)


@check(
    "KEY-04",
    1,
    1,
    "zfs create -u under an encrypted parent: its key, its properties, not mounted",
    depends=("recover's empty datasets (commands/recover.py create_excluded, M5b-13)",),
)
def key_04(b: Bench) -> None:
    """``zfs create -u -o canmount=on -o mountpoint=... -o org.zark:...``
    under an encrypted dataset whose key is loaded: encrypted, with the
    parent's encryption root and key, its properties ``local``, and not
    mounted. The pool is imported with an altroot, which ``zfs get``
    prepends to the mountpoint it shows. OpenZFS cmd/zfs/zfs_main.c:1154-1155,
    :1342-1345; lib/libzfs/libzfs_crypto.c:1079-1080;
    module/zfs/dsl_crypt.c:1946-1948, :1957-1960;
    lib/libzfs/libzfs_dataset.c:2697-2706."""
    ds = f"{b.a}/key04"
    _ = b.ok(
        "zfs create -u -o canmount=on -o mountpoint=/selftest/key04 "
        + f"-o org.zark:replicate=no {ds}"
    )
    got = b.value("encryption", ds)
    expect(got not in ("off", "-", ""), "encrypted", got)
    for prop, want in (("encryptionroot", b.a), ("keystatus", "available"), ("mounted", "no")):
        got = b.value(prop, ds)
        expect(got == want, f"{prop} {want}", got)
    shown = f"{_altroot(b.a)}/selftest/key04"
    for prop, want in (("mountpoint", shown), ("org.zark:replicate", "no")):
        got = b.value(prop, ds)
        expect(got == want, f"{prop} {want}", got)
        got = b.source(prop, ds)
        expect(got == "local", f"{prop} local", got)
