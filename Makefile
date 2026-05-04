.PHONY: help \
        test test-real test-deps test-all \
        test-phase1 test-phase2 test-phase3 test-cleanup \
        setup clean version \
        check mypy pylint ruff ruff-strict format pre-commit lint tox \
        manpage manpage-view manpage-lint manpage-clean \
        dist dist-check fulltest fulltest-clean deb deb-source deb-ppa deb-ppa-test deb-ppa-resume

SHELL  := /bin/bash
ZARK   := ./zark
PYTHON := python3
TOX    := tox

# Resolve version from the single source of truth (lib/config.py).
# Used by `make dist` to build a properly-named tarball.
VERSION := $(shell $(PYTHON) -c "from lib.config import VERSION; print(VERSION)")

# Lintian tags suppressed during local builds. Kept centralised so the
# rationale lives in one place and `make deb` / `make deb-source` stay
# in sync.
#
# bad-distribution-in-changes-file:
#   debian/changelog uses "unstable" as the base distribution — the
#   Debian convention for "no specific series, will be retargeted at
#   upload time". `make deb-ppa` rewrites it to a concrete Ubuntu
#   series (noble/oracular/...) per upload, so the actual files going
#   to Launchpad never carry "unstable". Local `make deb` / `make
#   deb-source` runs do produce .changes files with Distribution:
#   unstable, which lintian (Ubuntu) flags as not-a-valid-Ubuntu-series.
#   That is by design; the override silences the noise locally without
#   masking real distribution typos at PPA upload time (deb-ppa runs
#   debuild without this flag).
LINTIAN_SUPPRESS_TAGS := bad-distribution-in-changes-file

# ── Help ─────────────────────────────────────────────────────────────────

help: ## Show this help
	@echo ""
	@echo "  zark — Full bare-metal ZFS recovery for Ubuntu Encrypted ZFS"
	@echo "  v$(VERSION)"
	@echo ""
	@echo "  Usage:  make <target>"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "    \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ── Tests ────────────────────────────────────────────────────────────────

test: ## Run unit tests (no root, no ZFS needed)
	$(PYTHON) tests/test_unit.py

test-deps: ## Install integration test dependencies (qemu, ovmf, genisoimage)
	@if [ "$$(id -u)" -ne 0 ]; then \
		echo "  test-deps installs system packages — run with sudo:"; \
		echo "    sudo make test-deps"; \
		exit 1; \
	fi
	apt install -y qemu-system-x86 ovmf genisoimage

test-real: ## Run full integration test in QEMU (all 3 phases)
	@test -n "$(ISO)" || { echo "  Usage: sudo make test-real ISO=/path/to/ubuntu.iso"; exit 1; }
	$(PYTHON) tests/test_integration.py --iso $(ISO)

test-phase1: ## Run Phase 1 only (create encrypted ZFS test system + backup)
	@test -n "$(ISO)" || { echo "  Usage: sudo make test-phase1 ISO=/path/to/ubuntu.iso"; exit 1; }
	$(PYTHON) tests/test_integration.py --phase 1 --iso $(ISO)

test-phase2: ## Run Phase 2 only (recover from existing backup.img)
	@test -n "$(ISO)" || { echo "  Usage: sudo make test-phase2 ISO=/path/to/ubuntu.iso"; exit 1; }
	$(PYTHON) tests/test_integration.py --phase 2 --iso $(ISO)

test-phase3: ## Run Phase 3 only (boot recovered target.img — no ISO needed)
	$(PYTHON) tests/test_integration.py --phase 3

test-cleanup: ## Remove all integration test artifacts (source/backup/target images)
	$(PYTHON) tests/test_integration.py --cleanup

test-all: test check ## Run unit tests and syntax check (no QEMU required)

# ── Static analysis ──────────────────────────────────────────────────────

check: ## Syntax-check all Python files (fast, no dependencies)
	@# Restrict to source dirs and skip caches/.tox to keep the run fast
	@# and predictable. Use a single Python process via xargs (-exec ... +)
	@# instead of one process per file. The entry-point script `zark` has
	@# no .py extension and is checked separately.
	find lib commands tests -name '*.py' -print0 \
		| xargs -0 $(PYTHON) -m py_compile
	$(PYTHON) -m py_compile zark
	@echo "  All files OK"

mypy: ## Type-check with mypy (strict — fails on any error)
	@command -v mypy >/dev/null || { \
		echo "  mypy not installed. Install with: pip install mypy --break-system-packages"; \
		exit 1; \
	}
	mypy .

pylint: ## Run pylint on the whole project
	@command -v pylint >/dev/null || { \
		echo "  pylint not installed. Install with: pip install pylint --break-system-packages"; \
		exit 1; \
	}
	pylint lib commands tests zark

