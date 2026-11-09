# zark usage

The commands, a first backup and a recovery step by step, what zark needs, and its drive registry.

## Commands

`zark --help` lists them in the same groups and order.

### Backup

| Command    | Description |
|------------|-------------|
| `backup`   | Incremental encrypted backup: one restore point per run |
| `exclude`  | Leave datasets out of backups, of sanoid's snapshots, or both ([example](#example-a-downloads-folder-out-of-snapshots-and-backups)) |
| `prepare`  | Initialize a new blank drive as a backup target |
| `prune`    | Reclaim space in the source or on a backup drive (interactive) |
| `purge`    | Securely wipe a managed backup drive |
| `registry` | List, forget or fix drives; set each drive's retention |

### Recovery (live USB)

| Command                | Description |
|------------------------|-------------|
| `chroot`               | Open an interactive chroot into the installed system (live USB) |
| `finish`               | Post-recovery finalization (run from the recovered system) |
| `fix-rpool-mountpoint` | Give a restored rpool back `mountpoint=/` (live USB) |
| `recover`              | Full bare-metal system recovery from backup |
| `repair-boot`          | Fix boot issues from a live USB without full recovery |
| `simulate`             | Boot the target disk in QEMU/KVM to verify the boot chain |

### Inspection

| Command   | Description |
|-----------|-------------|
| `explore` | Scan for ZFS pools, show known/unknown drives |
| `fullcheck` | Scrub a backup drive and read a point back, changing no data |
| `health`  | Non-destructive risk checks on a backup drive |
| `monitor` | Live view of a running command and the backups' state (no root) |
| `mount`   | Mount a backup pool - or the local system (`mount local`) - for inspection/chroot |
| `status`  | Source pools' fill level and each drive's backup age |
| `umount`  | Unmount a backup pool, or the local system (`umount local`) |

### Setup and upkeep

| Command    | Description |
|------------|-------------|
| `alerts`   | Install or remove the status MOTD and hourly timer |
| `clean`    | Release what an interrupted zark run left mounted or imported |
| `selftest` | Check that this platform's ZFS and tools behave as zark relies on |
| `setup`    | Install dependencies, configure sanoid for automatic snapshots |

bash completes the commands, their options and arguments: installed with the package, or `source completion/zark` from a portable tree.

## Quick start

### First-time setup

Once per machine - installs sanoid for automatic snapshots and registers your backup drive:

```bash
sudo ./zark setup     # install sanoid + zfs tooling, configure snapshots
sudo ./zark prepare   # initialize a blank drive as a backup target
```

On Ubuntu 26.10, where `/boot` is on ext4 and there is no bpool, `setup` asks to create `rpool/ROOT/<be>/zark-boot`: a copy of `/boot` inside rpool that every backup refreshes before it takes its point, so each restore point holds the root and its `/boot` together. `backup` and `prepare` refuse until it exists.

`prepare` asks its questions, creates the backup pool, sends the first backup point, reads the pool back and only then registers the drive's GUID in `etc/known_drives.json`. After this, `zark backup` finds the drive automatically every time you connect it.

### Back up your system

```bash
# Connect your backup drive, then:
sudo ./zark backup
```

zark detects the backup drive by GUID, takes one backup point `zark_<UTC>` of every dataset at once, and brings each dataset on the drive to it with an incremental raw send. Each backup adds a restore point; after a complete backup, the drive's retention removes what falls outside it (every snapshot of its window, 30 days by default, stays, and before that the newest point of each month; see [Retention on the drive](RETENTION.md#retention-on-the-drive)). A typical incremental backup takes seconds.

To watch it, run `zark monitor` in another terminal, as any user: it shows the running command's step, the bytes sent, the rate and the time left, says when zark is waiting for an answer, and below that the pools, the drives and their last backups. It only reads zark's status files (`docs/STATUS-FILES.md`) and lists the pools with `zfs list` and `zpool list`; it never takes the lock or imports a pool. `zark monitor --once` prints one screen, and `--once --state` only the state word, for scripts.

