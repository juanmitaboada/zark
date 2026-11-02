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
Which boot layout a system has (M12-1 1a).

Two layouts are backed up: ``bpool``, the 24.04 and 26.04 installers'
(``/boot`` on a pool of its own), and ``ext4``, the 26.10 installer's on
UEFI (``/boot`` a plain ext4 partition on rpool's disk, no bpool;
subiquity ``requirements.py:133-138`` at 26.10's installer). Anything
else is refused with its reason.

The ext4 layout is read at every ``backup`` and ``prepare``, not once at
``setup``: an fstab mount that failed at boot leaves ``/boot`` an empty
directory of the root, and a ``/boot`` without a kernel of this root
would make a point that cannot boot (M12R-7).
"""

import os
import shlex
import tempfile
from dataclasses import dataclass, field
from typing import Literal

from lib import sh
from lib.config import zark_command
from lib.identity import disks_under
from lib.initrd import installed_kernels
from lib.log import Log

Layout = Literal["bpool", "ext4"]

# The dataset of origin's rpool that holds a copy of an ext4 /boot, one
# per boot environment (M12-2 2e).
BOOT_COPY = "zark-boot"


def boot_copy(be: str) -> str:
    """The copy of ``/boot`` for boot environment ``be``."""
    return f"rpool/ROOT/{be}/{BOOT_COPY}"


@dataclass(frozen=True)
class BootMount:
    """``/boot`` as ``findmnt`` shows it when it is a mount of its own."""

    fstype: str
    source: str
    uuid: str


def running_be() -> str:
    """The boot environment mounted at ``/`` (``ubuntu_<id>``), "" when
    ``/`` is not a dataset of ``rpool/ROOT``."""
    r = sh.run("findmnt -n -o SOURCE /")
    source = r.output.strip() if r.ok else ""
    if source.startswith("rpool/ROOT/") and source.count("/") == 2:
        return source.removeprefix("rpool/ROOT/")
    return ""


def boot_mount() -> BootMount | None:
    """``/boot``'s mount, or None when it is a directory of the root."""
    r = sh.run("findmnt -n -o FSTYPE,SOURCE,UUID /boot")
    fields = r.output.split() if r.ok else []
    if len(fields) < 2:
        return None
    return BootMount(fields[0], fields[1], fields[2] if len(fields) > 2 else "")


def ext4_problems(mount: BootMount, rpool_disks: set[str], root: str = "") -> list[str]:
    """Why an ext4 ``/boot`` is not the installer's layout; [] when it is.

    ``rpool_disks`` are the whole disks under rpool's vdevs: the installer
    puts ``/boot`` on a partition of that disk. ``root`` prefixes ``/boot``
    and ``/lib/modules`` (tests).
    """
    problems: list[str] = []
    if not mount.source.startswith("/dev/"):
        problems.append(f"/boot's source {mount.source} is not a block device")
    elif not rpool_disks:
        problems.append("rpool's disk cannot be read, so /boot's place cannot be checked")
    elif not disks_under(mount.source) & rpool_disks:
        problems.append(f"/boot ({mount.source}) is not on rpool's disk")
    if not installed_kernels(root):
        problems.append(
            "/boot holds no kernel of this root (a vmlinuz-<version> with its "
            + "/lib/modules/<version>): it may not have been mounted at boot"
        )
    return problems


def system_layout() -> Layout | None:
    """This system's layout as far as it can be told without refusing:
    ``bpool`` when bpool is imported, ``ext4`` when ``/boot`` is an ext4
    mount, None otherwise (``setup`` asks nothing then; ``backup`` and
    ``prepare`` refuse with the reason)."""
    if sh.run("zpool list -H -o name bpool").ok:
        return "bpool"
    mount = boot_mount()
    return "ext4" if mount is not None and mount.fstype == "ext4" else None


def boot_copies() -> list[str]:
    """Every ``zark-boot`` under ``rpool/ROOT`` (one per boot environment)."""
    r = sh.run("zfs list -H -o name -r rpool/ROOT")
    return [ln.strip() for ln in r.lines if ln.strip().endswith(f"/{BOOT_COPY}")] if r.ok else []


def require_boot_copy(log: Log) -> str:
    """The running boot environment, whose ``zark-boot`` must exist on an
    ext4 system before ``backup`` or ``prepare`` write anything (M12 plan
    5.2): a point is never taken without its ``/boot``."""
    be = running_be()
    copy = boot_copy(be) if be else ""
    if not be or copy not in boot_copies():
        log.fatal(
            f"This system has no {copy or 'rpool/ROOT/<be>/zark-boot'}",
            causes=[
                "/boot is an ext4 partition, outside ZFS: backup keeps a copy of it in that "
                + "dataset with every point",
                *([] if be else ["/ is not a dataset of rpool/ROOT (findmnt -n -o SOURCE /)"]),
            ],
            solutions=[f"Create it: {zark_command()} setup", "Nothing was written"],
        )
    return be


# (path relative to /boot, size, mtime in ns, inode) of every entry.
Manifest = tuple[tuple[str, int, int, int], ...]


def manifest(boot: str = "/boot") -> Manifest:
    """What ``/boot`` holds, the ESP mounted at ``/boot/efi`` left out."""
    entries: list[tuple[str, int, int, int]] = []
    for top, dirs, files in os.walk(boot):
        rel_top = os.path.relpath(top, boot)
        if rel_top == ".":
            dirs[:] = [d for d in dirs if d != "efi"]
        for name in [*dirs, *files]:
            path = os.path.join(top, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            entries.append((os.path.relpath(path, boot), st.st_size, st.st_mtime_ns, st.st_ino))
    return tuple(sorted(entries))


def manifest_diff(before: Manifest, after: Manifest) -> list[str]:
    """The paths that differ between two manifests, sorted."""
    a = {e[0]: e for e in before}
    b = {e[0]: e for e in after}
    return sorted(p for p in a.keys() | b.keys() if a.get(p) != b.get(p))


def refresh_boot_copy(be: str, log: Log) -> bool:
    """rsync ``/boot`` into the boot environment's ``zark-boot``, mounted
    at a temporary directory (M12-2 2e). Any rsync status but 0 is a
    failure, 23 and 24 included (M12R-7); the dataset is always unmounted."""
    ds = boot_copy(be)
    tmp = tempfile.mkdtemp(prefix="zark-boot-")
    try:
        if not sh.run(f"mount -t zfs {shlex.quote(ds)} {tmp}", log=log).ok:
            log.error(f"Cannot mount {ds} to copy /boot into it")
            return False
        try:
            r = sh.run(
                "rsync -aHAX --numeric-ids --delete --inplace --one-file-system "
                + f"--exclude=/efi/ /boot/ {tmp}/",
                log=log,
            )
        finally:
            if not sh.run(f"umount {tmp}", log=log).ok:
                log.warn(f"Cannot unmount {ds} from {tmp}")
        if not r.ok:
            log.error(
                f"Copying /boot into {ds} failed (rsync status {r.returncode}): "
                + (r.stderr.strip() or "no message")
            )
            return False
    finally:
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    log.ok(f"/boot copied into {ds}")
    return True


@dataclass
class BootRefresh:
    """The copy of ``/boot`` a run takes before its point, and whether
    ``/boot`` changed between that copy and the point (a kernel installed
    with dpkg -i, which the apt guard does not see: M12R-7)."""

    be: str
    log: Log
    before: Manifest = ()
    changed: list[str] = field(default_factory=list)
    stale: str = ""  # set by check(): why the point does not hold its root's /boot

    def refresh(self) -> bool:
        """Before the point: the manifest, then the copy."""
        self.before = manifest()
        return refresh_boot_copy(self.be, self.log)

    def check(self) -> str:
        """After the point: why the point does not hold its root's /boot, "" when it does."""
        self.changed = manifest_diff(self.before, manifest())
        if self.changed:
            shown = ", ".join(self.changed[:5]) + (" ..." if len(self.changed) > 5 else "")
            self.stale = f"/boot changed during the run ({shown}): the point is not marked complete"
        return self.stale
