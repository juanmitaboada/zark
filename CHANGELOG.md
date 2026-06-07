# Changelog

All notable changes to **zark** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.12] — 2026-06-04

### Exact-device import + post-export read-back verification — surviving a second FUA-lie corruption mode

v1.0.11 addressed the *catastrophic* form of USB-SATA bridge corruption: all four pool labels lost after unplug. Field use surfaced a second, subtler mode on the same `0634:5604` Micron enclosures, and a device-identity trap that turned a recoverable situation into an hour of misdiagnosis. This release fixes the import path and adds a verification step that converts a silent, deferred backup loss into an immediate, visible failure.

#### The incident

A `zark backup` to drive `black` had completed successfully on a previous run (`[OK] Pool black exported`), yet the next backup failed to import it:

```
zpool import black
cannot import 'black': insufficient replicas
        Destroy and re-create the pool from a backup source.
```

Forensics told a layered story. `zpool import` (label scan) reported the pool **ONLINE** with all four labels present (`zdb -l … labels = 0 1 2 3`, 32 valid uberblocks, txg 58702→58918 all within a 2-second window — the signature of a syncoid closing burst). But a real open failed deep in `vdev_load`:

```
spa_load(black): using uberblock with txg=58918
...
disk vdev '…': metaslab_init failed [error=52]
spa_load(black): FAILED: vdev_load failed [error=52]
zdb: can't open 'black': Invalid exchange
```

`error=52` is `EBADE`. The labels and uberblocks (fixed-position writes) persisted; the **spacemaps / `metaslab_array`** (written during the closing burst) did not. The bridge had acknowledged a flush whose data never reached NAND — the same FUA lie as v1.0.11, but landing on allocation metadata rather than labels. Because *every* uberblock in the ring references the same corrupt `metaslab_array`, rewind recovery (`-F`, `-FX`, explicit `-t <txg>`) had no consistent transaction to roll back to. The pool was unrecoverable and had to be recreated from source (`rpool` was intact — `black` is a backup *target*).

Two contributing traps were identified and are fixed here.

#### Fix 1 — Import by the exact device, not by scanning a directory

The bridge exposes a **generic, non-unique WWN** (`wwn-0x5000000000000001`) identical across enclosures — both `blue` and `black` presented the same identifier. `commands/backup.py` already computed the precise device path (`…-part1`), but `ZFS.pool_import` discarded it: it passed `os.path.dirname(device)` to `-d`, turning a precise device into a directory scan. ZFS then resolved the vdev through the generic WWN alias, which points at the **whole disk** rather than the labelled partition, contributing to the `insufficient replicas` confusion.

`ZFS.pool_import` now builds an **ordered** candidate list and tries the **exact device first** (without `-f`, then with), falling back to directory scan and finally auto-scan only if the exact device fails:

1. `-d <exact device>` — the precise `…-part1` the caller supplied
2. `-d <device directory>` — fallback
3. `-d /dev/disk/by-id/` — fallback
4. auto-scan (no `-d`) — last resort

The previous loop ordering (all directories without force, then all with force) is replaced by per-candidate `(plain, force)` so that an exact-device import needing only `-f` is still preferred over a directory scan that could latch onto the wrong alias. The public signature is unchanged; callers that import without a device (`recover`, `mount`, `repair-divergent`, all passing `device=None`) are unaffected and keep the fallback-only behaviour.

This was validated live: importing by `…-part1` makes the vdev appear under its unique serial (`usb-Micron_…_<serial>-0:0`) instead of the bogus WWN.

#### Fix 2 — Post-export read-back verification (always on)

The hard lesson: **on these bridges, a `zpool export` that returns 0 does not prove the pool is reimportable.** The only reliable signal is to read it back. A new `ZFS.verify_exported_pool_readback(name, device)` runs after `Cleanup.run()` has exported and flushed, and before the success banner:

1. `sync`, then drop the kernel page cache (`/proc/sys/vm/drop_caches`) — so the read-back comes from NAND, not from RAM still holding what was just written. Without this we would verify against cache and a silently-corrupt pool would pass.
2. Re-import **read-only, no-mount, by the exact device** (reusing Fix 1).
3. Require health `ONLINE`.
4. Re-export, restoring the on-entry state.

If the re-import fails — the `error=52` signature — `backup` now emits a red **`BACKUP NOT VERIFIED`** banner pointing at `docs/HARDWARE.md`, fires a failure notification, and returns *without* reaching the "safe to unplug" prompt. The operator is told the backup is not trustworthy even though syncoid and export both reported success. This is **detection, not prevention**: it cannot stop the bridge lying, but it surfaces the loss while the operator can still act, instead of at restore time months later.

The `backup` flow is renumbered 1/10…10/10 (verification is step 10). The check is unconditional — no flag — because the failure is silent and the cost (one read-only import/export cycle) is seconds. Per-FUA-lie scrubbing was considered and rejected (see below). Note the v1.0.11 "no rapid export/import cycles" rule applies only to the live-USB overlay during `recover`, not to external backup targets, so the extra cycle here is safe.

#### Fix 3 — `zark health` and risk checks in `prepare`

A new non-destructive command, `zark health [device]`, inspects kernel/sysfs state for the conditions correlated with bridge-induced corruption: a bridge that reports `doesn't support DPO or FUA`, the UAS transport on a USB device, and the bridge's VID:PID against a known-problematic list (`0634:5604` and `0634:5607`, the two Micron CT2000X10* USB-SATA enclosures observed to misbehave). It writes nothing, so it flags **risk**, never asserting a drive is safe — that can only be proven under write load. The checks live in `lib/health.py` as a small declarative list (`_check_fua`, `_check_transport`, `_check_known_bridge`) designed to grow as new hardware is identified; `KNOWN_BAD_BRIDGES` is a module constant.

`prepare` now runs the same readonly check before doing any work and, if a risk factor is present, warns and asks for confirmation (default no) rather than aborting — because with the usb-storage quirk such a bridge works correctly, so a hard refusal would block a usable drive. `prepare` also performs a read-back verification at the very end: since `prepare` writes the entire rpool raw send (real write-under-load with transaction churn), its post-export re-import is itself a destructive confirmation that the bridge survives load. If the read-back fails, the drive is **not registered** in `known_drives.json`, even though every prior step reported success.

`zark health` is fully interactive (no flags): it gathers all choices up front, then runs unattended except for one pause. Beyond the read-only checks it offers a **destructive** mode that creates a throwaway pool, writes ~2/15 GB (or whole-disk "surface", capped) with transaction churn, exports, and re-imports to verify the bridge persisted the writes. An optional **cold** pass powers the device down (`eject`), waits for the operator to physically reconnect, and only then re-imports — the only check that reads strictly from NAND rather than possibly from the bridge's own DRAM. Profiles show pessimistic time estimates derived from a measured write speed. The throwaway pool is always destroyed and the device wiped (`wipefs` + `sgdisk --zap-all`) on exit, leaving it genuinely blank rather than carrying orphan `zfs_member` partitions that would look like real data. Whenever any risk or test failure is found, a self-contained diagnostic report (environment, drive, bridge VID:PID, findings, dmesg tail) is written to `/tmp`, with instructions for filing a GitHub issue and an option to obfuscate serials/GUIDs (offered in both read-only and destructive modes).