ruff: ## Run ruff (full ruleset, as configured in pyproject.toml)
	@command -v ruff >/dev/null || { \
		echo "  ruff not installed. Install with: pip install ruff --break-system-packages"; \
		exit 1; \
	}
	ruff check .

ruff-strict: ## Run ruff with --select=RUF027 --preview (matches the CI lint job)
	@# CI's lint job runs only RUF027 (the stripped-`f`-string rule that
	@# caused the v1.0.4 regression) under --preview. Keeping a separate
	@# target makes the CI behaviour reproducible locally without having to
	@# remember the flag combination.
	@command -v ruff >/dev/null || { \
		echo "  ruff not installed. Install with: pip install ruff --break-system-packages"; \
		exit 1; \
	}
	ruff check --select=RUF027 --preview --no-fix lib commands tests zark

format: ## Run black + isort on the whole project
	@command -v black >/dev/null && command -v isort >/dev/null || { \
		echo "  black or isort missing. Install with:"; \
		echo "    pip install black isort --break-system-packages"; \
		exit 1; \
	}
	isort lib commands tests zark
	black lib commands tests zark

pre-commit: ## Run all pre-commit hooks against every tracked file
	@command -v pre-commit >/dev/null || { \
		echo "  pre-commit not installed. Install with:"; \
		echo "    pip install pre-commit --break-system-packages"; \
		exit 1; \
	}
	pre-commit run --all-files

lint: check mypy pylint ## Run all static checks (compile + mypy + pylint)

tox: ## Run unit tests on Python 3.12, 3.13, 3.14 + lint (in isolated venvs)
	@command -v $(TOX) >/dev/null || { \
		echo "  tox not installed. Install with: pip install tox --break-system-packages"; \
		exit 1; \
	}
	# --skip-missing-interpreters=false makes the run fail loudly if any of
	# python3.12 / python3.13 / python3.14 is not on PATH. This is the
	# explicit guarantee `make tox` provides over plain `tox` (which would
	# silently skip missing interpreters per tox.ini default).
	$(TOX) --skip-missing-interpreters=false

# ── Documentation ────────────────────────────────────────────────────────
#
# The manpage source lives at debian/zark.1.md and is converted to roff
# at build time (see debian/rules). These targets let you iterate on the
# Markdown without going through a full `make deb` cycle each time.
#
# The pipeline mirrors debian/rules verbatim — keep the two in sync if
# you ever change one. Both substitute @VERSION@ in the source against
# lib/config.py so we don't have to bump the manpage header by hand.

manpage: ## Build debian/zark.1 from debian/zark.1.md (pandoc + sed + awk)
	@command -v pandoc >/dev/null || { \
		echo "  pandoc not installed. Install with: sudo apt install pandoc"; \
		exit 1; \
	}
	@# We pipe the Markdown source through `sed` to substitute @VERSION@
	@# *before* pandoc sees it, rather than after. pandoc --ascii escapes
	@# any literal '@' in roff output as '\[at]', so a post-pandoc sed
	@# would have to match '\[at]VERSION\[at]' — fragile. Substituting
	@# upstream of pandoc keeps the substitution as a plain ASCII match.
	sed 's/@VERSION@/$(VERSION)/g' debian/zark.1.md \
	  | pandoc -s -f markdown-smart -t man --ascii -o debian/zark.1.raw
	@sed -E \
	    -e 's/\\f\[CB\]/\\fB/g' \
	    -e 's/\\f\[CI\]/\\f(BI/g' \
	    -e 's/\\f\[C\]/\\fB/g' \
	    -e 's/\\f\[BI\]/\\f(BI/g' \
	    -e 's/\\f\[B\]/\\fB/g' \
	    -e 's/\\f\[I\]/\\fI/g' \
	    -e 's/\\f\[R\]/\\fR/g' \
	    -e 's/\\f\[\]/\\fR/g' \
	    debian/zark.1.raw \
	  | awk '/^\.PP$$/ && prev ~ /^\.S[HS] / { next } { print; prev = $$0 }' \
	  > debian/zark.1
	@rm -f debian/zark.1.raw
	@echo "  Built: debian/zark.1 (zark $(VERSION))"
	@echo "  View with: make manpage-view"

manpage-view: manpage ## Render debian/zark.1 with man (after rebuilding it)
	@command -v man >/dev/null || { \
		echo "  man not installed. Install with: sudo apt install man-db"; \
		exit 1; \
	}
	man -l debian/zark.1

