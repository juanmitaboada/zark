.PHONY: help \
        test test-real test-deps test-all \
        test-phase1 test-phase2 test-phase3 test-cleanup \
        setup clean version \
        check mypy pylint format pre-commit lint tox \
        manpage manpage-view manpage-clean \
        dist deb deb-source deb-ppa

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
	# .tar.* (debian tarball, debian.tar.xz) and .orig.tar.* (upstream).
	rm -f ../zark_$(VERSION)*.deb ../zark_$(VERSION)*.dsc \
	      ../zark_$(VERSION)*.changes ../zark_$(VERSION)*.buildinfo \
	      ../zark_$(VERSION)*.build ../zark_$(VERSION)*.tar.* \
	      ../zark_$(VERSION)*.orig.tar.* \
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
		tar -czf zark_$(VERSION).tar.gz -C $$tmpdir zark && \
		rm -rf $$tmpdir && \
		ls -la zark_$(VERSION).tar.gz

version: ## Show current version
	@echo "zark v$(VERSION)"

# ── Packaging (Debian/Ubuntu .deb + Launchpad PPA) ───────────────────────
#
# Uses debuild (devscripts) so lintian runs automatically. See
# debian/README.packaging.md for the full guide, including GPG setup,
# PPA upload procedure, and version-suffix rules.
#
# Series covered by the PPA: 24.04 → 26.04. Codenames are stable
# (resolute = 26.04 LTS Resolute Raccoon).
PPA_SERIES   := noble oracular plucky questing resolute
# Upload target. We use a named profile (`zark-ppa`) rather than the
# canonical `ppa:juanmitaboada/zark` shorthand because dput-ng's default
# profile uses FTP, which is increasingly blocked by ISPs and didn't
# work for us. The profile is defined in ./dput.cf at the repo root and
# uses HTTPS.
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
	@# DEBEMAIL/DEBFULLNAME tell dch which identity to record in the new
	@# changelog entry. Without these dch falls back to whoami@hostname.
	@#
	@# We refresh the .orig tarball before each series because dch
	@# modifies debian/changelog (which is fine, debian/ isn't compared)
	@# but ALSO because if the working tree has any uncommitted change
	@# outside debian/, we need that to be reflected in .orig too —
	@# otherwise dpkg-source -b aborts with "unexpected upstream changes".
	@set -e; \
	cp debian/changelog debian/changelog.bak; \
	trap 'mv -f debian/changelog.bak debian/changelog 2>/dev/null || true' EXIT INT TERM; \
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
		cp debian/changelog.bak debian/changelog; \
		DEBEMAIL="juanmi@juanmitaboada.com" DEBFULLNAME="Juanmi Taboada" \
		    dch --newversion "$(VERSION)-1$$suffix" --distribution "$$series" \
		        --force-distribution --force-bad-version \
		        "Build for Ubuntu $$series."; \
		debuild -S -sa -us -uc; \
		debsign -k$(GPG_KEYID) "../zark_$(VERSION)-1$${suffix}_source.changes"; \
		head -1 "../zark_$(VERSION)-1$${suffix}_source.changes" | grep -q "BEGIN PGP SIGNED MESSAGE" \
		    || { echo "  ERROR: $$series .changes is NOT signed. Aborting."; exit 1; }; \
		dput -c dput.cf $(PPA_DPUT_TGT) ../zark_$(VERSION)-1$${suffix}_source.changes; \
	done
	@echo ""
	@echo "  All series uploaded. Launchpad will email build results."
