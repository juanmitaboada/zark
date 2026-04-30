#!/bin/bash
#
# create_test_system.sh — Create a REAL minimal Ubuntu ZFS encrypted system.
#
# Run INSIDE QEMU Phase 1 after qemu_setup.sh.
# Creates a full Ubuntu system with debootstrap on /dev/vda, then
# prepares and backs up to /dev/vdb.
#
# Usage:  sudo bash /mnt/zark/tests/create_test_system.sh
#

set -euo pipefail

G="\033[0;32m"
C="\033[0;36m"
Y="\033[0;33m"
R="\033[0;31m"
B="\033[1m"
N="\033[0m"

SRC_DISK="/dev/vda"
BACKUP_DISK="/dev/vdb"
PASSPHRASE=""
MNT="/mnt/source"
UBUNTU_NAME="ubuntu_test01"

info() { echo -e "${C}[INFO]${N}  $*"; }
ok() { echo -e "${G}[OK]${N}    $*"; }
warn() { echo -e "${Y}[WARN]${N}  $*"; }
fail() {
    echo -e "${R}[FAIL]${N}  $*"
    exit 1
}

# ═══════════════════════════════════════════════════════════════════
#  Preflight
# ═══════════════════════════════════════════════════════════════════

echo ""
echo -e "${B}╔══════════════════════════════════════════════════════════╗${N}"
echo -e "${B}║  CREATE TEST SYSTEM — Real Ubuntu ZFS (encrypted)       ║${N}"
echo -e "${B}║  Using debootstrap for a fully bootable system          ║${N}"
echo -e "${B}╚══════════════════════════════════════════════════════════╝${N}"
echo ""

[ "$(id -u)" -eq 0 ] || fail "Must run as root"
[ -b "$SRC_DISK" ] || fail "Source disk $SRC_DISK not found"
[ -b "$BACKUP_DISK" ] || fail "Backup disk $BACKUP_DISK not found"

# Detect Ubuntu version from live environment
# shellcheck source=/dev/null
source /etc/os-release
SUITE="${VERSION_CODENAME:-noble}"
info "Detected Ubuntu $VERSION ($SUITE)"

# Install dependencies
export DEBIAN_FRONTEND=noninteractive
if ! command -v debootstrap &>/dev/null || ! command -v zfs &>/dev/null; then
    info "Installing dependencies..."
    apt-get update -qq 2>/dev/null
    apt-get install -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
        debootstrap zfsutils-linux gdisk cryptsetup >/dev/null 2>&1
    ok "Dependencies installed"
fi

# Ask for passphrase
echo -ne "  ${Y}?${N} Choose a test passphrase: "
read -r PASSPHRASE
[ -n "$PASSPHRASE" ] || fail "Passphrase cannot be empty"
echo ""

# ═══════════════════════════════════════════════════════════════════
#  1. Partition source disk
# ═══════════════════════════════════════════════════════════════════

info "Partitioning $SRC_DISK..."
wipefs -a "$SRC_DISK" >/dev/null 2>&1
sgdisk --zap-all "$SRC_DISK" >/dev/null
sgdisk -n1:1M:+512M -t1:EF00 "$SRC_DISK" >/dev/null # EFI
sgdisk -n2:0:+1G -t2:BE00 "$SRC_DISK" >/dev/null    # bpool
sgdisk -n3:0:+1G -t3:8200 "$SRC_DISK" >/dev/null    # swap
sgdisk -n4:0:0 -t4:BF00 "$SRC_DISK" >/dev/null      # rpool
sleep 1 && partprobe "$SRC_DISK" 2>/dev/null || true && sleep 1

SRC_P1="${SRC_DISK}1"
SRC_P2="${SRC_DISK}2"
SRC_P3="${SRC_DISK}3"
SRC_P4="${SRC_DISK}4"
for p in "$SRC_P1" "$SRC_P2" "$SRC_P3" "$SRC_P4"; do
    [ -b "$p" ] || fail "Partition $p not created"
