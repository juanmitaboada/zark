# Changelog

All notable changes to **zark** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- Pools backed up with v1.0.6 or earlier may have leftover `syncoid_carmen_*` snapshots in source `rpool` and `bpool` (with no useful function). Cleanup is optional and one-shot:
  ```
  sudo zfs list -H -o name -t snapshot -r rpool | grep '@syncoid_carmen_' | xargs -r -n1 sudo zfs destroy
  sudo zfs list -H -o name -t snapshot -r bpool | grep '@syncoid_carmen_' | xargs -r -n1 sudo zfs destroy
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
