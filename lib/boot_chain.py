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
The boot chain of a restored system: its datasets mounted under
RECOVER_MNT, the chroot, zpool.cache and grub.cfg, the signed GRUB and
shim on the ESP, the keystore hook of dracut or initramfs-tools, and the
guards.
"""

import re
from pathlib import Path

from lib import apt_guard, grub_guard, sh
from lib.cleanup import Cleanup
from lib.config import zark_command
from lib.identity import disks_under
from lib.log import Log
from lib.zfs import ZFS, fix_grub_bpool_uuid

# Additional bpool features for Ubuntu 25.04+ (dracut systems).
#
# These are the bpool features Ubuntu's installer activates by default on
# 25.04+. Enabling them on the recovered bpool keeps the pool layout
# consistent with a stock install and avoids surprises on the first
# `apt upgrade` (which may auto-enable them).
#
# CRITICAL: this list MUST be a subset of /usr/share/zfs/compatibility.d/grub2
# (the features GRUB2's built-in ZFS reader supports). GRUB reads bpool to
# load the kernel during boot. If bpool has features GRUB doesn't recognize,
# GRUB rejects the entire filesystem as unreadable and boot fails with
# "file '/BOOT/.../vmlinuz-...' not found" - even though Linux can read the
# pool fine.
#
# Specifically excluded: head_errlog, vdev_zaps_v2 - both unsupported by
# GRUB up to and including 2.14 (the version shipped in Ubuntu 26.04).
# rpool is unaffected by this restriction since rpool is read by the
# kernel's full ZFS module, never by GRUB.
BPOOL_FEATURES_EXTENDED = "userobj_accounting project_quota spacemap_v2 log_spacemap"


RECOVER_MNT = "/mnt/recover"


def _force_latest_signed_alternative(
    chroot_path: str,
    name: str,
    latest_path: str,
    log: Log,
) -> None:
    """Force update-alternatives to point at the ``.signed.latest`` variant.

    Since shim-signed 1.51 (Ubuntu 22.04 SRU and 24.04+), the package ships
    two variants under update-alternatives: ``shimx64.efi.signed.latest``
    (currently shim 15.8) and ``shimx64.efi.signed.previous`` (currently
    15.4). Same split exists for grub-efi-amd64-signed.

    Subiquity in Ubuntu 26.04 has been observed to leave systems pointing
    at ``.previous``, which is then revoked by SBAT_LEVEL updates and
    causes ``Verifying shim SBAT data failed: Security Policy Violation``
    on the next boot. To avoid recover propagating this state to the
    restored system, we explicitly switch to ``.latest`` before invoking
    ``dpkg-reconfigure`` (which copies the chosen alternative to the ESP).

    Defensive about older releases: if the ``.latest`` variant doesn't
    exist in the chroot (e.g. very old Ubuntu without this split), the
    function logs a debug note and returns without changes. The
    subsequent ``dpkg-reconfigure`` still runs and uses whatever the
    default alternative is.

    Args:
        chroot_path: filesystem root where the recovered Ubuntu lives.
        name: alternative name (e.g. "shimx64.efi.signed").
        latest_path: absolute path inside the chroot to the .latest file
            (e.g. "/usr/lib/shim/shimx64.efi.signed.latest").
        log: logger.
    """
    if not Path(f"{chroot_path}{latest_path}").exists():
        log.dbg(f"  {name}: no .latest variant in chroot - using default alternative")
        return
    r = sh.run(
        f"chroot {chroot_path} update-alternatives --set {name} {latest_path}",
        log=log,
    )
    if r.ok:
        log.ok(f"  {name} → {latest_path} (latest variant forced)")
    else:
        log.warn(f"  {name}: update-alternatives --set failed (rc={r.returncode})")


def _install_keystore_dracut(recover_mnt: str, ubuntu_name: str, log: Log):
    """Install dracut 89keystore module (Ubuntu 25.04+)."""
    ks_mod_dir = Path(f"{recover_mnt}/usr/lib/dracut/modules.d/89keystore")
    ks_mod_dir.mkdir(parents=True, exist_ok=True)

    _ = (ks_mod_dir / "module-setup.sh").write_text("""#!/bin/bash
# dracut module: 89keystore - unlock ZFS keystore zvol

