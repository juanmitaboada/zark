.PHONY: help \
        test test-real test-deps test-all \
        test-phase1 test-phase2 test-phase3 test-cleanup \
        setup clean version \
        check mypy pylint format pre-commit lint \
        dist

SHELL  := /bin/bash
ZARK   := ./zark
PYTHON := python3

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
	@find . -name '*.py' -exec $(PYTHON) -m py_compile {} \; && echo "  All files OK"

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

# ── Lifecycle ────────────────────────────────────────────────────────────

setup: ## Run zark setup (install sanoid, register drive)
	sudo $(ZARK) setup

clean: ## Remove __pycache__ and temp files
	find . -type d -name __pycache__   -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache   -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	rm -f /tmp/zark_dist_*.tar.gz 2>/dev/null || true

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