manpage-lint: manpage ## Validate the generated manpage with mandoc + groff
	@# mandoc -Tlint and groff -ww -z catch different things — mandoc's
	@# parser is stricter on macro semantics, groff's is the reference
	@# rendering implementation. Both must be clean for the manpage to be
	@# considered ready for shipping.
	@command -v mandoc >/dev/null || { \
		echo "  mandoc not installed. Install with: sudo apt install mandoc"; \
		exit 1; \
	}
	@command -v groff >/dev/null || { \
		echo "  groff not installed. Install with: sudo apt install groff"; \
		exit 1; \
	}
	mandoc -Tlint debian/zark.1
	groff -ww -z -man debian/zark.1
	@echo "  Manpage lints clean"

manpage-clean: ## Remove the generated manpage (keeps debian/zark.1.md)
	rm -f debian/zark.1 debian/zark.1.raw

# ── Lifecycle ────────────────────────────────────────────────────────────

setup: ## Run zark setup (install sanoid, register drive)
	sudo $(ZARK) setup

clean: ## Remove __pycache__ and temp files
	find . -type d -name __pycache__   -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache   -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache   -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .tox          -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	rm -f /tmp/zark_dist_*.tar.gz 2>/dev/null || true
	# Debian build artefacts land in the parent directory. Limit the
	# wildcard to our own version to avoid touching unrelated files.
	# Covers: .deb, .dsc, .changes, .buildinfo, .build (build log),
	# .tar.* (debian tarball, debian.tar.xz), .orig.tar.* (upstream),
	# and .upload (dput's "already-uploaded" marker — it lingers from
	# previous dput runs and would suppress legitimate re-uploads when
	# the previous Launchpad-side outcome was a rejection that dput
	# never observed locally).
	rm -f ../zark_$(VERSION)*.deb ../zark_$(VERSION)*.dsc \
	      ../zark_$(VERSION)*.changes ../zark_$(VERSION)*.buildinfo \
	      ../zark_$(VERSION)*.build ../zark_$(VERSION)*.tar.* \
	      ../zark_$(VERSION)*.orig.tar.* \
	      ../zark_$(VERSION)*.upload \
	      2>/dev/null || true
	rm -rf debian/.debhelper debian/files debian/zark debian/debhelper-build-stamp \
	       2>/dev/null || true
	# The generated manpage is a build artefact (source is debian/zark.1.md).
	rm -f debian/zark.1 debian/zark.1.raw 2>/dev/null || true
	# `dist/` is created by CI (.github/workflows/ci.yml) to stage build
	# artefacts inside the workspace before upload. Cleaning it here is
	# defensive — local builds drop artefacts in the parent directory.
	rm -rf dist 2>/dev/null || true

# ── Release ──────────────────────────────────────────────────────────────