check() { require_binaries cryptsetup || return 1; return 0; }
depends() { echo "zfs crypt"; }
install() {
    inst_hook pre-mount 01 "$moddir/keystore-open.sh"
    inst_multiple cryptsetup blkid mount mkdir zfs awk
}
""")
    (ks_mod_dir / "module-setup.sh").chmod(0o755)

    _ = (ks_mod_dir / "keystore-open.sh").write_text(f"""#!/bin/bash
# Open rpool/keystore LUKS zvol and mount at /run/keystore/rpool.
# Runs as dracut pre-mount hook (priority 01 = before ZFS mount at ~90).
# At this point, rpool is imported (zfs-import-cache.service) so the zvol exists.

KEYSTORE_MNT="/run/keystore/rpool"
MAPPER_NAME="keystore-rpool"

# Already mounted? Skip.
[ -f "$KEYSTORE_MNT/system.key" ] && exit 0

# Find the keystore zvol device
ZVOL_DEV=""
if [ -b /dev/zvol/rpool/keystore ]; then
    ZVOL_DEV="/dev/zvol/rpool/keystore"
else
    for dev in /dev/zd*; do
        [ -b "$dev" ] || continue
        case "$dev" in /dev/zd[0-9]*p*) continue;; esac
        if blkid -s TYPE -o value "$dev" 2>/dev/null | grep -q crypto_LUKS; then
            ZVOL_DEV="$dev"
            break
        fi
    done
fi

[ -z "$ZVOL_DEV" ] && exit 0

# Open LUKS - password prompt via systemd-ask-password (integrates with plymouth)
if [ ! -b "/dev/mapper/$MAPPER_NAME" ]; then
    info "zark: Opening ZFS keystore ($ZVOL_DEV)..."
    PW="$(systemd-ask-password 'Passphrase for ZFS keystore:')"
    printf '%s' "$PW" | cryptsetup open --key-file=- "$ZVOL_DEV" "$MAPPER_NAME"
    _rc=$?
    unset PW
    [ "$_rc" -eq 0 ] && info "zark: LUKS keystore opened" || warn "zark: cryptsetup failed (rc=$_rc)"
fi

# Mount the key filesystem
if [ -b "/dev/mapper/$MAPPER_NAME" ] && [ ! -f "$KEYSTORE_MNT/system.key" ]; then
    mkdir -p "$KEYSTORE_MNT"
    mount "/dev/mapper/$MAPPER_NAME" "$KEYSTORE_MNT" 2>/dev/null
fi

if [ -f "$KEYSTORE_MNT/system.key" ]; then
    info "zark: system.key available"
    # Load ZFS encryption keys - try stored keylocation first, then explicit path
    if ! zfs load-key -a 2>/dev/null; then
        for ds in $(zfs list -H -o name,encryptionroot -r rpool 2>/dev/null | \\
                     awk '$2 != "-" && $1 == $2 {{{{print $1}}}}'); do
            zfs load-key -L "file://$KEYSTORE_MNT/system.key" "$ds" 2>/dev/null
        done
    fi
    keystatus="$(zfs get -H -o value keystatus rpool/ROOT/{ubuntu_name} 2>/dev/null)"
    [ "$keystatus" = "available" ] && info "zark: ZFS keys loaded" || warn "zark: key load failed"
else
    warn "zark: system.key NOT found after keystore open"
fi
""")  # noqa: E501
    (ks_mod_dir / "keystore-open.sh").chmod(0o755)
    log.ok("dracut keystore module installed (89keystore)")


def _install_keystore_initramfs(recover_mnt: str, ubuntu_name: str, log: Log):
    """Install initramfs-tools keystore hook + script (Ubuntu 24.04)."""
    hooks_dir = Path(f"{recover_mnt}/etc/initramfs-tools/hooks")
    scripts_dir = Path(f"{recover_mnt}/etc/initramfs-tools/scripts/local-premount")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Hook: include required binaries in the initrd
    _ = (hooks_dir / "keystore").write_text("""#!/bin/sh
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in prereqs) prereqs; exit 0;; esac

. /usr/share/initramfs-tools/hook-functions

copy_exec /sbin/cryptsetup /sbin
copy_exec /sbin/blkid /sbin
copy_exec /sbin/zfs /sbin
copy_exec /usr/bin/awk /usr/bin

# askpass for plymouth-integrated password prompt
if [ -x /lib/cryptsetup/askpass ]; then
    copy_exec /lib/cryptsetup/askpass /lib/cryptsetup
