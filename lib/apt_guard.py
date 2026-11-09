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
APT guard installer - single source of truth for the apt/dpkg backup guard.

This module deposits four small, self-contained files onto a target system:

  * ``/usr/local/lib/zark/apt-zfs-backup-guard`` - a POSIX ``sh`` script wired
    in as a ``DPkg::Pre-Install-Pkgs`` hook. APT runs it *before* dpkg unpacks
    or removes anything; a non-zero exit aborts the whole transaction. It
    refuses the install of a boot-critical package (kernel / GRUB / shim /
    ZFS) while an external (non rpool/bpool) ZFS pool is connected, and its
    removal while one is imported (a zark backup, mount or full check
    running): the
    situations that half-apply a kernel update and leave an unbootable
    system or dpkg half-done.
  * ``/etc/apt/apt.conf.d/09zark-zfs-backup-guard`` - the APT config that
    registers the hook and asks for protocol version 3 (every action, the
    removals included, on stdin).
  * ``/etc/kernel/prerm.d/00-zark-backup-guard`` - the same removal rule at
    dpkg's level: every kernel package's prerm runs it before any file is
    deleted, so a ``dpkg -r`` by hand, or a backup imported after APT's hook
    ran, cannot leave a kernel half-removed.
  * ``/etc/update-motd.d/99-zark-external-pool`` - a login-time reminder of
    what the guards refuse while a backup pool is plugged in (boot-critical
    installs) or imported (those, kernel removals and update-grub).

Crucially, all four are **standalone**: they detect pools with ``zpool``
directly and never call ``zark``. The recovered system runs without zark
present, so a zark-dependent guard would be inert exactly where it matters.
This mirrors ``lib/grub_guard.py`` - the script body lives here as the single
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
# run-parts takes only ^[a-zA-Z0-9_-]+$ (no dot) and sorts the files of
# /etc/kernel/prerm.d and /usr/share/kernel/prerm.d in one C-locale list:
# "00-" runs before dkms (which removes the kernel's modules) and before
# linux-base's zz-legacy-boot (which can delete the image).
PRERM_RELATIVE_PATH = "etc/kernel/prerm.d/00-zark-backup-guard"