dist: ## Build upstream release tarball: zark_<version>.tar.gz (no debian/)
	@echo "  Building zark_$(VERSION).tar.gz..."
	@# Why we exclude debian/ from the tarball:
	@#
	@# The Debian source format 3.0 (quilt) deliberately keeps the
	@# upstream tarball (.orig.tar.gz) and the packaging (debian/)
	@# separate. Including debian/ in the .orig.tar.gz produces three
	@# concrete problems:
	@#
	@#   1) Lintian flags `no-debian-changes` because the
	@#      debian.tar.xz adds nothing on top of an upstream that
	@#      already contains debian/.
	@#   2) Re-uploading the same upstream version with a tweaked
	@#      debian/ (e.g. 1.0.5-1 -> 1.0.5-1~ubuntu24.04.2) regenerates
	@#      the .orig.tar.gz with a different sha256, which Launchpad
	@#      rejects ("File already exists with different contents").
	@#   3) Conceptually wrong: this is exactly the separation that
	@#      3.0 (quilt) was designed to enforce.
	@#
	@# Users browsing the GitHub release tarball don't lose anything:
	@# they can see debian/ in the git repo. The tarball is the
	@# upstream slice; debian/ is the packaging slice.
	@#
	@# The exclusion list (caches, pyc, etc.) MUST stay in sync with
	@# debian/source/options. Both must agree or the next
	@# `make deb-source` fails with "unexpected upstream changes".
	@#
	@# REPRODUCIBILITY: tar(1) defaults bake in volatile state — file
	@# mtimes, the user's UID/GID, the order in which the filesystem
	@# returns directory entries — that all produce a different
	@# byte-for-byte tarball each run, even with identical source.
	@# Launchpad rejects an .orig.tar.gz upload if the same filename
	@# already exists with different contents:
	@#
	@#   File zark_X.Y.Z.orig.tar.gz already exists in <PPA>, but
	@#   uploaded version has different contents.
	@#
	@# That happens between a `make deb-ppa-test` and a
	@# `make deb-ppa-resume`: the first leaves a tarball on Launchpad,
	@# the second tries to upload a freshly-rebuilt tarball with the
	@# same filename but different bytes.
	@#
	@# We pin everything tar(1) and gzip(1) can vary:
	@#   --sort=name       — directory entries in stable order
	@#   --owner=0 --group=0 --numeric-owner
	@#                     — strip the maintainer's UID/GID
	@#   --mtime=@$$ts     — single timestamp for every entry, taken
	@#                       from the last git commit (SOURCE_DATE_EPOCH
	@#                       convention). Falls back to debian/changelog
	@#                       timestamp if the working tree isn't a git
	@#                       checkout (release tarball downloaded via
	@#                       `wget` from GitHub, etc.).
	@#   gzip -n           — no original-filename / no mtime in the
	@#                       gzip header
	@#
	@# Net effect: `make dist` is byte-for-byte reproducible across
	@# runs of the same source revision.
	@tmpdir=$$(mktemp -d) && \
		cp -a . $$tmpdir/zark && \
		rm -rf $$tmpdir/zark/debian; \
		find $$tmpdir/zark -type d -name __pycache__   -exec rm -rf {} + 2>/dev/null; \
		find $$tmpdir/zark -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; \
		find $$tmpdir/zark -type d -name .mypy_cache   -exec rm -rf {} + 2>/dev/null; \
		find $$tmpdir/zark -type d -name .ruff_cache   -exec rm -rf {} + 2>/dev/null; \
		find $$tmpdir/zark -type d -name .tox          -exec rm -rf {} + 2>/dev/null; \
		find $$tmpdir/zark -type d -name .git          -exec rm -rf {} + 2>/dev/null; \
		find $$tmpdir/zark -name '*.pyc'               -delete 2>/dev/null; \
		find $$tmpdir/zark -name 'zark.log'            -delete 2>/dev/null; \
		find $$tmpdir/zark -name '.DS_Store'           -delete 2>/dev/null; \
		ts=$$(git log -1 --format=%ct 2>/dev/null \
		      || date -u -d "$$(dpkg-parsechangelog -SDate)" +%s 2>/dev/null \
		      || echo 0); \
		tar --sort=name \
		    --owner=0 --group=0 --numeric-owner \
		    --mtime="@$$ts" \
		    -cf - -C $$tmpdir zark | gzip -n > zark_$(VERSION).tar.gz && \
		rm -rf $$tmpdir && \
		ls -la zark_$(VERSION).tar.gz

dist-check: ## Validate the dist tarball: no debian/ inside, dpkg-source -b succeeds
	@# This target is a non-invasive end-to-end check of the source-package
	@# pipeline: build the upstream tarball, set it up as the .orig.tar.gz
	@# in the parent directory (where dpkg-source expects it), then invoke
	@# `dpkg-source -b .` to produce the .debian.tar.xz and .dsc.
	@#
	@# It catches the regressions that bit us in v1.0.6 cycle:
	@#   - debian/ leaking into .orig.tar.gz (lintian no-debian-changes)
	@#   - new tool caches in the working tree breaking dpkg-source
	@#     ("unexpected upstream changes") when not in source/options
	@#
	@# All artefacts are removed at the end — the working tree is left
	@# exactly as it was found.
	@command -v dpkg-source >/dev/null || { \
		echo "  dpkg-source not installed. Install with: sudo apt install dpkg-dev"; \
		exit 1; \
	}
	@$(MAKE) --no-print-directory dist
	@# Verify upstream-purity: zark-X.Y.Z/debian/ must NOT exist in the tarball
	@if tar tzf zark_$(VERSION).tar.gz | grep -q '^zark-$(VERSION)/debian/'; then \
		echo "  FAIL: zark_$(VERSION).tar.gz contains debian/ — this should not happen"; \
		rm -f zark_$(VERSION).tar.gz; \
		exit 1; \
	fi
	@echo "  OK: tarball is upstream-pure (no debian/ inside)"
	@# Set up .orig.tar.gz where dpkg-source expects it (parent dir) and
	@# clean any prior debian artefacts of the same version that might
	@# confuse dpkg-source.
	@cp zark_$(VERSION).tar.gz ../zark_$(VERSION).orig.tar.gz
	@rm -f ../zark_$(VERSION)-1.dsc ../zark_$(VERSION)-1.debian.tar.xz
	@# A previous `make deb` (or fakeroot dpkg-buildpackage) leaves the
	@# dh staging tree in debian/zark/ and the debhelper bookkeeping in
	@# debian/.debhelper, debian/files, debian/debhelper-build-stamp.
	@# dpkg-source -b would then complain about "unwanted binary file"s
	@# (the gzipped manpage and changelog inside debian/zark/). Wipe
	@# them — same logic as `make clean` but limited to the debian/
	@# bookkeeping so we don't disturb caches the previous fulltest
	@# steps just produced.
	@rm -rf debian/.debhelper debian/files debian/zark debian/debhelper-build-stamp 2>/dev/null || true
	@# `make dist` left zark_X.Y.Z.tar.gz in the repo root (CI consumes
	@# it from there). dpkg-source -b sees that file as an unrepresented
	@# change vs the .orig (which is the same bytes!) and aborts. Remove
	@# it now — we've already captured it as ../zark_X.Y.Z.orig.tar.gz.
	@rm -f zark_$(VERSION).tar.gz
	@# Run dpkg-source -b . — the same call debuild would make, without
	@# the build/lintian/sign overhead. Capture rc to clean up before
	@# propagating it.
	@dpkg-source -b . ; rc=$$? ; \
		rm -f ../zark_$(VERSION).orig.tar.gz \
		      ../zark_$(VERSION)-1.dsc \
		      ../zark_$(VERSION)-1.debian.tar.xz ; \
		if [ $$rc -ne 0 ]; then \
			echo "  FAIL: dpkg-source -b . returned $$rc"; \
			exit $$rc; \
		fi
	@echo "  OK: dpkg-source -b . succeeds"

