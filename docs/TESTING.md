# Testing zark

The unit suite, the QEMU integration harness and the static analysis.

## Testing

zark has two layers of automated testing.

### Unit tests

Pure Python, no ZFS, no real disks. Every shell call is intercepted by a mock framework (`tests/mock_sh.py`); the few tests that need root and mount namespaces run in a namespace of their own and are skipped without them.

```bash
make test       # fast path: invokes the test runner directly
make tox        # full path: runs the suite under Python 3.12, 3.13 and 3.14
```

The suite covers every command and module: the planner and the engine on a model of ZFS (`tests/fake_zfs.py`), the recovery path, the boot chain, the guards, signals and cleanup, and the self-test checks that model can run.

GitHub Actions runs the unit-test suite on every push and pull request, with one job per supported Python version plus a separate lint job (mypy + pylint + ruff + basedpyright), all on Ubuntu 24.04. On a release tag, and when run by hand, it also builds the `.deb` on 24.04 and installs it on 24.04 and 26.04. See `.github/workflows/ci.yml`.

### Integration tests (QEMU)

End-to-end test that creates a real encrypted ZFS Ubuntu system inside QEMU, backs it up, recovers to a second virtual disk, and boots the recovered disk to verify the full chain. Requires KVM and an Ubuntu live ISO. The harness dates from zark 1.x and was not run for 2.0.0, whose validation on hardware is the self-test and a checklist on real machines.

```bash
make test-deps                                # one-time: qemu, ovmf, genisoimage
sudo make test-real ISO=/path/to/ubuntu.iso   # full run (all 3 phases)
```

**Integration tests do not run in GitHub Actions.** GitHub-hosted runners lack nested KVM, the recovery flow needs root and the ZFS kernel modules, and the full run takes ~15 minutes per phase. They are meant for local runs on a workstation with KVM enabled.

The harness can also run individual phases - useful while iterating on a single phase without re-creating earlier artifacts:

```bash
sudo make test-phase1 ISO=/path/to/ubuntu.iso  # create test system + backup
sudo make test-phase2 ISO=/path/to/ubuntu.iso  # recover to target disk
sudo make test-phase3                          # boot the recovered disk
sudo make test-cleanup                         # remove all test artifacts
```

See `tests/test_integration.py` for harness internals and `tests/create_test_system.sh` for the synthetic-system fixture.

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
