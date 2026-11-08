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
GRUB guard installer - single source of truth for /etc/grub.d/09_zfs_backup_guard.

The guard is a small shell script dropped into /etc/grub.d that runs as part
of `update-grub`. Its purpose is to abort the run when external ZFS pools are
visible to the system: Ubuntu's `10_linux_zfs` auto-imports every visible pool
and tries to mount their encrypted datasets, fails, and leaves grub.cfg with
no kernel entries - a hard-to-debug brick.

This module centralises both the script body and the install logic so all
three callers (`zark recover`, `zark repair-boot`, `zark finish`) deposit the same
file. Previously each command duplicated the script in a heredoc, with the
obvious risk that fixing a typo in one wouldn't reach the others.

Beside the guard it installs /etc/default/grub.d/zark-backup-guard.cfg, which
grub-mkconfig sources before it runs the grub.d scripts: it keeps pools that
are only importable (a backup drive plugged in, not imported) out of
10_linux_zfs's scan, so update-grub succeeds with the system's entries and the
guard is left to refuse an imported backup only (M18, option C).

And it installs /etc/grub.d/99_zark_cfg_check, which runs after every other
grub.d script and stops update-grub when the grub.cfg being written has no
kernel entry, so the previous grub.cfg stays (M18E-4).
"""

from pathlib import Path

from lib.grub_cfg import LINUX_LINE_ERE, OWN_SCRIPTS
from lib.log import Log

GUARD_RELATIVE_PATH = "etc/grub.d/09_zfs_backup_guard"

GUARD_SCRIPT = """\
#!/bin/sh
# 09_zfs_backup_guard - Prevent update-grub from running when external
# ZFS backup pools are connected.  10_linux_zfs auto-imports ALL visible
# pools and tries to mount their (encrypted) datasets.  When it fails,
# the bootlist parsing breaks and grub.cfg ends up with NO kernel entries.
#
# Installed by zark.  Safe to remove if you don't use zark.

set -e

# Pools that should be importable - only rpool and bpool
ALLOWED_POOLS="rpool bpool"

# List pools available for import (exported or on connected drives)
available="$(zpool import 2>/dev/null | awk '/pool:/{print $2}' | sort -u)"

# 10_linux_zfs also takes, from every imported pool, the filesystems whose
# mountpoint is the pool's altroot (or / without one) as root datasets: an
# imported backup (an altroot, or zark's metadata on its root) is external too.
imported="$(zpool list -H -o name,altroot 2>/dev/null | while read -r name altroot; do
    case " $ALLOWED_POOLS " in *" $name "*) continue ;; esac
    if [ "$altroot" != "-" ]; then echo "$name"; continue; fi
    fmt="$(zfs get -H -o value org.zark:format "$name" 2>/dev/null || true)"
    if [ -n "$fmt" ] && [ "$fmt" != "-" ]; then echo "$name"; fi
done || true)"

extra=""
for pool in $available $imported; do
    is_allowed=false
    for a in $ALLOWED_POOLS; do
        [ "$pool" = "$a" ] && is_allowed=true
    done
    $is_allowed || extra="$extra $pool"
done

if [ -n "$extra" ]; then
    echo "ERROR: External ZFS pool(s) detected:$extra" >&2
    echo "" >&2
    echo "  update-grub cannot run safely while external ZFS pools are visible." >&2
    echo "  The 10_linux_zfs script will try to mount their encrypted datasets," >&2
    echo "  fail, and generate a grub.cfg WITHOUT kernel entries." >&2
    echo "" >&2
    # Name what finishes the job: dpkg is left half-done when a maintainer
    # script runs update-grub (dpkg exports DPKG_MAINTSCRIPT_NAME to it).
    if [ -n "${DPKG_MAINTSCRIPT_NAME:-}" ]; then
        echo "  A package is being installed or removed: when$extra is exported" >&2
        echo '  (the backup ends, or `sudo zark umount`), run: sudo apt -f install' >&2
    else
        echo "  When$extra is exported (the backup ends, or" >&2
        echo '  `sudo zark umount`), then run: sudo update-grub' >&2
    fi
    echo "" >&2
    exit 1
