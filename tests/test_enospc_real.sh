#!/bin/bash
# zark — Level B ENOSPC test (REAL ZFS, REAL kernel)
#
# This script provokes a real out-of-space condition during a `zfs send |
# zfs receive` on top of two loop-backed pools, captures the kernel's
# stderr, and verifies that lib/sh.py's is_enospc() helper would catch
# it. It exists to *empirically calibrate* the markers in
# lib.sh._ENOSPC_MARKERS against the actual ZFS version installed on
# this machine — not as a per-commit gate.
#
# WHEN TO RUN:
#   - After upgrading openzfs-related packages (zfsutils-linux).
#   - On a new distro / kernel / ZFS major version.
#   - When investigating a reported ENOSPC handling bug.
#
# This script is NOT invoked by `make fulltest` and is NOT part of CI.
# It is opt-in via `make test-enospc-real`.
#
# RISK PROFILE:
#   - Requires root (ZFS ioctls are privileged).
#   - Creates loop-backed pools on /tmp; disk image files and pool
#     names are uniquely tagged with our PID + a random suffix to make
#     collision with real pools (rpool, bpool, backup, …) impossible.
#   - Pools are created with `cachefile=none` so they never enter
#     /etc/zfs/zpool.cache and never auto-import on boot.
#   - A hard pre-flight check refuses to run if any leftovers from a
#     previous crashed run are still present.
#   - `trap EXIT` runs an idempotent cleanup that exports both pools
#     and frees both loops, even on Ctrl-C or unexpected error.
#
# WHAT IT DOES (AND DOES NOT) TEST:
#   - DOES test:   the real kernel ENOSPC stderr string against
#                  lib.sh.is_enospc() to confirm our mock-based unit
#                  tests align with reality.
#   - DOES NOT:    invoke any zark command, touch known_drives.json,
#                  read any zark config, or interact with the user's
#                  rpool/bpool in any way.
#
# Author: zark project — see CONTRIBUTING.md
# License: GPL-3.0 (matches the project)

set -u  # Treat unset variables as errors. We deliberately do NOT use
        # `set -e`: cleanup must run even if a command fails, and we
        # check return codes explicitly where it matters.

# ── Pre-flight: must run as root ─────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "  This script must run as root (ZFS ioctls are privileged)."
    echo "  Run with: sudo $0"
    exit 2
fi

# ── Pre-flight: required tools ───────────────────────────────────────────
for tool in zpool zfs losetup truncate python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "  Required tool not found: $tool"
        echo "  Install zfsutils-linux and util-linux, then retry."
        exit 2
    fi
done

# ── Identifiers ──────────────────────────────────────────────────────────
# Tag every artefact with PID + 4 random hex chars so two concurrent
# runs (or a crash followed by a fresh run) cannot stomp each other.
PID=$$
RAND=$(tr -dc 'a-f0-9' </dev/urandom | head -c 4)
TAG="${PID}_${RAND}"

SRC_POOL="zark_enospc_src_${TAG}"
DST_POOL="zark_enospc_dst_${TAG}"
SRC_IMG="/tmp/zark_enospc_${TAG}_src.img"
DST_IMG="/tmp/zark_enospc_${TAG}_dst.img"

# Image sizes: source large enough to hold a payload that overflows
# the destination by a wide margin. Both pools use cachefile=none.
SRC_IMG_SIZE_MB=512
DST_IMG_SIZE_MB=128

# Path to project root, so we can call into lib.sh.is_enospc() from a
# tiny Python one-liner to validate the captured stderr.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Pre-flight: refuse to run if leftovers from a previous run exist ─────
preflight_check_leftovers() {
    local leftover_pools leftover_loops
    leftover_pools=$(zpool list -H -o name 2>/dev/null | grep -E '^zark_enospc_' || true)
    if [ -n "$leftover_pools" ]; then
        echo "  Refusing to run: leftover pools from a previous run are still imported:"
        # Indent each line by four spaces. Use bash parameter expansion
        # rather than `sed` (shellcheck SC2001) — it's both faster and
        # avoids spawning an extra process.
        echo "    ${leftover_pools//$'\n'/$'\n    '}"
        echo ""
        echo "  Clean them up manually first:"
        echo "    sudo zpool destroy <name>"
        echo "  And free any /tmp/zark_enospc_*.img loops:"
        echo "    sudo losetup -l | grep zark_enospc"
        exit 2
    fi
    leftover_loops=$(losetup -l -O BACK-FILE 2>/dev/null | grep -E '/tmp/zark_enospc_' || true)
    if [ -n "$leftover_loops" ]; then
        echo "  Refusing to run: leftover loop devices still attached to /tmp/zark_enospc_*:"
        echo "    ${leftover_loops//$'\n'/$'\n    '}"
        echo ""
        echo "  Clean them up manually first:"
        echo "    sudo losetup -d <loop>"
        exit 2
    fi
}