fulltest: check ruff ruff-strict mypy pylint test manpage-lint dist-check tox deb ## Everything safe to commit (no install, no sign, no QEMU)
	@# fulltest is the "before-commit" gate. Make's default fail-fast
	@# behaviour gives us free short-circuit semantics: the moment any
	@# dependency fails, the rest is skipped.
	@#
	@# Order is fast→slow so failures surface early:
	@#   check       — py_compile, milliseconds
	@#   ruff        — full ruleset, ~1s
	@#   ruff-strict — RUF027 only (CI subset), ~1s
	@#   mypy        — ~10-15s (cold) / ~2-3s (warm)
	@#   pylint      — ~15-20s
	@#   test        — 94 unit tests, ~1-2s
	@#   manpage-lint — pandoc + sed + awk + mandoc + groff, ~1s
	@#   dist-check  — make dist + dpkg-source -b, ~3-5s
	@#   tox         — tests on python3.12/3.13/3.14 in venvs, ~30-60s
	@#                 (requires all three Pythons on PATH; install with
	@#                  pyenv or deadsnakes if missing)
	@#   deb         — full debuild -b (binary .deb), ~10-15s
	@#                 LAST on purpose: it stages the install tree under
	@#                 debian/zark/, which would confuse mypy in tox -e lint
	@#                 (duplicate-module error from debian/zark/commands/
	@#                 vs ./commands/) if it ran earlier.
	@#
	@# What fulltest does NOT cover and why:
	@#   - dpkg -i     : would alter the local zark installation.
	@#   - test-real   : QEMU integration tests need root and ZFS modules.
	@#   - pre-commit  : black/isort would rewrite files in place; not
	@#                   "non-invasive". Run separately when desired.
	@#   - make deb-source / deb-ppa : require the GPG signing key and
	@#                   are part of the release flow, not the pre-commit
	@#                   gate.
	@#
	@# Leave the working tree exactly as we found it: tests, manpage-lint
	@# and deb generated artefacts that we now clean up. The caches mypy
	@# / ruff / pytest / tox leave behind are intentionally PRESERVED —
	@# they speed up the next fulltest run dramatically.
	@$(MAKE) --no-print-directory fulltest-clean
	@echo ""
	@echo "  fulltest: all checks passed."

fulltest-clean:
	@# Internal target invoked at the end of fulltest. Removes the
	@# packaging artefacts that `make deb` and `make manpage-lint` drop
	@# in the working tree and the parent directory, but PRESERVES the
	@# tooling caches (__pycache__, .mypy_cache, .ruff_cache,
	@# .pytest_cache, .tox) that the next fulltest run will reuse.
	@#
	@# Mirrors the logic in `make clean` line-by-line, with the cache
	@# wipes deliberately omitted. Keep this in sync with `make clean`
	@# whenever a new artefact path is added there.
	@rm -f /tmp/zark_dist_*.tar.gz 2>/dev/null || true
	@rm -f ../zark_$(VERSION)*.deb ../zark_$(VERSION)*.dsc \
	       ../zark_$(VERSION)*.changes ../zark_$(VERSION)*.buildinfo \
	       ../zark_$(VERSION)*.build ../zark_$(VERSION)*.tar.* \
	       ../zark_$(VERSION)*.orig.tar.* \
	       ../zark_$(VERSION)*.upload \
	       2>/dev/null || true
	@rm -rf debian/.debhelper debian/files debian/zark debian/debhelper-build-stamp \
	        2>/dev/null || true
	@rm -f debian/zark.1 debian/zark.1.raw 2>/dev/null || true
	@# `make deb` (transitively, via the $(ORIG_TARBALL) prerequisite)
	@# also leaves a copy of zark_X.Y.Z.tar.gz in the repo root.
	@rm -f zark_$(VERSION).tar.gz 2>/dev/null || true

