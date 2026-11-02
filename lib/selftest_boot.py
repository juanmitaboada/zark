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
Level-1 self-test checks for a system whose ``/boot`` is an ext4 partition
(M12 readings section 8, M13 plan section 3): what zark reads of it, the
file system recover makes, the copy of ``/boot`` in rpool and the rewrites
of a recovered system.

Like :mod:`lib.selftest_l1`, each docstring names the source of the
behaviour it relies on.
"""

import re
import shlex
import shutil
from pathlib import Path

from lib import boot_rewrite, exclusions, platform_facts, pool_record, sh, upgrade_risk
from lib.boot_layout import BOOT_COPY, boot_mount
from lib.recover_ext4 import mkfs_boot_command
from lib.selftest import Bench, Fail, Skip, check, exit_status, expect, text_of


def _q(s: str) -> str:
    return shlex.quote(s)


def _ext4_boot() -> tuple[str, str]:
    """(source, UUID) of the host's ext4 ``/boot``; Skip on any other host."""
    mount = boot_mount()
    if mount is None or mount.fstype != "ext4":
        raise Skip("/boot is not an ext4 mount here (a bpool host)")
    return mount.source, mount.uuid


def _need(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        raise Skip(f"missing {', '.join(missing)}")


# ── what zark reads of the host ──────────────────────────────────────────


@check(
    "BOOT-01",
    1,
    1,
    "an ext4 /boot: findmnt and grub-probe agree, and every field of the record reads",
    depends=("pool_record.capture_boot (lib/pool_record.py)", "M12 plan 5.3"),
)
def boot_01(_b: Bench) -> None:
    """``findmnt`` gives ``/boot`` an ext4 mount of its own and GRUB's probe
    of it is ``ext2`` (grub2 grub-core/fs/ext2.c, the module that reads
    ext4); :func:`lib.pool_record.capture_boot` (lib/pool_record.py:381)
    builds the record from ``tune2fs -l``, ``lsblk -b -n -d -o
    SIZE,PARTTYPE,PARTUUID`` and ``blkid -p``, and its UUID is blkid's."""
    source, uuid = _ext4_boot()
    _need("grub-probe", "tune2fs", "blkid", "lsblk")
    probe = sh.run("grub-probe -t fs /boot")
    expect(
        probe.output.strip() == "ext2",
        "grub-probe -t fs /boot: ext2",
        "another answer",
        text_of(probe),
    )
    rec, why = pool_record.capture_boot("")
    expect(rec is not None, "a whole /boot record", "none", why)
    assert rec is not None
    blkid = sh.run(f"blkid -p -s UUID -o value {_q(source)}").output.strip()
    expect(
        rec.uuid == uuid == blkid,
        "one UUID for findmnt, blkid and the record",
        "they differ",
        f"findmnt {uuid}, blkid {blkid}, record {rec.uuid}",
    )
    expect(rec.size_bytes > 0 and rec.partition.size_bytes > 0, "sizes read", str(rec.size_bytes))
    expect(bool(rec.features), "features of the allowlist read", "none")


_ESP_GRUB = Path("/boot/efi/EFI/ubuntu/grub.cfg")


@check(
    "BOOT-05",
    1,
    1,
    "the ESP's EFI/ubuntu/grub.cfg searches /boot's UUID",
    depends=("boot_rewrite.grub_uuid_problems (check a, M12R-13)",),
)
def boot_05(_b: Bench) -> None:
    """On an ext4 ``/boot`` host the ESP's ``EFI/ubuntu/grub.cfg`` is the
    three-line form grub-install writes (grub2 util/grub-install.c:1615-1629
    at 2.14-2ubuntu2.1): ``search.fs_uuid <uuid> root``, with ``/boot``'s
    UUID, which recover's check (a) reads after its rewrite."""
    _, uuid = _ext4_boot()
    path = _ESP_GRUB
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise Fail(f"{path} readable", str(e)) from e
    first = (text.splitlines() or [""])[0].split()
    expect(
        first[:1] == ["search.fs_uuid"] and len(first) >= 3 and first[2] == "root",
        "search.fs_uuid <uuid> root",
        "another first line",
        text,
    )
    expect(first[1].lower() == uuid.lower(), "/boot's UUID", "another UUID", text)


_SIGNED_GRUB = Path("/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed")


@check(
    "BOOT-06",
    1,
    1,
    "the signed GRUB's modules: zfs recorded, needed on a bpool host only",
    depends=("the signed boot chain of a bpool point (lib/boot_chain.py:273)",),
)
def boot_06(b: Bench) -> None:
    """Whether Ubuntu's signed GRUB carries the ``zfs`` modules (1.215+2.14-
    2ubuntu1 carries ``zfs``, ``zfscrypt`` and ``zfsinfo``): a bpool host
    boots through them; an ext4 ``/boot`` does not need them. The names are
    read as NUL-terminated strings in the image, logged as a record, and
    their absence fails on a bpool host only (lib/boot_chain.py:273)."""
    try:
        data = _SIGNED_GRUB.read_bytes()
    except OSError as e:
        raise Skip(f"{_SIGNED_GRUB} not readable: {e}") from e
    names = [m for m in ("zfs", "zfscrypt", "zfsinfo") if b"\0" + m.encode() + b"\0" in data]
    b.log.info(f"BOOT-06: {_SIGNED_GRUB.name} names {', '.join(names) or 'no zfs module'}")
    mount = boot_mount()
    if mount is not None and mount.fstype == "ext4":
        return
    expect("zfs" in names, "the zfs module in the signed GRUB of a bpool host", ", ".join(names))


# ── the file system recover makes ────────────────────────────────────────


def _record(label: str) -> pool_record.BootRecord:
    """A record with the allowlist, for the commands recover builds from it."""
    return pool_record.BootRecord(
        dataset=f"rpool/ROOT/x/{BOOT_COPY}",
        fstype="ext4",
        uuid="",
        label=label,
        size_bytes=1,
        features=pool_record.EXT4_FEATURES,
        mount_options="defaults",
        partition=pool_record.Partition(1, ""),
        esp=None,
        swap=None,
    )


@check(
    "BOOT-02",
    1,
    1,
    "mkfs.ext4 -O none,<allowlist> gives exactly the allowlist, and a new UUID",
    depends=("recover_ext4.mkfs_boot_command (M12-3 3a)",),
)
def boot_02(b: Bench) -> None:
    """recover's ``mkfs.ext4 -F -O none,<EXT4_FEATURES> -L <label>``
    (lib/recover_ext4.py:86) on a file: accepted; ``tune2fs -l`` lists
    exactly the allowlist (``none`` clears mke2fs.conf's defaults,
    e2fsprogs lib/e2p/feature.c:353-358 at 1.47.4); ``blkid -p`` reads the
    new UUID and the label."""
    _need("mkfs.ext4", "tune2fs", "blkid")
    img = f"{b.dir}/boot02.img"
    with open(img, "wb") as f:
        f.truncate(64 * 1024**2)
    _ = b.ok(mkfs_boot_command(_record("t"), img))
    t = b.ok(f"tune2fs -l {_q(img)}")
    got = set(pool_record._tune2fs(t.stdout)[0])  # pylint: disable=protected-access
    want = set(pool_record.EXT4_FEATURES)
    expect(
        got == want,
        "exactly the allowlist",
        f"{len(got ^ want)} feature(s) differ",
        " ".join(sorted(got ^ want)),
    )
    uuid = b.ok(f"blkid -p -s UUID -o value {_q(img)}").output.strip()
    expect(re.fullmatch(boot_rewrite.UUID, uuid) is not None, "a UUID", "not a UUID", uuid)
    label = b.ok(f"blkid -p -s LABEL -o value {_q(img)}").output.strip()
    expect(label == "t", "label t", "another label", label)


# ── the copy of /boot in rpool ───────────────────────────────────────────


def _copy_tree(b: Bench) -> tuple[str, str, str]:
    """An encrypted pool with a boot environment and its ``zark-boot`` as
    setup creates it (lib/setup: one zfs create); (pool, be, copy)."""
    c = b.make_pool(
        "c",
        f"-O encryption=aes-256-gcm -O keyformat=raw -O keylocation=file://{b.key} "
        + "-O acltype=posixacl",
    )
    be = b.fs(f"{c}/be")
    copy = f"{be}/{BOOT_COPY}"
    _ = b.ok(f"zfs create -o canmount=noauto -o mountpoint=legacy {copy}")
    return c, be, copy


_ROUND_TRIP = r"""
set -e
I={img}; S={dir}/s; D={dir}/d; B={dir}/back
mkdir -p $S $D $B
mount -o loop $I $S
mkdir -p $S/grub $S/efi
printf k > $S/vmlinuz-1; ln $S/vmlinuz-1 $S/hard; ln -s vmlinuz-1 $S/vmlinuz
printf g > $S/grub/grub.cfg; chmod 600 $S/vmlinuz-1; printf e > $S/efi/skip
setfacl -m u:65534:r $S/grub/grub.cfg; setfattr -n user.zark -v 1 $S/grub/grub.cfg
mount -t zfs {copy} $D
rsync -aHAX --numeric-ids --delete --inplace --one-file-system --exclude=/efi/ $S/ $D/
echo "RSYNC $?"
mount -t tmpfs -o size=16m tmpfs $B
rsync -aHAX --numeric-ids $D/ $B/
echo "BACK $?"
for t in $S $D $B; do
  ( cd $t && stat -c '%n %h %a %u %g %s %F' vmlinuz-1 hard vmlinuz grub/grub.cfg
    getfacl -n --omit-header grub/grub.cfg | tr '\n' ' '; echo
    getfattr -n user.zark --only-values grub/grub.cfg; echo
    readlink vmlinuz; cat vmlinuz-1 grub/grub.cfg; echo
    ls efi 2>/dev/null | wc -l ) | sed "s|^|$(basename $t) |"
done
umount $B; umount $D; umount $S
"""


@check(
    "BOOT-03",
    1,
    1,
    "rsync -aHAX keeps /boot's links, modes, ACLs and xattrs through zark-boot and back",
    depends=("boot_layout.refresh_boot_copy (M12-2 2e)", "boot_chain.restore_boot_copy"),
)
def boot_03(b: Bench) -> None:
    """In its own namespace: an ext4 image with a hard link, a symlink, a
    mode, an ACL and an xattr, copied with backup's rsync
    (lib/boot_layout.py:179) into a dataset made as ``zark-boot`` (child of
    an encrypted dataset, ``acltype=posixacl``, ``xattr=sa``, ``legacy``)
    and back with recover's (lib/boot_chain.py:249): status 0 both ways,
    every attribute equal, ``efi`` left out."""
    _need("mkfs.ext4", "rsync", "setfacl", "getfacl", "setfattr", "getfattr")
    c, _, copy = _copy_tree(b)
    try:
        img = f"{b.dir}/boot03.img"
        with open(img, "wb") as f:
            f.truncate(64 * 1024**2)
        _ = b.ok(mkfs_boot_command(_record(""), img))
        r = b.in_namespace(_ROUND_TRIP.format(img=_q(img), dir=_q(b.dir), copy=_q(copy)))
        expect(r.ok, "the round trip", exit_status(r), text_of(r))
        ok = "RSYNC 0" in r.lines and "BACK 0" in r.lines
        expect(ok, "rsync status 0 twice", "another status", text_of(r))
        views: dict[str, list[str]] = {}
        for line in r.lines:
            who, _, rest = line.partition(" ")
            if who in ("s", "d", "back"):
                views.setdefault(who, []).append(rest)
        src = views.get("s", [])
        expect(
            bool(src) and src[-1] == "1", "the source has its efi file", "no efi file", text_of(r)
        )
        for who in ("d", "back"):
            got = views.get(who, [])
            differ = sum(a != b for a, b in zip(got[:-1], src[:-1], strict=False))
            same = len(got) == len(src) and not differ
            expect(same, f"{who} equal to the source", f"{differ} line(s) differ", text_of(r))
            expect(got[-1:] == ["0"], f"no efi in {who}", "an efi file", text_of(r))
    finally:
        b.drop_pool(c)


@check(
    "BOOT-04",
    1,
    1,
    "zark-boot: encrypted by its parent, ACLs and xattrs inherited, left out of zfs mount -a",
    depends=("setup.keep_boot (M12-2 2e)", "the raw sends of a point (lib/engine.py)"),
)
def boot_04(b: Bench) -> None:
    """A child made with ``canmount=noauto mountpoint=legacy`` under an
    encrypted dataset: its parent's encryption root, ``acltype`` and
    ``xattr`` inherited; ``zfs mount`` refuses it (a legacy mountpoint,
    which ``zfs mount -a`` and zark's tree mount skip, lib/mount.py:263);
    ``mount -t zfs`` mounts it, and a snapshot read-only; raw full and
    incremental sends of the parent and the child receive, as a point's."""
    c, be, copy = _copy_tree(b)
    try:
        root = b.value("encryptionroot", copy)
        expect(root == c, "the parent as encryption root", "another root", root)
        for prop, want in (("acltype", "posix"), ("xattr", "sa")):
            got, src = b.value(prop, copy), b.source(prop, copy)
            ok = got == want and src.startswith("inherited")
            expect(ok, f"{prop} {want} inherited", "not inherited", f"{got} {src}")
        b.write(be, 16)
        _ = b.ok(f"zfs snapshot -r {be}@p1")
        _ = b.refused(f"zfs mount {copy}", "legacy")
        m = b.mnt
        script = (
            f"mount -t zfs {copy} {m} && echo MOUNT && umount {m}; "
            + f"mount -t zfs -o ro {copy}@p1 {m} && echo SNAP && umount {m}"
        )
        r = b.in_namespace(script)
        ok = "MOUNT" in r.lines and "SNAP" in r.lines
        expect(ok, "both mounts", "a mount failed", text_of(r))
        _ = b.pipe_ok(f"zfs send -w {be}@p1", f"zfs receive -u {b.b}/be")
        _ = b.pipe_ok(f"zfs send -w {copy}@p1", f"zfs receive -u {b.b}/be/{BOOT_COPY}")
        b.write(be, 16)
        _ = b.ok(f"zfs snapshot -r {be}@p2")
        _ = b.pipe_ok(f"zfs send -w -i @p1 {copy}@p2", f"zfs receive -u {b.b}/be/{BOOT_COPY}")
        names = b.snapshot_names(f"{b.b}/be/{BOOT_COPY}")
        expect(
            names == ["p1", "p2"],
            "p1 and p2 received",
            f"{len(names)} snapshot(s)",
            " ".join(names),
        )
    finally:
        b.drop_pool(c)


# ── what GRUB and setup do with zark-boot ────────────────────────────────


def _grub_roots(rows: list[tuple[str, str, str]]) -> list[str]:
    """10_linux_zfs's root candidates as M13's review read them: a dataset
    with canmount on or noauto whose mountpoint is ``/``."""
    return [n for n, can, mp in rows if can in ("on", "noauto") and mp == "/"]


def _grub_boots(root: str, rows: list[tuple[str, str, str]]) -> list[str]:
    """Its ``/boot`` candidates for ``root``: the child named ``<root>/boot``
    whatever its properties; a dataset whose last name part is the root's
    and mounts at ``/boot`` with canmount on or noauto; any with canmount on
    and mountpoint ``/boot``."""
    base = root.rsplit("/", 1)[-1]
    return [
        n
        for n, can, mp in rows
        if n == f"{root}/boot"
        or (n.rsplit("/", 1)[-1] == base and can in ("on", "noauto") and mp == "/boot")
        or (can == "on" and mp == "/boot")
    ]


@check(
    "BOOT-09",
    1,
    1,
    "GRUB's ZFS script never takes zark-boot for a root or a /boot",
    depends=("the name and properties of zark-boot (M12-2 2e, lib/mount_props.py:108)",),
)
def boot_09(b: Bench) -> None:
    """On the bench's ``zfs list -H -o name,canmount,mountpoint`` of a boot
    environment with a ``zark-boot`` made as setup makes it, the selections
    of Ubuntu's 10_linux_zfs as the M13 review read them in grub2
    2.14-2ubuntu2.1 (root candidates: canmount on or noauto, mountpoint
    ``/``; ``/boot``: the child named ``<root>/boot``, a dataset named as
    the root mounted at ``/boot``, a canmount=on one at ``/boot``) never
    give ``zark-boot``; recover's inferred mount properties agree
    (lib/mount_props.py:108-112)."""
    c, be, copy = _copy_tree(b)
    try:
        _ = b.ok(f"zfs set mountpoint=/ canmount=noauto {be}")
        r = b.ok(f"zfs list -H -o name,canmount,mountpoint -r {c}")
        rows = [tuple(ln.split("\t")) for ln in r.lines if ln.count("\t") == 2]
        roots = _grub_roots(rows)  # type: ignore[arg-type]
        ok = be in roots and copy not in roots
        expect(
            ok,
            "the boot environment a root, its copy not",
            f"{len(roots)} root(s)",
            " ".join(roots),
        )
        boots = _grub_boots(be, rows)  # type: ignore[arg-type]
        expect(copy not in boots, "the copy not a /boot", f"{len(boots)} /boot(s)", " ".join(boots))
    finally:
        b.drop_pool(c)


@check(
    "BOOT-10",
    1,
    1,
    "setup gives zark-boot no sanoid rule of its own, and it is never excluded",
    depends=("setup._classify (commands/setup.py:139)", "exclusions.never_excluded"),
)
def boot_10(_b: Bench) -> None:
    """``_classify`` (commands/setup.py:139-191) returns no rule for
    ``rpool/ROOT/<be>/zark-boot`` (the boot environment's recursive
    ``production`` rule covers it, as it does every child), and
    ``never_excluded`` (lib/exclusions.py:94-101) refuses it."""
    from commands.setup import _classify  # pylint: disable=import-outside-toplevel
    from lib.zfs import DatasetInfo  # pylint: disable=import-outside-toplevel

    be, copy = "rpool/ROOT/ubuntu_abc123", f"rpool/ROOT/ubuntu_abc123/{BOOT_COPY}"
    rule = _classify(DatasetInfo(be, type="filesystem"), "ubuntu_abc123")
    expect(rule is not None and rule["recursive"], f"{be} production, recursive", str(rule))
    got = _classify(DatasetInfo(copy, type="filesystem"), "ubuntu_abc123")  # pylint: disable=protected-access
    expect(got is None, f"no rule for {copy}", str(got))
    why = exclusions.never_excluded(copy)
    expect(bool(why), f"{copy} never excluded", why or "excludable")


# ── the rewrites of a recovered system ───────────────────────────────────

_OLD = "0b0e7e57-0000-4000-8000-000000000001"
_NEW = "11111111-2222-4333-8444-555555555555"
_OTHER = "0b0e7e57-0000-4000-8000-0000000000f0"
_GRUB_CFG = f"""insmod ext2
if [ x$feature_platform_search_hint = xy ]; then
  search --no-floppy --fs-uuid --set=root --hint-bios=hd0,gpt2 --hint-efi=hd0,gpt2  {_OLD}
else
  search --no-floppy --fs-uuid --set=root {_OLD}
fi
menuentry 'Ubuntu' {{
	search --no-floppy --fs-uuid --set=root {_OLD}
	linux	"/vmlinuz-7.3.0-10-generic" root=ZFS="rpool/ROOT/ubuntu_abc123" ro quiet
}}
menuentry 'Other' {{
	search --no-floppy --fs-uuid --set=root {_OTHER}
}}
"""
_ESP_CFG = (
    f"search.fs_uuid {_OLD} root hd0,gpt2 \n"
    + "set prefix=($root)'/grub'\nconfigfile $prefix/grub.cfg\n"
)


@check(
    "BOOT-07",
    1,
    1,
    "grub.cfg's search lines move to the new /boot, and check (a) sees it",
    depends=("boot_rewrite.rewrite_grub_uuid (M12-3 3a)", "check (a) (M12R-13)"),
)
def boot_07(_b: Bench) -> None:
    """On samples shaped as the 26.10 installer's ``grub.cfg`` and the ESP's
    (tests/fixtures/boot-ext4/README.md:1): every ``search`` line naming the
    old UUID names the new one, the foreign ``search`` and the ``linux``
    lines stay byte for byte; check (a) passes after, names each failure
    before; a file without the old UUID changes nothing."""
    cfg, n = boot_rewrite.rewrite_grub_uuid(_GRUB_CFG, _OLD, _NEW)
    expect(n == 3 and _OLD not in cfg, "3 search lines moved", str(n))
    kept = [ln for ln in _GRUB_CFG.splitlines() if _OLD not in ln]
    expect(kept == [ln for ln in cfg.splitlines() if _NEW not in ln], "every other line kept", "")
    esp, m = boot_rewrite.rewrite_grub_uuid(_ESP_CFG, _OLD, _NEW)
    expect(m == 1, "the ESP's line moved", str(m))
    fstab = f"/dev/disk/by-uuid/{_NEW} /boot ext4 defaults 0 1\n"
    after = boot_rewrite.grub_uuid_problems(cfg, esp, fstab, _OLD, _NEW)
    expect(not after, "check (a) passes", "; ".join(after))
    before = boot_rewrite.grub_uuid_problems(_GRUB_CFG, _ESP_CFG, fstab, _OLD, _NEW)
    expect(len(before) == 4, "check (a): 4 failures before", "; ".join(before))
    same = boot_rewrite.rewrite_grub_uuid(cfg, _OLD, _NEW)
    expect(same == (cfg, 0), "nothing to move twice", str(same[1]))


_FSTAB = f"""# /etc/fstab
/dev/disk/by-uuid/{_OLD} /boot ext4 defaults 0 1
/dev/disk/by-uuid/1A2B-3C4D /boot/efi vfat defaults 0 1
"""
_CRYPTTAB = (
    "dm_crypt-0 PARTUUID=00000000-0000-4000-8000-0000000000a3 /dev/urandom "
    + "swap,initramfs,cipher=aes-cbc-essiv:sha256,size=256\n"
)


@check(
    "BOOT-08",
    1,
    1,
    "fstab and crypttab: only /boot's UUID and the swap's PARTUUID change",
    depends=("boot_rewrite.rewrite_fstab, rewrite_crypttab (M12-3 3a)",),
)
def boot_08(_b: Bench) -> None:
    """On samples shaped as the 26.10 installer's (tests/fixtures/boot-ext4/
    README.md:1): ``rewrite_fstab`` changes the ``/boot`` line alone,
    ``rewrite_crypttab`` the swap's ``PARTUUID=`` alone, every other byte as
    it was."""
    fstab, n = boot_rewrite.rewrite_fstab(_FSTAB, _OLD, _NEW)
    expect(n == 1 and fstab == _FSTAB.replace(_OLD, _NEW), "the /boot line alone", str(n))
    part = "00000000-0000-4000-8000-0000000000b4"
    crypttab, m = boot_rewrite.rewrite_crypttab(_CRYPTTAB, part)
    expect(m == 1, "the swap line", str(m))
    expect(
        crypttab == _CRYPTTAB.replace("00000000-0000-4000-8000-0000000000a3", part),
        "only its PARTUUID",
        crypttab.strip(),
    )


# ── the 24.04 to 26.04 upgrade ───────────────────────────────────────────


@check(
    "UPG-01",
    1,
    1,
    "24.04: rpool's mount units are bound to zfs-load-key-rpool.service by the shell generator",
    depends=("upgrade_risk.exposed (M12P.3, M12R-9)",),
)
def upg_01(_b: Bench) -> None:
    """On Ubuntu 24.04 with an encrypted rpool, what
    :func:`lib.upgrade_risk.exposed` (lib/upgrade_risk.py:67) reads holds:
    the installed zfs-mount-generator is the shell script
    (lib/upgrade_risk.py:52) and ``var-lib-dpkg.mount`` is bound to
    ``zfs-load-key-rpool.service`` (lib/upgrade_risk.py:61). SKIP on 26.04
    and 26.10, which build the C generator with the ``@`` name, so their
    upgrades rename nothing; and when that mount is bound to no key unit
    (rpool not encrypted, or not mounted through zfs-list.cache). It reads
    no pool: only the generator's file and systemd."""
    if platform_facts.ubuntu_release() != "24.04":
        raise Skip("not Ubuntu 24.04: 26.04 and later build the C generator, nothing renames")
    binds = upgrade_risk.binds_to()
    if not any(u.startswith("zfs-load-key") for u in binds):
        raise Skip(f"{upgrade_risk.PROBE_UNIT} is bound to no ZFS key unit here")
    expect(upgrade_risk.generator_is_shell(), "the shell generator", str(upgrade_risk.GENERATOR))
    expect(
        upgrade_risk.KEY_UNIT in binds,
        f"{upgrade_risk.PROBE_UNIT} bound to {upgrade_risk.KEY_UNIT}",
        "bound to another name",
        " ".join(binds),
    )