# Boot-critical package globs. This is the single source of truth: the shell
# `case` pattern below is generated from it, and the tests assert coverage
# against it. Order is irrelevant; `case` matches the first that fits.
SENSITIVE_GLOBS: tuple[str, ...] = (
    "linux-image-*",
    "linux-headers-*",
    "linux-modules-*",
    "linux-modules-extra-*",
    # 26.10 ships the ZFS and v4l2loopback modules apart (M18E-1).
    "linux-main-modules-*",
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
# NOTE: not an f-string - the script contains ${VAR:-} expansions whose braces
# would collide with str.format/f-string. The one dynamic part (the case
# pattern) is substituted via .replace() below.
_HOOK_TEMPLATE = """\
#!/bin/sh
# apt-zfs-backup-guard - Abort apt/dpkg kernel & boot package operations while
# a zark backup drive could break them.
#
# Why: a kernel / GRUB / shim / ZFS package installed while a backup drive is
# plugged in, or removed while a backup pool is imported, can half-apply: the
# kernel's maintainer scripts run update-grub, which the grub guard refuses
# then, and dpkg is left half-done with a grub.cfg that does not match /boot.
#
# Wired in as a DPkg::Pre-Install-Pkgs hook (protocol version 3), this runs
# BEFORE dpkg unpacks or removes anything; exiting non-zero aborts the whole
# transaction so nothing is touched. Boot-unrelated packages, and configures,
# are always allowed through.
#
# Installed by zark. Safe to remove if you don't use zark:
#   sudo rm __APT_CONF_ABS__ __HOOK_ABS__
set -e
set -f
PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH
export PATH

# zark's own recovery operations import the backup pool deliberately and set
# ZARK_INTERNAL=1 so this guard does not block them.
[ "${ZARK_INTERNAL:-}" = "1" ] && exit 0

# What APT sends. Version 2 or 3: "VERSION <n>", the configuration (one
# key=value per line) up to the first empty line, then one line per action
# whose first field is the package's name and whose action is the rest of
# the line after the fixed fields (8 in version 3, 4 in version 2): a .deb
# path (install or upgrade), **REMOVE** (removal or purge), **CONFIGURE** or
# **ERROR**. Version 1 (no VERSION line): one .deb path per line. A line of
# any other shape counts as a removal of its first field, so nothing slips
# through. Read first: ordinary transactions exit before the zpool scan.
installs=""
removals=""
first=true
in_config=false
v1=true
version=""
while IFS= read -r line || [ -n "$line" ]; do
    if $first; then
        first=false
        case "$line" in
            "VERSION "*) v1=false; in_config=true; version="${line#VERSION }"; continue ;;
        esac
    fi
    if $in_config; then
        [ -n "$line" ] || in_config=false
        continue
    fi
    [ -n "$line" ] || continue
    if $v1; then
        case "$line" in
            *.deb) ;;
            *) continue ;;
        esac
        name="${line##*/}"
        name="${name%%_*}"
        kind=install
    else
        # The action is the rest of the line after the fixed fields (8 in
        # version 3, 4 in version 2), so a .deb path may hold a space
        # (M18R-20); a line with fewer fields is malformed: a removal.
        case "$version" in
            2) fixed=4 ;;
            *) fixed=8 ;;
        esac
        name="${line%% *}"
        action="$line"
        i=0
        while [ "$i" -lt "$fixed" ]; do
            case "$action" in
                *" "*) action="${action#* }" ;;
                *) action=""; break ;;
            esac
            i=$((i + 1))
        done
        kind=remove
        case "$action" in
            "**CONFIGURE**"|"**ERROR**") continue ;;
            "**REMOVE**") kind=remove ;;
            /*) kind=install ;;
        esac
    fi
    case "$name" in
        __SENSITIVE_CASE__)
            if [ "$kind" = install ]; then
                installs="$installs $name"
            else
                removals="$removals $name"
            fi
            ;;
    esac
done

# Nothing boot-critical in this transaction: allow it, no zpool scan needed.
[ -n "$installs$removals" ] || exit 0

# External pools: available for import (plugged in), and imported as a
# backup (under an altroot, or zark's metadata on its root).
ALLOWED_POOLS="rpool bpool"
available="$(zpool import 2>/dev/null | awk '/pool:/{print $2}' | sort -u)"
imported="$(zpool list -H -o name,altroot 2>/dev/null | while read -r name altroot; do
    case " $ALLOWED_POOLS " in *" $name "*) continue ;; esac
    if [ "$altroot" != "-" ]; then echo "$name"; continue; fi
    fmt="$(zfs get -H -o value org.zark:format "$name" 2>/dev/null || true)"
    if [ -n "$fmt" ] && [ "$fmt" != "-" ]; then echo "$name"; fi
done || true)"
plugged=""
held=""
for pool in $available $imported; do
    case " $ALLOWED_POOLS " in *" $pool "*) continue ;; esac
    case " $plugged " in *" $pool "*) ;; *) plugged="$plugged $pool" ;; esac
done
for pool in $imported; do
    case " $held " in *" $pool "*) ;; *) held="$held $pool" ;; esac
done

# An install while a backup drive is plugged in or imported; a removal only
# while one is imported (with it only plugged in, the grub guard's scan file
# keeps update-grub safe, so the removal completes).
refused=false
if [ -n "$installs" ] && [ -n "$plugged" ]; then
    echo "zark: Refusing boot-critical package install(s):$installs:" >&2
    echo "  backup pool$plugged is plugged in. Unplug the drive (or let the" >&2
    echo "  backup end), then run apt again." >&2
    refused=true
fi
if [ -n "$removals" ] && [ -n "$held" ]; then
    echo "zark: Refusing boot-critical package removal(s):$removals:" >&2
    echo "  backup pool$held is imported (a zark backup, mount or full check is" >&2
    echo '  running). When it ends (or after `sudo zark umount`), run apt again.' >&2
    refused=true
fi
$refused || exit 0
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
# Version 3: with the default (1) APT passes only the .deb paths of installs,
# so a removal reached the hook as an empty stdin. The option's key is the
# hook's command up to its first space (apt-pkg/deb/dpkgpm.cc); the hook
# reads versions 2 and 1 too, in case a local file lowers it.
APT_CONF = (
    f'DPkg::Pre-Install-Pkgs {{ "{_HOOK_ABS}"; }};\n'
    f'DPkg::Tools::Options::{_HOOK_ABS}::Version "3";\n'
)
# The file zark 2.2 and earlier installed: remove() still recognises it.
_APT_CONF_V1 = f'DPkg::Pre-Install-Pkgs {{ "{_HOOK_ABS}"; }};\n'

# ── Kernel prerm.d backstop ──────────────────────────────────────────────
PRERM_SCRIPT = r"""#!/bin/sh
# 00-zark-backup-guard - Refuse a kernel removal while a zark backup pool is
# imported.
#
# Every kernel package's prerm runs /etc/kernel/prerm.d (run-parts, sorted)
# before dpkg deletes any file; this one sorts first. With a backup pool
# imported the kernel's postrm would run update-grub, which the grub guard
# refuses then, after the files are gone: dpkg half-done and a grub.cfg that
# lists a deleted kernel. A refusal here leaves the package as it was.
#
# Installed by zark. Safe to remove if you don't use zark.
set -e
PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH
export PATH

[ "${ZARK_INTERNAL:-}" = "1" ] && exit 0

ALLOWED_POOLS="rpool bpool"
held="$(zpool list -H -o name,altroot 2>/dev/null | while read -r name altroot; do
    case " $ALLOWED_POOLS " in *" $name "*) continue ;; esac
    if [ "$altroot" != "-" ]; then echo "$name"; continue; fi
    fmt="$(zfs get -H -o value org.zark:format "$name" 2>/dev/null || true)"
    if [ -n "$fmt" ] && [ "$fmt" != "-" ]; then echo "$name"; fi
