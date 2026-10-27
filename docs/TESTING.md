# Testing zark

The unit suite, the static analysis and the validation on hardware.

## Testing

zark has two layers of automated testing, the unit suite and the static analysis; on hardware, `zark selftest` and a checklist on real machines for each release.

### Unit tests

Pure Python, no ZFS, no real disks. Every shell call is intercepted by a mock framework (`tests/mock_sh.py`); the few tests that need root and mount namespaces run in a namespace of their own and are skipped without them.

```bash
make test       # fast path: invokes the test runner directly
make tox        # full path: runs the suite under Python 3.12, 3.13 and 3.14
```

The suite covers every command and module: the planner and the engine on a model of ZFS (`tests/fake_zfs.py`), the recovery path, the boot chain, the guards, signals and cleanup, and the self-test checks that model can run.

GitHub Actions runs the unit-test suite on every push and pull request, with one job per supported Python version plus a separate lint job (mypy + pylint + ruff + basedpyright), all on Ubuntu 24.04. On a release tag, and when run by hand, it also builds the `.deb` on 24.04 and installs it on 24.04 and 26.04. See `.github/workflows/ci.yml`.

### Static analysis

```bash
make check          # py_compile every .py file (fast, no dependencies)
make ruff           # ruff's lint, full ruleset
make format-check   # ruff's format check
make mypy           # type-check with mypy (strict)
make pylint         # run pylint
make basedpyright   # type-check with basedpyright (strict)
make lint           # check + ruff + format-check + mypy + pylint + basedpyright
make format         # reformat and auto-fix with ruff (rewrites files)
make pre-commit     # run every pre-commit hook against every tracked file
make fulltest       # everything safe to run before a commit, with the pinned tools
```

These targets run the tools in `PATH`; `make fulltest` and `make tox` run the versions pinned in `pyproject.toml` and `tox.ini`. Tool configuration lives in `pyproject.toml` (ruff, mypy, pyright for basedpyright, pylint). Pre-commit hooks are wired in `.pre-commit-config.yaml`.