version: ## Show current version
	@echo "zark v$(VERSION)"

# ── Packaging (Debian/Ubuntu .deb + Launchpad PPA) ───────────────────────
#
# Uses debuild (devscripts) so lintian runs automatically. See
# debian/README.packaging.md for the full guide, including GPG setup,
# PPA upload procedure, and version-suffix rules.
#
# Series covered by the PPA. Only currently-supported Ubuntu releases:
# Launchpad rejects uploads to EOL series with "<series> is obsolete and
# will not accept new uploads." Update this list when adding/dropping
# support for a release. As of May 2026:
#   - noble    (24.04 LTS) — supported until 2036
#   - questing (25.10)     — supported until 2026-07
#   - resolute (26.04 LTS) — to be released, accepted as upcoming
# Releases dropped along the way (do not re-add without checking
# https://wiki.ubuntu.com/Releases first):
#   - oracular (24.10) — EOL 2025-07
#   - plucky   (25.04) — EOL 2026-01
PPA_SERIES   := noble questing resolute
# Upload target. We use a named profile (`zark-ppa`) rather than the
# canonical `ppa:juanmitaboada/zark` shorthand so the configuration
# travels with the repo (./dput.cf) and any maintainer can clone+upload
# without setting up ~/.dput.cf. See dput.cf and debian/README.packaging.md
# for the rationale (SFTP method, hardcoded Launchpad ID).
PPA_DPUT_TGT := zark-ppa

# Helper: regenerate the orig tarball next to the source tree, named
# the way dpkg-source expects for a non-native package.
#
# `make dist` writes zark_$(VERSION).tar.gz into the project root.
# `dpkg-source -b .` (invoked by debuild) then computes a diff between
# the .orig and the working tree to produce the debian tarball; if the
# release tarball is sitting in the root at that point, dpkg-source
# treats it as an unrepresentable binary change and aborts. Moving
# (not copying) the file fixes that — the orig becomes the canonical
# upstream source, and there's nothing left in the working tree that
# diverges from it.
ORIG_TARBALL := ../zark_$(VERSION).orig.tar.gz

$(ORIG_TARBALL): dist
	@mv zark_$(VERSION).tar.gz $(ORIG_TARBALL)
	@echo "  Orig tarball ready: $(ORIG_TARBALL)"

deb: $(ORIG_TARBALL) ## Build unsigned binary .deb locally (-us -uc)
	@command -v debuild >/dev/null || { \
		echo "  debuild not installed. Install with: sudo apt install devscripts debhelper"; \
		exit 1; \
	}
	debuild -us -uc -b --lintian-opts --suppress-tags $(LINTIAN_SUPPRESS_TAGS)
	@# Make the upstream tarball available in cwd for downstream steps
	@# (CI's "stage release artefacts" step in .github/workflows/ci.yml
	@# expects to find zark_$(VERSION).tar.gz in the project root). The
	@# canonical copy is the .orig.tar.gz in the parent directory; we
	@# just expose a renamed link/copy for convenience.
	@cp $(ORIG_TARBALL) zark_$(VERSION).tar.gz
	@echo ""
	@echo "  Built: $$(ls -1t ../zark_$(VERSION)-*_all.deb | head -1)"
	@echo "  Inspect: lintian -i ../zark_$(VERSION)-*.changes"