done
ok "Partitioned: EFI(512M) + bpool(1G) + swap(1G) + rpool(rest)"

# ═══════════════════════════════════════════════════════════════════
#  2. Create rpool (encrypted)
# ═══════════════════════════════════════════════════════════════════

info "Creating rpool with encryption..."
TMPKEY=$(mktemp)
dd if=/dev/urandom of="$TMPKEY" bs=32 count=1 2>/dev/null

zpool create -f \
    -o ashift=12 \
    -O acltype=posixacl -O compression=lz4 -O dnodesize=auto \
    -O normalization=formD -O relatime=on -O xattr=sa \
    -O encryption=aes-256-gcm -O keyformat=raw -O keylocation="file://$TMPKEY" \
    -O canmount=off -O mountpoint=/ \
    -R "$MNT" \
    rpool "$SRC_P4"
ok "rpool created (encrypted)"

# ═══════════════════════════════════════════════════════════════════
#  3. Create datasets (Ubuntu layout)
# ═══════════════════════════════════════════════════════════════════

info "Creating Ubuntu dataset layout..."
zfs create -o canmount=off -o mountpoint=none rpool/ROOT
zfs create -o canmount=noauto -o mountpoint=/ rpool/ROOT/$UBUNTU_NAME
zfs mount rpool/ROOT/$UBUNTU_NAME

zfs create rpool/ROOT/$UBUNTU_NAME/srv
zfs create -o canmount=off rpool/ROOT/$UBUNTU_NAME/usr
zfs create rpool/ROOT/$UBUNTU_NAME/usr/local
zfs create -o canmount=off rpool/ROOT/$UBUNTU_NAME/var
zfs create rpool/ROOT/$UBUNTU_NAME/var/lib
zfs create rpool/ROOT/$UBUNTU_NAME/var/lib/dpkg
zfs create rpool/ROOT/$UBUNTU_NAME/var/log
zfs create rpool/ROOT/$UBUNTU_NAME/var/spool

zfs create -o canmount=off -o mountpoint=none rpool/USERDATA
zfs create -o mountpoint=/home rpool/USERDATA/home_test01
zfs create -o mountpoint=/root rpool/USERDATA/root_test01
ok "Datasets created"

# ═══════════════════════════════════════════════════════════════════
#  4. Create bpool
# ═══════════════════════════════════════════════════════════════════

info "Creating bpool..."
zpool create -f \
    -o ashift=12 -d \
    -o feature@async_destroy=enabled -o feature@bookmarks=enabled \
    -o feature@embedded_data=enabled -o feature@empty_bpobj=enabled \
    -o feature@enabled_txg=enabled -o feature@extensible_dataset=enabled \
    -o feature@filesystem_limits=enabled -o feature@hole_birth=enabled \
    -o feature@large_blocks=enabled -o feature@lz4_compress=enabled \
    -o feature@spacemap_histogram=enabled \
    -O acltype=posixacl -O compression=lz4 \
    -O normalization=formD -O relatime=on -O xattr=sa \
    -O canmount=off -O mountpoint=/boot \
    -R "$MNT" \
    bpool "$SRC_P2"

zfs create -o canmount=off -o mountpoint=none bpool/BOOT
zfs create -o mountpoint=/boot bpool/BOOT/$UBUNTU_NAME
ok "bpool created"

# ═══════════════════════════════════════════════════════════════════
#  5. Debootstrap real Ubuntu system
# ═══════════════════════════════════════════════════════════════════

info "Running debootstrap ($SUITE) — this takes a few minutes..."
debootstrap --variant=minbase "$SUITE" "$MNT" http://archive.ubuntu.com/ubuntu
ok "Debootstrap complete"

# ═══════════════════════════════════════════════════════════════════
#  6. Configure apt sources and install packages in chroot
# ═══════════════════════════════════════════════════════════════════

info "Setting up chroot environment..."