fi
"""

CHECK_RELATIVE_PATH = "etc/grub.d/99_zark_cfg_check"

# Run by grub-mkconfig after every other grub.d script (the glob sorts 99_
# last), with its stdout the grub.cfg.new being written (exec > at
# grub-mkconfig.in:279 on noble, :280 on resolute and stonking); a non-zero
# exit ends grub-mkconfig under its set -e before grub.cfg is replaced
# (:325/:326). 10_linux_zfs's EXIT trap exits 0 after a failure (M18E-4), so
# the entries are counted here. Any kernel's line counts, not the running
# kernel's: a chroot from a live (repair-boot, zark chroot) runs the live's
# kernel; but only inside the sections of 10_linux_zfs and 10_linux, the same
# scope as grub_cfg.missing_entries (M18R-23). POSIX sh, nothing on stdout.
CHECK_SCRIPT = """\
#!/bin/sh
# 99_zark_cfg_check - Refuse a grub.cfg without any kernel entry.
#
# grub-mkconfig runs this last, its stdout being the grub.cfg.new it is
# writing. 10_linux_zfs can fail half way (a busy mount) and still exit 0,
# leaving no kernel entry, and such a grub.cfg boots nothing. When the
# sections of 10_linux_zfs and 10_linux hold no kernel line this exits 1:
# grub-mkconfig, under set -e, stops before it replaces grub.cfg, so the
# previous one stays. Entries of 30_os-prober or 40_custom boot other
# systems and do not count.
#
# Installed by zark.  Safe to remove if you don't use zark.

# Without /proc this shell cannot see its own stdout: say so and let
# grub-mkconfig go on, since a refusal would stop every run there.
if [ ! -d "/proc/$$" ]; then
    echo "zark: /proc is not mounted: 99_zark_cfg_check could not read the new grub.cfg" >&2
    exit 0
fi
# The file grub-mkconfig is writing, through this shell's own descriptor.
# Anything else (a terminal, a pipe, a closed stdout) has nothing to check.
new="/proc/$$/fd/1"
[ -f "$new" ] || exit 0
# grep -q, never a redirect of its stdout: dash applies a simple command's
# redirections in this shell, and /proc/$$/fd/1 would then be that target.
if sed -n __OWN_SECTIONS__ "$new" 2>/dev/null | grep -Eq -- '__LINUX_LINE_ERE__'; then
    exit 0
fi
{
    echo "ERROR: no kernel entry was written to the new grub.cfg."
    echo ""
    echo "  10_linux_zfs may have failed on a busy mount and still exited 0."
    echo "  grub.cfg is left as it was. Run again: sudo update-grub"
    echo "  After a kernel removal: sudo apt -f install (dpkg --configure -a"
    echo "  does not finish a pending removal)."
    echo "  From a live USB: sudo zark repair-boot"
    echo ""
} >&2
exit 1
""".replace("__LINUX_LINE_ERE__", LINUX_LINE_ERE).replace(
    "__OWN_SECTIONS__",
    # One sed range per script, BEGIN to END markers ("." escaped, "|" as the
    # delimiter since the paths hold "/").
    " ".join(
        "-e '\\|^### BEGIN {0} ###$|,\\|^### END {0} ###$|p'".format(path.replace(".", "\\."))
        for path in OWN_SCRIPTS
    ),
)

CFG_RELATIVE_PATH = "etc/default/grub.d/zark-backup-guard.cfg"

# A path no package creates: with it as zpool's only search directory the
# scan finds nothing (zpool_main.c splits the value on ':'; libzutil skips a
# missing directory on ENOENT). Never empty: an empty value is the full scan.
NO_IMPORT_PATH = "/nonexistent/zark-no-import"

# Sourced, not run: by grub-mkconfig under `set -e`, by grub-install's config
# loader (`sh -c ". cfg; printf ..."`, stdout parsed) and by grub's postinst
# config_item(). Hence no exit, nothing on stdout, every status handled, and
# `:` last so the `.` itself returns 0 (a false last test would end
# grub-mkconfig). The zark_ prefix keeps clear of grub-mkconfig's variables.
CFG_TEXT = """\
# zark-backup-guard.cfg - Keep external ZFS pools out of update-grub's scan.
#
# 10_linux_zfs imports every pool it can find and mounts their root
# datasets; a backup pool's locked dataset aborts it and grub.cfg loses its
# kernel entries. When no importable pool is named rpool or bpool, zpool's
# device search is pointed at a directory that does not exist: 10_linux_zfs
# and 09_zfs_backup_guard then find no importable pool, while imported ones
# (rpool, bpool, a backup zark holds) are listed as before.
#
# Acts only when grub-mkconfig sources it; grub-install and grub's postinst
# also source this directory, so it never exits and writes nothing to stdout.
#
# Installed by zark.  Safe to remove if you don't use zark.
if [ "${self:-}" = "grub-mkconfig" ]; then
    # The listing decides, whatever the status (M18R-19): a status-1 listing
    # that names a foreign rpool still keeps it in the menu.
    zark_pools="$(zpool import 2>/dev/null)" || :
    zark_pools="$(printf '%s\\n' "$zark_pools" |
        awk '$1 == "pool:" {print $2}' 2>/dev/null | sort -u 2>/dev/null)" || zark_pools=""
    zark_hidden=""
    zark_system=false
    for zark_p in $zark_pools; do
        case "$zark_p" in
            rpool|bpool) zark_system=true ;;
            *) zark_hidden="$zark_hidden $zark_p" ;;
        esac
    done
    if [ "$zark_system" = false ]; then
        ZPOOL_IMPORT_PATH=__NO_IMPORT_PATH__
        export ZPOOL_IMPORT_PATH
        if [ -n "$zark_hidden" ]; then
            echo "zark: left out of the boot menu scan:$zark_hidden" >&2
        fi
    fi
    unset zark_p zark_pools zark_hidden zark_system
