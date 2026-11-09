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
│   ├── boot_layout.py         # Which boot layout a system has: bpool, or /boot on ext4
│   ├── boot_rewrite.py        # recover's rewrites for a new ext4 /boot: fstab, crypttab
│   ├── certificates.py        # Self-test reports, the local record, certificates
│   ├── cleanup.py             # Cleanup and safe teardown
│   ├── cli_spec.py            # The command line declared once: groups, help, words
│   ├── completion.py          # The shell completion's engine, from cli_spec (any user)
│   ├── config.py              # Version, paths and configuration
│   ├── dependents.py          # What a destroy on a drive must take first, and in which order
│   ├── dpkg_lock.py           # dpkg's locks: no backup pool imported while apt or dpkg runs
│   ├── drive_contents.py      # What a backup drive holds, for status.json (read-only)
│   ├── drive_marks.py         # What zark records on a backup drive and reads back
│   ├── drives.py              # Drive detection and identification
│   ├── engine.py              # Backup executor: point, send/receive, anchors
│   ├── exclusions.py          # Datasets left out of backups or snapshots
│   ├── forensics.py           # Pool state captured before it is changed
│   ├── fullcheck.py           # fullcheck's reader of a point, and its record
│   ├── grub_cfg.py            # grub.cfg regeneration with an honest verdict
│   ├── grub_guard.py          # The update-grub guard
│   ├── health.py              # Drive health checks and the destructive test
│   ├── identity.py            # Device identity: how every command names a disk
│   ├── initrd.py              # initrd regeneration for a mounted target system
│   ├── invoker.py             # The user behind a run (pkexec, sudo or root), for zark.log
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
│   ├── pending.py             # pending_drives.json: drives prepare could not read back yet
│   ├── platform_facts.py      # The platform, as the self-test records it
│   ├── pool_record.py         # How origin's pools were at a backup point (features, versions)
│   ├── progress.py            # /run/zark/progress.json: what the running command does
│   ├── reclaim.py             # What removing snapshots from a drive frees
│   ├── recover_checks.py      # What recover refuses from a point's record, before erasing
│   ├── recover_ext4.py        # recover's disk for a point whose /boot was on ext4
│   ├── registry.py            # known_drives.json: parsing, validation, atomic writes
│   ├── replication.py         # Backup planner: what each dataset needs (pure)
│   ├── restore_plan.py        # recover's plan: the point and what each dataset gets
│   ├── restore_points.py      # Restore points of a backup pool
│   ├── retention.py           # Destination retention: which snapshots may go
│   ├── sanoid_retention.py    # sanoid's retention, read for the drive's window
│   ├── scrub.py               # A backup pool's scrub: its state, and rule R1
│   ├── selftest.py            # The platform self-test: checks, bench, runner
│   ├── selftest_boot.py       # Level-1 checks for a /boot on ext4
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
│   ├── upgrade_risk.py        # Whether the 24.04 to 26.04 upgrade would stop rpool's mounts
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
│   ├── fullcheck.py           # Scrub a drive and read a point back, changing no data
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
├── completion/
│   └── zark                   # bash completion (installed for bash-completion)
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

zark installs three files for `update-grub`. `/etc/default/grub.d/zark-backup-guard.cfg` is sourced by `grub-mkconfig` before it writes anything: unless an importable pool is named `rpool` or `bpool`, it sets `ZPOOL_IMPORT_PATH=/nonexistent/zark-no-import`, a directory that does not exist, for the scripts of `/etc/grub.d`. `zpool import` then scans only that directory (zfs 2.2.2 to 2.4.4 read the variable the same way), so `10_linux_zfs` imports nothing, while `rpool`, `bpool` and any imported pool are listed as before. A backup drive that is only plugged in, even one plugged in during the run, is left out of the boot menu, and `update-grub` names it on stderr. The file acts only for `grub-mkconfig`: `grub-install` and grub's postinst source the same directory, so it never exits and writes nothing to stdout.

`09_zfs_backup_guard` then blocks `update-grub` with a clear error message before any damage occurs while an external pool is still visible: an imported one other than `rpool`/`bpool` that has an alternate root or carries `org.zark:format` (a zark backup, mount or full check is running; a zark drive imported by hand without an altroot would add its kernels to `grub.cfg`), or, without the cfg file, one `zpool import` lists. A permanent data pool imported without an altroot does not block it. Its message says what to run once the pool is exported: `sudo apt -f install` when a package's maintainer script ran `update-grub`, `sudo update-grub` otherwise.