fi
""")
    (hooks_dir / "keystore").chmod(0o755)

    # Boot-time script: runs in local-premount (after ZFS import, before root mount)
    # PREREQ="zfs" ensures this runs after the zfs initramfs script imports pools.
    _ = (scripts_dir / "ORDER-keystore").write_text(f"""#!/bin/sh
# zark: Open rpool/keystore LUKS zvol and load ZFS encryption keys.
# Runs in local-premount phase (after zfs local-top imports pools).
PREREQ="zfs"
prereqs() {{ echo "$PREREQ"; }}
case "$1" in prereqs) prereqs; exit 0;; esac

. /scripts/functions

KEYSTORE_MNT="/run/keystore/rpool"
MAPPER_NAME="keystore-rpool"

# Already mounted? Skip.
[ -f "$KEYSTORE_MNT/system.key" ] && exit 0

# Find the keystore zvol device
ZVOL_DEV=""
if [ -b /dev/zvol/rpool/keystore ]; then
    ZVOL_DEV="/dev/zvol/rpool/keystore"
else
    for dev in /dev/zd*; do
        [ -b "$dev" ] || continue
        case "$dev" in /dev/zd[0-9]*p*) continue;; esac
        if blkid -s TYPE -o value "$dev" 2>/dev/null | grep -q crypto_LUKS; then
            ZVOL_DEV="$dev"
            break
        fi
    done
fi

[ -z "$ZVOL_DEV" ] && exit 0

# Open LUKS - password prompt via askpass (integrates with plymouth)
if [ ! -b "/dev/mapper/$MAPPER_NAME" ]; then
    log_begin_msg "zark: Opening ZFS keystore ($ZVOL_DEV)"
    if [ -x /lib/cryptsetup/askpass ]; then
        PW="$(/lib/cryptsetup/askpass 'Passphrase for ZFS keystore: ')"
    else
        # Fallback: direct read from console
        printf 'Passphrase for ZFS keystore: ' >/dev/console
        read -r PW </dev/console
    fi
    printf '%s' "$PW" | cryptsetup open --key-file=- "$ZVOL_DEV" "$MAPPER_NAME"
    _rc=$?
    unset PW
    if [ "$_rc" -eq 0 ]; then
        log_end_msg 0
    else
        log_end_msg 1
        log_warning_msg "zark: cryptsetup failed (rc=$_rc)"
    fi
fi

# Mount the key filesystem
if [ -b "/dev/mapper/$MAPPER_NAME" ] && [ ! -f "$KEYSTORE_MNT/system.key" ]; then
    mkdir -p "$KEYSTORE_MNT"
    mount "/dev/mapper/$MAPPER_NAME" "$KEYSTORE_MNT" 2>/dev/null
fi

if [ -f "$KEYSTORE_MNT/system.key" ]; then
    log_success_msg "zark: system.key available"
    # Load ZFS encryption keys - try stored keylocation first, then explicit path
    if ! zfs load-key -a 2>/dev/null; then
        for ds in $(zfs list -H -o name,encryptionroot -r rpool 2>/dev/null | \
                     awk '$2 != "-" && $1 == $2 {{print $1}}'); do
            zfs load-key -L "file://$KEYSTORE_MNT/system.key" "$ds" 2>/dev/null
        done
    fi
    keystatus="$(zfs get -H -o value keystatus rpool/ROOT/{ubuntu_name} 2>/dev/null)"
    if [ "$keystatus" = "available" ]; then
        log_success_msg "zark: ZFS keys loaded"
    else
        log_warning_msg "zark: key load failed"
    fi
else
    log_warning_msg "zark: system.key NOT found after keystore open"
