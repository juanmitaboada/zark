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
Level-2 self-test checks: a scratch stick, erased (M4s-2, M4s-4).

They run zark's own code where it has it (identity, prepare's pool shape,
``ZFS.import_backup_pool``, purge's lock and wipe), so a PASS certifies the
platform for exactly what zark runs. The stick was blank when the run began
(``zark selftest`` refuses one that is not) and is left blank.
"""

import os
import shlex
import time
from pathlib import Path

from lib import identity, sh
from lib.cleanup import Cleanup
from lib.identity import DiskIdentity
from lib.log import Log
from lib.selftest import Bench, Fail, check, exit_status, expect, observed, text_of
from lib.zfs import ZFS, backup_altroot, create_command

SETTLE = "udevadm settle --timeout=30"


class StickBench(Bench):
    """The level-2 bench: the stick, one pool on it at a time."""

    def __init__(self, log: Log, cleanup: Cleanup, stick: DiskIdentity):
        super().__init__(log, cleanup)
        self.stick = stick
        self.disk = stick.disk
        self.by_id = stick.by_id_path or stick.disk
        self.part1 = stick.part1

    @property
    def s(self) -> str:  # pylint: disable=invalid-name
        """The stick's pool."""
        return self.pool("s")

    def masks(self) -> dict[str, str]:
        out = super().masks()
        out[self.s] = "<pool-s>"
        for name in (self.stick.by_id, *self.stick.aliases):
            if name:
                out[name] = "<by-id>"
        if self.stick.serial:
            out[self.stick.serial] = "<serial>"
        return out

    def setup(self) -> None:
        """Only the cleanup: the stick is the bench."""
        self.cleanup.track_action(self.teardown)

    def same_disk(self) -> bool:
        """Whether the stick's by-id name still names the disk it named when
        the run began, with the same serial (nothing else is ever wiped)."""
        if not self.stick.by_id:
            return False
        try:
            now = identity.resolve_disk(self.by_id)
        except identity.IdentityError:
            return False
        return now.disk == self.disk and now.serial == self.stick.serial

    def prepare(self) -> str:
        """Create the stick's pool as ``zark prepare`` does, export it; its GUID."""
        self.forget()
        altroot = backup_altroot(self.s)
        _ = sh.run(f"mkdir -p {altroot}")
        _ = self.ok(create_command(self.s, self.by_id, altroot, ""))
        self.guids[self.s] = self.pool_value("guid", self.s)
        _ = self.ok(f"zpool export {self.s}")
        _ = self.ok(SETTLE)
        return self.guids[self.s]

    def forget(self) -> None:
        """Destroy the stick's pool if it is imported (and the run's)."""
        guid = self.guids.pop(self.s, "")
        if guid and sh.run(f"zpool get -Hp -o value guid {self.s}").output == guid:
            _ = self.run(f"zpool destroy -f {self.s}")

    def pool_on_stick(self) -> bool:
        """Whether an imported pool has a vdev on the stick (or the listing
        failed, which cannot tell)."""
        r = sh.run("zpool list -vHP", log=self.log)
        if not r.ok:
            return True
        names = {f"{identity.BY_ID_DIR}/{n}" for n in (self.stick.by_id, *self.stick.aliases) if n}
        for line in r.lines:
            path = line.strip().split("\t")[0]
            if any(path.startswith(n + "-part") for n in names) or path == self.disk:
                return True
            if path.startswith(self.disk) and path[len(self.disk) :].lstrip("p").isdigit():
                return True
        return False

    def blank(self) -> None:
        """Erase what the run left on the stick as purge erases a drive: both
        ends overwritten (part1's four ZFS labels lie within them), then the
        signatures and the partition table. ``wipefs`` alone leaves the
        labels: it erases what it finds at the disk's own offsets."""
        if not self.same_disk():
            self.log.warn(f"{self.by_id} no longer names the stick the run began with: not wiped")
            return
        if self.pool_on_stick():
            self.log.warn(
                f"A pool is still imported from {self.disk}, or it cannot be told: not wiped"
            )
            return
        from commands import purge  # pylint: disable=import-outside-toplevel # purge's own wipe

        self.log.info(f"Leaving {self.disk} blank, as zark purge leaves a drive")
        marks = purge._wipe(self.disk, self.part1, self.log)  # pylint: disable=protected-access
        for ok, text in marks:
            if ok is False:
                self.log.warn(f"Wiping the stick: {text}")
        _ = self.run(SETTLE)

    def teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        self.forget()
        self.blank()
        if sh.run(f"blkid -p {self.disk}").returncode != 2:
            self.log.warn(f"{self.disk} still has a signature: wipe it with zark purge")
        _ = sh.run(f"rmdir {backup_altroot(self.s)}")
        self.cleanup.untrack_action(self.teardown)


def _stick(b: Bench) -> StickBench:
    assert isinstance(b, StickBench)
    return b


def _partitions(disk: str) -> list[str]:
    r = sh.run(f"lsblk -nr -o NAME,TYPE {disk}")
    return [ln.split()[0] for ln in r.lines if ln.split()[1:2] == ["part"]]


