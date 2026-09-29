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
initrd regeneration for a mounted target system (recover, repair-boot).

Only kernels that are really installed are regenerated: a version needs
both ``/boot/vmlinuz-<v>`` and ``/lib/modules/<v>/modules.dep[.bin]`` in the
target. ``dracut --regenerate-all`` iterates every ``/lib/modules/*`` with a
``modules.dep``, including the directory the Ubuntu installer leaves for
the live ISO kernel (P0-7), and returns non-zero when any of them fails;
zark used to ignore that exit status. Each version is now run on its own
with ``dracut --force --kver=<v>`` (what ``--regenerate-all`` does per
directory) and every failure is reported.
"""

from pathlib import Path

from lib import sh
from lib.log import Log


def installed_kernels(root: str) -> list[str]:
    """Kernel versions with both a vmlinuz in /boot and modules in /lib/modules."""
    versions: list[str] = []
    for vmlinuz in sorted(Path(f"{root}/boot").glob("vmlinuz-*")):
        v = vmlinuz.name[len("vmlinuz-") :]
        mods = Path(f"{root}/lib/modules/{v}")
        if (mods / "modules.dep").exists() or (mods / "modules.dep.bin").exists():
            versions.append(v)
    return versions


def regenerate_initrd(root: str, log: Log) -> list[str]:
    """Regenerate the initrd of every installed kernel in ``root``.

    Returns human-readable failures (empty on full success). Uses dracut
    when the target has it, initramfs-tools otherwise.
    """
    kernels = installed_kernels(root)
    if not kernels:
        return [f"no installed kernel found under {root}/boot"]
    failures: list[str] = []
    if Path(f"{root}/usr/bin/dracut").exists():
        log.info(f"Regenerating initrd (dracut) for: {', '.join(kernels)}")
        machine_id = sh.run(f"cat {root}/etc/machine-id").output.strip()
        for v in kernels:
            if machine_id:
                _ = sh.run(f"mkdir -p {root}/boot/efi/{machine_id}/{v}")
            r = sh.run(f"chroot {root} dracut --force --kver={v}", log=log)
            if r.ok:
                log.ok(f"initrd regenerated for {v} (dracut) ✓")
            else:
                failures.append(f"dracut failed for {v} (rc={r.returncode})")
                log.error(f"dracut failed for {v} (rc={r.returncode})")
    elif Path(f"{root}/usr/sbin/update-initramfs").exists():
        log.info(f"Regenerating initrd (update-initramfs) for: {', '.join(kernels)}")
        for v in kernels:
            r = sh.run(f"chroot {root} update-initramfs -u -k {v}", log=log)
            if r.ok:
                log.ok(f"initrd regenerated for {v} ✓")
            else:
                failures.append(f"update-initramfs failed for {v} (rc={r.returncode})")
                log.error(f"update-initramfs failed for {v} (rc={r.returncode})")
    else:
        failures.append("no initrd generator (dracut / update-initramfs) in the target")
    return failures