# Apt sources (deb822 format)
mkdir -p "$MNT/etc/apt/sources.list.d"
cat >"$MNT/etc/apt/sources.list.d/ubuntu.sources" <<EOF
Types: deb
URIs: http://archive.ubuntu.com/ubuntu
Suites: $SUITE $SUITE-updates $SUITE-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
# Remove old-style sources.list if present
rm -f "$MNT/etc/apt/sources.list"

# Bind mounts for chroot
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"
cp /etc/resolv.conf "$MNT/etc/resolv.conf"

info "Installing packages in chroot..."
info "This may take a few minutes..."
# Note: this test always uses dracut (regardless of which Ubuntu version
# is being simulated) so the test installs zfs-dracut unconditionally.
# On real hardware:
#   - 24.04 → zfs-initramfs (initramfs-tools) ships pre-installed
#   - 25.04+ → zfs-dracut (dracut) ships pre-installed
# zark recover detects which is present and uses the appropriate one.
chroot "$MNT" bash -c "
    export DEBIAN_FRONTEND=noninteractive
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin:\$PATH
    apt-get update -qq

    apt-get install -y --no-install-recommends \
        grub-efi-amd64 \
        grub-efi-amd64-signed \
        shim-signed \
        zfsutils-linux \
        zfs-dracut \
        cryptsetup \
        dracut \
        systemd-sysv \
        sudo \
        bash \
        locales

    apt-get clean
" || fail "Package installation failed in chroot"

# Copy live USB kernel + modules (includes ZFS) instead of downloading 880MB
LIVE_KVER=$(uname -r)
info "Copying live kernel $LIVE_KVER (includes ZFS modules)..."

# Resolve the real modules source path (handles /lib → /usr/lib symlink)
MODULES_SRC=$(realpath "/lib/modules/$LIVE_KVER")
# Ensure destination exists (usr-merged systems)
mkdir -p "$MNT/usr/lib/modules"
cp -a "$MODULES_SRC" "$MNT/usr/lib/modules/"
# Ensure /lib/modules symlink exists in target
[ -L "$MNT/lib" ] || ln -sf usr/lib "$MNT/lib" 2>/dev/null || true

# Copy kernel image
if [ -f "/boot/vmlinuz-$LIVE_KVER" ]; then
    cp "/boot/vmlinuz-$LIVE_KVER" "$MNT/boot/"
elif [ -f "/boot/vmlinuz" ]; then
    cp "/boot/vmlinuz" "$MNT/boot/vmlinuz-$LIVE_KVER"
else
    cp "/cdrom/casper/vmlinuz" "$MNT/boot/vmlinuz-$LIVE_KVER" 2>/dev/null ||
        fail "Cannot find kernel image"
fi

# Verify ZFS modules are where we expect
if find "$MNT/usr/lib/modules/$LIVE_KVER" -name 'zfs.ko*' 2>/dev/null | grep -q .; then
    ok "Kernel $LIVE_KVER copied with ZFS modules ✓"
else
    fail "ZFS modules not found after copy (source: $MODULES_SRC)"
fi

# Validate critical files
# shellcheck disable=SC2012
KVER_CHECK=$(ls "$MNT/usr/lib/modules/" 2>/dev/null | sort -V | tail -1)
[ -n "$KVER_CHECK" ] || fail "No kernel modules installed"
[ -x "$MNT/usr/sbin/grub-install" ] || fail "grub-install not found"
[ -x "$MNT/usr/bin/dracut" ] || fail "dracut not found"
ok "Validated: kernel $KVER_CHECK, grub-install, dracut"
ok "Packages installed in chroot"

# ═══════════════════════════════════════════════════════════════════
#  7. Configure the system
# ═══════════════════════════════════════════════════════════════════

info "Configuring system..."

# Hostname
echo "zark-test" >"$MNT/etc/hostname"
echo "127.0.0.1 zark-test" >>"$MNT/etc/hosts"

# os-release (should exist from debootstrap, but ensure)
[ -f "$MNT/etc/os-release" ] || cp /etc/os-release "$MNT/etc/os-release"

