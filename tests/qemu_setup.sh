#!/bin/bash
#
# qemu_setup.sh — Prepare QEMU Ubuntu Live environment for zark testing.
#
# Run from the QEMU console after boot:
#   sudo mkdir -p /mnt/zark && sudo mount /dev/sr1 /mnt/zark && sudo bash /mnt/zark/tests/qemu_setup.sh
#
# This script:
#   1. Installs required packages (zfsutils, gdisk, cryptsetup, ssh)
#   2. Sets ubuntu:ubuntu password (no complexity restrictions)
#   3. Enables SSH
#   4. Mounts zark ISO at /mnt/zark
#   5. Prints SSH connection instructions
#

set -euo pipefail

G="\033[0;32m"
C="\033[0;36m"
Y="\033[0;33m"
B="\033[1m"
N="\033[0m"

info() { echo -e "${C}[SETUP]${N} $*"; }
ok() { echo -e "${G}[OK]${N}    $*"; }

echo ""
echo -e "${B}╔══════════════════════════════════════════════════════════╗${N}"
echo -e "${B}║  QEMU Environment Setup for zark testing                ║${N}"
echo -e "${B}╚══════════════════════════════════════════════════════════╝${N}"
echo ""

if [ "$(id -u)" -ne 0 ]; then
    echo "Usage: sudo bash $0"
    exit 1
fi

# ── 1. Install packages ─────────────────────────────────────────────
info "Installing packages (this may take a minute)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq 2>/dev/null
apt-get install -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
    zfsutils-linux gdisk cryptsetup openssh-server debootstrap >/dev/null 2>&1
ok "Packages installed"

# ── 2. Set password (bypass PAM quality checks entirely) ─────────────
info "Setting ubuntu:ubuntu password..."
usermod -p "$(openssl passwd -6 ubuntu)" ubuntu
ok "Password set (ubuntu:ubuntu)"

# ── 3. Enable SSH ────────────────────────────────────────────────────
info "Enabling SSH..."
# Allow password auth
sed -i 's/#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true
systemctl enable --now ssh >/dev/null 2>&1 || service ssh start 2>/dev/null || true
ok "SSH enabled on port 22 (host: 2222)"

# ── 3b. Install authorized_keys from etc/ if present ────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AUTH_KEYS="$SCRIPT_DIR/etc/authorized_keys"
if [ -f "$AUTH_KEYS" ]; then
    UBUNTU_HOME="/home/ubuntu"
    mkdir -p "$UBUNTU_HOME/.ssh"
    cp "$AUTH_KEYS" "$UBUNTU_HOME/.ssh/authorized_keys"
    chmod 700 "$UBUNTU_HOME/.ssh"
    chmod 600 "$UBUNTU_HOME/.ssh/authorized_keys"
    chown -R ubuntu:ubuntu "$UBUNTU_HOME/.ssh"
    ok "authorized_keys installed from etc/authorized_keys"
fi

# ── 4. Limit ZFS ARC to avoid OOM ────────────────────────────────────
modprobe zfs 2>/dev/null || true
if [ -f /sys/module/zfs/parameters/zfs_arc_max ]; then
    echo 536870912 >/sys/module/zfs/parameters/zfs_arc_max # 512MB
    mkdir -p /etc/modprobe.d
    echo "options zfs zfs_arc_max=536870912" >/etc/modprobe.d/zfs-arc.conf
    ok "ZFS ARC limited to 512MB (persistent)"
fi

# ── 5. Mount zark ISO ────────────────────────────────────────────────
ZARK_MNT="/mnt/zark"
if ! mountpoint -q "$ZARK_MNT" 2>/dev/null; then
    mkdir -p "$ZARK_MNT"
    # Find the zark ISO (sr0 or sr1)
    for dev in /dev/sr1 /dev/sr0; do
        if blkid "$dev" 2>/dev/null | grep -q "ZARK"; then
            mount "$dev" "$ZARK_MNT" 2>/dev/null && break
        fi
    done
    # Fallback: try sr1 directly
    mountpoint -q "$ZARK_MNT" 2>/dev/null || mount /dev/sr1 "$ZARK_MNT" 2>/dev/null || true
fi

if mountpoint -q "$ZARK_MNT" 2>/dev/null; then
    ok "zark mounted at $ZARK_MNT"
else
    echo -e "${Y}[WARN]${N}  Could not mount zark ISO — mount manually: sudo mount /dev/sr1 /mnt/zark"
fi

# ── Done ─────────────────────────────────────────────────────────────
# IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo -e "${G}${B}╔══════════════════════════════════════════════════════════╗${N}"
echo -e "${G}${B}║  ✅ READY — Connect via SSH from your host:              ║${N}"
echo -e "${G}${B}║                                                          ║${N}"
echo -e "${G}${B}║  ssh -o StrictHostKeyChecking=no -o                      ║${N}"
echo -e "${G}${B}║    UserKnownHostsFile=/dev/null -p 2222 ubuntu@localhost  ║${N}"
echo -e "${G}${B}║  password: ubuntu                                        ║${N}"
echo -e "${G}${B}╚══════════════════════════════════════════════════════════╝${N}"
echo ""

# ── 6. Offer to disable desktop (frees ~1.5GB RAM) ──────────────────
echo -ne "  ${Y}?${N} Disable desktop to free RAM? (recommended, this terminal will close) [Y/n]: "
read -r DISABLE_DESKTOP
if [ -z "$DISABLE_DESKTOP" ] || echo "$DISABLE_DESKTOP" | grep -qi "^y"; then
    info "Disabling desktop — connect via SSH to continue..."
    sleep 1
    systemctl isolate multi-user.target 2>/dev/null || true
fi
