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
The POSIX ``sh`` block every installed guard uses to read pools (M26, eli B1d).

On eli, with backup2's drive gone and its pool suspended, the login reminder
(``/etc/update-motd.d/99-zark-external-pool``) ran a bare ``zpool import``.
That scan reads every block device, the suspended pool's zvols included, and
such a read never returns: the process sits in state D, where no signal ends
it, and each ssh login waited on its reminder. The APT hook, the kernel prerm
guard and the GRUB guard ran the same commands.

So every guard reads pools through this block:

  * the pools' states first, from ``/proc/spl/kstat/zfs/<pool>/state``, a
    read that never waits on a pool (``lib.pool_state``); while any pool is
    not ONLINE or DEGRADED no ``zpool`` or ``zfs`` command is started at all;
  * the scan names its devices: the disks and partitions of
    ``/sys/class/block`` that udev already records as ZFS members
    (``E:ID_FS_TYPE=zfs_member`` in ``/run/udev/data/b<major>:<minor>``,
    blkid's result from when the device appeared or changed), never a zvol,
    a device-mapper or md device, a loop or a ram disk (zark's keystore
    mapping sits on a zvol). Deciding opens no device: a disk that sleeps is
    not woken at each login (M26R-40). Without udev's records (no
    ``/run/udev/data``: a chroot without /run) every disk and partition is
    named, as before. A ``ZPOOL_IMPORT_PATH`` already set (the GRUB scan
    file's) is kept as is;
  * a state other than ONLINE, DEGRADED or SUSPENDED (a pool being imported
    reads TRANSITIONING) is read a second time after a second before the
    pool counts as not healthy (M26R-45);
  * every ``zpool`` and ``zfs`` call runs in the background and is waited for
    a few seconds at most: one that has not ended is left behind, its output
    discarded, and the guard decides without it (the reminder says it could
    not check; the APT hook, the prerm and the GRUB guard refuse);
  * one call left behind at most: its pid and start time go to a file under
    ``/run``, and while that process still runs as ``zpool`` or ``zfs`` no
    guard starts another call and each answers as for a call that did not
    end (M26R-43). Only a file owned by the guard's own user is read, and
    only root can write in ``/run``.

The block is text inserted into each script, which stays standalone: the
installed files never call zark. Its path lines are what the tests point
elsewhere.
"""

KSTAT_LINE = "zark_kstat=/proc/spl/kstat/zfs"
BLOCK_LINE = "zark_block=/sys/class/block"
UDEV_LINE = "zark_udev=/run/udev/data"
CALL_LINE = "zark_callf=/run/zark-guard-call"

# Not an f-string: the shell's ${...} and $((...)) would collide with it.
POOL_READ_SH = (
    """\
# -- zark: read pools without waiting on one (see lib/guard_sh.py) --------
# A bare `zpool import` reads every block device, a suspended pool's zvols
# included, and such a read never returns (state D). So: the kstat states
# first (a read that never waits); no zpool or zfs command at all while a
# pool is not ONLINE or DEGRADED; the scan names disks and partitions only;
# every call waited for a few seconds at most, then left behind (one at
# most: no new call while an earlier one still runs).
__KSTAT_LINE__
__BLOCK_LINE__
__UDEV_LINE__
__CALL_LINE__
zark_unhealthy=""
for zark_s in "$zark_kstat"/*/state; do
    # The pattern itself when nothing matches; a state file that is there
    # but cannot be read counts as not healthy (the cat below fails).
    [ -e "$zark_s" ] || [ -L "$zark_s" ] || continue
    zark_n="${zark_s%/state}"
    # A scan's or an import's transient pool ($import-<hex>-<pool> on 2.4,
    # $import on 2.2) reads TRANSITIONING while it is being looked at.
    case "${zark_n##*/}" in '$'*) continue ;; esac
    zark_w="$(cat "$zark_s" 2>/dev/null)" || zark_w="UNREADABLE"
    case "$zark_w" in
        ONLINE|DEGRADED|SUSPENDED) ;;
        *)
            # A pool being imported reads TRANSITIONING for a moment: read
            # it again before calling it not healthy (M26R-45).
            sleep 1
            [ -e "$zark_s" ] || [ -L "$zark_s" ] || continue
            zark_w="$(cat "$zark_s" 2>/dev/null)" || zark_w="UNREADABLE"
            ;;
    esac
    case "$zark_w" in
        ONLINE|DEGRADED) ;;
        *) zark_unhealthy="$zark_unhealthy ${zark_n##*/}" ;;
    esac
done

# zark_proc <pid>: that process's state letter, start time (field 22 of its
# stat, counted after the last ")") and name, in zark_pst, zark_ptk and
# zark_pcm. Returns 1 when it is gone.
zark_proc() {
    zark_st="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
    zark_pcm="${zark_st#*(}"
    zark_pcm="${zark_pcm%)*}"
    zark_st="${zark_st##*) }"
    # $zark_st is split on purpose: one field each.
    set -- $zark_st
    zark_pst=$1
    zark_ptk=${20}
    return 0
}