`99_zark_cfg_check` runs after every other script of `/etc/grub.d`, while `grub-mkconfig`'s stdout is the `grub.cfg.new` it is writing. When the sections `grub-mkconfig` writes for `10_linux_zfs` and `10_linux` (between their `### BEGIN /etc/grub.d/<name> ###` and `### END` lines) hold no kernel line, a `linux` line whose image is named `vmlinuz-`, `vmlinux-` or `kernel-` (the names those two scripts take; `repair-boot` and `finish` read entries the same way), it exits 1 with a message on stderr, and `grub-mkconfig`, under `set -e`, stops before it replaces `grub.cfg`: the previous one stays. `10_linux_zfs` can fail half way (a mount it finds busy) and still exit 0, its EXIT trap ignoring the failure; without the check `update-grub` would install a `grub.cfg` that boots nothing, every exit status 0 (eli, M18E-4). Any kernel's line counts, not only the running kernel's, since a chroot from a live USB (`repair-boot`, `zark chroot`) runs the live's kernel; but not the entries of `30_os-prober` or `40_custom`, which boot other systems. With stdout a terminal or a pipe it checks nothing; with `/proc` not mounted it cannot read its stdout, says so in one line on stderr and lets `grub-mkconfig` go on. Its message says what to run: `sudo update-grub` again, `sudo apt -f install` after a kernel removal (`dpkg --configure -a` does not finish a pending removal), or `sudo zark repair-boot` from a live USB. `setup` installs the three files on the running system, as `recover`, `repair-boot` and `finish` do.

### The apt guard

The GRUB guard fires late: it can refuse to regenerate `grub.cfg`, but by then a kernel package has already been unpacked into `/boot`, or a removed kernel's files deleted - leaving dpkg half-done and a `grub.cfg` that does not match `/boot`. This is exactly how a background `unattended-upgrades` run, with a backup drive still connected, could break a system.