deb-source: $(ORIG_TARBALL) ## Build SIGNED source package for PPA upload
	@command -v debuild >/dev/null || { \
		echo "  debuild not installed. Install with: sudo apt install devscripts debhelper"; \
		exit 1; \
	}
	@# Discover the maintainer's GPG signing key.
	@#
	@# Some keyrings end up with several historical keys for the same
	@# email (rotated yearly, expired, revoked, replaced...). The
	@# 'sec' record's second column tells us per-key validity:
	@#   - 'u' = ultimate trust — the maintainer's current key
	@#   - '-' = valid, secret available, but not marked as own key
	@#   - 'r' = revoked
	@#   - 'e' = expired
	@# We require 'u' and fall back to '-' only when no 'u' exists,
	@# because GPG defaults (e.g. `gpg --clearsign` with no -u) also
	@# pick the 'u' key — keeping our build consistent with what the
	@# user sees interactively.
	@#
	@# We extract the full fingerprint (the `fpr` record immediately
	@# following the chosen `sec`) rather than the 16-char long key ID:
	@# `debsign` prefers fingerprints and warns about long IDs.
	$(eval GPG_KEYID := $(shell gpg --list-secret-keys --with-colons juanmi@juanmitaboada.com 2>/dev/null | awk -F: 'BEGIN{want=0;u="";v=""} $$1=="sec" {want=($$2=="u")?1:(($$2=="-")?2:0); next} $$1=="fpr" && want==1 && !u {u=$$10} $$1=="fpr" && want==2 && !v {v=$$10} END{print (u ? u : v)}'))
	@test -n "$(GPG_KEYID)" || { \
		echo "  No usable GPG secret key for juanmi@juanmitaboada.com"; \
		echo "  (revoked/expired keys are ignored — see debian/README.packaging.md §1.2)"; \
		exit 1; \
	}
	@echo "  Signing with GPG key: $(GPG_KEYID)"
	@# Build the source package unsigned, then sign with debsign
	@# explicitly. This separation is more reliable than debuild's
	@# combined build+sign mode, whose -k / DEBSIGN_KEYID handling
	@# has had inconsistent semantics across versions and silently
	@# falls back to unsigned in some configurations.
	debuild -S -sa -us -uc --lintian-opts --suppress-tags $(LINTIAN_SUPPRESS_TAGS)
	debsign -k$(GPG_KEYID) ../zark_$(VERSION)-1_source.changes
	@# Verify the .changes is actually signed before claiming success.
	@# An unsigned upload would be rejected by Launchpad with "Bad
	@# signature", so failing here saves a round-trip.
	@head -1 ../zark_$(VERSION)-*_source.changes | grep -q "BEGIN PGP SIGNED MESSAGE" || { \
		echo ""; \
		echo "  ERROR: source.changes is NOT signed. Aborting."; \
		echo "  Check gpg-agent / pinentry interaction or run debsign manually:"; \
		echo "    debsign -k$(GPG_KEYID) ../zark_$(VERSION)-1_source.changes"; \
		exit 1; \
	}
	@echo ""
	@echo "  Source package built and signed:"
	@ls -1 ../zark_$(VERSION)-*_source.changes

deb-ppa-test: ## Smoke-test the PPA pipeline by uploading only the first series
	@# A safer way to validate the deb-ppa flow end-to-end without
	@# burning all five Ubuntu version slots if something is broken.
	@#
	@# This target re-invokes `make deb-ppa` but overrides PPA_SERIES
	@# from the make command line, so only the first series in the
	@# default list (noble = 24.04 LTS) gets built and uploaded. The
	@# original PPA_SERIES is not touched on disk, no editing required.
	@#
	@# Use this when:
	@#   - First time setting up the PPA pipeline
	@#   - After changes to debian/ that might affect the source build
	@#   - After a Launchpad outage / config change you want to verify
	@#   - Before a multi-day absence, to keep the pipeline warm
	@#
	@# Once the test upload reaches Launchpad and the build farm picks
	@# it up successfully, run the full `make deb-ppa` for the rest:
	@#
	@#   make deb-ppa-resume
	@#
	@# (or edit PPA_SERIES inline as a last resort, see deb-ppa-resume
	@# below for the canonical form).
	@$(MAKE) --no-print-directory deb-ppa PPA_SERIES=$(firstword $(PPA_SERIES))

deb-ppa-resume: ## Upload remaining series after a deb-ppa-test (skips first series)
	@# Counterpart to deb-ppa-test. After a successful test upload of
	@# the first series, this uploads the rest. Together they cover
	@# the same ground as `make deb-ppa` but split into two phases.
	@#
	@# The remaining series list is computed at runtime by stripping
	@# the first word from PPA_SERIES, so it stays in sync if the
	@# default series list ever changes.
	@$(MAKE) --no-print-directory deb-ppa PPA_SERIES="$(wordlist 2,$(words $(PPA_SERIES)),$(PPA_SERIES))"