fi
""")
    (scripts_dir / "ORDER-keystore").chmod(0o755)
    log.ok("initramfs-tools keystore module installed (hook + local-premount)")


def mount_restored_system(ubuntu_name: str, bpool_received: bool, log: Log) -> None:
    """Mount the restored system under RECOVER_MNT, root first.

    Receives were ``-u``, so nothing is mounted yet. The root is mounted
    explicitly (``zfs mount -a`` skips a ``canmount=noauto`` boot
    environment), then bpool on its ``/boot``, then everything else. A
    bpool mounted before the root would be shadowed by it.
    """
    root_ok = sh.run(f"zfs mount rpool/ROOT/{ubuntu_name}", log=log).ok
    if not root_ok or not Path(f"{RECOVER_MNT}/usr/bin/bash").exists():
        log.fatal(
            f"Cannot mount the restored root at {RECOVER_MNT}",
            solutions=[f"Boot the live USB again and run: {zark_command()} repair-boot"],
        )
    if bpool_received:
        boot_ok = sh.run(f"zfs mount bpool/BOOT/{ubuntu_name}", log=log).ok
        if not boot_ok or not sh.run(f"findmnt -n {RECOVER_MNT}/boot").ok:
            log.fatal(
                f"Cannot mount the restored bpool at {RECOVER_MNT}/boot",
                solutions=[f"Boot the live USB again and run: {zark_command()} repair-boot"],
            )
        # Regular vmlinuz-<v> files only: the kernel's postinst keeps the
        # vmlinuz and vmlinuz.old links next to them (M1P.8).
        kernel_count = sum(
            1
            for k in Path(f"{RECOVER_MNT}/boot").glob("vmlinuz-*")
            if k.is_file() and not k.is_symlink()
        )
        log.ok(f"bpool mounted at {RECOVER_MNT}/boot ✓  ({kernel_count} kernel(s))")
    _ = sh.run("zfs mount -a")
    mounted = sh.run(f"zfs mount | grep -c {RECOVER_MNT}").output
    log.ok(f"System mounted at {RECOVER_MNT} ({mounted} datasets)")


def install_boot_chain(  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    internal_disk: str,
    ubuntu_name: str,
    zfs: ZFS,
    cleanup: Cleanup,
    log: Log,
) -> list[str]:
    """Chroot binds, zpool.cache, grub.cfg fix, signed GRUB/shim, keystore hook, guards.

    Returns the errors for recover's summary: the ESP device not recorded
    in debconf (M4c-9), a signed EFI binary missing after the reconfigures
    (M4c-7).
    """
    errors: list[str] = []
    # Bind mounts for chroot
    for d in ("proc", "sys", "dev", "dev/pts", "run"):
        _ = sh.run(f"mkdir -p {RECOVER_MNT}/{d}")
        _ = sh.run(f"mount --bind /{d} {RECOVER_MNT}/{d}")
        cleanup.track_mount(f"{RECOVER_MNT}/{d}")

    _ = sh.run(f"mkdir -p {RECOVER_MNT}/sys/firmware/efi/efivars")
    _ = sh.run(f"mount -t efivarfs efivarfs {RECOVER_MNT}/sys/firmware/efi/efivars")
    cleanup.track_mount(f"{RECOVER_MNT}/sys/firmware/efi/efivars")

    # zpool.cache - required so grub and initrd can locate both pools
    cache_path = f"{RECOVER_MNT}/etc/zfs/zpool.cache"
    zfs.write_zpool_cache(cache_path, ["rpool", "bpool"])
    log.ok("zpool.cache written")

    # Fix bpool GUID in grub.cfg (bpool was created fresh - new GUID)
    bpool_guid = zfs.pool_guid("bpool")
    if bpool_guid:
        bpool_hex = format(int(bpool_guid), "016x")
        _ = fix_grub_bpool_uuid(Path(f"{RECOVER_MNT}/boot/grub/grub.cfg"), bpool_hex, log)
    else:
        log.warn("Cannot read bpool GUID - grub.cfg may have stale UUID")

    # ── Install GRUB bootloader (standard Ubuntu procedure) ─────────────
    # Replicate exactly what Ubuntu does on every grub package update:
    #   1. grub-install → GRUB modules + bootstrap grub.cfg + EFI binary
    #   2. reinstall grub-efi-amd64-signed + shim-signed → signed EFI binaries
    #   3. update-grub → regenerate main /boot/grub/grub.cfg
    # This is 100% standard - future apt upgrades work identically.

    # Step 1: grub-install (modules + bootstrap grub.cfg on EFI partition)
    log.info("Running grub-install...")
    r = sh.run(
        f"chroot {RECOVER_MNT} grub-install --target=x86_64-efi "
        + "--efi-directory=/boot/efi --bootloader-id=ubuntu "
        + f"--skip-fs-probe {internal_disk}",
        log=log,
    )
    if r.ok:
        log.ok("grub-install succeeded ✓")
    else:
        log.warn(f"grub-install failed: {r.stderr.strip()}")

    # Step 2: Re-run signed package postinst scripts (no network needed)
    # dpkg-reconfigure triggers the same postinst that apt runs on install:
    # grub-efi-amd64-signed postinst → grub-install with signed binary
    # shim-signed postinst → installs shimx64.efi
    #
    # The postinst tries to mount the ESP at /var/lib/grub/esp using the
    # device path stored in debconf (grub-efi/install_devices).  After a
    # recovery the target disk differs from the original, so the old by-id
    # path no longer exists.  Fix: (a) bind-mount the already-mounted ESP
    # so the postinst finds it, and (b) update debconf so future apt
    # upgrades use the correct device.
    log.info("Configuring signed GRUB + shim (Secure Boot)...")

    # (a) Bind-mount EFI partition at /var/lib/grub/esp inside chroot
    grub_esp = Path(f"{RECOVER_MNT}/var/lib/grub/esp")
    grub_esp.mkdir(parents=True, exist_ok=True)
    efi_bind_ok = sh.run(
        f"mount --bind {RECOVER_MNT}/boot/efi {grub_esp}",
    ).ok
    if efi_bind_ok:
        cleanup.track_mount(str(grub_esp))

    # (b) Update debconf to point to the new EFI partition
    new_efi_part = sh.part(internal_disk, 1)
    new_efi_byid = sh.run(
        f"find /dev/disk/by-id/ -lname '*/{Path(new_efi_part).name}' | head -1",
    ).output.strip()
    # No by-id link (e.g. virtio): the kernel name.
    esp_dev = new_efi_byid or new_efi_part
    if esp_dev and not set_install_devices(RECOVER_MNT, esp_dev, log):
        errors.append(
            f"debconf grub-efi/install_devices not set to {esp_dev}: "
            + "a later GRUB update may not find the ESP",
        )

    # ── Force .latest signed variant before dpkg-reconfigure ────────────
    # shim-signed and grub-efi-amd64-signed ship both .latest and .previous
    # binaries since 1.51. Subiquity may leave the alternative pointing at
    # .previous (revoked by recent SBAT updates), so we explicitly switch
    # to .latest before dpkg-reconfigure copies it to the ESP. See
    # _force_latest_signed_alternative for full rationale.
    log.info("Pinning Secure Boot binaries to .latest variant...")
    _force_latest_signed_alternative(
        RECOVER_MNT,
        "shimx64.efi.signed",
        "/usr/lib/shim/shimx64.efi.signed.latest",
        log,
    )
    _force_latest_signed_alternative(
        RECOVER_MNT,
        "grubx64.efi.signed",
        "/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed.latest",
        log,
    )

    r = sh.run(
        f"chroot {RECOVER_MNT} dpkg-reconfigure -f noninteractive grub-efi-amd64-signed",
        log=log,
    )
    if r.ok:
        log.ok("grub-efi-amd64-signed configured ✓")
    else:
        log.warn("grub-efi-amd64-signed configure failed - manual copy as fallback")
        # Fallback: copy signed binaries manually
        efi_ubuntu = Path(f"{RECOVER_MNT}/boot/efi/EFI/ubuntu")
        grub_signed = Path(f"{RECOVER_MNT}/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed")
        if grub_signed.exists():
            _ = sh.run(f"cp {grub_signed} {efi_ubuntu}/grubx64.efi")

    r = sh.run(
        f"chroot {RECOVER_MNT} dpkg-reconfigure -f noninteractive shim-signed",
        log=log,
    )
    if r.ok:
        log.ok("shim-signed configured ✓")
    else:
        log.warn("shim-signed configure failed - manual copy as fallback")
        efi_ubuntu = Path(f"{RECOVER_MNT}/boot/efi/EFI/ubuntu")
        shim_src = Path(f"{RECOVER_MNT}/usr/lib/shim/shimx64.efi.signed")
        if shim_src.exists():
            _ = sh.run(f"cp {shim_src} {efi_ubuntu}/shimx64.efi")

    # Step 3: grub.cfg - we keep the backup's original grub.cfg (bpool UUID
    # already fixed above).  update-grub cannot run in a chroot with ZFS
    # altroot because 10_linux_zfs fails to mount encrypted datasets
    # ("encryption key not loaded").  The backup grub.cfg is valid: it
    # references rpool/bpool by name, and the first kernel update on the
    # running system will regenerate it via the standard apt trigger.
    log.ok("grub.cfg preserved from backup (bpool UUID updated) ✓")

    # Verify Secure Boot chain
    shim_ok = Path(f"{RECOVER_MNT}/boot/efi/EFI/ubuntu/shimx64.efi").exists()
    grub_ok = Path(f"{RECOVER_MNT}/boot/efi/EFI/ubuntu/grubx64.efi").exists()
    if shim_ok and grub_ok:
        log.ok("Secure Boot chain present (shim + grub) ✓")
    else:
        # M4c-7: a missing binary is an error of the recovery, not a warning.
        gone = [n for n, ok in (("shimx64.efi", shim_ok), ("grubx64.efi", grub_ok)) if not ok]
        log.error(f"{', '.join(gone)} missing in /boot/efi/EFI/ubuntu - boot may fail")
        errors.append(
            f"EFI: {', '.join(gone)} missing in /boot/efi/EFI/ubuntu after the signed packages' "
            + "reconfigure",
        )

    # Install keystore boot module - opens LUKS keystore zvol AFTER ZFS import
    # but BEFORE dataset mounting, so /run/keystore/rpool/system.key is available.
    # This is needed because systemd-cryptsetup runs BEFORE ZFS import (the zvol
    # device doesn't exist yet), creating a chicken-and-egg problem.
    #
    # dracut (Ubuntu 25.04+): 89keystore module with pre-mount hook
    # initramfs-tools (Ubuntu 24.04): hook + local-premount script

    if Path(f"{RECOVER_MNT}/usr/bin/dracut").exists():
        _install_keystore_dracut(RECOVER_MNT, ubuntu_name, log)
        # Enable extended bpool features (safe for GRUB on 25.04+)
        for feat in BPOOL_FEATURES_EXTENDED.split():
            _ = sh.run(f"zpool set feature@{feat}=enabled bpool")
        log.ok(f"bpool extended features enabled ({len(BPOOL_FEATURES_EXTENDED.split())})")
    else:
        _install_keystore_initramfs(RECOVER_MNT, ubuntu_name, log)

    # Install grub guard - prevents update-grub from running with external
    # ZFS pools connected (which corrupts grub.cfg with no kernel entries)
    _ = grub_guard.install(target_root=RECOVER_MNT, log=log)

    # Install the apt guard - stops a later kernel/GRUB/ZFS upgrade on the
    # recovered system from half-applying while a backup drive is connected.
    apt_guard.install(target_root=RECOVER_MNT, log=log)
    return errors


# The template is a multiselect owned by grub-efi-amd64 (and grub2-common);
# a line without its owner is "owner question type" to debconf-set-selections,
# which warns "Unknown type", skips it and exits 0 (P0-9, M4c-9).
_INSTALL_DEVICES_RE = re.compile(r"^\s*\*?\s*grub-efi/install_devices:\s*(.*?)\s*$", re.MULTILINE)


def set_install_devices(root: str, device: str, log: Log) -> bool:
    """Record ``device`` as the ESP in ``root``'s debconf and read it back."""
    line = f"grub-efi-amd64 grub-efi/install_devices multiselect {device}"
    r = sh.run(f"chroot {root} bash -c \"echo '{line}' | debconf-set-selections\"", log=log)
    if not r.ok or "warning" in r.stderr.lower():
        log.warn(f"debconf-set-selections did not take {line!r}: {r.stderr.strip()}")
        return False
    shown = sh.run(f"chroot {root} debconf-show grub-efi-amd64")
    m = _INSTALL_DEVICES_RE.search(shown.output) if shown.ok else None
    if m is None or m.group(1) != device:
        got = m.group(1) if m else "nothing"
        log.warn(f"debconf grub-efi/install_devices reads back {got}, not {device}")
        return False
    log.ok(f"debconf grub-efi/install_devices -> {device}")
    return True


_ESP_PARTTYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"


def esp_of_rpool_disk() -> str:
    """EFI System Partition on the disk holding rpool's vdev, or ""."""
    vdevs = sh.run("zpool list -vHP rpool")
    for line in vdevs.lines[1:] if vdevs.ok else []:
        path = line.strip().split("\t")[0]
        if not path.startswith("/dev/"):
            continue
        for disk in sorted(disks_under(path)):
            parts = sh.run(f"lsblk -nr -o NAME,PARTTYPE {disk}")
            for p in parts.lines if parts.ok else []:
                fields = p.split()
                if len(fields) == 2 and fields[1].lower() == _ESP_PARTTYPE:
                    return f"/dev/{fields[0]}"
    return ""