# ── Cleanup (idempotent, runs on EXIT including Ctrl-C and errors) ───────
SRC_LOOP=""
DST_LOOP=""

# shellcheck disable=SC2317,SC2329
# Both SC2317 ("command appears to be unreachable") and SC2329
# ("function is never invoked") fire because shellcheck cannot
# statically prove this function is invoked — it is, via
# `trap cleanup EXIT INT TERM` below. Different shellcheck versions
# emit one rule or the other for the same indirection, so we silence
# both.
cleanup() {
    local rc=$?
    set +e  # cleanup must never abort midway

    echo ""
    echo "  Cleanup..."

    # Export both pools (force; they may have a busy receive in flight)
    if zpool list -H -o name 2>/dev/null | grep -qx "$DST_POOL"; then
        zpool export -f "$DST_POOL" >/dev/null 2>&1
    fi
    if zpool list -H -o name 2>/dev/null | grep -qx "$SRC_POOL"; then
        zpool export -f "$SRC_POOL" >/dev/null 2>&1
    fi

    # Detach loops
    if [ -n "$DST_LOOP" ] && losetup "$DST_LOOP" >/dev/null 2>&1; then
        losetup -d "$DST_LOOP" >/dev/null 2>&1
    fi
    if [ -n "$SRC_LOOP" ] && losetup "$SRC_LOOP" >/dev/null 2>&1; then
        losetup -d "$SRC_LOOP" >/dev/null 2>&1
    fi

    # Remove backing files
    rm -f "$SRC_IMG" "$DST_IMG"

    if [ "$rc" -eq 0 ]; then
        echo "  Cleanup done — host left untouched."
    else
        echo "  Cleanup done (script exited with rc=$rc)."
        echo "  Verify nothing leaked:"
        echo "    sudo zpool list | grep zark_enospc  # should be empty"
        echo "    sudo losetup -l | grep zark_enospc  # should be empty"
        echo "    ls /tmp/zark_enospc_*.img            # should be no such file"
    fi
}
trap cleanup EXIT INT TERM

# ── Main ─────────────────────────────────────────────────────────────────
echo "  zark — Level B real-ENOSPC test"
echo "  PID=$PID  RAND=$RAND"
echo "  Source pool : $SRC_POOL  (image: $SRC_IMG, ${SRC_IMG_SIZE_MB} MiB)"
echo "  Target pool : $DST_POOL  (image: $DST_IMG, ${DST_IMG_SIZE_MB} MiB)"
echo ""

preflight_check_leftovers

# ── Step 1: create backing images ────────────────────────────────────────
echo "  [1/6] Creating backing images..."
truncate -s "${SRC_IMG_SIZE_MB}M" "$SRC_IMG" || { echo "  truncate src failed"; exit 1; }
truncate -s "${DST_IMG_SIZE_MB}M" "$DST_IMG" || { echo "  truncate dst failed"; exit 1; }

SRC_LOOP=$(losetup -f --show "$SRC_IMG") || { echo "  losetup src failed"; exit 1; }
DST_LOOP=$(losetup -f --show "$DST_IMG") || { echo "  losetup dst failed"; exit 1; }
echo "    src loop: $SRC_LOOP"
echo "    dst loop: $DST_LOOP"

# ── Step 2: create both pools (cachefile=none for safety) ────────────────
echo "  [2/6] Creating pools..."
zpool create -f -o cachefile=none -O compression=off -O mountpoint=none \
    "$SRC_POOL" "$SRC_LOOP" \
    || { echo "  zpool create src failed"; exit 1; }