deb-ppa: $(ORIG_TARBALL) ## Upload a signed source package per Ubuntu series to the PPA
	@command -v dput >/dev/null || { \
		echo "  dput not installed. Install with: sudo apt install dput-ng"; \
		exit 1; \
	}
	$(eval GPG_KEYID := $(shell gpg --list-secret-keys --with-colons juanmi@juanmitaboada.com 2>/dev/null | awk -F: 'BEGIN{want=0;u="";v=""} $$1=="sec" {want=($$2=="u")?1:(($$2=="-")?2:0); next} $$1=="fpr" && want==1 && !u {u=$$10} $$1=="fpr" && want==2 && !v {v=$$10} END{print (u ? u : v)}'))
	@test -n "$(GPG_KEYID)" || { \
		echo "  No usable GPG secret key for juanmi@juanmitaboada.com"; \
		echo "  (revoked/expired keys are ignored — see debian/README.packaging.md §1.2)"; \
		exit 1; \
	}
	@echo "  This will build and upload one source package per series:"
	@echo "    $(PPA_SERIES)"
	@echo "  to $(PPA_DPUT_TGT)."
	@read -r -p "  Continue? [y/N] " ans && [ "$$ans" = "y" ] || { echo "  Aborted."; exit 1; }
	@# All shell commands run in a single sub-shell (one '\' line) so we
	@# can use trap to restore debian/changelog regardless of which step
	@# fails. The inner cp/mv-based logic was fragile: nested error
	@# branches could move .bak twice, leaving the working tree with a
	@# half-rewritten changelog and a confusing "cannot stat .bak" error.
	@#
	@# IMPORTANT: the backup MUST live outside the package source tree.
	@# dh_clean (run by `debian/rules clean`, invoked by every
	@# dpkg-buildpackage) deletes any *.bak file under the working tree
	@# by default — see the find(1) expression in dh_clean. Storing the
	@# backup at debian/changelog.bak meant the first series uploaded
	@# fine, but `debuild -S` of that series wiped the backup, so the
	@# second series failed with "cannot stat 'debian/changelog.bak'".
	@# Use mktemp under /tmp instead.
	@#
	@# DEBEMAIL/DEBFULLNAME tell dch which identity to record in the new
	@# changelog entry. Without these dch falls back to whoami@hostname.
	@#
	@# We refresh the .orig tarball before each series because dch
	@# modifies debian/changelog (which is fine, debian/ isn't compared)
	@# but ALSO because if the working tree has any uncommitted change
	@# outside debian/, we need that to be reflected in .orig too —
	@# otherwise dpkg-source -b aborts with "unexpected upstream changes".
	@set -e; \
	changelog_backup=$$(mktemp -t zark-changelog-XXXXXX); \
	cp debian/changelog "$$changelog_backup"; \
	trap 'cp -f "$$changelog_backup" debian/changelog 2>/dev/null || true; rm -f "$$changelog_backup"' EXIT INT TERM; \
	for series in $(PPA_SERIES); do \
		case $$series in \
			noble)     suffix="~ubuntu24.04.1" ;; \
			oracular)  suffix="~ubuntu24.10.1" ;; \
			plucky)    suffix="~ubuntu25.04.1" ;; \
			questing)  suffix="~ubuntu25.10.1" ;; \
			resolute)  suffix="~ubuntu26.04.1" ;; \
			*)         echo "  Unknown series: $$series"; exit 1 ;; \
		esac; \
		echo ""; \
		echo "  ── Building for $$series ($(VERSION)-1$$suffix) ──"; \
		cp "$$changelog_backup" debian/changelog; \
		DEBEMAIL="juanmi@juanmitaboada.com" DEBFULLNAME="Juanmi Taboada" \
		    dch --newversion "$(VERSION)-1$$suffix" --distribution "$$series" \
		        --force-distribution --force-bad-version \
		        "Build for Ubuntu $$series."; \
		debuild -S -sa -us -uc; \
		debsign -k$(GPG_KEYID) "../zark_$(VERSION)-1$${suffix}_source.changes"; \
		head -1 "../zark_$(VERSION)-1$${suffix}_source.changes" | grep -q "BEGIN PGP SIGNED MESSAGE" \
		    || { echo "  ERROR: $$series .changes is NOT signed. Aborting."; exit 1; }; \
		: "Remove dput's stale .upload marker before each push. dput" ; \
		: "writes that file when the SFTP transfer completes and refuses" ; \
		: "to re-send the same .changes if it exists, even if the previous" ; \
		: "Launchpad-side outcome was a rejection (which dput does not see" ; \
		: "— rejections come as asynchronous emails). Our deb-ppa has its" ; \
		: "own version-bumping safeguards, so dput's lock adds no value" ; \
		: "for us and only causes false 'already uploaded' failures." ; \
		rm -f "../zark_$(VERSION)-1$${suffix}_source.$(PPA_DPUT_TGT).upload"; \
		dput -c dput.cf $(PPA_DPUT_TGT) ../zark_$(VERSION)-1$${suffix}_source.changes; \
	done
	@echo ""
	@# Print the series that just finished uploading. PPA_SERIES is the
	@# authoritative list (deb-ppa-test and deb-ppa-resume override it
	@# from the make command line). $(words) gives the count, so the
	@# message reads naturally whether it was 1 series, 4 series, or 5.
	@nseries="$(words $(PPA_SERIES))"; \
	if [ "$$nseries" = "1" ]; then \
		echo "  Uploaded 1 series ($(PPA_SERIES)). Launchpad will email build results."; \
	else \
		echo "  Uploaded $$nseries series ($(PPA_SERIES)). Launchpad will email build results."; \
	fi