fi
:
""".replace("__NO_IMPORT_PATH__", NO_IMPORT_PATH)


def _path(target_root: str, rel: str) -> Path:
    root = target_root.rstrip("/")
    return Path(f"{root}/{rel}") if root else Path(f"/{rel}")


def _holds(path: Path, text: str, *, executable: bool) -> bool:
    """Whether ``path`` holds ``text`` (and is executable when asked)."""
    try:
        return path.read_text(encoding="utf-8") == text and (
            not executable or bool(path.stat().st_mode & 0o111)
        )
    except (OSError, UnicodeDecodeError):
        return False


def install(target_root: str, log: Log, *, overwrite: bool = True) -> bool:
    """
    Write the GRUB guard script to {target_root}/etc/grub.d/09_zfs_backup_guard
    and the entry check to {target_root}/etc/grub.d/99_zark_cfg_check
    (executable) and the scan file to
    {target_root}/etc/default/grub.d/zark-backup-guard.cfg (0644, sourced).

    Args:
        target_root: filesystem root where the guard should be installed.
            Use "" or "/" for the running system; use a chroot/altroot path
            (e.g. "/mnt/recover") when installing into a recovered system.
        log: logger for the success message.
        overwrite: if False, leave an existing file untouched. Defaults to
            True because every caller in zark wants the canonical version.

    Returns True when it wrote any file, False when all were already
    there (current, or kept because ``overwrite`` is False; M5a-24).
    """
    wrote = False
    for rel, text, mode in (
        (GUARD_RELATIVE_PATH, GUARD_SCRIPT, 0o755),
        (CHECK_RELATIVE_PATH, CHECK_SCRIPT, 0o755),
        (CFG_RELATIVE_PATH, CFG_TEXT, 0o644),
    ):
        path = _path(target_root, rel)
        if path.exists() and not overwrite:
            continue
        if _holds(path, text, executable=mode == 0o755):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        wrote = True
    if wrote:
        log.ok(
            "Grub guard installed "
            + "(09_zfs_backup_guard, 99_zark_cfg_check, zark-backup-guard.cfg) ✓"
        )
    else:
        log.ok("Grub guard already current ✓")
    return wrote


def _zarks(path: Path, canonical: str, header: str) -> bool:
    """This zark's file, or an older zark's (its header and "Installed by zark")."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    return text == canonical or (header in text and "Installed by zark" in text)


def remove(target_root: str, log: Log) -> list[str]:
    """Remove the guard, the entry check and the scan file :func:`install` wrote under
    ``target_root``: this zark's, or an older zark's (its header and
    "Installed by zark"); return the paths left because they are not zark's
    (M3P.2)."""
    left: list[str] = []
    removed = 0
    for rel, text, header in (
        (GUARD_RELATIVE_PATH, GUARD_SCRIPT, "# 09_zfs_backup_guard -"),
        (CHECK_RELATIVE_PATH, CHECK_SCRIPT, "# 99_zark_cfg_check -"),
        (CFG_RELATIVE_PATH, CFG_TEXT, "# zark-backup-guard.cfg -"),
    ):
        path = _path(target_root, rel)
        if not path.exists():
            continue
        if _zarks(path, text, header):
            path.unlink()
            removed += 1
        else:
            left.append(str(path))
    log.ok(f"Grub guard removed ({removed} file(s))")
    return left


def is_current(target_root: str = "") -> bool:
    """True when the guard and the entry check under ``target_root`` hold
    this zark's scripts and are executable, and the scan file holds this
    zark's text (what :func:`install` would leave; M4c-10)."""
    return (
        _holds(_path(target_root, GUARD_RELATIVE_PATH), GUARD_SCRIPT, executable=True)
        and _holds(_path(target_root, CHECK_RELATIVE_PATH), CHECK_SCRIPT, executable=True)
        and _holds(_path(target_root, CFG_RELATIVE_PATH), CFG_TEXT, executable=False)
    )