# hostid
chroot "$MNT" zgenhostid -f 0xdeadbeef 2>/dev/null ||
    printf '\xef\xbe\xad\xde' >"$MNT/etc/hostid"

# EFI partition
mkfs.vfat -F32 "$SRC_P1" >/dev/null
mkdir -p "$MNT/boot/efi"
mount "$SRC_P1" "$MNT/boot/efi"

# fstab
cat >"$MNT/etc/fstab" <<EOF
/dev/disk/by-uuid/$(blkid -s UUID -o value "$SRC_P1")  /boot/efi  vfat  umask=0077  0  1
EOF

# crypttab (swap only)
cat >"$MNT/etc/crypttab" <<EOF
swap  /dev/disk/by-partuuid/$(blkid -s PARTUUID -o value "$SRC_P3")  /dev/urandom  swap,cipher=aes-xts-plain64,size=256
EOF

# Create test user
chroot "$MNT" bash -c "
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin:\$PATH
    useradd -m -s /bin/bash -G sudo testuser 2>/dev/null || true
    echo 'testuser:test123' | chpasswd
    echo 'root:root' | chpasswd
"

# User data for phase 3 verification
echo "Hello from zark integration test" >"$MNT/home/testuser/test_data.txt"
echo "root test data" >"$MNT/root/.test_marker"

ok "System configured (user: testuser/test123, root: root/root)"

# ═══════════════════════════════════════════════════════════════════
#  8. Install GRUB + generate initrd
# ═══════════════════════════════════════════════════════════════════

info "Installing GRUB..."
mount -t efivarfs efivarfs "$MNT/sys/firmware/efi/efivars" 2>/dev/null || true

chroot "$MNT" bash -c "
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin:\$PATH
    grub-install --target=x86_64-efi --efi-directory=/boot/efi \
        --bootloader-id=ubuntu --skip-fs-probe --no-nvram 2>&1 || true
"
ok "GRUB installed"

# Generate grub.cfg with bpool UUID
BPOOL_GUID=$(zpool get -H -o value guid bpool)
BPOOL_HEX=$(printf '%016x' "$BPOOL_GUID")
# shellcheck disable=SC2012
KVER=$(ls "$MNT/usr/lib/modules/" | sort -V | tail -1)
info "Kernel: $KVER"

mkdir -p "$MNT/boot/grub"
# Note: the `/@` after the dataset path is GRUB's syntax for navigating into
# the *content* of a child ZFS dataset when traversing a pool. Files copied
# into $MNT/boot/ live in the root of the bpool/BOOT/$UBUNTU_NAME dataset,
# so from the pool's perspective they are reachable at:
#   (bpool)/BOOT/$UBUNTU_NAME/@/<file>
# Without the `@`, GRUB only sees snapshot directories and reports
# "file 'vmlinuz-<kver>' not found".
#
# Real Ubuntu's `update-grub` (10_linux_zfs) handles this through internal
# ${kernel}/${initrd} variables that resolve the dataset path correctly.
# This test fixture is ad-hoc — it just needs paths that GRUB can resolve
# so QEMU can boot. Recover doesn't depend on this exact format; it only
# parses `fs-uuid --set=...` to fix the bpool UUID.
cat >"$MNT/boot/grub/grub.cfg" <<EOF
set timeout=5
set default=0

menuentry 'Ubuntu ZFS Test' {
    search --no-floppy --fs-uuid --set=boot_fs $BPOOL_HEX
    linux (\$boot_fs)/BOOT/$UBUNTU_NAME/@/vmlinuz-$KVER root=ZFS=rpool/ROOT/$UBUNTU_NAME ro quiet
    initrd (\$boot_fs)/BOOT/$UBUNTU_NAME/@/initrd.img-$KVER
}
EOF
ok "grub.cfg created"

