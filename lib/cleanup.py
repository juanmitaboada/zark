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

This module also exposes the two device-flush primitives used by
commands that finish with an external (removable) drive still attached:

  * ``flush_device_cache(log)`` — pushes kernel buffers to the device
    via ``sync(2)`` and pauses to let the bridge firmware drain. Called
    unconditionally by ``Cleanup.run()`` after every successful pool
    export; commands that do not go through ``Cleanup`` (prepare, purge,
    umount) call it inline at the equivalent point.

  * ``eject_device(device, log)`` — issues SCSI ``SYNCHRONIZE CACHE``
    followed by ``START STOP UNIT`` (i.e. ``eject(1)``). Powers the
    bridge down. Only invoked when the operator has confirmed they
    want to physically disconnect the drive — never automatically,
    because commands like ``prepare`` are typically followed by
    ``backup`` against the same drive, and a forced eject between them
    would require the operator to unplug-and-replug for nothing.
"""

import atexit
import signal
import time
from pathlib import Path

from lib.keystore import Keystore
from lib.log import Log
from lib.sh import run

# Window between `sync` and any subsequent device-level action. The
# pause gives a USB-SATA bridge with broken FUA a chance to drain its
# internal write queue once the kernel has emitted all dirty buffers.
# Two seconds covers the worst-case observed bridge firmware behaviour
# without making the user wait noticeably at the end of a backup. Kept
# as a module-level constant so tests can patch it to 0.
USB_FLUSH_DELAY_SEC = 2

# Countdown applied to the eject prompt when a drive has autoeject enabled.
# Fixed (not configurable): the per-drive autoeject flag is the on/off knob;
# the duration is one value for everyone. 10 s is enough for a present
# operator to react ("press any key to decide") while keeping an unattended
# run moving.
EJECT_TIMEOUT_SECONDS = 10


def flush_device_cache(log: Log) -> None:
    """Kernel-side flush + grace window.

    Issues ``sync(2)`` to push the kernel page cache and dirty
    block-device buffers to the device, then pauses for
    ``USB_FLUSH_DELAY_SEC`` so misbehaving USB-SATA bridges have time
    to write their internal DRAM to NAND before any subsequent action.

    Does **not** eject the device — the bridge stays powered on and the
    drive stays visible in ``/dev``. Use ``eject_device()`` separately
    when the operator has confirmed they want to physically remove the
    drive.

    Failures are non-fatal: by the time this runs, ``zpool export`` has
    already returned success and the on-disk data is logically durable.
    What this layer protects is the operator's option to physically
    remove the drive a few seconds later without losing what the
    firmware has not yet confirmed.
    """
    r = run("sync")
    if not r.ok:
        log.warn(
            f"sync failed (rc={r.returncode}); device flush window may be incomplete",
        )
    time.sleep(USB_FLUSH_DELAY_SEC)


def eject_device(device: str, log: Log) -> bool:
    """Power down the bridge after issuing SYNCHRONIZE CACHE.

    Wraps ``eject(1)`` from ``util-linux``, which sends SCSI
    ``SYNCHRONIZE CACHE (0x35)`` followed by ``START STOP UNIT``
    (stop=1). SYNCHRONIZE CACHE is the device's most authoritative
    flush primitive — distinct from inline FUA — and most bridges that
    cheat on FUA still honour it. STOP UNIT then asks the controller to
    power down; note that some USB-SATA bridges ignore it and keep the
    device enumerated, so a successful eject does not guarantee the
    device left the bus.

    Returns True on success, False otherwise. A failure is reported as
    WARN, never fatal: the on-disk data is already durable from the
    preceding sync; eject is belt-and-suspenders against the
    bridge-specific cache layer.

    Side effect: after a successful eject the device usually disappears
    from ``/dev`` until physically replugged — though bridges that
    ignore STOP UNIT may keep it present. This is fine either way: the
    operator just told us they were going to unplug, and the data is
    durable.
    """
    r = run(f"eject {device}")
    if r.ok:
        log.ok(
            f"Device {device} eject sent — caches flushed (some USB bridges "
            "ignore the controller-stop request, so the device may stay "
            "powered; the data is already durable either way)",
        )
        return True
    log.warn(
        f"eject {device} failed (rc={r.returncode}); data is durable but "
        "device-internal flush could not be confirmed — this enclosure "
        "may not honour SYNCHRONIZE CACHE, unplug at your own risk",
    )
    return False


def prompt_eject_or_attach(
    device: str | None,
    pool: str,
    log: Log,
    default_eject: bool,
    autoeject: bool = False,
) -> None:
    """Interactive eject decision after a successful pool export.

    Asks the operator whether to power the bridge down now, then emits
    the matching end-of-output banner:

      * yes → ``eject_device()`` then ``banner_safe_unplug(pool)``
      * no  → ``banner_drive_attached(pool)`` (drive stays in ``/dev``)

    ``default_eject`` is the command-specific default applied when the
    operator just hits Enter, and also when there is no interactive
    stdin (cron, systemd timer, scripted run). The choice of default
    reflects the typical workflow after each command:

      * ``backup``, ``umount``, ``purge``, ``recover``  → True
        Operator is typically done with the drive; one Enter unplugs.

      * ``prepare``, ``repair-divergent``               → False
        Typical next step is ``backup`` against the same drive; auto-
        ejecting would force a pointless unplug/replug cycle.

    Independent of the prompt, the kernel-side flush
    (``flush_device_cache``) has already run — issued by ``Cleanup.run()``
    after the export, or inline by commands that do not go through
    ``Cleanup``. The operator's "no" therefore leaves the drive in a
    consistent state: pool exported, kernel buffers flushed, bridge
    still powered. A clean disconnect from this state is achieved
    later via ``zark umount`` (which will prompt again) or simply by
    running another command that ends with the prompt.

    ``device=None`` means the underlying ``/dev/disk/by-id/...`` could
    not be resolved (typically a pool registered without a stable
    drive_id, or imported via ``zark mount`` outside the registry).
    No prompt is shown in that case — the banner alone tells the
    operator the drive is still attached and suggests ``zark umount``
    for a later clean disconnect.

    ``autoeject`` (per-drive, from ``known_drives.json``) opts this
    drive into a timed prompt: a 10 s countdown
    (``EJECT_TIMEOUT_SECONDS``) after which the drive is **ejected**
    (auto-eject means auto-eject, so the command's no-eject default —
    used by ``prepare``/``repair-divergent`` for the manual path — is
    overridden here). Any keypress cancels the countdown and falls back to
    a normal prompt. When ``False`` (default), the prompt waits for the
    operator indefinitely, exactly as before.
    """
    if device is None:
        log.warn(
            f"Device path for drive '{pool}' could not be resolved; "
            "eject skipped — use 'zark umount' for a clean disconnect later.",
        )
        log.banner_drive_attached(pool)
        return

    question = f"Eject drive '{pool}' now? (powers the device down)"
    if autoeject:
        # The operator opted this drive into auto-eject, so the countdown
        # ejects on expiry regardless of the command's own default. This is
        # what the name promises: in prepare/repair-divergent the no-eject
        # default exists only for the manual path; once auto-eject is on, the
        # drive is meant to power down on its own.
        decision = log.ask_timeout(question, True, EJECT_TIMEOUT_SECONDS)
    else:
        decision = log.ask(question, default=default_eject)
    if decision:
        eject_device(device, log)
        log.banner_safe_unplug(pool)
    else:
        log.banner_drive_attached(pool)


class Cleanup:  # pylint: disable=too-many-instance-attributes
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
        self._exported_pools: list[str] = []  # populated by run() — query via exported_pools()
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
        """Track a pool for export on exit. Call before importing or opening.

        Cleanup is intentionally device-agnostic: a successful export is
        always followed by ``flush_device_cache()``, never by ``eject``.
        The decision to power the bridge down is the calling command's,
        based on operator intent (typically asked interactively after
        the success banner) — see ``eject_device()`` for the eject
        primitive.
        """
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

    def exported_pools(self) -> list[str]:
        """Pools that the most recent ``run()`` call exported successfully.

        Callers use this after a successful operation to know which
        drives' caches were flushed and may be safe to physically
        disconnect (subject to the operator confirming an eject —
        Cleanup itself never ejects). Empty before ``run()`` is called,
        and reset on each ``run()`` invocation.
        """
        return list(self._exported_pools)

    def run(self):  # pylint: disable=too-many-branches,too-many-statements
        """Execute cleanup. Safe to call multiple times."""
        if getattr(self, "_disabled", False):
            return
        if not (self._mounts or self._keystores or self._pools or self._dirs):
            return
        self.log.info("Cleaning up...")

        self._exported_pools = []

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

        # Export pools — each successful export is followed by the
        # kernel-side flush (sync + sleep). Cleanup never ejects; that
        # is the calling command's call, made after the success banner
        # based on whether the operator intends to disconnect now.
        # See flush_device_cache() and eject_device() in this module.
        for pool in self._pools:
            if run(f"zpool list {pool}").ok:
                run(f"zfs unload-key -r {pool}")
                exported_ok = False
                r = run(f"zpool export {pool}")
                if r.ok:
                    self.log.ok(f"Pool {pool} exported")
                    exported_ok = True
                else:
                    r2 = run(f"zpool export -f {pool}")
                    if r2.ok:
                        self.log.ok(f"Pool {pool} exported (forced)")
                        exported_ok = True
                    else:
                        self.log.warn(f"Could not export {pool}")
                if exported_ok:
                    flush_device_cache(self.log)
                    self._exported_pools.append(pool)
        self._pools.clear()

        # Remove temp dirs
        for d in reversed(self._dirs):
            p = Path(d)
            if p.exists():
                run(f"rm -rf {d}")
        self._dirs.clear()

        self.log.dbg("Cleanup complete")
