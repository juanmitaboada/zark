# zark design

How zark is laid out, the decisions behind it, what it is compatible with, and its security notes.

## Architecture

```
zark/
├── zark                       # Entry point (#!/usr/bin/env python3)
├── lib/
│   ├── alerts.py              # Space and backup-age alerts: MOTD script, timer
│   ├── anchors.py             # Anchors and backup points in origin, outside a run
│   ├── apt_guard.py           # The apt/dpkg guard against kernel upgrades with a drive in
│   ├── backup_layout.py       # What a backup pool holds, read by recover and mount
│   ├── boot_chain.py          # A restored system's boot chain: mounts, GRUB, initrd scripts
│   ├── certificates.py        # Self-test reports, the local record, certificates
│   ├── cleanup.py             # Cleanup and safe teardown
│   ├── config.py              # Version, paths and configuration
│   ├── drive_contents.py      # What a backup drive holds, for status.json (read-only)
│   ├── drive_marks.py         # What zark records on a backup drive and reads back
│   ├── drives.py              # Drive detection and identification
│   ├── engine.py              # Backup executor: point, send/receive, anchors
│   ├── exclusions.py          # Datasets left out of backups or snapshots
│   ├── forensics.py           # Pool state captured before it is changed
│   ├── grub_cfg.py            # grub.cfg regeneration with an honest verdict
│   ├── grub_guard.py          # The update-grub guard
│   ├── health.py              # Drive health checks and the destructive test
│   ├── identity.py            # Device identity: how every command names a disk
│   ├── initrd.py              # initrd regeneration for a mounted target system
│   ├── keystore.py            # The LUKS keystore and the key loads
│   ├── live.py                # The live session's installer probe
│   ├── live_pools.py          # The monitor's two read-only pool listings
│   ├── lock.py                # One lock for every command that changes state
│   ├── log.py                 # Colored output, frames, logging
│   ├── monitor_view.py        # What zark monitor shows, as styled lines
│   ├── mount.py               # Mounting the installed system's pools from a live USB
│   ├── mount_props.py         # Where a restored dataset's mount properties come from
│   ├── notify.py              # Desktop notifications from root
│   ├── origin_prune.py        # What `zark prune` can remove in origin
│   ├── platform_facts.py      # The platform, as the self-test records it
│   ├── pool_record.py         # How origin's pools were at a backup point (features, versions)
│   ├── progress.py            # /run/zark/progress.json: what the running command does
│   ├── reclaim.py             # What removing snapshots from a drive frees
│   ├── registry.py            # known_drives.json: parsing, validation, atomic writes
│   ├── replication.py         # Backup planner: what each dataset needs (pure)
│   ├── restore_plan.py        # recover's plan: the point and what each dataset gets
│   ├── restore_points.py      # Restore points of a backup pool
│   ├── retention.py           # Destination retention: which snapshots may go
│   ├── sanoid_retention.py    # sanoid's retention, read for the drive's window
│   ├── selftest.py            # The platform self-test: checks, bench, runner
│   ├── selftest_host.py       # Level-1 checks on the host's own tools
│   ├── selftest_l1.py         # Level-1 checks on ZFS, file-backed pools
│   ├── selftest_l2.py         # Level-2 checks on a scratch stick
│   ├── sh.py                  # Shell command runner with logging
│   ├── signals.py             # How every command stops on a signal
│   ├── status.py              # What status reports, the MOTD shows, the timer notifies
│   ├── status_file.py         # /var/lib/zark/status.json, for other programs
│   ├── status_reader.py       # The reference reader of the status files
│   ├── statusdirs.py          # /run/zark and /var/lib/zark, readable by anyone
│   ├── survey.py              # Reads of origin and of a drive, and the checks before a run
│   ├── transfer_eta.py        # The monitor's time left, from the run's own pace
│   ├── zfs.py                 # ZFS pool and dataset operations
│   └── zfs_features.py        # OpenZFS pool features and their first release
├── commands/
│   ├── alerts.py              # Install or remove the alerts
│   ├── backup.py              # Back up this system to a registered drive
│   ├── chroot.py              # Interactive chroot into the installed system (live USB)
│   ├── clean.py               # Release what an interrupted run left
│   ├── exclude.py             # Leave datasets out of backups or snapshots
│   ├── explore.py             # Scan for ZFS pools and show their status
│   ├── finish.py              # Post-recovery finalization
│   ├── fix_rpool_mountpoint.py  # Give a restored rpool back mountpoint=/
│   ├── health.py              # Drive analysis: read-only and destructive
│   ├── monitor.py             # Live view, a reader of the status files
│   ├── mount.py               # Backup pool mounting (+ `mount local` for the system)
│   ├── prepare.py             # New drive initialization
│   ├── prune.py               # Reclaim space in origin or on a drive
│   ├── purge.py               # Secure drive wipe
│   ├── recover.py             # Full bare-metal recovery
│   ├── registry.py            # Inspect and repair known_drives.json
│   ├── repair_boot.py         # Boot chain repair from live USB
│   ├── selftest.py            # Check that this platform behaves as zark relies on
│   ├── setup.py               # Dependencies, sanoid policy, guards, alerts
│   ├── simulate.py            # QEMU boot test (read-only by default)
│   ├── status.py              # How full the pools are and how old each backup is
│   └── umount.py              # Backup pool unmounting (+ `umount local`)
├── certificates/
│   └── selftest.json          # Platforms the self-test has certified
└── etc/
    └── known_drives.json.example  # The registry's format (the real one is local)
```

