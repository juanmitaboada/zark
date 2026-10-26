# zark usage

The commands, a first backup and a recovery step by step, what zark needs, and its drive registry.

## Commands

| Command            | Description                                                  |
|--------------------|--------------------------------------------------------------|
| `explore`          | Scan for ZFS pools, show known/unknown drives                |
| `setup`            | Install dependencies, configure sanoid for automatic snapshots |
| `prepare`          | Initialize a new blank drive as a backup target              |
| `backup`           | Incremental encrypted backup: one restore point per run      |
| `recover`          | Full bare-metal system recovery from backup                  |
| `finish`           | Post-recovery finalization (run from the recovered system)   |
| `repair-boot`      | Fix boot issues from a live USB without full recovery        |
| `chroot`           | Open an interactive chroot into the installed system (live USB) |
| `mount`            | Mount a backup pool - or the local system (`mount local`) - for inspection/chroot |
| `umount`           | Unmount a backup pool, or the local system (`umount local`)  |
| `clean`            | Release what an interrupted zark run left mounted or imported |
| `purge`            | Securely wipe a managed backup drive                         |
| `monitor`          | Live view of a running command and the backups' state (no root) |
| `simulate`         | Boot the target disk in QEMU/KVM to verify the boot chain    |
| `health`           | Non-destructive risk checks on a backup drive                |
| `prune`            | Reclaim space in the source or on a backup drive (interactive) |
| `status`           | Source pools' fill level and each drive's backup age         |
| `alerts`           | Install or remove the status MOTD and hourly timer           |
| `registry`         | List, forget or fix drives; set each drive's retention       |
| `exclude`          | Leave datasets out of backups, of sanoid's snapshots, or both |
| `fix-rpool-mountpoint` | Give a restored rpool back `mountpoint=/` (live USB)     |
| `selftest`         | Check that this platform's ZFS and tools behave as zark relies on |

## Quick start

### First-time setup

Once per machine - installs sanoid for automatic snapshots and registers your backup drive:

```bash
sudo ./zark setup     # install sanoid + zfs tooling, configure snapshots
sudo ./zark prepare   # initialize a blank drive as a backup target
```

`prepare` asks its questions, creates the backup pool, sends the first backup point, reads the pool back and only then registers the drive's GUID in `etc/known_drives.json`. After this, `zark backup` finds the drive automatically every time you connect it.

### Back up your system

```bash
# Connect your backup drive, then:
sudo ./zark backup
```

zark detects the backup drive by GUID, takes one backup point `zark_<UTC>` of every dataset at once, and brings each dataset on the drive to it with an incremental raw send. Each backup adds a restore point and removes none. A typical incremental backup takes seconds.

To watch it, run `zark monitor` in another terminal, as any user: it shows the running command's step, the bytes sent, the rate and the time left, says when zark is waiting for an answer, and below that the pools, the drives and their last backups. It only reads zark's status files (`docs/STATUS-FILES.md`) and lists the pools with `zfs list` and `zpool list`; it never takes the lock or imports a pool. `zark monitor --once` prints one screen, and `--once --state` only the state word, for scripts.

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

## Requirements

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
| `last_backup_at` | string  | no       | ISO-8601 UTC of the last successful backup; auto-written by `zark backup`. Drives the staleness reporting. |
| `retention`      | object  | no       | The drive's retention: `window_days` (default 30), `max_age_days` (default 0, off) and `margin_pct` (default 10). `prepare` asks the first two; `zark registry retention <name> window=N max_age=N\|off margin=N` changes them. |
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
