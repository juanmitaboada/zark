.PHONY: help \
        test test-real test-deps test-all \
        test-phase1 test-phase2 test-phase3 test-cleanup \
        setup clean version \
        check mypy pylint format pre-commit lint tox \
        dist deb deb-source deb-ppa

SHELL  := /bin/bash
ZARK   := ./zark
PYTHON := python3
TOX    := tox

# Resolve version from the single source of truth (lib/config.py).
# Used by `make dist` to build a properly-named tarball.
VERSION := $(shell $(PYTHON) -c "from lib.config import VERSION; print(VERSION)")

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
	# `dist/` is created by CI (.github/workflows/ci.yml) to stage build
	# artefacts inside the workspace before upload. Cleaning it here is
	# defensive — local builds drop artefacts in the parent directory.
	rm -rf dist 2>/dev/null || true

# ── Release ──────────────────────────────────────────────────────────────

dist: ## Build release tarball: zark_<version>.tar.gz
	@echo "  Building zark_$(VERSION).tar.gz..."
	@tmpdir=$$(mktemp -d) && \
		cp -a . $$tmpdir/zark && \
		find $$tmpdir/zark -type d -name __pycache__   -exec rm -rf {} + 2>/dev/null; \
		find $$tmpdir/zark -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; \
		find $$tmpdir/zark -type d -name .mypy_cache   -exec rm -rf {} + 2>/dev/null; \
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
# Series covered by the PPA:                   24.04 → 26.04
PPA_SERIES   := noble oracular plucky questing resolute
# (resolute is provisional for 26.04 — adjust when the codename is set.)
PPA_DPUT_TGT := ppa:juanmitaboada/zark

# Helper: regenerate the orig tarball next to the source tree, named
# the way dpkg-source expects for a non-native package.
ORIG_TARBALL := ../zark_$(VERSION).orig.tar.gz

$(ORIG_TARBALL): dist
	@cp zark_$(VERSION).tar.gz $(ORIG_TARBALL)
	@echo "  Orig tarball ready: $(ORIG_TARBALL)"

deb: $(ORIG_TARBALL) ## Build unsigned binary .deb locally (-us -uc)
	@command -v debuild >/dev/null || { \
		echo "  debuild not installed. Install with: sudo apt install devscripts debhelper"; \
		exit 1; \
	}
	debuild -us -uc -b
	@echo ""
	@echo "  Built: $$(ls -1t ../zark_$(VERSION)-*_all.deb | head -1)"
	@echo "  Inspect: lintian -i ../zark_$(VERSION)-*.changes"

deb-source: $(ORIG_TARBALL) ## Build SIGNED source package for PPA upload
	@command -v debuild >/dev/null || { \
		echo "  debuild not installed. Install with: sudo apt install devscripts debhelper"; \
		exit 1; \
	}
	@gpg --list-secret-keys juanmi@juanmitaboada.com >/dev/null 2>&1 || { \
		echo "  No GPG secret key for juanmi@juanmitaboada.com — see debian/README.packaging.md §1.2"; \
		exit 1; \
	}
	debuild -S -sa
	@echo ""
	@echo "  Source package built and signed:"
	@ls -1 ../zark_$(VERSION)-*_source.changes

deb-ppa: ## Upload a signed source package per Ubuntu series to the PPA
	@command -v dput >/dev/null || { \
		echo "  dput not installed. Install with: sudo apt install dput-ng"; \
		exit 1; \
	}
	@gpg --list-secret-keys juanmi@juanmitaboada.com >/dev/null 2>&1 || { \
		echo "  No GPG secret key for juanmi@juanmitaboada.com — see debian/README.packaging.md §1.2"; \
		exit 1; \
	}
	@echo "  This will build and upload one source package per series:"
	@echo "    $(PPA_SERIES)"
	@echo "  to $(PPA_DPUT_TGT)."
	@read -r -p "  Continue? [y/N] " ans && [ "$$ans" = "y" ] || { echo "  Aborted."; exit 1; }
	@cp debian/changelog debian/changelog.bak
	@for series in $(PPA_SERIES); do \
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
		dch --newversion "$(VERSION)-1$$suffix" --distribution "$$series" \
		    --force-distribution --preserve \
		    "Build for Ubuntu $$series."; \
		debuild -S -sa || { mv debian/changelog.bak debian/changelog; exit 1; }; \
		dput $(PPA_DPUT_TGT) ../zark_$(VERSION)-1$${suffix}_source.changes \
		    || { mv debian/changelog.bak debian/changelog; exit 1; }; \
	done
	@mv debian/changelog.bak debian/changelog
	@echo ""
	@echo "  All series uploaded. Launchpad will email build results."