**Updates with a backup drive connected.** zark's guards keep the boot consistent ([The apt guard](DESIGN.md#the-apt-guard)). Unplug the drive before installing or upgrading a kernel, GRUB, shim or ZFS: apt refuses those while a backup drive is plugged in, before it changes anything. Removing an old kernel (`apt autoremove`, or unattended-upgrades) works with the drive plugged in: the drive is left out of `update-grub`'s scan. While a backup, a `zark mount` or a `zark fullcheck` runs, the backup pool is imported: apt then refuses kernel removals too (run apt again once the backup ends, or after `sudo zark umount`); removing a 26.10 kernel set by hand with `dpkg -r` removes its `linux-main-modules-*` packages before the image's guard refuses, so use apt, which refuses the whole set; and `update-grub` is refused for any other package that runs it, which dpkg leaves half-configured until `sudo apt -f install` is run after that. A system left half-done by zark 2.2 or earlier is finished the same way; `finish` names its packages. zark does not import a backup pool while apt, dpkg or unattended-upgrades runs: `backup`, `mount`, `prune`, `purge`, `fullcheck` and `prepare` stop with exit status 75, naming the process, and you run them again once it has ended. An `apt-get` or `dpkg` started during the seconds of zark's import fails at its lock, `apt` waits for it, and unattended-upgrades' daily run is skipped to the next day. The read-back at the end of a backup or a prepare waits for apt to end, up to 15 minutes; on a terminal you can skip it (`y`). A read-back that was skipped or timed out is reported as not done: BACKUP NOT VERIFIED, exit status 1, no "safe to unplug", and the next backup reads the drive back. A `zark fullcheck` keeps the drive imported for hours: during its scrub, kernel installs and removals are refused (unattended-upgrades tries again the next day), `update-grub` is refused, and other zark commands stop with exit status 75; apt itself is not blocked.

### Check a backup drive

```bash
sudo ./zark fullcheck            # both parts; or: fullcheck restore, fullcheck scrub
```