The destructive test also scans the kernel log for **transport errors during the run** (`DID_ERROR`, UAS aborts/resets, non-clean failed cache flushes), captured between a pre-test dmesg marker and a scan taken after the re-import. This closes a blind spot found on real hardware: a bridge under UAS can stumble at the bus level (logging `DID_ERROR`) yet still happen to persist the data, so the read-back alone reports a misleading PASS. When the data survives but transport errors were logged, the result is surfaced as **"passed, but the enclosure is not trustworthy"** (a WARN, included in the report) rather than a clean OK. A clean `DID_NO_CONNECT` disconnect is explicitly excluded, consistent with the `hostbyte` distinction below. The device picker also lists bare connected USB disks (not just pool-bearing/registered drives), so a blank enclosure — the device a destructive evaluation actually targets — is selectable without passing an explicit path.

#### Per-drive auto-eject (timed eject prompt)

The eject prompt can now time out for drives that opt in. A new per-drive `autoeject` boolean in `known_drives.json` (default false, asked by `prepare`, editable by hand) makes the prompt show a 10-second countdown (`EJECT_TIMEOUT_SECONDS`, a fixed constant — the per-drive flag is the only knob) and then **eject** the drive automatically. Auto-eject always ejects on expiry: the command's own no-eject default (used by `prepare`/`repair-divergent` so the manual path leaves the drive attached for a following backup) is overridden once the flag is on, because a field named "auto-eject" must do what it says. Any keypress cancels the countdown and falls back to the normal blocking prompt, so a present operator always keeps control; with no TTY (cron/systemd) the drive is ejected immediately. This is implemented as `Log.ask_timeout` using `select.select` (no signal handlers, no threads). When `autoeject` is false or absent — the default for every existing drive — behaviour is unchanged. The `known_drives.json` schema (including this field) is now documented in the README.

#### Disconnect noise is not a health signal

The kernel always attempts a final cache flush at USB disconnect; on these bridges it routinely "fails". An A/B test on the same hardware showed the `hostbyte` — not the message — is what matters: under UAS with dirty data in flight it is `DID_ERROR` (the corrupting path); at clean unplug of an exported pool it is `DID_NO_CONNECT` (harmless "device gone"). zark therefore must **not** treat `Synchronize Cache failed` as a fault indicator — it appears on every clean disconnect. The authoritative signal is the read-back, which is exactly what Fix 2 implements. This reasoning is documented in `docs/HARDWARE.md`.

#### Documentation

New `docs/HARDWARE.md` records: the offending bridge `idVendor=0634 idProduct=5604`; both corruption modes (lost labels vs. `metaslab_init error 52`); the bogus shared-WWN trap; the `hostbyte` distinction at disconnect; the read-back mitigation; and the system-level `usb-storage quirks=0634:5604:u` quirk that forces the conservative transport (with the caveat that read-back runs unconditionally regardless, because a drive may be used on a machine without the quirk).

#### Rejected approaches and their flaws

- **Rewind recovery (`-F` / `-FX` / `-t <txg>`)** to save the corrupt pool: every uberblock referenced the same broken `metaslab_array`; there was no consistent transaction to return to. Confirmed dead end via `zdb -e`.
- **`zpool import -m`** (allow missing log device): the corruption was in the main vdev's spacemaps, not a separate log; failed identically.
- **`zpool scrub` as part of read-back verification:** correct in spirit but far too costly (hours on a 1.8 TB target) for an always-on post-backup step. A read-only re-import already surfaces structural corruption like `error=52` at open time, which is what we need. Left out; could be an opt-in flag later.
- **Relying on the UAS quirk alone** as the fix: the quirk is a system-level change outside zark's portable, zero-install scope, and does not protect a drive used on another machine. It is documented as a recommended mitigation, but the read-back is the in-tool guarantee.
- **Treating `Synchronize Cache failed` in `dmesg` as the failure signal:** it fires on every clean disconnect too (`DID_NO_CONNECT`); using it would produce a false positive on every successful backup. Discarded in favour of the read-back.

### `repair-boot` hardening — guaranteed clean export and automatic forced-import fallback

A kernel update applied in the background by `unattended-upgrades` while a backup drive was connected left the system unbootable: the `09_zfs_backup_guard` correctly aborted `update-grub` (external pool visible), but the kernel transaction had already swapped `/boot` contents, so `grub.cfg` ended up referencing a kernel that autoremove had pulled. Recovery with `repair-boot` then hit two latent bugs in the command itself.

#### Bug 1 — `repair-boot` never registered its `Cleanup`, so a mid-run fatal left the pools imported (and forced)

`commands/repair_boot.py` constructed a `Cleanup` but, unlike every other command, never called `cleanup.register()`. `Cleanup.run()` was only invoked explicitly at the final step. Any `log.fatal()` before that point — e.g. the import step failing — exited without exporting `rpool`/`bpool`. A pool left imported is marked in-use by the live host, so the next real boot's initramfs import (which carries no `-f`) failed: `repair-boot` could *manufacture* the very forced-import-at-boot problem it exists to fix. Now `cleanup.register()` runs immediately after construction, so `atexit`/`SIGTERM` guarantee a clean export on every exit path. A clean export is precisely what lets the next boot import without `-f`.

#### Bug 2 — the import lacked a `-f` fallback, so an unclean pool aborted the repair

Step 2 issued a raw `zpool import -R /mnt/repair -N <pool>` with no force fallback and `log.fatal()` on failure. But the operator reaches `repair-boot` *because* the system shut down uncleanly, which is exactly when a pool is left "in use" and a plain import fails — forcing the operator to know to run `zpool import -f` by hand outside zark. The import now routes through `ZFS.pool_import(pool, altroot=…, no_mount=True)`, which already tries a clean import first and only then `-f` (and is regression-tested). The forced state is transient: the guaranteed clean export from Bug 1's fix consumes it, so the next boot imports without `-f`. The end-of-run banner adds a note that a one-off forced-import prompt at the next boot is benign.

#### Bug 3 — tautological live-USB guard

The environment check read `Path("/").stat().st_dev == os.stat("/").st_dev` — comparing a value with itself, always true — and then tested `zfs get … mounted rpool/ROOT`, a container dataset that is never mounted. The guard never did anything. Replaced with `sh.is_live_usb()` plus an explicit confirmation prompt: if we are not on live media, warn and ask before continuing (pointing at `zark finish` for the installed system), rather than either silently proceeding or hard-aborting. The confirmation, not a hard refusal, avoids locking the operator out on exotic/remastered live media where casper markers are absent.

`is_live_usb()` is promoted to `lib/sh.py` as the single source of truth (routed through `run` so it is mock-testable) and `recover.py`'s private copy now delegates to it, removing the duplicated detection logic.

#### Rejected approaches

- **Adding a `--force` flag to `repair-boot`:** violates zark's "explicit confirmation, no `--force`/`--yes`" design, and is unnecessary — the clean-import-then-`-f` fallback inside `pool_import` already does the right thing without operator ceremony, and the guaranteed export neutralises the forced state.
- **Hard-aborting when not on live media** (the apparent intent of the old guard): rejected for the remastered-media false-negative; a confirmation prompt is safer and still steers the operator to `zark finish`.
- **Leaving `is_live_usb()` duplicated** in `recover` and `repair-boot`: rejected on the project's no-duplication principle; a single mock-testable helper in `lib/sh.py` is cheaper to keep correct.

### New `zark chroot` command + local-system mount/umount

The recovery toolkit could mount and chroot a *backup* pool (`zark mount` prints chroot instructions for the nested `<pool>/rpool`), but offered no first-class way to chroot the **installed system itself** from a live USB — the exact thing needed to fix a broken boot by hand (run `apt`, `update-grub`, `dpkg-reconfigure` inside the real system). Operators were left assembling `zpool import` / keystore-unlock / bind-mount / `chroot` by hand, getting the import-without-`-f` and clean-export details wrong in precisely the way that breaks the next boot.