## Key design decisions

### Why raw `zfs send` instead of file-level backup?

Block-level replication via `zfs send -w` (raw/encrypted) is fundamentally different from file-level tools like rsync:

- **Atomic snapshots** - the backup represents an exact point-in-time state, created in milliseconds without interrupting running services.
- **Encryption preserved** - raw send transmits encrypted blocks directly. The backup drive holds ciphertext; keys are never exposed during transfer.
- **Efficiency** - incremental sends only transmit changed blocks since the last snapshot, regardless of file count or size.

### Why not syncoid?

zark 1.x replicated with syncoid. syncoid bases each incremental on the newest snapshot common to both sides and receives with `-F`, so every backup destroyed the drive's snapshots newer than that base - the previous backups' restore points - once the source had pruned them. zark 2 runs its own loop: the base is always the drive's newest snapshot, found in the source as a snapshot or, once pruned there, through this drive's bookmark; receives are never forced; anything that would destroy data on the drive is a question asked before the transfer starts.

### The GRUB guard

When an external ZFS backup pool is connected, Ubuntu's `10_linux_zfs` GRUB script auto-imports all visible pools and attempts to mount their encrypted datasets. When this fails (no key loaded), it generates a `grub.cfg` with zero kernel entries - an unbootable system.

zark installs `09_zfs_backup_guard`, a lightweight script that detects external pools and blocks `update-grub` with a clear error message before any damage occurs. An external pool is one `zpool import` lists, or an imported one other than `rpool`/`bpool` that has an alternate root or carries `org.zark:format` (a zark drive imported by hand without an altroot would add its kernels to `grub.cfg`); a permanent data pool imported without an altroot does not block it. `setup` installs it on the running system, as `recover`, `repair-boot` and `finish` do.

### The apt guard

The GRUB guard fires late: it can refuse to regenerate `grub.cfg`, but by then a kernel package upgrade has already unpacked the new kernel into `/boot` and autoremove may have pulled the old one - leaving a `grub.cfg` that points at a kernel which no longer exists. This is exactly how a background `unattended-upgrades` run, with a backup drive still connected, can brick a system.

To close that vector, `setup` (on the running system) and `recover`/`finish` (on a recovered system) install an apt guard: a standalone `/usr/local/lib/zark/apt-zfs-backup-guard` script wired in as a `DPkg::Pre-Install-Pkgs` hook. APT runs it *before* dpkg unpacks anything; when a boot-critical package (`linux-image-*`, `linux-headers-*`, `grub-*`, `shim-*`, `zfs-*`) is being installed while an external ZFS pool is connected (detected as the GRUB guard does), the hook aborts the entire transaction. It detects pools with `zpool` directly (no dependency on zark itself), so it keeps protecting a recovered system after the live USB is gone. zark's own recovery flows set `ZARK_INTERNAL=1` to bypass it; a login-time MOTD reminder (`/etc/update-motd.d/99-zark-external-pool`) warns when an external pool is attached.

### One run at a time, and `zark clean`

Every command that changes state takes one lock, `/run/zark/zark.lock` (`flock(2)`), before it starts; a second run is refused at once, naming the one in progress, with exit status 75. The kernel releases the lock however the run ends, so it is never left stale. `status`, `explore`, `monitor` and `registry list` run without it.

When a run was interrupted, `zark clean` lists what zark itself left (mounts below its own roots, pools it imported under its altroots, its keystore mappings, a stopped sanoid timer), names the imported pools it leaves alone, asks, and releases only that, without forcing anything. Only directories left empty are removed, and only once none of its pools is still imported; anything that stays ends `CLEANUP INCOMPLETE`, naming it.

Before a damaged pool is touched or destroyed (a failed read-back, a backup pre-check that finds errors, `purge`), zark writes `zark-forensics-<pool>-<UTC>.txt` next to `zark.log`: `zpool status`, the pool's events and history, the kernel log and the USB bridge ids. `purge` checks every step and verifies the disk is blank; otherwise it ends `PURGE INCOMPLETE` and keeps the registry entry and the anchors.

### Boot chain integrity

zark never calls `grub-install` alone. The correct Secure Boot procedure is:

1. `grub-install` - installs GRUB modules and bootstrap
2. `dpkg-reconfigure grub-efi-amd64-signed` - overwrites with Canonical-signed binary
3. `dpkg-reconfigure shim-signed` - ensures shim chain is intact
4. `update-grub` - regenerates grub.cfg

This produces a boot chain identical to a fresh Ubuntu installation.

## Compatibility