# zark_bounded <seconds> <command...>: the command's stdout in zark_out.
# Returns 1, zark_out empty and the reason in zark_why, when it could not
# run or has not ended after <seconds>: it is left running, never waited
# for, and nothing of it reaches this script. The child keeps no descriptor
# of the guard's above 2 (debconf's 3 under a maintainer script). A call
# left behind is noted in $zark_callf, and while it still runs no other
# call is started (M26R-43).
zark_why=""
zark_bounded() {
    zark_t=$1
    shift
    zark_out=""
    if [ -O "$zark_callf" ] && read -r zark_cp zark_ck < "$zark_callf" 2>/dev/null; then
        if zark_proc "$zark_cp" && [ "$zark_pst" != Z ] && [ "$zark_ptk" = "$zark_ck" ] \\
            && { [ "$zark_pcm" = zpool ] || [ "$zark_pcm" = zfs ]; }; then
            zark_why="an earlier $zark_pcm call has not ended (pid $zark_cp)"
            return 1
        fi
        rm -f "$zark_callf"
    fi
    zark_d="$(mktemp -d 2>/dev/null)" || {
        zark_why="no temporary directory could be made (mktemp failed)"
        return 1
    }
    # The command itself is the background job, so $! is its own pid.
    "$@" > "$zark_d/out" 2>/dev/null < /dev/null 3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&- &
    zark_c=$!
    zark_i=0
    while zark_proc "$zark_c" && [ "$zark_pst" != Z ]; do
        if [ "$zark_i" -ge $((zark_t * 10)) ]; then
            ( umask 077 && printf '%s %s\\n' "$zark_c" "$zark_ptk" > "$zark_callf.$$" \\
                && mv -f "$zark_callf.$$" "$zark_callf" ) 2>/dev/null || :
            rm -rf "$zark_d"
            zark_why="a pool did not answer within $zark_t s"
            return 1
        fi
        sleep 0.1
        zark_i=$((zark_i + 1))
    done
    wait "$zark_c" 2>/dev/null || :
    zark_out="$(cat "$zark_d/out" 2>/dev/null)" || zark_out=""
    rm -rf "$zark_d"
    return 0
}

# zark_scan <seconds>: the pools available for import, in zark_avail.
# Returns 1 when they could not be read: a pool not healthy (no scan is
# started) or a scan that did not end in time.
zark_scan() {
    zark_avail=""
    [ -z "$zark_unhealthy" ] || return 1
    zark_devs=""
    if [ -z "${ZPOOL_IMPORT_PATH:-}" ]; then
        for zark_b in "$zark_block"/*; do
            [ -e "$zark_b" ] || continue
            case "${zark_b##*/}" in
                zd*|dm-*|md*|loop*|ram*|zram*|sr*|nbd*) continue ;;
            esac
            # Only what udev records as a ZFS member: no device is opened
            # to decide (a disk that sleeps stays asleep; M26R-40).
            if [ -d "$zark_udev" ]; then
                zark_mm="$(cat "$zark_b/dev" 2>/dev/null)" || continue
                grep -qx 'E:ID_FS_TYPE=zfs_member' "$zark_udev/b$zark_mm" 2>/dev/null || continue
            fi
            zark_devs="$zark_devs -d /dev/${zark_b##*/}"
        done
        [ -n "$zark_devs" ] || return 0
    fi
    # $zark_devs is split on purpose: one -d per device.
    zark_bounded "$1" zpool import $zark_devs || return 1
    zark_avail="$(printf '%s\\n' "$zark_out" | awk '/pool:/{print $2}' | sort -u)"
    return 0
}

# zark_held <seconds>: the imported backup pools (under an altroot, or with
# zark's metadata on the root), in zark_imp. A pool not healthy counts and
# is not read; one whose metadata could not be read in time counts too.
# Returns 1 when the list of pools could not be read in time.
zark_held() {
    zark_imp=""
    for zark_p in $zark_unhealthy; do
        case " rpool bpool " in *" $zark_p "*) continue ;; esac
        zark_imp="$zark_imp $zark_p"
    done
    [ -z "$zark_unhealthy" ] || return 0
    zark_bounded "$1" zpool list -H -o name,altroot || return 1
    zark_list="$zark_out"
    while read -r zark_name zark_alt; do
        [ -n "$zark_name" ] || continue
        case " rpool bpool " in *" $zark_name "*) continue ;; esac
        if [ "$zark_alt" != "-" ]; then zark_imp="$zark_imp $zark_name"; continue; fi
        if ! zark_bounded "$1" zfs get -H -o value org.zark:format "$zark_name"; then
            zark_imp="$zark_imp $zark_name"
            continue
        fi
        if [ -n "$zark_out" ] && [ "$zark_out" != "-" ]; then zark_imp="$zark_imp $zark_name"; fi
    done <<ZARK_LIST
$zark_list
ZARK_LIST
    return 0
}
# -- end of zark's pool read ---------------------------------------------
""".replace("__KSTAT_LINE__", KSTAT_LINE)
    .replace("__BLOCK_LINE__", BLOCK_LINE)
    .replace("__UDEV_LINE__", UDEV_LINE)
    .replace("__CALL_LINE__", CALL_LINE)
)

# What a script sourced into another shell (the GRUB scan file) unsets after
# the block, so nothing of it outlives the file.
UNSET_SH = (
    "unset zark_kstat zark_block zark_udev zark_callf zark_unhealthy zark_s zark_w zark_n "
    "zark_t zark_out zark_why zark_d zark_i zark_c zark_cp zark_ck zark_st zark_pst zark_ptk "
    "zark_pcm zark_avail zark_devs zark_b zark_mm zark_imp zark_p zark_list zark_name "
    "zark_alt\n"
    "unset -f zark_proc zark_bounded zark_scan zark_held\n"
)
