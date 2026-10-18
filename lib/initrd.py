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
initrd regeneration for a mounted target system (recover, repair-boot) or
the running one (finish, ``root=""``).

Only kernels that are really installed are regenerated: a version needs a
regular file ``/boot/vmlinuz-<v>`` (never the ``vmlinuz``/``vmlinuz.old``
links the kernel's postinst keeps) and ``/lib/modules/<v>/modules.dep[.bin]``
in the target. ``dracut --regenerate-all`` iterates every ``/lib/modules/*``
with a ``modules.dep``, including the directory the Ubuntu installer leaves
for the live ISO kernel (P0-7), and returns non-zero when any of them
fails. Each version is run on its own and every failure is reported.

dracut is given the output path, ``dracut --force /boot/initrd.img-<v> <v>``,
as Ubuntu's own update-initramfs wrapper for dracut does: without it dracut
picks its path itself, and an ESP restored with a ``loader/entries``
directory sends the initrd there, where 10_linux_zfs never looks (M4c-5).
"""

from pathlib import Path

from lib import sh
from lib.log import Log


def installed_kernels(root: str) -> list[str]:
    """Kernel versions with both a vmlinuz in /boot and modules in /lib/modules."""
    versions: list[str] = []
    for vmlinuz in sorted(Path(f"{root}/boot").glob("vmlinuz-*")):
        if vmlinuz.is_symlink() or not vmlinuz.is_file():
            continue
        v = vmlinuz.name[len("vmlinuz-") :]
        mods = Path(f"{root}/lib/modules/{v}")
        if (mods / "modules.dep").exists() or (mods / "modules.dep.bin").exists():
            versions.append(v)
    return versions


def regenerate_initrd(root: str, log: Log) -> list[str]:
    """Regenerate the initrd of every installed kernel in ``root``.

    ``root`` is a mounted target, run through ``chroot``, or "" for the
    running system. Returns human-readable failures, each naming its kernel
    (empty on full success). Uses dracut when the target has it,
    initramfs-tools otherwise.
    """
    kernels = installed_kernels(root)
    if not kernels:
        return [f"no installed kernel found under {root}/boot"]
    inside = f"chroot {root} " if root else ""
    failures: list[str] = []
    if Path(f"{root}/usr/bin/dracut").exists():
        log.info(f"Regenerating initrd (dracut) for: {', '.join(kernels)}")
        for v in kernels:
            r = sh.run(f"{inside}dracut --force /boot/initrd.img-{v} {v}", log=log)
            if r.ok:
                log.ok(f"initrd regenerated for {v} (dracut) ✓")
            else:
                failures.append(f"dracut failed for {v} (rc={r.returncode})")
                log.error(f"dracut failed for {v} (rc={r.returncode})")
    elif Path(f"{root}/usr/sbin/update-initramfs").exists():
        log.info(f"Regenerating initrd (update-initramfs) for: {', '.join(kernels)}")
        for v in kernels:
            r = sh.run(f"{inside}update-initramfs -u -k {v}", log=log)
            if r.ok:
                log.ok(f"initrd regenerated for {v} ✓")
            else:
                failures.append(f"update-initramfs failed for {v} (rc={r.returncode})")
                log.error(f"update-initramfs failed for {v} (rc={r.returncode})")
    else:
        failures.append("no initrd generator (dracut / update-initramfs) in the target")
    return failures
