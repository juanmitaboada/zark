# Changelog

All notable changes to **zark** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- Renamed syncoid flag `--exclude` to `--exclude-datasets` (the older form is deprecated upstream and emits a warning on every run). Available in sanoid ≥ 2.2.0, present in Ubuntu 24.04 and later, which is the supported range.

### Tests

- 137 unit tests, ruff clean, pylint 10.00/10.

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
