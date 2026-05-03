# Changelog

All notable changes to **zark** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