#### `zark chroot [device]`

A new interactive command that, from a live session where `rpool`/`bpool` are not imported:

1. Imports both pools under an altroot (`/mnt/zark/chroot`) via `ZFS.pool_import`, inheriting the clean-then-`-f` fallback.
2. Unlocks the keystore (passphrase) and loads all dataset keys.
3. Mounts the boot environment and its children, then `bpool/BOOT/<be>` at `/boot`.
4. Sets up the bind mounts a chroot needs (`/proc`, `/sys`, `/dev`, `/dev/pts`, `/run`), `efivars`, and the ESP at `/boot/efi`.
5. Drops the operator into `/bin/bash --login` inside the system.
6. On shell exit (or Ctrl-C, or any fatal) tears everything down and **exports both pools cleanly**, so the next real boot imports without `-f`.

Safety: it refuses when `rpool` is already imported (you are either already inside the running system, or a previous run left it imported — the message points at `zark clean`), and warns+confirms when not on live media. The interactive shell is the one place zark deliberately does not route through `sh.run` (which captures output and would break a live TTY); it execs `chroot … /bin/bash` with the controlling terminal attached, via a fixed argument vector (no shell, no string interpolation).

#### `zark mount local` / `zark umount local`

The "generalised mount/umount" the operator asked for, kept deliberately light: `mount` and `umount` now accept an explicit `local` (alias `system`/`rpool`) target to operate on the installed system's pools rather than a removable backup drive. `zark mount local` imports + unlocks + mounts the system read-only (default) or read-write for inspection and leaves it mounted; `zark umount local` exports it again. The no-argument behaviour of both commands is unchanged (scan backup drives).

`umount local` carries the critical safety discriminator: zark always imports the system under an altroot beneath `/mnt/zark`, whereas the *running* system's `rpool` has `altroot = -`. `umount local` refuses to export any pool whose altroot is not under `/mnt/zark`, so it can never pull the root filesystem out from under a running machine.

#### Shared system-mount helper

The top-level `rpool`/`bpool` mounting logic (distinct from the nested backup-pool layout `MountedPool.mount_rpool` handles) is centralised in `lib/mount.py` as `mount_system_pools()` + `find_system_root_dataset()`, used by both `zark chroot` and `zark mount local`. It mounts via altroot + each dataset's *stored* mountpoint and **never runs `zfs set mountpoint`**, honouring the absolute rule against mountpoint changes while the keystore zvol is imported (`zfs mount <ds>` mounts `canmount=noauto` boot environments without touching properties). `repair-boot`'s own inline mount path is intentionally left as-is — it interleaves grub regeneration and is field-validated — rather than retrofitted in the same change that just hardened it.

#### Rejected approaches

- **Overloading `zark mount` to auto-detect the system pool** (no explicit `local` keyword): rejected — the default scan-backup-drives behaviour must stay predictable, and silently importing the system pool because no backup drive was found would be surprising and risky. An explicit keyword is clearer.
- **A single generic mount path for both backup (nested) and system (top-level) layouts:** rejected as premature unification; the keystore location, dataset hierarchy and root-dataset discovery differ enough that one branchy function would be harder to reason about than two focused ones.
- **Refusing `zark chroot` outright when not on live media:** rejected for the same reason as `repair-boot` — a confirmation prompt handles remastered media without locking the operator out — though chroot additionally hard-refuses when `rpool` is already imported, which unambiguously means the running system.

### Test-suite lint cleanup (pylint 10.00/10)

Pulled the `lib.health` imports in the health test classes up to module level and collapsed three identical inline `FakeZFS` helpers into one module-level `_FakeHealthZFS` (with a docstring). These were the last `import-outside-toplevel` (C0415) and `missing-class-docstring` (C0115) findings; with them gone the aggregate `pylint lib commands tests zark` is a clean 10.00/10. No behavioural change — test-only.

### New apt guard — block kernel/GRUB upgrades with an external pool attached

The boot-loss incident this release addresses had a root cause upstream of `repair-boot`: a background `unattended-upgrades` kernel upgrade ran *while a zark backup drive was connected*. The grub guard (`09_zfs_backup_guard`) correctly aborted the `update-grub` step, but by then the kernel package transaction had already swapped `/boot`, and autoremove had pulled the previous kernel — so `grub.cfg` ended up referencing a kernel that no longer existed. The grub guard fires too late in the package lifecycle to prevent this; it can only stop `grub.cfg` regeneration, not the kernel unpack that precedes it.

The new guard moves the defence earlier, to before dpkg unpacks anything.

#### Layer A — `DPkg::Pre-Install-Pkgs` hook (the load-bearing defence)

A new installer module `lib/apt_guard.py` (mirroring `lib/grub_guard.py` as the single source of truth) deposits three standalone files:

- `/usr/local/lib/zark/apt-zfs-backup-guard` — a POSIX `sh` hook. APT runs it before dpkg unpacks (Version 1 protocol: `.deb` paths on stdin); a non-zero exit aborts the entire transaction. The hook collects the boot-critical packages in the transaction first (so ordinary installs exit before paying for any device scan), and only then — if any `linux-image-*` / `linux-headers-*` / `linux-modules-*` / `grub-*` / `shim-*` / `zfs-*` / `zfsutils-*` package is present — checks for an external (non rpool/bpool) pool via `zpool import`. External pool + boot-critical package ⇒ refuse, with a message naming the pool(s) and the `zpool export` fix.
- `/etc/apt/apt.conf.d/09zark-zfs-backup-guard` — registers the hook.
- (Layer B, below.)

The hook is **standalone by design**: it detects pools with `zpool` directly and never calls `zark`. This matters because the recovered system runs without zark present — a zark-dependent hook would be inert exactly where the protection is needed. It also helps any ZFS-on-root system, not just zark installs.

A `ZARK_INTERNAL=1` environment escape lets zark's own flows that import the backup pool or run apt deliberately (`recover`, `finish`, `repair-boot`, and `setup` — whose dependency install must not be refused by a guard a previous setup installed) bypass the hook. It is set centrally in the dispatcher for those commands only; routine operations (`backup`, `mount`, `chroot`, `prepare`) are left unmarked so the guard still protects against an accidental upgrade while a drive is connected.

#### Layer B — login-time MOTD reminder (secondary defence)

`/etc/update-motd.d/99-zark-external-pool` prints a one-line warning at login when an external pool is visible, so an interactive operator sees that boot updates are currently blocked even outside an apt run. A full `wall`/`notify-send`-on-attach path (which would need a udev/systemd trigger and fragile graphical-session detection) was deliberately deferred — the apt hook is what actually closes the vector; the MOTD line is a cheap, reliable reminder.

#### Installation

`zark setup` installs the guard on the running productive system (this is the system most exposed to the `unattended-upgrades` vector); `recover` and `finish` install it into the recovered system (`finish` non-clobbering, like the grub guard). `install()` is idempotent: when the three files already hold the canonical content it writes nothing (no mtime churn), so re-running `zark setup` is a true no-op.

#### Rejected approaches