zpool create -f -o cachefile=none -O compression=off -O mountpoint=none \
    "$DST_POOL" "$DST_LOOP" \
    || { echo "  zpool create dst failed"; exit 1; }

# ── Step 3: write a payload to source that does NOT fit in the target ────
echo "  [3/6] Writing payload to source (overflows target)..."
zfs create -o "mountpoint=/var/tmp/zark_enospc_${TAG}_mnt" "$SRC_POOL/data" \
    || { echo "  zfs create src/data failed"; exit 1; }

# Write ~250 MiB. Source has ~512 MiB usable, target has ~128 MiB usable
# after pool overhead. The receive will ENOSPC well before completion.
PAYLOAD_MB=250
dd if=/dev/urandom of="/var/tmp/zark_enospc_${TAG}_mnt/big.bin" \
   bs=1M count=$PAYLOAD_MB status=none \
    || { echo "  dd failed (source may be too small)"; exit 1; }
echo "    wrote ${PAYLOAD_MB} MiB to $SRC_POOL/data"

# ── Step 4: snapshot ─────────────────────────────────────────────────────
echo "  [4/6] Snapshotting source..."
zfs snapshot "$SRC_POOL/data@s1" || { echo "  snapshot failed"; exit 1; }

# ── Step 5: send | receive — capture stderr ──────────────────────────────
echo "  [5/6] zfs send | zfs receive (expecting ENOSPC)..."
STDERR_FILE="/tmp/zark_enospc_${TAG}_stderr.log"
# Run send|receive with stderr captured. We don't care about stdout (it's
# raw zfs stream binary going through the pipe). We DO care about each
# side's stderr — capture both via the pipefail-aware idiom: redirect each
# end's stderr to the same file (append on second), so the unified buffer
# matches what lib/sh.py's run_pipe() would build for is_enospc().
{
    zfs send "$SRC_POOL/data@s1" 2>>"$STDERR_FILE" \
        | zfs receive -F "$DST_POOL/data" 2>>"$STDERR_FILE"
}
RC=${PIPESTATUS[1]}
RC_SEND=${PIPESTATUS[0]}

echo "    rc(zfs send) = $RC_SEND"
echo "    rc(zfs receive) = $RC"
echo "    captured stderr ($STDERR_FILE):"
sed 's/^/      | /' "$STDERR_FILE"

# Receive is expected to fail. If it succeeded, our payload sizing is off
# and the test isn't actually exercising ENOSPC.
if [ "$RC" -eq 0 ]; then
    echo "  FAIL: receive succeeded — payload did not overflow target."
    echo "        Increase PAYLOAD_MB or shrink DST_IMG_SIZE_MB."
    exit 1
fi

# ── Step 6: validate against lib.sh.is_enospc() ──────────────────────────
echo "  [6/6] Validating captured stderr matches lib.sh.is_enospc()..."
# Call into the project's is_enospc() with the captured text. This proves
# round-trip: real kernel stderr → our markers → True. If False, our
# marker list is stale relative to this kernel/zfs version and needs
# updating in lib/sh.py (_ENOSPC_MARKERS).
MATCH=$(
    cd "$PROJECT_ROOT" && \
    python3 -c "
import sys
sys.path.insert(0, '.')
from lib.sh import is_enospc
text = open('$STDERR_FILE').read()
print('YES' if is_enospc(text) else 'NO')
"
)

echo "    is_enospc(captured stderr) = $MATCH"

if [ "$MATCH" = "YES" ]; then
    echo ""
    echo "  PASS: kernel ENOSPC stderr is recognised by lib.sh.is_enospc()."
    echo "        The mock-based unit tests are calibrated correctly."
    exit 0
fi

echo ""
echo "  FAIL: lib.sh.is_enospc() did NOT recognise the captured stderr."
echo ""
echo "  This means the unit-test mocks may be out of date relative to the"
echo "  real ZFS / kernel version on this machine. Inspect the captured"
echo "  stderr above and add the missing marker (lower-cased) to:"
echo "      lib/sh.py    _ENOSPC_MARKERS"
echo "  Then re-run this script to confirm."
exit 1
