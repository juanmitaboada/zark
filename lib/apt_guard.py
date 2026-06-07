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
APT guard installer — single source of truth for the apt/dpkg backup guard.

This module deposits three small, self-contained files onto a target system:

  * ``/usr/local/lib/zark/apt-zfs-backup-guard`` — a POSIX ``sh`` script wired
    in as a ``DPkg::Pre-Install-Pkgs`` hook. APT runs it *before* dpkg unpacks
    anything; a non-zero exit aborts the whole transaction. The script refuses
    boot-critical package operations (kernel / GRUB / shim / ZFS) while an
    external (non rpool/bpool) ZFS pool is connected — the exact situation that
    half-applies a kernel upgrade (new kernel unpacked, ``update-grub`` blocked
    by the grub guard, old kernel autoremoved) and leaves an unbootable system.
  * ``/etc/apt/apt.conf.d/09zark-zfs-backup-guard`` — the one-line APT config
    that registers the hook (Version 1 protocol: ``.deb`` paths on stdin).
  * ``/etc/update-motd.d/99-zark-external-pool`` — a login-time reminder that
    an external pool is attached and boot updates are therefore blocked.

Crucially, all three are **standalone**: they detect pools with ``zpool``
directly and never call ``zark``. The recovered system runs without zark
present, so a zark-dependent guard would be inert exactly where it matters.
This mirrors ``lib/grub_guard.py`` — the script body lives here as the single
source of truth, while ``recover`` / ``finish`` / ``setup`` deposit identical
copies via :func:`install`.

zark's own recovery flows import the backup pool on purpose; they export
``ZARK_INTERNAL=1`` so the hook does not block them.
"""

# pylint: disable=duplicate-code
# The external-pool detection block (ALLOWED_POOLS + the loop that filters
# rpool/bpool out of `zpool import`) is intentionally duplicated between
# apt_guard and grub_guard. Both emit *standalone* POSIX-sh scripts that are
# installed into the system and must run with zark absent, so they cannot
# share a runtime helper. Factoring the shell snippet into a shared Python
# constant would couple the two guards against their by-design autonomy for
# no runtime benefit, so the duplication is kept and documented here.

from pathlib import Path

from lib.log import Log

HOOK_RELATIVE_PATH = "usr/local/lib/zark/apt-zfs-backup-guard"
APT_CONF_RELATIVE_PATH = "etc/apt/apt.conf.d/09zark-zfs-backup-guard"
MOTD_RELATIVE_PATH = "etc/update-motd.d/99-zark-external-pool"

# Boot-critical package globs. This is the single source of truth: the shell
# `case` pattern below is generated from it, and the tests assert coverage
# against it. Order is irrelevant; `case` matches the first that fits.
SENSITIVE_GLOBS: tuple[str, ...] = (
    "linux-image-*",
    "linux-headers-*",
    "linux-modules-*",
    "linux-modules-extra-*",
    "grub-*",
    "grub2-*",
    "grub-efi-*",
    "shim-*",
    "shim-signed",
    "zfs-*",
    "zfsutils-*",
    "zfs-initramfs",
    "zfs-dkms",
)

# Absolute path the hook is installed at (used inside the apt.conf snippet and
# the hook's own self-removal hint). Kept as a constant so the apt.conf line
# and the script's help text cannot drift apart.
_HOOK_ABS = f"/{HOOK_RELATIVE_PATH}"
_APT_CONF_ABS = f"/{APT_CONF_RELATIVE_PATH}"

# The shell `case` alternation, e.g. "linux-image-*|grub-*|...". Built from
# SENSITIVE_GLOBS so the two never diverge.
_SENSITIVE_CASE = "|".join(SENSITIVE_GLOBS)

# ── Hook script ──────────────────────────────────────────────────────────
# NOTE: not an f-string — the script contains ${VAR:-} expansions whose braces
# would collide with str.format/f-string. The one dynamic part (the case
# pattern) is substituted via .replace() below.
_HOOK_TEMPLATE = """\
#!/bin/sh
# apt-zfs-backup-guard — Abort apt/dpkg kernel & boot package operations while
# an external (non rpool/bpool) ZFS pool is connected.
#
# Why: a kernel / GRUB / shim / ZFS upgrade run with a zark backup drive
# attached can half-apply — the new kernel is unpacked into /boot and the old
# one autoremoved, while the kernel postinst's update-grub is aborted by the
# grub guard (10_linux_zfs would otherwise brick grub.cfg). The result is a
# grub.cfg pointing at a kernel that no longer exists: an unbootable system.
#
# Wired in as a DPkg::Pre-Install-Pkgs hook, this runs BEFORE dpkg unpacks
# anything; exiting non-zero aborts the whole transaction so nothing is
# touched. Boot-unrelated package operations are always allowed through.
#
# Installed by zark. Safe to remove if you don't use zark:
#   sudo rm __APT_CONF_ABS__ __HOOK_ABS__
set -e
PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH
export PATH

# zark's own recovery operations import the backup pool deliberately and set
# ZARK_INTERNAL=1 so this guard does not block them.
[ "${ZARK_INTERNAL:-}" = "1" ] && exit 0

# Read the .deb paths APT sends on stdin (Version 1 protocol) and collect any
# boot-critical packages. Doing this FIRST means ordinary installs exit before
# paying for the zpool device scan below.
sensitive=""
while IFS= read -r deb; do
    [ -n "$deb" ] || continue
    case "$deb" in
        *.deb) ;;
        *) continue ;;
    esac
    pkg="$(dpkg-deb -f "$deb" Package 2>/dev/null || true)"
    [ -n "$pkg" ] || pkg="$(basename "$deb" | cut -d_ -f1)"
    case "$pkg" in
        __SENSITIVE_CASE__)
            sensitive="$sensitive $pkg"
            ;;
    esac
