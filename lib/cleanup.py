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
Cleanup and safe teardown.

Used by all commands that import pools or mount datasets.
Ensures pools are exported and mounts removed on exit.
"""

import atexit
import signal
from pathlib import Path

from lib.keystore import Keystore
from lib.log import Log
from lib.sh import run


class Cleanup:
    """
    Tracks resources to clean up on exit.
    Register pools, mount points, and keystore instances.
    """

    def __init__(self, log: Log):
        self.log = log
        self._pools: list[str] = []  # pools to export
        self._mounts: list[str] = []  # mount points to unmount (LIFO)
        self._keystores: list[Keystore] = []  # keystores to close
        self._dirs: list[str] = []  # temp dirs to remove
        self._registered = False
        self._disabled = False

    def register(self):
        """Install atexit and signal handlers. Call once."""
        if self._registered:
            return
        atexit.register(self.run)
        signal.signal(signal.SIGTERM, lambda *_: self.run())
        self._registered = True
        self._disabled = False

    def disable(self):
        """Disable cleanup — use when leaving resources mounted for the user."""
        self._disabled = True
        self._pools.clear()
        self._mounts.clear()
        self._keystores.clear()
        self._dirs.clear()

    def track_pool(self, name: str):
        """Track a pool for export on exit. Call before importing or opening."""
        if name not in self._pools:
            self._pools.append(name)

    def track_mount(self, path: str):
        """Track a mount point for unmounting on exit. Call after mounting."""
        if path not in self._mounts:
            self._mounts.append(path)

    def track_keystore(self, ks: Keystore):
        """Track a keystore for closing on exit. Call after opening."""
        if ks not in self._keystores:
            self._keystores.append(ks)

    def track_dir(self, path: str):
        """Track a temp directory for removal on exit. Call after creating."""
        if path not in self._dirs:
            self._dirs.append(path)

    def untrack_pool(self, name: str):
        """'Stop tracking a pool. Call if you export or close it manually."""
        if name in self._pools:
            self._pools.remove(name)

    def run(self):  # pylint: disable=too-many-branches,too-many-statements
        """Execute cleanup. Safe to call multiple times."""
        if getattr(self, "_disabled", False):
            return
        if not (self._mounts or self._keystores or self._pools or self._dirs):
            return
        self.log.info("Cleaning up...")

        # Unmount in reverse order (deepest first)
        for mnt in reversed(self._mounts):
            if Path(mnt).is_mount():
                r = run(f"umount {mnt}")
                if r.ok:
                    self.log.dbg(f"Unmounted {mnt}")
                else:
                    run(f"umount -l {mnt}")  # lazy fallback
                    self.log.dbg(f"Lazy unmounted {mnt}")
        self._mounts.clear()

        # Close keystores
        for ks in self._keystores:
            ks.umount()
        self._keystores.clear()

        # Export pools
        for pool in self._pools:
            if run(f"zpool list {pool}").ok:
                run(f"zfs unload-key -r {pool}")
                r = run(f"zpool export {pool}")
                if r.ok:
                    self.log.ok(f"Pool {pool} exported")
                    run("sync")
                else:
                    r2 = run(f"zpool export -f {pool}")
                    if r2.ok:
                        self.log.ok(f"Pool {pool} exported (forced)")
                    else:
                        self.log.warn(f"Could not export {pool}")
        self._pools.clear()

        # Remove temp dirs
        for d in reversed(self._dirs):
            p = Path(d)
            if p.exists():
                run(f"rm -rf {d}")
        self._dirs.clear()

        self.log.dbg("Cleanup complete")
