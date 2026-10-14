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
ZFS keystore operations.

The keystore is a ZFS zvol (rpool/keystore or <pool>/keystore) containing
a LUKS-encrypted volume with system.key. This key is used to unlock all
encrypted datasets in rpool.

The keystore's zvol device is found by its dataset name (zvol_id), never
by the order of the /dev/zd* devices.
"""

import re
from pathlib import Path
from typing import Protocol

from lib.log import Log
from lib.sh import run

# Prints a zvol device's dataset name; Ubuntu's zfsutils-linux installs it
# here, and udev's 60-zvol.rules builds /dev/zvol/<dataset> from it.
ZVOL_ID = "/usr/lib/udev/zvol_id"
_WHOLE_ZD = re.compile(r"^/dev/zd\d+$")

# Standard Ubuntu path where system.key is expected
KEYSTORE_MOUNT = "/run/keystore/rpool"
SYSTEM_KEY_PATH = f"{KEYSTORE_MOUNT}/system.key"


# pylint and basedpyright disagree about the body of a Protocol stub:
# without the `...` basedpyright reads the docstring-only body as
# `return None`, which contradicts the declared return type; with it,
# pylint calls the ellipsis unnecessary. The ellipsis is the one the
# type checkers need, so W2301 is silenced here.
class KeystoreLike(Protocol):
    """The slice of :class:`Keystore` that callers outside this module use.

    ``lib.mount.mount_system_pools`` unlocks the keystore and loads the
    pool keys, then hands the object to ``Cleanup.track_keystore``, which
    calls ``umount()`` on exit — so ``umount`` belongs in this protocol
    even though mount_system_pools never calls it itself.
    """

    bad_passphrase: bool  # set by mount(): True when LUKS rejected the passphrase

    # pylint: disable=unnecessary-ellipsis
    def mount(self, pool: str, passphrase: str, *, readonly: bool = False) -> bool:
        """Mount the keystore LUKS volume for a pool."""
        ...

    def load_pool_keys(self, pool_root: str) -> int:
        """Load encryption keys for all datasets under pool_root."""
        ...

    def umount(self):
        """Unmount keystore and close LUKS."""
        ...


class Keystore:
    """Manages keystore zvol mount/unmount and key loading."""

    def __init__(self, log: Log):
        self.log = log
        self._mapper_name: str | None = None
        self._mounted = False
        self.bad_passphrase = False

    @staticmethod
    def mapper_name_for(pool: str) -> str:
        """
        Return the LUKS device-mapper name zark uses for a pool's keystore.

        This is the name passed to `cryptsetup open ... <name>`; the resulting
        device is /dev/mapper/<name>. Exposed as a static helper so external
        callers (e.g. `zark umount`) can reason about an already-open keystore
        without poking at private state.
        """
        return f"zark_ks_{pool}"

    def attach_to_pool(self, pool: str) -> None:
        """
        Prepare this Keystore instance to operate on an already-open keystore
        belonging to `pool`. Use when `umount()` needs to be called without a
        prior `mount()` in this process — e.g. cleaning up after a previous
        zark run that left the LUKS device open, or closing a keystore opened
        by an earlier `zark mount`/`zark mount local` invocation.

        Detects the real on-system state: if the LUKS mapper for this pool is
        actually open, marks the instance as mounted so a following
        :meth:`umount` proceeds to unmount and close it (otherwise ``umount``
        would early-return on ``_mounted=False`` and silently leave the zvol
        held, which hangs a subsequent ``zpool export`` in ``taskq_wait``).
        """
        self._mapper_name = Keystore.mapper_name_for(pool)
        # If the device-mapper node exists, the keystore is open from a prior
        # invocation; reflect that so umount() actually tears it down.
        if Path(f"/dev/mapper/{self._mapper_name}").exists():
            self._mounted = True

    def find_zvol_for_pool(self, pool: str) -> str | None:
        """The /dev/zd* device of ``<pool>/keystore``, found by its dataset name.

        Each whole zvol device (``/dev/zdN``, not its ``pN`` partitions) is
        asked its dataset name with ``zvol_id`` (``ioctl(BLKZNAME)``, OpenZFS
        ``udev/zvol_id.c``); when that tool is missing, the udev link
        ``/dev/zvol/<pool>/keystore`` that it feeds is read instead. Device
        order says nothing about ownership (``zd112`` sorts before ``zd16``,
        and any other zvol shifts the numbers), so there is no guess.
        """
        want = f"{pool}/keystore"
        r = run("ls -1 /dev/zd* 2>/dev/null")
        devs = sorted(
            (line.strip() for line in r.lines if _WHOLE_ZD.match(line.strip())) if r.ok else (),
            key=lambda d: int(d[len("/dev/zd") :]),
        )
        self.log.dbg(f"Found zvol devices: {devs}")
        for dev in devs:
            name = run(f"{ZVOL_ID} {dev}")
            if name.ok and name.output.strip() == want:
                self.log.dbg(f"{want} is {dev} (zvol_id)")
                return dev
        link = run(f"readlink -f /dev/zvol/{want}")
        if link.ok and _WHOLE_ZD.match(link.output.strip()):
            self.log.dbg(f"{want} is {link.output.strip()} (/dev/zvol link)")
            return link.output.strip()
        self.log.warn(f"No zvol device carries {want} ({len(devs)} zvol device(s) checked)")
        return None

    def mount(self, pool: str, passphrase: str, *, readonly: bool = False) -> bool:
        """
        Mount the keystore LUKS volume for a pool.
        After this, system.key is accessible at SYSTEM_KEY_PATH.

        ``readonly=True`` is required when the pool was imported with
        ``readonly=on``: LUKS is opened ``--readonly`` and the ext4 inside is
        mounted ``ro,noload``. ``noload`` skips the journal: a keystore
        snapshot is taken while the filesystem is mounted, so its journal is
        flagged for recovery and a plain ``ro`` mount of a read-only device
        refuses to proceed. system.key is written once at install time, so
        skipping the replay cannot hide it.
        """
        zd_dev = self.find_zvol_for_pool(pool)
        if not zd_dev:
            self.log.error(f"No keystore zvol found for pool {pool}")
            return False

        self._mapper_name = Keystore.mapper_name_for(pool)
        mnt = Path(KEYSTORE_MOUNT)
        mnt.mkdir(parents=True, exist_ok=True)

        # Open LUKS
        ro_luks = " --readonly" if readonly else ""
        r = run(
            f"cryptsetup open{ro_luks} {zd_dev} {self._mapper_name}",
            input=passphrase + "\n",
            log=self.log,
        )
        # cryptsetup exits 2 for "no permission (bad passphrase)".
        self.bad_passphrase = r.returncode == 2
        if not r.ok:
            self.log.error(f"Cannot open keystore LUKS on {zd_dev} — wrong passphrase?")
            self.log.dbg(f"cryptsetup stderr: {r.stderr.strip()}")
            return False

        # Mount
        ro_mount = " -o ro,noload" if readonly else ""
        r = run(f"mount{ro_mount} /dev/mapper/{self._mapper_name} {mnt}", log=self.log)
        if not r.ok:
            run(f"cryptsetup close {self._mapper_name}")
            self.log.error("Cannot mount keystore filesystem")
            return False

        # Verify key exists
        if not Path(SYSTEM_KEY_PATH).exists():
            self.log.error(f"system.key not found at {SYSTEM_KEY_PATH}")
            self.umount()
            return False

        self._mounted = True
        self.log.ok(f"Keystore mounted: {SYSTEM_KEY_PATH} ✓")
        return True

    def umount(self):
        """Unmount keystore and close LUKS. Idempotent: safe to call multiple times."""
        if not self._mounted:
            # Already unmounted. Critical: do NOT touch KEYSTORE_MOUNT here.
            # If our mount overlaid an existing mount (e.g. backup pool's keystore
            # mounted on top of the live system's rpool keystore at the same path),
            # the underlying mount has now reappeared and unmounting it would
            # break the live system.
            return
        mnt = Path(KEYSTORE_MOUNT)
        if mnt.is_mount():
            run(f"umount {mnt}")
        if self._mapper_name:
            run(f"cryptsetup close {self._mapper_name}")
        self._mounted = False
        self.log.dbg("Keystore unmounted")

    @property
    def is_mounted(self) -> bool:
        """Check if keystore is currently mounted and system.key is accessible."""
        return self._mounted and Path(SYSTEM_KEY_PATH).exists()

    def load_pool_keys(self, pool_root: str) -> int:
        """Load encryption keys for all datasets under pool_root using system.key."""
        if not self.is_mounted:
            self.log.error("Keystore not mounted — cannot load keys")
            return 0

        # Lazy import to avoid a circular dependency between zfs ↔ keystore
        # at module load time.
        from lib.zfs import ZFS  # pylint: disable=import-outside-toplevel

        zfs = ZFS(self.log)

        count = 0
        for ds in zfs.datasets_needing_key(pool_root):
            rk = run(f"zfs load-key -L file://{SYSTEM_KEY_PATH} {ds}")
            if rk.ok:
                count += 1
                self.log.dbg(f"Key loaded: {ds}")
            else:
                self.log.dbg(f"Key load failed: {ds}: {rk.stderr.strip()}")
        return count


MAX_PASSPHRASE_ATTEMPTS = 3


def open_keystore(
    ks: KeystoreLike,
    pool: str,
    log: Log,
    *,
    readonly: bool = False,
) -> bool:
    """Ask for the passphrase and open ``pool``'s keystore, re-asking on a typo.

    Up to :data:`MAX_PASSPHRASE_ATTEMPTS` prompts while LUKS reports a wrong
    passphrase; any other failure (no zvol, mount error) returns at once.
    """
    for attempt in range(1, MAX_PASSPHRASE_ATTEMPTS + 1):
        passphrase = log.ask_password(f"Passphrase for {pool}")
        if ks.mount(pool, passphrase, readonly=readonly):
            return True
        if not ks.bad_passphrase:
            return False
        log.warn(f"Wrong passphrase ({attempt}/{MAX_PASSPHRASE_ATTEMPTS})")
    return False