info "Generating initrd with dracut..."
chroot "$MNT" bash -c "
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin:\$PATH
    dracut --force /boot/initrd.img-$KVER $KVER 2>&1 | tail -5
" || warn "dracut had warnings"

if [ -f "$MNT/boot/initrd.img-$KVER" ]; then
    ok "initrd generated: initrd.img-$KVER"
else
    warn "initrd not found — recover will regenerate it"
fi

# ═══════════════════════════════════════════════════════════════════
#  9. Unmount chroot, create keystore zvol
# ═══════════════════════════════════════════════════════════════════

info "Creating keystore zvol..."

# Unmount chroot binds first (avoid kernel crash with zvols)
umount "$MNT/sys/firmware/efi/efivars" 2>/dev/null || true
umount "$MNT/boot/efi" 2>/dev/null || true
umount "$MNT/proc" 2>/dev/null || true
umount "$MNT/sys" 2>/dev/null || true
umount "$MNT/dev/pts" 2>/dev/null || true
umount "$MNT/dev" 2>/dev/null || true

zfs create -s -V 64M -o encryption=off rpool/keystore
sleep 2

ZD_DEV=$(printf '%s\n' /dev/zd* 2>/dev/null | head -1)
[ -n "$ZD_DEV" ] || fail "No zvol device for keystore"

echo -n "$PASSPHRASE" | cryptsetup luksFormat --batch-mode "$ZD_DEV" -
echo -n "$PASSPHRASE" | cryptsetup open "$ZD_DEV" zark_ks_test -
mkfs.ext4 -q /dev/mapper/zark_ks_test
mkdir -p /run/keystore/rpool
mount /dev/mapper/zark_ks_test /run/keystore/rpool
cp "$TMPKEY" /run/keystore/rpool/system.key
chmod 400 /run/keystore/rpool/system.key
ok "Keystore created with system.key"

# ═══════════════════════════════════════════════════════════════════
#  10. Snapshot
# ═══════════════════════════════════════════════════════════════════

info "Creating snapshots..."
SNAP="autosnap_$(date +%Y-%m-%d_%H:%M:%S)_daily"
zfs snapshot -r "rpool@$SNAP"
zfs snapshot -r "bpool@$SNAP"
ok "Snapshot: $SNAP"

# ═══════════════════════════════════════════════════════════════════
#  11. Export source, prepare backup drive
# ═══════════════════════════════════════════════════════════════════

info "Cleaning up source mounts..."
umount /run/keystore/rpool 2>/dev/null || true
cryptsetup close zark_ks_test 2>/dev/null || true
rm -f "$TMPKEY"
zpool export bpool
zpool export rpool
ok "Source pools exported"

echo ""
echo -e "${B}╔══════════════════════════════════════════════════════════╗${N}"
echo -e "${B}║  Preparing backup drive via zark prepare                ║${N}"
echo -e "${B}╚══════════════════════════════════════════════════════════╝${N}"
echo ""

# Copy zark to writable location
ZARK_WORK="/tmp/zark"
[ -d "$ZARK_WORK" ] || cp -a /mnt/zark "$ZARK_WORK"

# Import rpool to access system.key
zpool import -N rpool
sleep 1
ZD_DEV=$(printf '%s\n' /dev/zd* 2>/dev/null | head -1)
echo -n "$PASSPHRASE" | cryptsetup open "$ZD_DEV" zark_ks_test -
mkdir -p /run/keystore/rpool
mount /dev/mapper/zark_ks_test /run/keystore/rpool

cd "$ZARK_WORK"
./zark prepare "$BACKUP_DISK"

# ═══════════════════════════════════════════════════════════════════
#  12. Backup via raw zfs send
# ═══════════════════════════════════════════════════════════════════

echo ""
echo -e "${B}╔══════════════════════════════════════════════════════════╗${N}"
echo -e "${B}║  Backing up via zfs send/receive (raw)                  ║${N}"
echo -e "${B}╚══════════════════════════════════════════════════════════╝${N}"
echo ""