- **Relying on the grub guard alone:** rejected — it fires after the kernel unpack, so it cannot prevent the half-applied state; it can only refuse to regenerate `grub.cfg`, which is what produced the brick in the first place.
- **A `DPkg::Pre-Invoke` hook (no package list):** rejected — it would have to block *all* apt operations whenever a drive is attached, since it cannot see which packages are involved. `Pre-Install-Pkgs` lets the guard be surgical: only boot-critical operations are refused.
- **Requesting the Version 2/3 hook protocol:** rejected as unnecessary — the Version 1 `.deb` file list is all the hook needs, and keeps the parsing trivial.
- **Implementing the hook in Python and invoking `zark`:** rejected for the standalone reason above — the recovered system has no zark, and an apt hook must not depend on a binary that may be absent.
- **A `systemd`/udev `wall`+`notify-send` notice now (Layer B2):** deferred — added machinery and a fragile graphical-session lookup for marginal gain over the apt hook plus the MOTD line.

## [1.0.11] — 2026-05-21

### Safe USB disconnect — `sync` + sleep + interactive `eject` + visible banner

Hardware validation of v1.0.10 uncovered a class of pool-corruption events that was not zark's fault but happened because zark gave the operator no visible signal of when the underlying device had actually flushed. A backup to drive `black` completed with `[OK] Pool black exported`, the operator extracted the USB drive shortly after, and the pool came back FAULTED on next import — all four redundant labels reported `failed to unpack label 0/1/2/3`. Forensic `dmesg` trace at unplug:

```
[27338.563] usb 2-1: USB disconnect
[27338.582] sd 0:0:0:0: [sda] Synchronizing SCSI cache
[27338.748] sd 0:0:0:0: [sda] Synchronize Cache(10) failed: hostbyte=DID_ERROR
```

and on attach, the smoking gun:

```
[27277.040] sd 0:0:0:0: [sda] Write cache: enabled, read cache: enabled,
                              doesn't support DPO or FUA
```

ZFS issues writes with FUA (Force Unit Access) on critical metadata — uberblocks and the four redundant pool labels — meaning "do not acknowledge until this byte is on persistent media, not in volatile cache." Many cheap USB-SATA bridge chipsets (including the one in the Micron CT2000X10* enclosures used here) silently ignore FUA: they acknowledge from their internal DRAM buffer before the data has reached NAND. ZFS believes the write is committed and `zpool export` reports success while the bridge still holds dirty pages. The kernel emits a final `SYNCHRONIZE CACHE` SCSI command on USB disconnect, but if the cable is already out it fails with `DID_ERROR` and the pending writes — including possibly the labels — are lost. This is a well-known failure mode of ZFS on USB-SATA; zark is *for* portable backup on pendrive and must therefore make this safe.

v1.0.11 introduces a kernel-side flush at every point where zark exits with a pool exported, plus an **interactive eject prompt** with command-specific defaults so the typical operator workflow — including `prepare` immediately followed by `backup` on the same drive — is preserved.

#### Layer 1 — kernel-side flush, always

A new module-level helper `flush_device_cache(log)` in `lib/cleanup.py` performs:

1. **`sync(2)`** — kernel-side flush. Blocks until the kernel has emitted all dirty buffers to the device. Protects against losing the kernel's write-back cache.
2. **2-second pause** (`USB_FLUSH_DELAY_SEC`, module-level constant, patchable in tests). Gives the bridge firmware a window to drain its internal write queue.

This happens unconditionally after every successful `zpool export`, via `Cleanup.run()` for commands that go through `Cleanup` (`backup`, `recover`, `repair-divergent`, `repair-boot`) and via direct calls inline for the rest (`prepare`, `purge`, `umount`). Failures are non-fatal: by the time it runs, the on-disk data is already durable; what this layer protects is the operator's option to physically remove the drive a few seconds later without losing what the firmware has not yet confirmed.

#### Layer 2 — interactive eject prompt

A new module-level helper `eject_device(device, log)` issues SCSI `SYNCHRONIZE CACHE (0x35)` followed by `START STOP UNIT (stop=1)` via `eject(1)` (core `util-linux`, no new dependency). SYNCHRONIZE CACHE is the device's most authoritative flush primitive — the bridge sees it as a distinguished operation ("host is asking for a global flush right now") and most chipsets that cheat on inline FUA still honour it. STOP UNIT then powers the controller down so any cache concern is moot.

**The eject is never automatic.** A new `prompt_eject_or_attach()` helper asks the operator after the operation's success banner. The default Y/N reflects the typical workflow:

| Command            | Default eject | Rationale |
|--------------------|---------------|-----------|
| `backup`           | **yes**       | Typical end-of-session: one Enter unplugs. Operators rotating multiple backups answer `n` to keep the drive attached for the next run. |
| `umount`           | **yes**       | The operator already signalled intent to disconnect by running `umount`. |
| `purge`            | **yes**       | A purged drive is being retired or repurposed — disconnect is the next step. |
| `recover`          | **yes**       | The printed next-steps tell the operator to unplug the live USB and reboot — they're done with the backup drive too. |
| `prepare`          | **no**        | Canonical next step is `backup` against the freshly-prepared drive — auto-ejecting would force a pointless unplug/replug cycle. |
| `repair-divergent` | **no**        | Canonical next step is `backup` to validate the fix. |
| `repair-boot`      | n/a           | Touches only internal rpool/bpool on the system disk; no removable drive involved. No prompt. |

EOF on stdin (cron / systemd timer / scripted run) uses the default. There is no `--eject` / `--no-eject` flag — the prompt with a sensible default is the only knob, consistent with zark's "explicit confirmation by design" policy.

#### Two banners, distinct intent

- **Green `banner_safe_unplug(drive)`** — `💾 Safe to unplug drive 'X'`. Emitted *after* a successful eject. The unambiguous end-of-output signal that the operator may now disconnect.
- **Cyan `banner_drive_attached(drive)`** — `🔌 Drive 'X' flushed, still attached`. Emitted when the operator declined the eject. Tells them the kernel-side flush is done but the drive is *still in `/dev`* and a physical unplug now is not recommended — they should run `zark umount` (or accept the eject in a later command) first.

Operators must never see one banner when the other was meant — the colour, icon, and wording are deliberately disjoint.

#### Architecture: `Cleanup` is device-agnostic

The classifier "external vs. internal pool" used to live inside `Cleanup` in earlier drafts of this change. It was moved out: `Cleanup` now does only what its name says — exports pools, unmounts mounts, closes keystores — and always pairs a successful export with `flush_device_cache()`. The eject decision is the calling command's, because only the command knows what the operator is likely to do next. Rejected approaches and their flaws:

- **Auto-eject in `Cleanup`** (the test1 prototype): broke the `prepare → backup` flow by ejecting the drive between the two commands. Recoverable only by unplug/replug. Discarded.
- **`hdparm -W 0`** to disable write cache permanently: ~3× slower backups, behaviour varies across enclosures, and the setting persists.
- **`sg_sync` / `sdparm --command=sync`** to flush without STOP UNIT: would let the device stay attached after a flush, but neither is core on a minimal Ubuntu install. `eject` is in `util-linux` — always available — and the STOP UNIT side-effect is fine *because the operator just told us they were about to unplug*.
- **Detecting USB-only and applying different code**: the FUA-broken behaviour is a property of misbehaving firmware, not USB specifically. The protection is unconditional.

#### Tests

Twenty new test cases in `tests/test_unit.py` across three classes: `TestCleanup` (export/sync/forced-export ordering, no-eject-from-Cleanup invariant, no-flush-on-failed-export, `exported_pools()` query API), `TestFlushAndEjectHelpers` (sync issued, never ejects, sync failure handling, eject_device return contract), `TestPromptEjectOrAttach` (yes/no branches emit the correct banner, default-on-EOF for both `True` and `False` cases, unresolvable device falls through to attached banner without prompting), plus the `banner_safe_unplug` / `banner_drive_attached` assertions in `TestLog`. `USB_FLUSH_DELAY_SEC` is a module-level constant precisely so tests can patch it to 0; the full unit suite (299 tests) runs in under 2 seconds.

