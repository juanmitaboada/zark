# zark
**The Zettabyte Ark - Full bare-metal ZFS recovery with encrypted boot**

*A Noah's Ark for ZFS-on-root Ubuntu: when the disaster comes, your system makes it across.*

[![CI](https://github.com/juanmitaboada/zark/actions/workflows/ci.yml/badge.svg)](https://github.com/juanmitaboada/zark/actions/workflows/ci.yml) [![PPA](https://img.shields.io/badge/Ubuntu-PPA-orange?logo=ubuntu)](https://launchpad.net/~juanmitaboada/+archive/ubuntu/zark) [![Latest release](https://img.shields.io/github/v/release/juanmitaboada/zark?filter=!*-*&sort=semver&display_name=tag&label=release&color=blue)](https://github.com/juanmitaboada/zark/releases/latest) [![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-yellow)](https://opensource.org/license/apache-2.0) [![Python Version](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/downloads/) [![Ubuntu Version](https://img.shields.io/badge/Ubuntu-24.04%20%7C%2026.04-green.svg)](https://ubuntu.com/download) [![Changelog](https://img.shields.io/badge/changelog-CHANGELOG.md-orange.svg)](CHANGELOG.md)

zark is a portable Python-based suite for backing up and fully recovering Ubuntu systems running ZFS with full-disk encryption. It runs from any location - USB drive, live session, or local directory - with zero installation required.

One command to back up. One command to recover. Boot chain identical to a fresh Ubuntu install.

> **Project status.** zark 2.0.0 is validated end to end on real hardware (a Dell XPS 13 9333) on Ubuntu 24.04 and 26.04, each with the installer's encrypted ZFS layout: setup, the self-test at both levels, prepare, backups, exclusions, prune, and a recover from an Ubuntu 26.04 live followed by finish and a boot. zark 1.x runs in production on the maintainer's machines; [upgrading from it](docs/UPGRADING.md) has a page of its own. Changes are tracked in [`CHANGELOG.md`](CHANGELOG.md).

---

## ⚠️ Warning

zark performs **destructive operations** on ZFS pools and disk devices, including pool destruction, dataset rollback, partition table rewriting, and boot chain modification.

- **You can lose all data** on the target drives if you misidentify a device.
- **You can render your system unbootable** if recovery is interrupted or misconfigured.
- Always test on **non-production hardware** first.
- Always keep **at least one independent backup** outside of zark's control.
- The authors assume no responsibility for data loss, hardware damage, or system downtime resulting from the use of this tool.

This software is provided "as is", without warranty of any kind, as detailed in the [Apache License 2.0](LICENSE).

---

## Why zark?

Recovering a ZFS-on-root Ubuntu system with full-disk encryption is notoriously difficult. The boot chain involves GRUB, EFI, initramfs/dracut, encrypted datasets, keystore volumes, and Secure Boot - all tightly coupled. A single misstep leaves you at an emergency shell with no clear path forward.

zark automates the entire process:

- **Full bare-metal recovery in ~1 minute** - from backup drive to bootable system, including encrypted datasets, boot pool, keystore, and EFI partition.
- **100% standard Ubuntu boot chain** - no custom binaries, no patched configs. The recovered system is indistinguishable from a fresh install and survives `apt upgrade` indefinitely.
- **Secure Boot compliant** - proper signed GRUB chain (shimx64 → grubx64 signed by Canonical), never just `grub-install`.
- **Portable, zero install** - the entire suite lives in a single directory. Copy it to a USB drive and carry your disaster recovery in your pocket.
- **Full-disk encryption throughout** - raw `zfs send` preserves encryption natively. Keys never touch disk in cleartext during transfer.

---

## Installation

zark ships in three complementary forms; **all three are first-class** and serve different use cases.

### Apt via PPA (recommended for productive systems)

For machines where zark drives the day-to-day backup routine:

```bash
sudo add-apt-repository ppa:juanmitaboada/zark
sudo apt update
sudo apt install zark
```

Supported series: noble (24.04 LTS), resolute (26.04 LTS). The package installs zark under `/usr/share/zark/`, exposes it as `/usr/bin/zark`,
and creates `/etc/zark/` for `known_drives.json`. Logs go to `/var/log/zark.log`.

### Standalone .deb download (when PPA isn't an option)

If you cannot or prefer not to add a PPA - restricted networks, offline systems, or simply a one-shot install - every release ships a prebuilt `.deb` as a release asset on GitHub:

```bash
wget https://github.com/juanmitaboada/zark/releases/latest/download/zark_<VERSION>-1_amd64.deb
sudo apt install ./zark_*.deb
```

Built from the same source and with the same layout as the PPA's packages, but versioned `<VERSION>-1`: the PPA's carry a series suffix (`~ubuntu24.04.1`, `~ubuntu26.04.1`) and sort below it, so on a machine with the PPA, install with apt. Updates are manual: re-download when a new release is announced.

### Portable tarball (required for live-USB recovery)

For disaster recovery from a live USB - when there is no installed system to `apt install` into - head to the [**Releases page**](https://github.com/juanmitaboada/zark/releases/latest) and
download the `zark_X.Y.Z.tar.gz` asset attached to the latest release. Then:

```bash
tar xzf zark_*.tar.gz
cd zark
sudo ./zark explore
```

The tarball runs from any directory (USB pendrive, `/opt`, `~/bin`) without installation. When zark detects it is running on a live USB session, it logs
to `<zark_root>/zark.log` next to the script (which survives reboot, since the pendrive does) instead of `/var/log/`.

> **Why three?** The `.deb` package (PPA or direct) cannot help during recovery because the live USB does not have zark installed and you cannot `apt install`
> in a casper environment. The portable tarball is the only path for the recover command. The PPA is the most ergonomic for routine backups (apt updates handle versioning), and the standalone `.deb` covers air-gapped or offline deployments where the PPA channel is impractical.

## Documentation

- [Usage](docs/USAGE.md): the commands, a first backup, a recovery, requirements and the drive registry.
- [Upgrading from zark 1.x](docs/UPGRADING.md).
- [Design](docs/DESIGN.md): architecture, key decisions, compatibility and security notes.
- [Drive rotation and retention](docs/RETENTION.md).
- [Safe USB disconnect](docs/SAFE-DISCONNECT.md) and [hardware notes](docs/HARDWARE.md).
- [Status files for other programs](docs/STATUS-FILES.md).
- [Testing](docs/TESTING.md).
- [Troubleshooting and FAQ](docs/TROUBLESHOOTING.md).
- The manpage, `man zark`, once the package is installed.

---

## License

zark is licensed under the [Apache License, Version 2.0](LICENSE).

- Full license text: [`LICENSE`](LICENSE)
- Attribution requirements (propagated by redistributors): [`NOTICE`](NOTICE)

Apache 2.0 includes an explicit patent grant from contributors to users and an "AS IS" disclaimer of warranties. See sections 3 (Grant of Patent License), 7 (Disclaimer of Warranty), and 8 (Limitation of Liability) of the license text for the legal specifics.

---