done

# Nothing boot-critical in this transaction → allow it, no zpool scan needed.
[ -n "$sensitive" ] || exit 0

# Boot-critical packages ARE present. Now check for an external pool: any pool
# available for import that is not rpool/bpool.
ALLOWED_POOLS="rpool bpool"
available="$(zpool import 2>/dev/null | awk '/pool:/{print $2}' | sort -u)"
extra=""
for pool in $available; do
    is_allowed=false
    for a in $ALLOWED_POOLS; do
        [ "$pool" = "$a" ] && is_allowed=true
    done
    $is_allowed || extra="$extra $pool"
done

# No external pool → the upgrade is safe, allow it.
[ -n "$extra" ] || exit 0

# External pool + boot-critical package(s): refuse the transaction.
echo "ERROR: zark apt guard — external ZFS pool(s) detected:$extra" >&2
echo "" >&2
echo "  Refusing boot-critical package operation(s):$sensitive" >&2
echo "" >&2
echo "  Upgrading the kernel / GRUB / shim / ZFS while an external ZFS backup" >&2
echo "  pool is connected risks a half-applied boot update (new kernel unpacked" >&2
echo "  but update-grub blocked), leaving an unbootable system." >&2
echo "" >&2
echo "  Fix: disconnect or export the external pool(s), then run apt again:" >&2
echo "      sudo zpool export$extra" >&2
echo "" >&2
echo "  (Installed by zark. Bypass once with ZARK_INTERNAL=1, or remove" >&2
echo "  __APT_CONF_ABS__ to disable.)" >&2
exit 1
"""

HOOK_SCRIPT = (
    _HOOK_TEMPLATE.replace("__SENSITIVE_CASE__", _SENSITIVE_CASE)
    .replace("__APT_CONF_ABS__", _APT_CONF_ABS)
    .replace("__HOOK_ABS__", _HOOK_ABS)
)

# ── APT config snippet ───────────────────────────────────────────────────
# Version 1 (the default): APT passes the .deb file paths on the hook's stdin.
# We deliberately do NOT request Version 2/3 — the file list is all the hook
# needs, and Version 1 keeps the parsing trivial.
APT_CONF = f'DPkg::Pre-Install-Pkgs {{ "{_HOOK_ABS}"; }};\n'

# ── MOTD reminder (login-time, secondary defence) ────────────────────────
MOTD_SCRIPT = """\
#!/bin/sh
# 99-zark-external-pool — Login-time reminder that an external ZFS pool is
# connected, which blocks kernel/GRUB/ZFS upgrades (see the zark apt guard).
#
# Installed by zark. Safe to remove if you don't use zark.
set -e
PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH
export PATH

ALLOWED_POOLS="rpool bpool"
available="$(zpool import 2>/dev/null | awk '/pool:/{print $2}' | sort -u)"
extra=""
for pool in $available; do
    is_allowed=false
    for a in $ALLOWED_POOLS; do
        [ "$pool" = "$a" ] && is_allowed=true
    done
    $is_allowed || extra="$extra $pool"
done

[ -n "$extra" ] || exit 0

printf '\\n\\033[1;33mWARNING (zark):\\033[0m external ZFS pool(s) connected:%s\\n' "$extra"
printf '  Kernel / GRUB / ZFS upgrades are blocked while these are attached.\\n'
printf '  Disconnect or export them before upgrading:  sudo zpool export%s\\n\\n' "$extra"
"""


def _write(path: Path, content: str, *, overwrite: bool) -> bool:
    """Write `content` to `path` (0755). Return True only if it changed.

    No-op (returns False) when the file already holds the canonical content
    with the executable bit set — so re-running ``install`` on an up-to-date
    system touches nothing. Also a no-op when the file exists and `overwrite`
    is False, regardless of content.
    """
    if path.exists():
        if not overwrite:
            return False
        try:
            current: str | None = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
        if current == content and (path.stat().st_mode & 0o111):
            return False  # already current — leave mtime untouched
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return True


def install(target_root: str, log: Log, *, overwrite: bool = True) -> None:
    """
    Install the apt guard, its APT config and the MOTD reminder under
    `target_root` and make the scripts executable.

    Idempotent: when the three files already hold the canonical content,
    nothing is written (no mtime churn) and the log says so. This keeps
    ``zark setup`` a true no-op on re-run.

    Args:
        target_root: filesystem root to install into. Use "" or "/" for the
            running system; use a chroot/altroot path (e.g. "/mnt/recover")
            when installing into a recovered system.
        log: logger for the success message.
        overwrite: if False, leave any already-present file untouched. Defaults
            to True because every caller wants the canonical version; `finish`
            passes False to avoid clobbering an operator's edits on first boot.
    """
    root = target_root.rstrip("/")
    base = f"{root}/" if root else "/"

    hook = Path(f"{base}{HOOK_RELATIVE_PATH}")
    conf = Path(f"{base}{APT_CONF_RELATIVE_PATH}")
    motd = Path(f"{base}{MOTD_RELATIVE_PATH}")

    changed = _write(hook, HOOK_SCRIPT, overwrite=overwrite)
    changed = _write(conf, APT_CONF, overwrite=overwrite) or changed
    changed = _write(motd, MOTD_SCRIPT, overwrite=overwrite) or changed

    if changed:
        log.ok("APT backup guard installed (kernel/GRUB upgrades blocked with external pool) ✓")
    else:
        log.ok("APT backup guard already current ✓")