done || true)"
[ -n "$held" ] || exit 0

pools=""
for pool in $held; do pools="$pools $pool"; done
echo "zark: not removing kernel ${1:-}: backup pool$pools is imported." >&2
echo '  When the backup, mount or full check ends (or after `sudo zark umount`),' >&2
echo '  run: sudo apt -f install' >&2
exit 1
"""

# ── MOTD reminder (login-time, secondary defence) ────────────────────────
MOTD_SCRIPT = """\
#!/bin/sh
# 99-zark-external-pool - Login-time reminder that an external ZFS pool is
# connected, which blocks kernel/GRUB/ZFS upgrades (see the zark apt guard).
#
# Installed by zark. Safe to remove if you don't use zark.
set -e
PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH
export PATH

ALLOWED_POOLS="rpool bpool"
available="$(zpool import 2>/dev/null | awk '/pool:/{print $2}' | sort -u)"
imported="$(zpool list -H -o name,altroot 2>/dev/null | while read -r name altroot; do
    case " $ALLOWED_POOLS " in *" $name "*) continue ;; esac
    if [ "$altroot" != "-" ]; then echo "$name"; continue; fi
    fmt="$(zfs get -H -o value org.zark:format "$name" 2>/dev/null || true)"
    if [ -n "$fmt" ] && [ "$fmt" != "-" ]; then echo "$name"; fi
done || true)"
# What the guards refuse depends on the state (M18): an install of a
# boot-critical package while a pool is plugged in or imported; a kernel's
# removal and update-grub only while one is imported (M20-7 7a).
held=""
for pool in $imported; do held="$held $pool"; done
plugged=""
for pool in $available; do
    case " $ALLOWED_POOLS $held " in *" $pool "*) continue ;; esac
    plugged="$plugged $pool"
done

[ -n "$plugged$held" ] || exit 0

if [ -n "$held" ]; then
    printf '\\n\\033[1;33mWARNING (zark):\\033[0m backup pool(s) imported:%s\\n' "$held"
    if [ -n "$plugged" ]; then printf '  Also plugged in:%s\\n' "$plugged"; fi
    printf '  Kernel, GRUB, shim and ZFS installs and kernel removals are refused, and\\n'
    printf '  update-grub stops, while a pool is imported. When the zark backup, mount or\\n'
    printf '  full check ends (or after sudo zark umount), run apt again.\\n\\n'
else
    printf '\\n\\033[1;33mWARNING (zark):\\033[0m backup pool(s) plugged in:%s\\n' "$plugged"
    printf '  Installs and upgrades of kernel, GRUB, shim and ZFS packages are refused\\n'
    printf '  while they are plugged in; unplug them before upgrading.\\n\\n'
fi
"""


def _write(path: Path, content: str, *, overwrite: bool) -> bool:
    """Write `content` to `path` (0755). Return True only if it changed.

    No-op (returns False) when the file already holds the canonical content
    with the executable bit set - so re-running ``install`` on an up-to-date
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
            return False  # already current - leave mtime untouched
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return True


