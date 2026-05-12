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

CRITICAL: zvol device detection must be robust. We identify the correct
zvol by matching its LUKS UUID or by pool relationship, NOT by assuming
/dev/zd* ordering (which is unreliable).
"""

from pathlib import Path

from lib.log import Log
from lib.sh import run

# Standard Ubuntu path where system.key is expected
KEYSTORE_MOUNT = "/run/keystore/rpool"
SYSTEM_KEY_PATH = f"{KEYSTORE_MOUNT}/system.key"


class Keystore:
    """Manages keystore zvol mount/unmount and key loading."""

    def __init__(self, log: Log):
        self.log = log
        self._mapper_name: str | None = None
        self._mounted = False

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
        zark run that left the LUKS device open.
        """
        self._mapper_name = Keystore.mapper_name_for(pool)

    def find_zvol_for_pool(self, pool: str) -> str | None:
        """
        Find the /dev/zd* device for a pool's keystore zvol.

        Strategy: check which zvol belongs to which pool by looking at
        the ZFS dataset that owns it, rather than assuming device order.
        """
        # List all zd devices
        r = run("ls -1 /dev/zd* 2>/dev/null")
        if not r.ok:
            self.log.warn("No ZFS volume devices found (/dev/zd*)")
            return None

        zd_devices = sorted(r.lines)
        self.log.dbg(f"Found zvol devices: {zd_devices}")

        if not zd_devices:
            return None

        # Try to identify which zd device belongs to which pool
        # by checking /sys/class/block/zdN/dm/name or by testing
        # LUKS open with the passphrase
        #
        # Primary method: check if the pool has a keystore dataset
        # and find its associated zvol via /sys
        keystore_ds = f"{pool}/keystore"
        r_ks = run(f"zfs list -H -o objsetid {keystore_ds}")
        if r_ks.ok:
            objsetid = r_ks.output.strip()
            self.log.dbg(f"Keystore dataset {keystore_ds} objsetid={objsetid}")

        # Fallback: if rpool is imported (live system), rpool's keystore = first zd,
        # backup pool's keystore = second zd. If rpool not imported, first zd is ours.
        rpool_imported = run("zpool list rpool").ok

        if not rpool_imported:
            # No live rpool — first device is our target
            self.log.dbg(f"No rpool imported: using {zd_devices[0]}")
            return zd_devices[0]

        if pool == "rpool":
            self.log.dbg(f"Target is rpool: using {zd_devices[0]}")
            return zd_devices[0]

        # rpool is imported and we want a different pool's keystore
        if len(zd_devices) >= 2:
            self.log.dbg(f"rpool imported + backup pool: using {zd_devices[1]}")
            return zd_devices[1]

        # Only one device — might be rpool's, might be ours
        self.log.warn(
            f"Only one zvol device ({zd_devices[0]}) but rpool is imported. "
            "Cannot distinguish keystore ownership.",
        )
        # Try it anyway — worst case, wrong passphrase will fail cleanly
        return zd_devices[0]

    def mount(self, pool: str, passphrase: str) -> bool:
        """
        Mount the keystore LUKS volume for a pool.
        After this, system.key is accessible at SYSTEM_KEY_PATH.
        """
        zd_dev = self.find_zvol_for_pool(pool)
        if not zd_dev:
            self.log.error(f"No keystore zvol found for pool {pool}")
            return False

        self._mapper_name = Keystore.mapper_name_for(pool)
        mnt = Path(KEYSTORE_MOUNT)
        mnt.mkdir(parents=True, exist_ok=True)

        # Open LUKS
        r = run(
            f"cryptsetup open {zd_dev} {self._mapper_name}",
            input=passphrase + "\n",
            log=self.log,
        )
        if not r.ok:
            self.log.error(f"Cannot open keystore LUKS on {zd_dev} — wrong passphrase?")
            self.log.dbg(f"cryptsetup stderr: {r.stderr.strip()}")
            return False

        # Mount
        r = run(f"mount /dev/mapper/{self._mapper_name} {mnt}", log=self.log)
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