#### Operational notes

- After answering "yes" to the eject prompt, the bridge is powered down. The drive **disappears from `/dev`** until physically replugged. This is intentional — the workflow is "we're done, disconnect" — but worth knowing if a follow-up command needs the same drive without replug.
- After answering "no", the drive stays in `/dev`, the kernel-side flush has run, and the next zark command against the drive will work normally and prompt again at its end.

## [1.0.10] — 2026-05-12
---

## [1.0.9] — 2026-05-12

### Snapshot retention — `template_minimal` raised to ~3 months

The original `template_minimal` policy in `zark setup` retained `daily=2` (two days, no weekly, no monthly) for `rpool`, `rpool/ROOT`, `rpool/var`, and `bpool` — every "container" dataset that barely changes. This was incompatible with any realistic drive-rotation schedule: a backup drive disconnected for more than 48 hours would lose its anchor in source and diverge on every container dataset, leaving the leaves (`rpool/ROOT/<ubuntu>`, `rpool/USERDATA`, all `production`-template) intact while syncoid aborted with `Cowardly refusing to destroy your existing target` on the rest.

- **`template_minimal` is now `daily=14 weekly=8 monthly=3`.** ~3 months of guaranteed overlap, ~25 snapshots per dataset. Container datasets weigh KB to a few MB so the metadata cost of the extra snapshots is negligible. `template_production` is unchanged — it already had ~3 months of headroom on the active leaves.
- **`zark setup` detects the migration.** The existing diff machinery now compares `[template_minimal]` against the expected values. When they disagree, the diff is shown alongside any other rule changes through the same "Apply these changes?" prompt — no separate flow. Saying "no" leaves the file untouched and re-running `zark setup` will offer the migration again (the choice is not persisted, by design — every setup run gets a fresh chance). Saying "yes" regenerates `sanoid.conf` with `.backup.<ts>` written first, preserving manual sections the user has added under non-managed prefixes (e.g. `[tank/games]`).

### `--no-sync-snap` for syncoid: end of the rotation-warning cascade

When a user rotated between two backup drives, syncoid emitted a long cascade of warnings on the second-to-second-drive run:

```
could not find any snapshots to destroy; check snapshot names.
WARNING:  zfs destroy 'blue/rpool'@syncoid_host_<ts> failed: 256 at /usr/sbin/syncoid line 1596.
```