def install(target_root: str, log: Log, *, overwrite: bool = True) -> None:
    """
    Install the apt guard, its APT config, the kernel prerm.d backstop and
    the MOTD reminder under `target_root` and make the scripts executable.
    /etc/kernel/prerm.d is created when missing (noble without dkms ships
    none; its kernel prerm runs the directory only when it exists).

    Idempotent: when the four files already hold the canonical content,
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
    prerm = Path(f"{base}{PRERM_RELATIVE_PATH}")

    changed = _write(hook, HOOK_SCRIPT, overwrite=overwrite)
    changed = _write(conf, APT_CONF, overwrite=overwrite) or changed
    changed = _write(prerm, PRERM_SCRIPT, overwrite=overwrite) or changed
    changed = _write(motd, MOTD_SCRIPT, overwrite=overwrite) or changed

    if changed:
        log.ok("APT backup guard installed (kernel/GRUB upgrades blocked with external pool) ✓")
    else:
        log.ok("APT backup guard already current ✓")


def _ours(path: Path, canonical: str, header: str) -> bool:
    """Whether ``path`` is one of zark's files: this zark's content, or an
    older zark's copy (its header line and the "Installed by zark" mark)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return text == canonical or (header in text and "Installed by zark" in text)


def _conf_lines(text: str) -> list[str]:
    """``text``'s lines as remove() compares them: stripped, no comments."""
    return [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith(("//", "#"))
    ]


def remove(target_root: str, log: Log) -> list[str]:
    """Remove the hook, its APT config, the kernel prerm.d backstop and the
    MOTD script that :func:`install` wrote under ``target_root``; return the
    paths left because they are not zark's (M3P.2). The APT config is
    recognised in this zark's form and in the one-line form of zark 2.2 and
    earlier.

    The APT config goes first, and the hook only once the config naming it
    is gone: APT runs each Pre-Install-Pkgs entry and fails the whole
    transaction when it cannot (apt 3.2.0 apt-pkg/deb/dpkgpm.cc:477-482,
    :520-521), so a config left pointing at a removed hook would break every
    later install.
    """
    root = target_root.rstrip("/")
    base = f"{root}/" if root else "/"
    hook = Path(f"{base}{HOOK_RELATIVE_PATH}")
    conf = Path(f"{base}{APT_CONF_RELATIVE_PATH}")
    motd = Path(f"{base}{MOTD_RELATIVE_PATH}")
    prerm = Path(f"{base}{PRERM_RELATIVE_PATH}")
    left: list[str] = []
    removed = 0
    if conf.exists():
        try:
            lines = _conf_lines(conf.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            lines = []
        if lines in (_conf_lines(APT_CONF), _conf_lines(_APT_CONF_V1)):
            conf.unlink()
            removed += 1
        else:
            left.append(str(conf))
    if hook.exists():
        if not conf.exists() and _ours(hook, HOOK_SCRIPT, "# apt-zfs-backup-guard -"):
            hook.unlink()
            removed += 1
            try:
                hook.parent.rmdir()  # only when nothing else is there
            except OSError:
                pass
        else:
            left.append(str(hook))
    for path, canonical, header in (
        (prerm, PRERM_SCRIPT, "# 00-zark-backup-guard -"),
        (motd, MOTD_SCRIPT, "# 99-zark-external-pool -"),
    ):
        if not path.exists():
            continue
        if _ours(path, canonical, header):
            path.unlink()
            removed += 1
        else:
            left.append(str(path))
    log.ok(f"APT backup guard removed ({removed} file(s))")
    return left


def is_current(target_root: str = "") -> bool:
    """True when the hook, its APT config, the prerm.d backstop and the MOTD
    script under
    ``target_root`` hold this zark's content and are executable (what
    :func:`install` with ``overwrite=True`` would leave; M4c-10)."""
    root = target_root.rstrip("/")
    base = f"{root}/" if root else "/"
    for rel, content in (
        (HOOK_RELATIVE_PATH, HOOK_SCRIPT),
        (APT_CONF_RELATIVE_PATH, APT_CONF),
        (PRERM_RELATIVE_PATH, PRERM_SCRIPT),
        (MOTD_RELATIVE_PATH, MOTD_SCRIPT),
    ):
        path = Path(f"{base}{rel}")
        try:
            if path.read_text(encoding="utf-8") != content or not path.stat().st_mode & 0o111:
                return False
        except (OSError, UnicodeDecodeError):
            return False
    return True