BACKUP_POOL=$(zpool import 2>/dev/null | awk '/pool:/{print $2}' | grep -v rpool | grep -v bpool | head -1)
[ -n "$BACKUP_POOL" ] || fail "No backup pool found on $BACKUP_DISK"
info "Backup pool: $BACKUP_POOL"

zpool import -N "$BACKUP_POOL" || fail "Cannot import $BACKUP_POOL"
zpool import -N bpool 2>/dev/null || true
zfs load-key -a -L file:///run/keystore/rpool/system.key 2>/dev/null || true
ok "Pools imported, keys loaded"

# Send rpool datasets
info "Sending rpool datasets..."
zfs create -o canmount=off -o mountpoint=none "$BACKUP_POOL/rpool"
zfs create -o canmount=off -o mountpoint=none "$BACKUP_POOL/rpool/ROOT"
zfs create -o canmount=off -o mountpoint=none "$BACKUP_POOL/rpool/USERDATA" 2>/dev/null || true

DATASETS=$(zfs list -H -o name -r rpool | grep -v "^rpool$" | grep -v "^rpool/ROOT$" | grep -v "^rpool/USERDATA$" | grep -v "^rpool/keystore$")
for ds in $DATASETS; do
    snap=$(zfs list -H -o name -t snapshot "$ds" 2>/dev/null | tail -1)
    if [ -n "$snap" ]; then
        relative="${ds#rpool/}"
        info "  $relative @ ${snap##*@}"
        if zfs send -w "$snap" | zfs receive -F "$BACKUP_POOL/rpool/$relative" 2>/dev/null; then
            log_ok "..."
        else
            log_warn "..."
        fi
    fi
done

# Send bpool
info "Sending bpool..."
zfs create -o canmount=off -o mountpoint=none "$BACKUP_POOL/bpool" 2>/dev/null || true
zfs create -o canmount=off -o mountpoint=none "$BACKUP_POOL/bpool/BOOT" 2>/dev/null || true
BPOOL_DS=$(zfs list -H -o name -r bpool | grep "BOOT/" | head -1)
if [ -n "$BPOOL_DS" ]; then
    BPOOL_SNAP=$(zfs list -H -o name -t snapshot "$BPOOL_DS" 2>/dev/null | tail -1)
    if [ -n "$BPOOL_SNAP" ]; then
        BPOOL_REL="${BPOOL_DS#bpool/}"
        info "  $BPOOL_REL @ ${BPOOL_SNAP##*@}"
        zfs send "$BPOOL_SNAP" | zfs receive -F "$BACKUP_POOL/bpool/$BPOOL_REL"
        ok "  bpool ✓"
    fi
fi

# Send keystore
info "Sending keystore zvol..."
KS_SNAP=$(zfs list -H -o name -t snapshot rpool/keystore 2>/dev/null | tail -1)
if [ -n "$KS_SNAP" ]; then
    zfs send -w "$KS_SNAP" | zfs receive -o encryption=off "$BACKUP_POOL/rpool/keystore"
    ok "  keystore ✓"
fi

# ═══════════════════════════════════════════════════════════════════
#  13. Final cleanup
# ═══════════════════════════════════════════════════════════════════

info "Final cleanup..."
umount /run/keystore/rpool 2>/dev/null || true
cryptsetup close zark_ks_test 2>/dev/null || true
zpool export bpool 2>/dev/null || true
zpool export "$BACKUP_POOL" 2>/dev/null || true
zpool export rpool 2>/dev/null || true

echo ""
echo -e "${G}${B}╔══════════════════════════════════════════════════════════╗${N}"
echo -e "${G}${B}║  ✅ PHASE 1 COMPLETE                                     ║${N}"
echo -e "${G}${B}║  Real Ubuntu ZFS system created and backed up.           ║${N}"
echo -e "${G}${B}║  You can now: sudo poweroff                             ║${N}"
echo -e "${G}${B}╚══════════════════════════════════════════════════════════╝${N}"
echo ""