A full check verifies the drive's copies without changing them, in two parts. The restore check imports the drive read-only, asks for a backup point (the newest complete one by default) and the drive's passphrase, loads every key, mounts each dataset `recover` would restore from that point's snapshot, read-only, and reads every file in it; a failed read is named with its path. The scrub then imports the drive read-write and has ZFS read every block of every point and verify its checksum, without the key; it writes only its own progress, the pool's labels and history, and rewrites a damaged metadata copy from a good one, never a file, a snapshot or a property. The passphrase is asked in the first minutes; the scrub then runs unattended for hours on a large drive (roughly 4-5 h for 1.8 TB on a 2.5" disk behind USB 3, 1.2-1.5 h on a SATA SSD; the estimate shown comes from the drive's last measured rate when there is one). Ctrl-C pauses the scrub and the next `zark fullcheck` resumes it. When the scrub finds damage, the passphrase is asked once more: ZFS names a damaged file of an encrypted dataset only while its key is loaded, so the keys are loaded from the keystore, opened read-only, for that one read and unloaded before the drive is exported. The scrub's result and the damage are recorded before that question, so a Ctrl-C there leaves them recorded, with the check not finished. That passphrase is waited for 10 minutes at most (`NAME_PASSPHRASE_TIMEOUT`); then the files stay unnamed and the drive is exported, so an unattended check does not keep it imported. The drive must not be imported when the check starts (`sudo zark umount` first); on a live session only the restore check runs.

The result is a banner (FULL CHECK PASSED, FOUND DAMAGE or NOT FINISHED) and the drive's `last_check` in `known_drives.json`, which `zark status`, `zark monitor` and `status.json` show (the MOTD too, unless it is clean); exit status 0 only when every part asked for passed.

### Recover from scratch

Boot from an Ubuntu live USB with the backup drive connected:

```bash
sudo ./zark recover
```

zark will:
1. Detect the internal disk and backup drive
2. Partition the internal disk (GPT + EFI + bpool + rpool)
3. Create the ZFS pools with encryption enabled
4. Restore all datasets from the most recent snapshot
5. Restore the boot pool, keystore, and EFI binaries
6. Install the GRUB guard and regenerate initrd
7. Display post-recovery instructions

Total recovery time: approximately one minute, plus the data transfer time itself.

### After first boot

Once the recovered system boots successfully:

```bash
sudo ./zark finish    # regenerate grub.cfg, finalize Secure Boot chain
```

`finish` is idempotent and safe to re-run. It never exports or re-imports a pool. It rebuilds the initrd of every installed kernel, then runs `update-grub`, so you don't need to invoke it separately, and it checks that `grub.cfg` has an entry for each kernel, that `/boot/efi` is the ESP of rpool's disk and holds the signed shim and GRUB, and that every `canmount=on` dataset is mounted. Its banner names anything that failed, and names what `setup` would still do, if anything.

### Fix a broken boot by hand (chroot)

When the pools are intact but the boot chain is broken - and you want to run a few commands inside the real system rather than a full `recover` - drop into a chroot from a live USB:

```bash
sudo ./zark chroot          # imports rpool/bpool, unlocks, mounts, chroots in
# inside the chroot, work as if booted:
update-grub
dpkg-reconfigure grub-efi-amd64-signed
exit                        # zark unmounts and exports both pools cleanly
```

For a quick look at the installed disk without a working shell, `sudo ./zark mount local` mounts it read-only (and `sudo ./zark umount local` releases it). Read-only imports both pools `readonly=on`: the live session writes nothing to them, not even their labels, and a dataset whose mountpoint directory is missing is listed as not mounted. `repair-boot` remains the one-shot, non-interactive option for the common grub-regeneration case.

### Test the recovered boot without rebooting

```bash
sudo ./zark simulate                          # boot the internal disk in QEMU (read-only by default)
sudo ./zark simulate --display 1920x1080      # override the default 2560×1440 resolution
```

Useful as a coherence check after `recover` (or any boot-chain change) without committing to a real reboot. By default, QEMU is started with `-snapshot` so any writes are discarded at shutdown and the underlying disk is never modified. Pass `--rw` (with explicit confirmation) if you actually want changes to persist.

## Example: a downloads folder out of snapshots and backups

Downloads, caches, build trees and VM images churn. On rpool, sanoid's snapshots keep every file deleted there until they expire, and every backup copies them to the drives. `zark exclude add <dataset> both` leaves a dataset out of both, but not a directory: the directory has to become a dataset of its own first, by hand, since zark does not move data on a running system. Once that is done, whatever is deleted there is freed at once and never reaches a drive. This example does it for `~/Downloads`; the same steps fit any folder of the home.

**The layout it assumes.** Ubuntu 26.04's installer makes one dataset for all of `/home`:

```
rpool/USERDATA              mountpoint none
rpool/USERDATA/home_<id>    mountpoint /home
rpool/USERDATA/root_<id>    mountpoint /root
```

`<id>` is the suffix the installer gave it, and `<user>` below is your login. Check yours with the first block; on another layout (a dataset per user) the names change accordingly. Two datasets are created:

- `rpool/USERDATA/home_<id>/<user>` with `canmount=off`: never mounted, so it does not cover your home (which stays a directory of `home_<id>`); it only gives the next one a parent.
- `rpool/USERDATA/home_<id>/<user>/downloads`, mounted at `/home/<user>/.downloads`, encrypted with rpool's key (nothing more to unlock). `~/Downloads` then becomes a symbolic link to `.downloads`. A mount under the home whose path has no hidden component is shown by GNOME Files and the top bar as a drive with an eject button; under `.downloads` it is not, and Files shows `Downloads` as a folder.

Mount properties are given when the dataset is created and never changed afterwards with `zfs set`.

Each block below starts and ends with `date`, stops before changing anything when a check fails, and ends with `rc=0` when it went through. Replace `<user>` and `<id>` first.

**1. Look first (read-only).** Close the browser and anything that writes to `~/Downloads`:

```bash
date
zfs list -H -o name,mountpoint,encryptionroot -r rpool/USERDATA
systemctl is-active zfs-zed
du -sh /home/<user>/Downloads
zfs list -H -o name,avail rpool
sudo lsof +D /home/<user>/Downloads
date
```

rpool needs at least the folder's size free, since the copy doubles it for a while. `zfs-zed` must be `active`: zed adds new datasets to `/etc/zfs/zfs-list.cache/rpool`, which the boot mounts from. `lsof` printing nothing means nothing is open there.

**2. Create the datasets, copy and compare.** Nothing is deleted:

```bash
date
( set -eo pipefail
  U=<user>; H=/home/$U; P=rpool/USERDATA/home_<id>
  [ "$(zfs get -H -o value mountpoint "$P")" = /home ] || { echo "$P is not /home: nothing done"; exit 1; }
  [ -d "$H/Downloads" ] && [ ! -L "$H/Downloads" ] && [ ! -e "$H/.downloads" ] || { echo "Downloads is not a folder, or .downloads exists: nothing done"; exit 1; }
  ! zfs list -H "$P/$U" >/dev/null 2>&1 || { echo "$P/$U exists: nothing done"; exit 1; }
  sudo zfs create -o canmount=off "$P/$U"
  sudo zfs create -o mountpoint="$H/.downloads" "$P/$U/downloads"
  zfs get -H -o name,property,value mountpoint,canmount,mounted,encryptionroot "$P/$U" "$P/$U/downloads"
  sudo chown --reference="$H/Downloads" "$H/.downloads"
  sudo chmod --reference="$H/Downloads" "$H/.downloads"
  sudo cp -a --preserve=all "$H/Downloads/." "$H/.downloads/"
  sudo diff -rq --no-dereference "$H/Downloads" "$H/.downloads" || { echo "COPY DIFFERS: stopped, Downloads kept"; exit 1; }
  echo "copy identical"
  grep -n "^$P/$U/downloads" /etc/zfs/zfs-list.cache/rpool || echo "not in zfs-list.cache yet"
)
echo "rc=$?"
date
```

Expected: `<user>` with `canmount off` and `mounted no`; `downloads` with `/home/<user>/.downloads`, `canmount on` and `mounted yes`; both with `encryptionroot rpool`; `copy identical`; the new dataset in `zfs-list.cache` (zed may take a second: run the `grep` again before worrying). Do not reboot while it is missing from the cache: the boot would not mount it. The `diff` stops the block by itself; written as `diff ... && echo` it would not, since bash ignores `set -e` inside an `&&` list.

**3. Exclude it from backups and snapshots:**

```bash
sudo zark exclude add rpool/USERDATA/home_<id>/<user>/downloads both
sudo zark exclude list
```

`both` sets `org.zark:replicate=no` (left out of every backup) and `org.zark:snapshot=no` (sanoid takes no more snapshots of it and removes the ones it took). zark asks twice, default no: for the exclusion, and for rewriting `/etc/sanoid/sanoid.conf`. sanoid does not honour a child's `autosnap = no` under a recursive rule, so the recursive rule on `rpool/USERDATA` becomes one rule per level: `rpool/USERDATA`, `home_<id>` and `<user>` keep `production` without recursion, `root_<id>` keeps `production (recursive)`, and `downloads` takes no snapshots. Your home and `/root` are snapshotted as before. zark 2.2.0 may also warn that a registered drive has no zark 2 anchor and should be backed up first; for a new dataset, which has no snapshot to drain and is on no drive, it does not apply.

**4. Replace the folder by the link,** once you have looked at the files. The copies are compared again first; a file downloaded since step 2 makes them differ, and then nothing is removed:

```bash
date
( set -eo pipefail
  U=<user>; H=/home/$U; P=rpool/USERDATA/home_<id>
  [ "$(findmnt -n -o SOURCE "$H/.downloads")" = "$P/$U/downloads" ] || { echo ".downloads is not the new dataset: nothing removed"; exit 1; }
  sudo diff -rq --no-dereference "$H/Downloads" "$H/.downloads" || { echo "copies differ: nothing removed"; exit 1; }
  sudo rm -rf -- "$H/Downloads"
  ln -s .downloads "$H/Downloads"
  ls -ld "$H/Downloads"
)
echo "rc=$?"
date
```

**5. After the next reboot,** `findmnt /home/<user>/.downloads` shows `rpool/USERDATA/home_<id>/<user>/downloads`, and `~/Downloads` still points to it.

**What to expect afterwards:**

- The old folder's space comes back only as the snapshots of `home_<id>` that hold it expire; what you download from now on is freed when you delete it.
- The next backup sends `rpool/USERDATA/home_<id>/<user>` (empty, a few KiB) as new and leaves `downloads` out.
- A dataset you create later under `home_<id>` is no longer under a recursive sanoid rule: `zark backup` and `zark status` say when sanoid.conf is not what `setup` would write, and `sudo zark setup` brings it in.
- If you destroy the dataset one day, its exclusion goes with it (it is a property of the dataset); run `sudo zark setup` afterwards to rewrite sanoid.conf, since zark warns about it only while another dataset stays excluded from snapshots.
- To include it again: `sudo zark exclude remove rpool/USERDATA/home_<id>/<user>/downloads both` (snapshots and backups again; the next backup is a full send of it).

## Requirements

- A supported system: Ubuntu 24.04 or 26.04 LTS as their installers lay out encrypted ZFS (rpool, bpool and the keystore), or Ubuntu 26.10 with `/boot` on ext4 (rpool and the keystore, no bpool); Secure Boot on
- Ubuntu live USB (for recovery operations)
- Python 3 (included in Ubuntu live environment)
- ZFS utilities (`zfsutils-linux`, included in Ubuntu desktop)
- sanoid (installed automatically by `zark setup`)
- An external drive for backup storage

## Configuration: `known_drives.json`

Registered backup drives live in `known_drives.json` (under `/etc/zark/` on a system install, or `etc/` in a portable checkout; overridable with `ZARK_CONFIG_DIR`). Each top-level key is a pool name; `prepare` creates entries automatically, but you can edit the file by hand. Fields per drive:

| Field            | Type    | Required | Meaning |
|------------------|---------|----------|---------|
| `guid`           | string  | yes      | The pool GUID (decimal). zark matches the connected drive by this. |
| `drive_id`       | string  | yes      | The stable `/dev/disk/by-id/` identifier (model + serial), without the `-part1` suffix. zark imports the pool by this exact device. |
| `enclosure_id`   | string  | no       | The enclosure's `/dev/disk/by-id/usb-` name, kept beside `drive_id` when `drive_id` is the disk's own name. zark writes it when `prepare` registers the drive, and adds it to an older entry the first time it verifies the drive (`backup`, `registry fix`); a drive behind an enclosure that hides the disk's name keeps the `usb-` name as `drive_id` and gets none. zark 2.1.1 reads a file that has it and keeps it. |
| `last_backup_at` | string  | no       | ISO-8601 UTC of the last successful backup; auto-written by `zark backup`. Drives the staleness reporting. |
| `retention`      | object  | no       | The drive's retention: `window_days` (default 30), `max_age_days` (default 0, off) and `margin_pct` (default 10). `prepare` asks the first two; `zark registry retention <name> window=N max_age=N\|off margin=N` changes them. |
| `last_check`     | object  | no       | The drive's last `zark fullcheck`: its result, point, the scrub's and the restore check's counters and durations; written by `fullcheck` (and by the next command for a check that died). `status.json` copies it. |
| `check_rates`    | object  | no       | Each part's newest measured size and duration (`scrub`: `examined_bytes`, `restore`: `bytes`, with `seconds` and `at`), which `fullcheck`'s next estimate uses; written by `fullcheck` when a part ran to its end. |
| `autoeject`      | boolean | no       | When `true`, the eject prompt for this drive shows a 10-second countdown and then applies the command's default automatically (any keypress cancels it and lets you answer by hand). Useful for unattended rotation. Absent or `false` (the default) means the prompt waits for you indefinitely, as it always has. `prepare` asks whether to enable this; you can also toggle it by editing the file. |

Example:

```json
{
  "backup": {
    "guid": "8963688414852777737",
    "drive_id": "usb-Vendor_Model_SERIAL-0:0",
    "last_backup_at": "2026-06-04T04:23:42Z",
    "autoeject": true
  },
  "black": {
    "guid": "14361060171807873218",
    "drive_id": "usb-Vendor_Model_OTHERSERIAL-0:0"
  }
}
```