- **Ubuntu 24.04 LTS** - initramfs-tools: the system's own zfs initramfs script opens the keystore after importing rpool, with `cryptsetup-initramfs`'s `cryptroot`, so recover adds no hook; a restored root without that package is an error in recover's banner, with the commands that install it. OpenZFS 2.2.2 tools on the HWE kernel's 2.4.1 module.
- **Recovery from an Ubuntu 26.04 or newer live**, for a 24.04 or a 26.04 system: each backup point records how origin's rpool and bpool were (their features, `compatibility`, and the Ubuntu and OpenZFS that ran them), and `recover` creates the new pools with exactly those features, so a 24.04 system restored from a 26.04 live keeps pools its own OpenZFS imports. Both pools' roots get the installer's properties (`devices=off`, `compression=lz4`, `relatime=on` among them). An older live gets a warning.
- **Ubuntu 26.04 LTS** - dracut: zark's keystore module (89keystore) asks for the passphrase through systemd-ask-password; zark detects which generator the system has at recovery time. Plus shim 15.8 (`.signed.latest`) pinning during recovery to avoid the SBAT revocation that affects fresh subiquity installs left pointing at `.signed.previous`.
- **Ubuntu 26.10 and later with Secure Boot** - the installer puts `/boot` on ext4, with no bpool: `backup` and `prepare` refuse such a system and name where `/boot` is.
- **Cross-host recovery** - backups are portable across machines: a backup taken on machine A can be restored onto machine B with a different drive layout / firmware. zark rewrites every `--fs-uuid` reference in `grub.cfg` (including those carrying `--hint-bios` / `--hint-efi` / `--hint-baremetal` options) so the recovered system boots regardless of where its disks land in the new BIOS enumeration.
- **ZFS encryption** - AES-256-GCM with `keyformat=raw`, encryption key on a LUKS-encrypted zvol (the keystore).
- **bpool features** - restricted to the GRUB-readable subset documented in `/usr/share/zfs/compatibility.d/grub2`. zark explicitly does not enable `head_errlog` or `vdev_zaps_v2` on bpool: even GRUB 2.14 (Ubuntu 26.04) cannot read either, and activating them produces an unbootable system. On recover both pools get the features origin had at the point; for a point without that record (written by zark 1.x or an early 2.0.0 development build) bpool gets the GRUB-readable set and rpool whatever features the running ZFS supports.
- **Platform self-test** - `sudo zark selftest` checks, on file-backed pools that touch no disk, the ZFS, util-linux, systemd, kernel and Python behaviours zark relies on that a test can reach without a boot or a change to the running system (`--stick <device>` adds the checks that need a real disk, on a scratch stick it erases); what it leaves out (GRUB, Secure Boot, dracut, USB bridges' cache flushes, among others) is listed among the self-test's known limitations in `CHANGELOG.md`. Each release ships the results certified on the maintainer's machine; `zark selftest status` says whether they cover yours, and `zark selftest report` prints a link to share yours. `zark selftest warnings on` (off by default) turns the coverage into warnings; the setting is per installation or checkout, in `settings.json` next to that copy's `known_drives.json`.
- **Drives readable by older live sessions** - `prepare` creates new drives with `compatibility=openzfs-2.2`, so an Ubuntu 24.04 live (OpenZFS 2.2) can read them; `recover` names, before importing anything, any feature of a drive the live's ZFS cannot read.
- **Secure Boot** - full compliance via signed GRUB chain (shimx64 → grubx64.signed → kernel).
- **Hardware tested:**
  - zark 2.0.0: Dell XPS 13 9333, with Ubuntu 24.04 and 26.04 installed on the installer's encrypted ZFS layout, and recovered from an Ubuntu 26.04 live; the self-test's certificates in `certificates/selftest.json` come from it.
  - zark 1.x: MINISFORUM UM890 (Ubuntu 24.04, 25.10, 26.04), Dell XPS 9315 (Ubuntu 25.10), and a disk-failure recovery on a separate Ubuntu 24.04 system.
- **CI/test:** GitHub Actions runs the unit suite and the static analysis; validation on hardware is the self-test and a checklist on the machine above for each release. The QEMU/OVMF integration harness 1.x had (`make test-real`) was retired in 2.1.0.

## Security notes

- zark handles ZFS encryption passphrases and raw key material at runtime. Passphrases are never written to disk or echoed to stdout. If you suspect a leak (e.g. from `set -x` debug output added during local development), rotate the key, and then retire and re-prepare every backup drive (see the next point).
- A backup drive's keystore is the copy `prepare` made of the source's keystore: its LUKS passphrase and its `system.key`. A later `cryptsetup luksChangeKey` in the source leaves the drive asking the old passphrase. A later `zfs change-key` of rpool makes the next backup rewrap the drive's datasets to the new key; the drive's keystore then no longer opens them, the following backup stops at the key load and `recover` from that drive cannot load its keys. After either change, run `zark purge` and `zark prepare` for each drive.
- Backup drives contain full copies of your encrypted datasets. Anyone with physical access to a backup drive **and** the passphrase can decrypt all data. Store backup drives physically secured.
- The keystore zvol holds the raw encryption key in a LUKS-encrypted volume. Its security ultimately reduces to the strength of the LUKS passphrase you set during `zark recover`.
- zark does not transmit data over the network. All operations are local to the machine and the connected backup drive.