To close that vector, `setup` (on the running system) and `recover`/`finish` (on a recovered system) install an apt guard: a standalone `/usr/local/lib/zark/apt-zfs-backup-guard` script wired in as a `DPkg::Pre-Install-Pkgs` hook with protocol version 3, so APT sends it every action of the transaction, removals included, *before* dpkg starts. For a boot-critical package (`linux-image-*`, `linux-modules-*`, 26.10's `linux-main-modules-*`, `linux-headers-*`, `grub-*`, `shim-*`, `zfs-*`) it aborts the whole transaction when the package is to be installed while an external pool is plugged in or imported, and when it is to be removed or purged while one is imported (a backup, a mount or a full check is running); with the drive only plugged in, the cfg file keeps `update-grub` safe, so a kernel removal by `apt autoremove` or unattended-upgrades completes. A line it cannot read counts as a removal. The same removal rule runs at dpkg's level in `/etc/kernel/prerm.d/00-zark-backup-guard`, which every kernel's prerm runs before any file is deleted, first of the directory: a `dpkg -r` by hand, or a pool imported after the hook ran, cannot leave a kernel half-removed. Both detect pools with `zpool` directly (no dependency on zark itself), so they keep protecting a recovered system after the live USB is gone. zark's own recovery flows set `ZARK_INTERNAL=1` to bypass them; a login-time MOTD reminder (`/etc/update-motd.d/99-zark-external-pool`) warns when an external pool is attached.

### No import while apt or dpkg runs

The hook and the prerm see a backup pool only once it is imported. zark therefore never imports a backup pool while apt, dpkg or unattended-upgrades runs, and none of them can start while zark's `zpool import` runs. Before every import of a backup pool on the installed system (`backup`, `mount`, `prune`, `purge`, `fullcheck`, the self-test, the read-back, `prepare`'s `zpool create`), zark takes `/var/lib/dpkg/lock-frontend` as APT does (an `fcntl` write lock, without waiting), checks `/var/lib/dpkg/lock`, and holds the first for the import alone. When either is held the command stops with exit status 75 and names the holder; `apt-get`, dpkg and unattended-upgrades started during zark's import fail at their own lock, `apt` waits for it, and the daily timer's run is skipped to the next day. The kernel releases the lock however zark ends. On a live session nothing is checked or held. The read-back after a backup or a prepare does not stop: it waits for apt or dpkg to end, holding nothing, for up to 15 minutes, and on a terminal asks whether to skip; a read-back that was skipped or timed out is reported as not done (see [Backup](USAGE.md#back-up-your-system)).

### The full check of a drive

`zark fullcheck` verifies a drive's copies in two parts, each in a fresh import of its own through the import rules above, after refusing a drive whose pool is already imported (by anyone, before the first question), so it never pulls a pool from under someone else's mounts.

The restore check writes nothing. The pool is imported `readonly=on`, so the module never starts its sync thread and no uberblock, label, history or error-log record is written. The drive's keystore is opened read-only from its head zvol (`cryptsetup open --readonly`, ext4 `ro,noload`), every encryption root's key is loaded (`zfs load-key` opens no transaction; a key that loads proves the drive's wrapping key unwraps the copies' master keys), and each dataset `recover` would restore from the chosen point is mounted from its snapshot with `mount -t zfs -o ro` (a snapshot is a legacy mount: no `mountpoint` or `canmount` is read or set) and walked: every regular file read to its end once per inode, symlinks read and not followed, directories listed, no device, fifo or socket opened. Every read checks the block's checksum and, encrypted, decrypts it and checks its MAC, so the point's plaintext is authenticated end to end.

The scrub needs a read-write import, and on OpenZFS 2.2 (noble) a scrub of a read-only or a suspended pool never returns: the ioctl is registered without a pool check there, and the task waits for a sync that cannot happen, in state D (fixed upstream by PR 17717; 2.4 refuses with `EROFS` or `EAGAIN`). Rule R1: before any `zpool scrub` (start, resume, pause), zark reads `readonly`, which must be `off`, and `health`, which must not be `SUSPENDED`, both answered from the module's memory; a read that fails is a refusal. All of zark's `zpool scrub` calls go through one function behind R1 (`lib/scrub.py`). A pool found suspended while the scrub runs is neither paused nor exported (both would wait for the drive); zark says how to bring it back. Rule R2: what a scrub writes is named in the command's text and the manpage: its own progress, the pool's labels and history, and a damaged metadata copy rewritten from a good one; no file, snapshot or property changes. ZFS lists the damaged files (`zpool status -v`) only while the key of every damaged block's dataset is loaded: one damaged block of an encrypted dataset whose key is not loaded makes it refuse the whole list (`List of errors unavailable: permission denied`, exit 0; `module/zfs/spa_errlog.c`, `DS_HOLD_FLAG_DECRYPT`). When the scrub's status has no list, `fullcheck` records the scrub and the damage (with the files unnamed) and refreshes `status.json`, then asks the passphrase again, opens the keystore read-only as the restore check does, loads the keys (a key load writes nothing, and no dataset is mounted: every one is `canmount=noauto`), reads the list, unloads every key and records the files' count, all before the export, outside the dpkg hold (no import); on a signal the cleanup closes the keystore, unloads the keys and exports, and a dead check's `zark clean` closes the mapper and exports, which drops every key. A scrub interrupted without a pause resumes at the next read-write import by anyone, so every other command's read-write import pauses a running scrub (only a scrub, not a resilver) after checking the pool's GUID, and `fullcheck` resumes it.

The result has one source, the drive's `last_check` in the registry (`known_drives.json`): result (`clean`, `damage`, `not_finished`, `failed`), the point, the scrub's and the restore check's counters and durations. Beside it, `check_rates` keeps each part's newest measured size and duration, which the next estimate uses, so a run that measured nothing (interrupted, a scrub-only resume) does not hide them. `status.json`'s drive entry and its `drive_check` notice are copied from it at every write, never carried forward from the previous file; a dead check's orphaned `progress.json` is recorded as `not_finished` by the next command. Nothing is written on the drive by zark, and nothing is recorded from a live session.

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
- **Ubuntu 26.10 and later on UEFI** - the installer puts `/boot` on an ext4 partition of rpool's disk, with no bpool, with Secure Boot on or off. zark reads the layout at every run. `setup` creates `rpool/ROOT/<be>/zark-boot` (`canmount=noauto`, `mountpoint=legacy`, rpool's encryption), and `backup` and `prepare` rsync `/boot` into it before every point, so the point's rpool snapshot holds the root and its `/boot` together. Each point's pool record carries the ext4 file system's identity, `written_by` and `min_recover`; a point without a record is ext4 when the drive holds its `zark-boot` or says `org.zark:origin-layout=ext4`, and such a point is refused. `recover` lays the disk out as the record says (ESP, `/boot`, swap, rpool; no bpool), makes `/boot` with origin's features and a new UUID, copies `zark-boot` into it, and rewrites the UUID in `grub.cfg`, fstab and crypttab, checking the result; `finish` regenerates `grub.cfg` once the drive is gone. zark 2.1.1 and earlier must not recover such a point: they erase the disk and leave no `/boot` ([#5](https://github.com/juanmitaboada/zark/issues/5)).
- **Cross-host recovery** - backups are portable across machines: a backup taken on machine A can be restored onto machine B with a different drive layout / firmware. zark rewrites every `--fs-uuid` reference in `grub.cfg` (including those carrying `--hint-bios` / `--hint-efi` / `--hint-baremetal` options) so the recovered system boots regardless of where its disks land in the new BIOS enumeration.
- **ZFS encryption** - AES-256-GCM with `keyformat=raw`, encryption key on a LUKS-encrypted zvol (the keystore).
- **bpool features** - restricted to the GRUB-readable subset documented in `/usr/share/zfs/compatibility.d/grub2`. zark explicitly does not enable `head_errlog` or `vdev_zaps_v2` on bpool: even GRUB 2.14 (Ubuntu 26.04) cannot read either, and activating them produces an unbootable system. On recover both pools get the features origin had at the point; for a point without that record (written by zark 1.x or an early 2.0.0 development build) bpool gets the GRUB-readable set and rpool whatever features the running ZFS supports.
- **Platform self-test** - `sudo zark selftest` checks, on file-backed pools that touch no disk, the ZFS, util-linux, systemd, kernel and Python behaviours zark relies on that a test can reach without a boot or a change to the running system (`--stick <device>` adds the checks that need a real disk, on a scratch stick it erases); what it leaves out (GRUB, Secure Boot, dracut, USB bridges' cache flushes, among others) is listed among the self-test's known limitations in `CHANGELOG.md`. Each release ships the results certified on the maintainer's machine; `zark selftest status` says whether they cover yours, and `zark selftest report` prints a link to share yours. `zark selftest warnings on` (off by default) turns the coverage into warnings; the setting is per installation or checkout, in `settings.json` next to that copy's `known_drives.json`.
- **Drives readable by older live sessions** - `prepare` creates new drives with `compatibility=openzfs-2.2`, so an Ubuntu 24.04 live (OpenZFS 2.2) can read them; `recover` names, before importing anything, any feature of a drive the live's ZFS cannot read.
- **Secure Boot** - full compliance via signed GRUB chain (shimx64 → grubx64.signed → kernel).
- **Hardware tested:**
  - zark 2.2.0: Dell XPS 13 9333, with Ubuntu 26.10 installed (`/boot` on ext4), backed up and recovered from an Ubuntu 26.10 live; its self-test certificates in `certificates/selftest.json` come from that system and from the Ubuntu 26.10, 26.04 and 24.04.5 lives.
  - zark 2.0.0: Dell XPS 13 9333, with Ubuntu 24.04 and 26.04 installed on the installer's encrypted ZFS layout, and recovered from an Ubuntu 26.04 live; the self-test's certificates in `certificates/selftest.json` come from it.
  - zark 1.x: MINISFORUM UM890 (Ubuntu 24.04, 25.04, 25.10, 26.04), Dell XPS 13 9315 (the maintainer's laptop, Ubuntu 25.10), and Dell XPS 13 9333 (while the 9315 was away for repairs).
- **CI/test:** GitHub Actions runs the unit suite and the static analysis; validation on hardware is the self-test and a checklist on the machine above for each release. The QEMU/OVMF integration harness 1.x had (`make test-real`) was retired in 2.1.0.

## Security notes

- zark handles ZFS encryption passphrases and raw key material at runtime. Passphrases are never written to disk or echoed to stdout. If you suspect a leak (e.g. from `set -x` debug output added during local development), rotate the key, and then retire and re-prepare every backup drive (see the next point).
- A backup drive's keystore is the copy `prepare` made of the source's keystore: its LUKS passphrase and its `system.key`. A later `cryptsetup luksChangeKey` in the source leaves the drive asking the old passphrase. A later `zfs change-key` of rpool makes the next backup rewrap the drive's datasets to the new key; the drive's keystore then no longer opens them, the following backup stops at the key load and `recover` from that drive cannot load its keys. After either change, run `zark purge` and `zark prepare` for each drive.
- Backup drives contain full copies of your encrypted datasets. Anyone with physical access to a backup drive **and** the passphrase can decrypt all data. Store backup drives physically secured.
- The keystore zvol holds the raw encryption key in a LUKS-encrypted volume. Its security ultimately reduces to the strength of the LUKS passphrase you set during `zark recover`.
- zark does not transmit data over the network. All operations are local to the machine and the connected backup drive.
