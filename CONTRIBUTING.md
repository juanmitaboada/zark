# Contributing to zark

Thanks for your interest, thank you!. zark is a small project, so the process is
informal — but a few conventions help keep contributions reviewable.

## Reporting bugs and requesting features

Open an issue at <https://github.com/juanmitaboada/zark/issues>.
For bugs include:

- The output of `zark --version`
- Your Ubuntu version (`lsb_release -d`) and ZFS version (`zfs version`)
- A clear description of what you expected vs. what happened
- The relevant slice of `/var/log/zark.log` (or `<zark_root>/zark.log`
  when running from a portable copy)
- For recovery issues: the QEMU command line if you reproduced inside
  the simulator, or the `dmesg` output around the failure

For **security** issues do **not** open a public issue — see
[`SECURITY.md`](SECURITY.md) for the responsible disclosure process.

## Setting up a development environment

zark is pure Python and runs on Ubuntu 24.04+. To work on the suite
itself you need the same dependencies the user-facing `setup` command
installs, plus the Python tooling:

```bash
sudo apt install zfsutils-linux sanoid cryptsetup-bin gdisk \
    grub2-common dracut python3-pip
git clone https://github.com/juanmitaboada/zark.git
cd zark
pip install --user tox pre-commit
pre-commit install
```

## Running checks

Every change must keep the full check suite green. CI enforces this on
every push and PR:

```bash
make tox       # tests on py3.12 / py3.13 / py3.14, plus lint
make test      # tests only, on the active Python
make lint      # ruff + pylint + mypy only, no tests
```

Specifically the bar is:

- **Tests**: 100% pass. Add new ones for behaviour you change. Tests
  use `tests/mock_sh.py` so they run without root or real ZFS.
- **mypy**: `mypy .` reports zero errors in default mode.
- **pylint**: `pylint lib commands tests zark` reports `10.00/10`.
- **ruff**: in particular the `RUF027` rule must pass — it catches the
  stripped-`f` bug that broke v1.0.4.

Integration tests (`tests/test_integration.py`) require root, KVM, and
a working OVMF setup; they are NOT run in CI and are exercised locally
by the maintainer before each release.

## Pull requests

- One logical change per PR. Refactors and behavioural changes go in
  separate PRs whenever practical.
- Commit messages: imperative mood subject under 72 chars, followed by
  a blank line and a paragraph explaining the *why* (not the *what* —
  the diff already shows that). See recent `git log` for the style.
- Update `CHANGELOG.md` under the appropriate section
  (`### Security`, `### Tooling`, `### Distribution`, etc.) for any
  user-visible change.
- Do not bump the version in `lib/config.py` — the maintainer does
  that as part of the release commit.

## Code style notes

- zark targets Ubuntu's stock Python (3.12+). Don't introduce
  dependencies on packages not in the Ubuntu archive.
- Module layout is intentional: `lib/` for reusable infrastructure
  (sh, log, zfs, mount, ...), `commands/` for one file per user-facing
  subcommand. Don't import command modules from `lib/`.
- Logging goes through `lib.log.Log`; never use `print()` for messages
  the user should see.
- Shell calls go through `lib.sh.run` / `lib.sh.run_pipe`; never use
  `subprocess` directly.

## Scope

zark deliberately stays narrow: ZFS-on-root Ubuntu with full-disk
encryption, UEFI Secure Boot, portable invocation. Proposals that
broaden this scope (other distributions, non-encrypted setups,
non-ZFS filesystems) are unlikely to be accepted; they are better
served as separate projects.