Repeated for every dataset, every alternation, every backup. The replication itself worked, but the operator was buried in apparent errors. Tracked down to the [`pruneoldsyncsnaps` mechanism in syncoid](https://discourse.practicalzfs.com/t/managing-syncoid-snapshots-with-multiple-destinations/4452): syncoid creates `@syncoid_<host>_<ts>` snapshots on source/target before each transfer and tries to garbage-collect older ones afterwards. With multiple destinations, the run to drive B destroys the source snapshot that drive A still needs as its anchor — the next run to drive A then warns loudly when the matching target snapshot has nothing to delete.

- **`zark backup` now passes `--no-sync-snap` to syncoid for both rpool and bpool transfers.** Syncoid no longer creates its own anchor snapshots; instead it picks the most recent existing snapshot in source as the anchor. Step 6 already takes a fresh `sanoid --take-snapshots` pass before each backup, so a clean anchor is guaranteed.
- **No filtering of stderr; the bug is fixed at the source.** Future warnings from syncoid (real ones) keep showing up unmuted.
- **`@syncoid_host_*` snapshots that already exist on drives are left in place.** They're inert — syncoid no longer references or recreates them. They die naturally when the drive is purged.
- **`--no-snapshot` + missing anchor surfaces a WARN.** When the operator passes `--no-snapshot`, `zark backup` no longer takes a fresh sanoid pass. If sanoid hasn't run independently, source has no recent anchor; the WARN tells the operator that syncoid may abort, but the run continues so syncoid's own (more authoritative) error wins if it does fail.

### Drive staleness reporting

A new `last_backup_at` field per drive in `etc/known_drives.json` records the ISO-8601 UTC timestamp of every successful backup. Reporting is purely informative — there is **no FATAL** based on staleness, because the actual divergence threshold depends on sanoid retention (which the operator can change) and a backup that has crossed the threshold may still succeed if some shared snapshot happens to remain. When syncoid does abort, the existing divergence handling in `lib/repair.py` and `commands/repair_divergent.py` already takes over.

- **Retention horizon is read at runtime from `/etc/sanoid/sanoid.conf`.** New `lib/sanoid_retention.py` parses the file and returns `worst_case_retention_days = max(daily, weekly×7, monthly×30)` over templates actually used by `[rpool*]` or `[bpool*]` sections. Adding the buckets together would double-count: the monthly snapshot, taken once per month, replaces older daily/weekly snapshots once they age past their bucket — so the longest single bucket defines the horizon.
- **End-of-backup banner gains two informative messages.** After `BACKUP COMPLETED`, if the selected drive was already past the retention horizon when the run started, a WARN explains the situation and points at `sudo ./zark purge && sudo ./zark prepare` (the only thing that actually fixes a fully-aged drive) plus an explicit note that `zark repair-divergent` does **not** fix staleness — it only fixes divergent datasets after a syncoid abort, which is a different problem. Below, an INFO list shows other known drives whose age has reached the danger zone (`≥ retention - 30` days).
- **`zark repair-divergent` shows the same staleness note** at the end of a run that found nothing divergent but selected a drive in the danger zone — the operator who came expecting a fix is told why this command can't help.
- **The field auto-populates on first successful backup.** Existing `known_drives.json` files keep parsing without modification; the field is emitted on disk only when populated, keeping the JSON minimal. Failure to persist the timestamp at the end of a backup is a WARN, not fatal — the backup itself succeeded.
- **No FATAL gate on staleness.** Earlier in development the design called for a 60-day FATAL guard. Discarded after testing because the actual horizon is whatever sanoid retains (90+ days with the new `template_minimal`), and bailing out before syncoid loses information that syncoid itself would surface clearly anyway.

### `zark repair-divergent` — interactive review

The previous `repair-divergent` only handled the "all divergent datasets are ≤ 64 MB" case (auto-destroyed silently) and aborted with FATAL on anything larger, telling the user to "use `zfs destroy` directly." That's the right safety posture for a non-interactive flow but leaves the operator with no zark-supported way to repair real divergence. The command is now a guided review:

- **Per-dataset context block.** For every divergent dataset above 64 MB, prints `used`, snapshot count and the date range on the target, the most recent snapshot suffix shared with source (or `none`), child-dataset count and aggregated `used`, plus a one-line hint classifying the pattern (orphan, container with intact leaves, leaf rotated past target).
- **Three-choice action prompt: destroy, skip, or abort all.** Default is `skip` — the only fully reversible choice. `abort` walks out without touching anything else, preserving the rest for a later session.
- **Typed `DESTROY` confirmation above 1 GiB.** When the operator picks `destroy` on a dataset over 1 GiB (`DOUBLE_CONFIRM_BYTES`), a second prompt requires typing the literal string `DESTROY` (case-sensitive). Anything else cancels the destroy. The threshold is hardcoded by explicit user request — no flag.
- **Mid-flight failure policy, asked once.** When a `zfs destroy` fails (busy zvol, lock contention), the operator is asked once how to handle the rest of the run: `continue`, `abort`, or `keep state and abort` (the third stops immediately and leaves the pool exactly as it is, no further cleanup-driven changes). The choice is sticky for the rest of the session — subsequent failures don't reprompt.
- **Final summary banner** lists the destroyed and skipped datasets and points at `sudo ./zark backup` (which recreates destroyed datasets via initial replication) plus a re-run hint when anything was skipped.

The 64 MB silent threshold matches `zark backup`'s auto-repair path so the two flows stay symmetric.

### Documentation

- **README: new "Drive rotation and retention policy" section** between Requirements and Testing. Explains the divergence mechanic, the two retention templates with their windows, the staleness reporting model, and the new interactive `repair-divergent`.
- **README: Apport popup screenshot** added inline at the top of the existing "System program problem detected" troubleshooting section. The image lives at `docs/images/apport-popup.png` (a new tree under the project root, not shipped in the deb).

### Process plumbing

- **`lib/sanoid_retention.py`** (new module) parses `/etc/sanoid/sanoid.conf` with the standard `configparser` and computes the worst-case retention horizon over templates referenced by managed sections. Returns `None` and emits a visible WARN when the file is absent — backup itself does not require sanoid.conf, so staleness reporting becomes unavailable rather than blocking.
- **`lib.config.now_utc_iso()` and `parse_utc_iso()`.** Single source of truth for the timestamp written to `last_backup_at`, including parse tolerance for both `Z`-suffix and explicit `+00:00`. Tests can monkeypatch the clock without reaching into a command module.
- **`DriveInfo.last_backup_at: str | None = None`.** Added field with a `None` default so existing call sites that construct `DriveInfo` keep working unchanged.
- **`drive_staleness_days(info)`, `is_drive_stale(info, threshold_days)`, `drives_in_danger_zone(...)`** in `lib.drives` wrap the timestamp arithmetic and the danger-zone selection. All accept an optional `now` argument so tests can pass a fixed clock.

### Tests

- **281 unit tests**, ruff clean, pylint 10.00/10. New coverage:
  - `TestConfigTimestamps` (5 tests): now_utc_iso shape, parse round-trip, Z and +00:00 suffix handling, garbage input.
  - `TestConfigKnownDrivesTimestamp` (5 tests): legacy file load, modern file with field, save omits None, save preserves, ignores non-string field.
  - `TestDrivesStaleness` (7 tests): None when absent or malformed, integer-day delta when present, threshold boundary at exactly N days (not stale), beyond N (stale), False when field absent.
  - `TestDrivesInDangerZone` (6 tests): empty, skip without field, include at-or-beyond threshold, exclude below threshold, exclude named drive, sort age-desc.
  - `TestSetupTemplateDiff` (5 tests): no-diff when matches, diff with old values, missing section, expected constants match generator, _print_diff includes template section.
  - `TestBackupStalenessReporting` (5 tests): silent when retention unknown, WARN when expired at start, no warn when fresh, lists other drives in danger zone, excludes just-backed-up drive.
  - `TestSanoidRetention` (6 tests): per-template horizon math, returns largest used template, smaller-when-only-minimal-used, None when file missing, None when no managed sections, ignores zvol sections without use_template.
  - `TestRepairDivergentHints` (3 tests), `TestRepairDivergentDoubleConfirm` (6), `TestRepairDivergentActionPrompt` (7), `TestRepairDivergentLoop` (7), `TestRepairDivergentSnapshotHelpers` (4): full coverage of interactive flow.

[1.0.9]: #109--2026-05-12

## [1.0.8] — 2026-05-08

### Out-of-space handling — backup

Two complementary safeguards for `zark backup`. The preventive guard avoids burning operator time on a backup we can prove will fail; the reactive handler catches the cases the preventive guard misses (the backup grew during the window, the threshold was tuned wrong, etc.) and turns them into clean fatal errors instead of silent fall-through.

- **Preventive ENOSPC guard.** Before kicking off `syncoid`, backup now compares `target.avail_bytes` against a threshold of `max(source.used_bytes / 100, 1 GiB)`. Below that, fatal with cause/solution lines (purge or larger drive). The 1% / 1 GiB choice is deliberately lax: it only fires on a target that is essentially full, where any incremental is mathematically guaranteed to ENOSPC mid-stream. A separate coherence check warns (does not fatal) when the target pool is physically smaller than the source pool — eventually fatal but the operator may know what they're doing (testing on a small loop, compression headroom, etc.).
- **Reactive ENOSPC detection in three call sites.** Each `syncoid` invocation in `backup` (rpool, bpool incremental, bpool from-scratch resync after `no snapshots matching`) now checks the captured stdout/stderr for ENOSPC markers and emits a fatal with ENOSPC-specific cause/solution lines. The order matters: the rpool check runs **before** the existing "syncoid had warnings but transferred N datasets" branch, otherwise an ENOSPC after the first dataset fell through as "partial success" and continued into the bpool sync, which would also ENOSPC, producing a confusing chain of generic warnings instead of one clear fatal at the source. bpool ENOSPC deliberately does **not** auto-recreate (the previous `no snapshots matching` path destroys the target bpool and re-sends from scratch); destroying a stale bpool when the drive is full would leave the user with no boot backup at all.

### Out-of-space handling — recover

Mirrors the backup story but adapted to recovery's invariants (target NVMe is wiped and re-partitioned, capacity comparison is against a physical disk rather than a pool).

- **Preventive disk-size check.** Before the `YES` confirmation prompt, `recover` measures the target disk via `lsblk -bdn -o SIZE` and compares against `backup.rpool.used_bytes × 1.05` (5% overhead margin for ESP, bpool, swap, ZFS metadata). Below that, fatal — the operator is not allowed to commit to wiping a disk we already know is too small. Silently deferred to the reactive handler if either size measurement returns 0 (transient `lsblk` or `zfs list` failure), so a probe glitch doesn't block a recovery that would otherwise succeed.
- **Reactive ENOSPC detection at every send/receive site.** `_raw_send` (every dataset except keystore), `_bpool_send_recv` (bpool send/receive), and the keystore final send each check stderr/stdout for ENOSPC markers and fatal with a recovery-specific message. Unlike backup, recover has no "stale data is better than nothing" tradeoff — partial recovery is unbootable, so every ENOSPC is fatal regardless of which dataset hit it. The `_raw_send` path preserves its non-ENOSPC `return False` for callers that intentionally tolerate individual dataset failures (`_ = _raw_send(...)` at the rpool/ROOT/USERDATA loops).

### Process plumbing

- **`lib.sh.run_pipe()` now reports a non-zero return code if either side of the pipeline failed.** The previous implementation returned only `p2.returncode`, which silently hid `p1` failures: `zfs send` of a deleted snapshot followed by a clean EOF on `zfs receive` was reported as success, the receiving dataset was incomplete, and downstream code proceeded as if the transfer had worked. The fix waits explicitly for `p1`, captures both stderr streams, concatenates them so callers (and `is_enospc()`) see the combined error output, and picks the most informative non-zero return code (`p2` takes precedence when both fail, since downstream errors are usually what the caller cares about). The specific case of ENOSPC during `zfs send | zfs receive` was already detected correctly even before this change — `zfs receive` itself emits ENOSPC and exits non-zero — so this is an improvement for the *other* failure modes that were hidden, not a fix to ENOSPC handling.
- **New `lib.sh.is_enospc(text)` helper** with case-insensitive match against four real-world markers observed on Ubuntu 24.04 / 25.04 / 25.10: `"no space left on device"`, `"out of space"`, `"enospc"`, `"disk quota exceeded"`. The last is technically `EDQUOT`, not `ENOSPC`, but the user-visible remediation is the same and treating it as ENOSPC avoids falling through to a generic "syncoid failed" message on quota-bound storage.
- **New `lib.sh.humanize_bytes(n)` helper** for IEC-formatted sizes (`512B`, `4.0M`, `1.9T`). Used in ENOSPC fatal messages so the operator sees both the human size from `zfs list` and the numeric byte count from the threshold computation in coherent units.
- **New `lib.zfs.dataset_used_bytes(name)`** wrapping `zfs list -H -p -o used`. Numeric companion to the existing `PoolInfo.size_bytes` (also new this release), used by recover's preventive guard to compare against the physical target disk in bytes.

### Backup — fresh snapshots before every replication

- **`zark backup` now invokes `sanoid --take-snapshots` automatically before running `syncoid`.** Previously, backup relied entirely on the `sanoid` systemd timer to populate the source pool with snapshots — typically hourly. That meant an interactive `zark backup` could be replicating state that is up to ~1 hour old. Since taking a snapshot is cheap (seconds, no I/O on the backup drive, idempotent within sanoid's retention windows), backup now takes a fresh pass itself before each replication. Result: the backup drive always holds the most current state of the source pool.
- **`--no-snapshot` flag** as escape hatch. Skips the sanoid stage and replicates whatever snapshots already exist. Realistic uses are narrow (re-running after a transient failure when fresh snapshots aren't wanted, or for scripts that have already triggered sanoid by other means) but it lets explicit-intent invocations declare their intent in one place.
- **A failure of `sanoid --take-snapshots` is non-fatal.** Sanoid sometimes emits warnings about a few datasets while still snapshotting the rest, and we'd rather proceed with backup (replicating slightly-stale state if necessary) than abort because of a noisy edge case. The operator sees the warning either way.
- **Backup is now a 9-step process** (was 8). The new step 6 ("Taking fresh snapshots before backup") sits between "Loading encryption key" and "Running syncoid". When `--no-snapshot` is passed, the step still appears in the log with an `--no-snapshot: replicating existing snapshots` line, so the step counter stays consistent.

### Simulate

- **GL acceleration auto-detect.** `zark simulate` now probes the host for `/dev/dri/renderD*` and for `virtio-vga-gl` in `qemu-system-x86_64 -device help`. When both are present, QEMU is launched with `-device virtio-vga-gl,edid=on,xres=W,yres=H -display gtk,gl=on`, giving the guest a real virtio-gpu DRM device, a synthetic EDID advertising the requested resolution, and an OpenGL-scaled GTK framebuffer that resizes with the window. When either probe fails, falls back to `virtio-vga + edid + plain gtk` with a warning that names the missing capability. Auto-detection (no flag) was chosen over an explicit `--gl` opt-in to keep the CLI uncluttered: there is no legitimate use case for the slower path when GL is available.
- **`--display WxH` flag.** Lets the operator override the default 2560×1440 EDID-advertised resolution. Validated with positive bounds checks (≤8K) and a `WxH` parse with both lower- and upper-case `x`. Combinable with the existing `disk` and `--rw` arguments in any order. Default tuned for the typical zark-on-Ubuntu hardware (4K-class developer machine); on a 1366×768 laptop the QEMU GTK frontend scales/scrolls the window so it doesn't overflow.

### Tests

- **214 unit tests**, ruff clean, pylint 10.00/10. New coverage:
  - `TestShRunPipe` (5 tests): both-succeed, p2-fail, p1-fail-with-clean-p2, stderr combination, p2-precedence-when-both-fail.
  - `TestShIsEnospc` (7 tests): empty input, unrelated error, each of the four markers individually, mixed stderr from a piped invocation.
  - `TestShHumanizeBytes` (8 tests): zero, raw bytes (no decimal), KiB boundary at 1024, KiB/MiB/GiB/TiB ranges, negative values.
  - `TestZfsDatasetUsedBytes` (3 tests): ok, lsblk-style failure, unparseable output.
  - `TestBackupCheckTargetSpace` (8 tests): plenty-of-space, below 1 GiB floor, below 1% of source, just above threshold, smaller-target warn, three "silent skip" paths (None src/dst, zero used_bytes).
  - `TestBackupParseArgs` (3 tests): default takes snapshots, `--no-snapshot` disables, unknown flags ignored.
  - `TestRecoverDiskSize` (9 tests): three for `_disk_size_bytes`, six for `_check_target_disk_size` covering pass/fatal/silent/boundary just-above and just-below.
  - `TestSimulateArgs` extended for `--display`: bad format, zero dimension, exceeds 8K, default when flag absent, combined with disk + --rw, both `x` and `X` separators.
- **New `make test-enospc-real` target** (`tests/test_enospc_real.sh`). Empirical calibration of `lib.sh._ENOSPC_MARKERS` against the actual ZFS / kernel installed on the machine: creates two PID-tagged loop-backed pools with `cachefile=none`, provokes a real out-of-space `zfs send | zfs receive`, captures stderr, and verifies that `is_enospc()` would catch it. Opt-in (root-only, not invoked by `make fulltest`, not in CI). Run after upgrading `zfsutils-linux` or moving to a new distro / kernel.

[1.0.8]: #108--2026-05-08

## [1.0.7] — 2026-05-06

### Backup model

- **Removed `--use-hold` from all syncoid invocations.** Earlier iterations experimented with ZFS holds to keep the syncoid anchor snapshot alive across long backup gaps, but holds retain blocks on the *source* pool until manually released — meaning a forgotten backup drive could silently keep tens of GB unreclaimable on `rpool` for months. The new model is simpler and honest: source retention is governed entirely by sanoid policy; the destination accumulates whatever it receives. If a drive sits offline longer than the source policy retains (~3 months on `production`), the next backup auto-resolves the resulting divergence.
- **`zark backup` now auto-repairs divergent datasets under 64 MB silently.** When syncoid aborts with `Cowardly refusing to destroy your existing target`, backup invokes the same logic as `repair-divergent` for any dataset under 64 MB (the threshold syncoid itself uses to flag "did you mistakenly run zfs create on the target?") and re-runs syncoid. Anything larger aborts with a clear pointer to interactive `zark repair-divergent` — no user data ever destroyed silently.
- **`bpool/BOOT` snapshot policy moved from `minimal` (2 days) to `production` (~3 months).** Kernel and initramfs history deserves the same rollback horizon as user data; ZFS deduplicates the extra hourly snapshots since `/boot` doesn't change between hours.

### Secure Boot

- **`zark recover` now pins the `.latest` signed shim/grub variant before reinstalling.** Subiquity has been observed leaving Ubuntu installations pointing at the older `.signed.previous` shim (15.4-0ubuntu9), which gets revoked by the next SBAT level update from `fwupd`, leaving the system unbootable with `Verifying shim SBAT data failed: Security Policy Violation`. Recover now switches the alternative to `.latest` before `dpkg-reconfigure`, so the recovered system uses the up-to-date binary regardless of what the original installation chose. No-op on releases that don't ship the `.latest`/`.previous` split.
- **`zark setup` adds a Secure Boot pre-check (step 5).** Inspects the live system's alternatives for both shim and grub signed binaries; if either points to anything other than `.latest`, prompts (default NO — boot chain changes warrant explicit consent) before switching and re-running the postinst. Helps the user fix the subiquity bug before it propagates to backups.
- **bpool features pruned to GRUB-compatible subset.** Earlier versions of zark activated `head_errlog` and `vdev_zaps_v2` on bpool for "Ubuntu 25.04+ dracut systems," based on the assumption that dracut implied a more capable GRUB. This was wrong: GRUB2's bundled ZFS reader (used during boot to load the kernel) does not support either feature even in GRUB 2.14, the version shipped in Ubuntu 26.04. With those features active, GRUB rejects bpool as unreadable and boot fails with `file '/BOOT/.../vmlinuz-...' not found` followed by `prohibited by secure boot policy` for every menu entry. Existing recovered systems hit by this need a fresh `zark recover` with v1.0.7+ (features cannot be disabled once active). The recover code now activates only features documented in `/usr/share/zfs/compatibility.d/grub2` on bpool. rpool is unaffected — it's read by the full ZFS kernel module and continues to use whatever features the running ZFS supports.
- **`fix_grub_bpool_uuid` now rewrites every `--fs-uuid` reference, including those with `--hint-*` options.** The previous regex required only whitespace between `--set=<name>` and the 16-hex UUID, which silently skipped Ubuntu's standard menu-entry lines (`search --fs-uuid --set=root --hint-bios=... --hint-efi=... --hint-baremetal=... <UUID>` — the only ones that actually run on grub 2.12+ when `feature_platform_search_hint` is set). The result was that the `if`-branch of every menu entry kept the source machine's stale UUID after recovery, while only the `else`-branch got rewritten. This bug was invisible whenever the recovered disk happened to match the source's BIOS index (e.g. always recovering the same physical machine), but produced `error: no such device: <old-uuid>` followed by `disk 'hdN,gptN' not found` and `you need to load the kernel first` on cross-host recovery (different machine, different drive enumeration). The new regex is permissive about what may appear between `--fs-uuid` and the UUID.
- **README troubleshooting section** documents two recurring scenarios users may hit: (1) the harmless "System program problem detected" Apport popup that appears during disk-intensive operations on the live USB (caused by unrelated GNOME/udev daemons reacting to rapid disk activity), and (2) the SBAT rescue procedure for users hit by the shim revocation issue with a pre-1.0.7 recover (boot live USB → temporarily disable Secure Boot → switch alternatives → reboot with Secure Boot back on).

### Refactor

- Moved divergence detection (`find_divergent`, `auto_repair_under_64mb`, `is_divergence_error`) from `commands/repair_divergent.py` to a new `lib/repair.py`, shared between `backup` (silent path) and `repair-divergent` (interactive path).
- Removed `lib.zfs.release_all_holds` and `lib.zfs.has_syncoid_holds` (no longer needed; both were broken anyway because `zfs holds -r <dataset>` only accepts snapshots).
- Renamed syncoid flag `--exclude` to `--exclude-datasets` *automatically* per installed syncoid version. Initially we hard-coded `--exclude-datasets` in test15, assuming Ubuntu's `2.2.0-2` package backported the rename. It didn't: only sanoid `2.3.0` upstream knows `--exclude-datasets`, and on Ubuntu that version is 26.04+ only. On 22.04, 24.04, 25.04, and 25.10 (all sanoid `2.1.0` or `2.2.0-2`), `--exclude-datasets` aborted syncoid with "Unknown option" and left the destination pool with keystore + bpool but **no rpool** — a silently broken backup. The new `syncoid_exclude_flag()` helper inspects `syncoid --help` at runtime and picks `--exclude-datasets` (2.3+) or `--exclude` (2.2-) accordingly. Both `prepare` and `backup` use it.

### Tests

- 147 unit tests, ruff clean, pylint 10.00/10.

### Breaking change

- Pools backed up with v1.0.6 or earlier may have leftover `syncoid_host_*` snapshots in source `rpool` and `bpool` (with no useful function). Cleanup is optional and one-shot:
  ```
  sudo zfs list -H -o name -t snapshot -r rpool | grep '@syncoid_host_' | xargs -r -n1 sudo zfs destroy
  sudo zfs list -H -o name -t snapshot -r bpool | grep '@syncoid_host_' | xargs -r -n1 sudo zfs destroy
  ```

[1.0.7]: #107--2026-05-06

## [1.0.6] — 2026-05-03

### Distribution

- Added a `zark(1)` manpage. The source lives at `debian/zark.1.md` (Markdown) and is converted to roff at build time via `pandoc + sed + awk` in `debian/rules` — the same pipeline is exposed as `make manpage` / `make manpage-view` / `make manpage-clean` for local iteration. Output passes `mandoc -Tlint` and `groff -ww -z` cleanly.
- Fixed `dpkg-source: error: aborting due to unexpected upstream changes` triggered when local tool caches (`.mypy_cache/`, `.ruff_cache/`, `.tox/`, ...) lived in the working tree but not in the `.orig.tar.gz`. Added `debian/source/options` with `tar-ignore` + `extend-diff-ignore` covering all such paths; kept in sync with the `find ... -delete` invocations in `make dist`.
- Fixed `lintian W: no-debian-changes`: `make dist` was bundling `debian/` inside the `.orig.tar.gz`, violating `3.0 (quilt)`'s upstream/packaging split. The dist target now strips `debian/` when assembling the upstream slice; the resulting tarball is upstream-pure (≈118 KB, down from ≈130 KB).
- Made `lib/config.py` (`VERSION`) the single source of truth for the version string everywhere it can be substituted at build time. The manpage source carries `@VERSION@` in its header, replaced by both `make manpage` and `debian/rules` before pandoc runs (substituting *after* would have to match pandoc's `\[at]` escape — fragile). The `bug_report.yml` issue template placeholder and the version examples in `debian/README.packaging.md` were moved to a generic `X.Y.Z` form so they no longer rot. Net effect: bumping a release now means editing `lib/config.py` + the changelogs only.

[1.0.6]: #106--2026-05-03

## [1.0.5] — 2026-05-01

### Security

- `recover` now aborts when the backup is missing its keystore zvol, instead of silently producing a system that boots into an emergency shell.
- The two unsafe fallbacks (embedding the raw key in the rootfs, switching `rpool` to `keylocation=prompt`) were considered and rejected for silently degrading the security model the user set up.

### Tooling

- Added `tox.ini` (py3.12/3.13/3.14 + lint env), with mypy/pylint/ruff versions declared in `pyproject.toml` under `[dependency-groups].dev` (PEP 735) as a single source of truth.
- Added Ruff's `RUF027` rule to pre-commit to catch stripped `f`-string prefixes — the v1.0.4 root cause.
- Added GitHub Actions CI: tests matrix (py3.12/3.13/3.14, fail-fast off) and a lint job; integration tests (QEMU) deliberately remain local.

### Distribution

- Added Debian/Ubuntu packaging (`debian/`, `3.0 (quilt)`) installing under `/usr/share/zark/` with `/usr/bin/zark` as a symlink — `lib/config.py`'s existing path search handles the layout without code changes.
- Added `make deb` / `make deb-source` / `make deb-ppa` driving `debuild`, with PPA uploads iterating over Ubuntu 24.04 → 26.04; see `debian/README.packaging.md` for the maintainer flow.
- Added a CI `package` job gated on `refs/tags/v*` that builds an unsigned `.deb` and attaches it to the GitHub Release; signed PPA upload stays local.

[1.0.5]: #105--2026-05-01

## [1.0.4] — 2026-04-30

Initial public release of zark under its current name.

zark is a portable Python-based bare-metal ZFS backup and recovery suite
for Ubuntu systems with full-disk encryption, UEFI Secure Boot, and
zero-installation portability (runs from a USB pendrive).

For the design philosophy, command set, and supported environments, see
the project README.

[1.0.4]: #104--2026-04-30