def _wait_gone(path: str, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while os.path.exists(path):
        if time.monotonic() > deadline:
            return False
        _ = sh.run(SETTLE)
        time.sleep(0.2)
    return True


@check(
    "DSK-01",
    1,
    2,
    "the stick's identity as zark reads it",
    depends=("identity.resolve_disk (lib/identity.py:206)", "health's bridge checks"),
)
def dsk_01(b: Bench) -> None:
    """``identity.resolve_disk``: lsblk's ``TYPE``, ``MODEL``, ``SERIAL``,
    ``SIZE``, ``TRAN``, the by-id names (``usb-...-0:0`` for a USB disk) and
    the USB vendor:product. util-linux misc-utils/lsblk.c:201-234; systemd
    rules.d/60-persistent-storage.rules.in:32-33, :72, :78, :83,
    src/udev/udev-builtin-usb_id.c:396-447."""
    s = _stick(b)
    ident = identity.resolve_disk(s.by_id)
    expect(ident.disk == s.disk, f"resolved to {s.disk}", ident.disk)
    expect(bool(ident.by_id), "a by-id name", "none")
    expect(bool(ident.size), "a size", "none")
    if ident.transport == "usb":
        usb = [n for n in ident.aliases if n.startswith("usb-") and n.endswith("-0:0")]
        expect(bool(usb), "a usb-...-0:0 by-id name", ", ".join(ident.aliases))
        vid, pid = identity.usb_ids(ident.name)
        expect(bool(vid and pid), "the USB vendor and product", f"{vid}:{pid}")


@check(
    "DSK-02",
    1,
    2,
    "prepare's pool on a whole disk, imported by its exact partition",
    depends=("prepare", "ZFS.import_backup_pool", "DiskIdentity.part1"),
)
def dsk_02(b: Bench) -> None:
    """prepare's ``zpool create ... -m none -R <root> <pool> <by-id>``: the
    disk gets part1 and part9 and udev links ``<by-id>-part1``;
    ``import_backup_pool`` by that device imports it read-only, nothing
    mounted, and ``zpool list -vHP`` names the by-id partition. systemd
    src/udev/udevadm-settle.c:27, :159-176, :202-212; Linux block/genhd.c:342-361,
    :405-428; OpenZFS as POOL-01, POOL-02, POOL-08."""
    s = _stick(b)
    _ = s.prepare()
    parts = _partitions(s.disk)
    expect(len(parts) == 2, "two partitions (1 and 9)", ", ".join(parts))
    expect(Path(s.part1).exists(), f"{s.part1}", "missing")
    altroot = backup_altroot(s.s)
    expect(
        ZFS(s.log).import_backup_pool(s.s, s.part1, altroot=altroot, readonly=True),
        "import_backup_pool by the exact partition",
        "it failed",
    )
    try:
        got = s.pool_value("readonly", s.s)
        expect(got == "on", "readonly on", got)
        r = s.ok(f"zpool list -vHP {s.s}")
        expect(s.part1 in r.stdout, f"{s.part1} in zpool list -vHP", "not there", text_of(r))
        mounted = [t for t in s.ok("findmnt -rn -o TARGET").lines if t.startswith(altroot)]
        expect(not mounted, "nothing mounted", ", ".join(mounted))
    finally:
        _ = s.run(f"zpool export {s.s}")


@check(
    "DSK-03",
    1,
    2,
    "zdb -l reads the stick's label",
    depends=("identity.read_pool_label", "purge and the registry match by GUID"),
)
def dsk_03(b: Bench) -> None:
    """``zdb -l <by-id>-part1`` reads the exported pool's label: its name,
    GUID and ``state: 1``. OpenZFS as POOL-07; module/zfs/vdev_label.c:162-170."""
    s = _stick(b)
    guid = s.prepare()
    r = s.ok(f"zdb -l {s.part1}")
    expect("state: 1" in r.stdout, "state: 1", "not there", text_of(r))
    label = identity.read_pool_label(s.part1)
    expect(
        label is not None and label.name == s.s and label.guid == guid,
        f"read_pool_label: {s.s} {guid}",
        "no label" if label is None else f"{observed(label.name)} {observed(label.guid)}",
    )


@check(
    "DSK-04",
    1,
    2,
    "udev re-reads the table when a writer closes the disk; LOCK_EX holds it off",
    depends=("purge's _held_from_udev (M4aF-14)",),
)
def dsk_04(b: Bench) -> None:
    """With purge's ``LOCK_EX`` held from before the write, overwriting the
    disk's first 10 MiB (protective MBR and GPT) leaves part1 in place; with
    no lock, closing the disk after that write makes udev re-read the table
    and part1 goes. systemd rules.d/60-block.rules:10-13,
    src/udev/udev-watch.c:221-297, src/shared/reread-partition-table.c:255-271,
    docs/BLOCK_DEVICE_LOCKING.md:21-48; Linux block/partitions/efi.c:596-612."""
    from commands import purge  # pylint: disable=import-outside-toplevel # the code certified

    s = _stick(b)
    head = f"dd if=/dev/zero of={s.disk} bs=1M count=10 conv=fsync"
    _ = s.prepare()
    with purge._held_from_udev(s.disk, s.log):  # pylint: disable=protected-access
        _ = s.ok(head)
        time.sleep(2)
        expect(os.path.exists(s.part1), f"{s.part1} kept while the lock is held", "it went")
    _ = s.prepare()
    _ = s.ok(head)
    expect(_wait_gone(s.part1), f"{s.part1} gone after the unlocked write", "still there")


@check(
    "DSK-05",
    1,
    2,
    "purge's wipe erases the labels and leaves the stick blank",
    depends=("purge's overwrite, label mark and final checks (M4a-13, M4aR-2)",),
)
def dsk_05(b: Bench) -> None:
    """purge's ``_wipe`` (both ends under its lock, ``wipefs -a``, ``sgdisk
    --zap-all``) on a stick with a pool: every step and check succeeds, and
    the label is gone from part1 after the overwrite (four 256 KiB labels,
    two at each end of the partition). OpenZFS module/zfs/vdev_label.c:162-170,
    include/sys/vdev_impl.h:535-562; commands/purge.py:188-207."""
    from commands import purge  # pylint: disable=import-outside-toplevel # the code certified

    s = _stick(b)
    _ = s.prepare()
    marks = purge._wipe(s.disk, s.part1, s.log)  # pylint: disable=protected-access
    bad = [text for ok, text in marks if ok is False]
    expect(not bad, "every wipe step and check succeeds", f"{len(bad)} failed", "\n".join(bad))
    texts = [text for ok, text in marks if ok]
    expect(
        "no ZFS label on the first partition after the overwrite" in texts,
        "the label checked gone after the overwrite",
        "not checked",
        "\n".join(texts),
    )


@check(
    "DSK-06",
    1,
    2,
    "wipefs -a erases the table and the partition nodes go",
    depends=("purge step 4",),
)
def dsk_06(b: Bench) -> None:
    """``wipefs -a <disk>`` (no ``-f``) erases every signature, re-reads the
    table, and the partition nodes go; exit 0. util-linux
    misc-utils/wipefs.c:520-528, :545-583, :598-613, :823, :843."""
    s = _stick(b)
    _ = s.prepare()
    _ = s.ok(f"wipefs -a {s.disk}")
    expect(_wait_gone(s.part1), f"{s.part1} gone", "still there")
    parts = _partitions(s.disk)
    expect(not parts, "no partition left", ", ".join(parts))


@check(
    "DSK-07",
    1,
    2,
    "sgdisk --zap-all, then blkid -p finds nothing",
    depends=("purge step 5 and its final checks", "prepare's and health's blank test"),
)
def dsk_07(b: Bench) -> None:
    """``sgdisk --zap-all``, ``blockdev --flushbufs``, then ``blkid -p
    <disk>`` exits 2 (nothing found) and lsblk lists no partition.
    util-linux misc-utils/blkid.c:31, :600-601, disk-utils/blockdev.c:197-199;
    sgdisk (gptfdisk) as observed, its version in the report."""
    s = _stick(b)
    _ = s.prepare()
    _ = s.ok(f"wipefs -a {s.disk}")
    _ = s.ok(f"sgdisk --zap-all {s.disk}")
    _ = s.ok(f"blockdev --flushbufs {s.disk}")
    _ = s.ok(SETTLE)
    r = s.run(f"blkid -p {s.disk}")
    expect(r.returncode == 2, "blkid -p exit 2", exit_status(r), text_of(r))
    parts = _partitions(s.disk)
    expect(not parts, "no partition", ", ".join(parts))


@check(
    "DSK-08",
    1,
    2,
    "a read-only pool is re-imported read-write before its destroy",
    depends=("purge's _destroy_labelled_pool (M4aF-9)",),
)
def dsk_08(b: Bench) -> None:
    """purge's order on a stick: imported read-only, exported, imported
    read-write, destroyed; then ``zpool import -d <part1>`` lists it only
    with ``-D``. OpenZFS module/zfs/spa.c:7851-7855, :7726;
    cmd/zpool/zpool_main.c:4384-4385, :3422-3426 (as POOL-04, POOL-05)."""
    s = _stick(b)
    guid = s.prepare()
    zfs = ZFS(s.log)
    altroot = backup_altroot(s.s)
    expect(
        zfs.import_backup_pool(s.s, s.part1, altroot=altroot, readonly=True),
        "a read-only import",
        "it failed",
    )
    _ = s.ok(f"zpool export {s.s}")
    expect(
        zfs.import_backup_pool(s.s, s.part1, altroot=altroot, guid=guid),
        "a read-write import",
        "it failed",
    )
    _ = s.ok(f"zpool destroy {s.s}")
    s.guids.pop(s.s, None)
    r = s.run(f"zpool import -d {shlex.quote(s.part1)}")
    expect(f"pool: {s.s}" not in r.stdout, "not listed without -D", "listed", text_of(r))
    r = s.run(f"zpool import -D -d {shlex.quote(s.part1)}")
    if f"pool: {s.s}" not in r.stdout or "(DESTROYED)" not in r.stdout:
        raise Fail(f"{s.s} listed (DESTROYED) with -D", "not listed so", text_of(r))
