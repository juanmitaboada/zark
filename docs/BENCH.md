# The VM bench

zark's end-to-end checks in QEMU/KVM, on the system Ubuntu's installer lays out. The code is `bench/`, the workflow `.github/workflows/bench.yml`, the tests `tests/test_bench.py`.

## What one run does

On a GitHub-hosted runner (`ubuntu-24.04` or `ubuntu-26.04`), with no person:

1. Fetches the newest Ubuntu 26.04 desktop point release from `releases.ubuntu.com`, checks `SHA256SUMS` against Ubuntu's CD image key (fingerprint `843938DF228D22F7B3742BC0D94AA3F0EFE21092`, from the runner's `ubuntu-keyring`) and the ISO against `SHA256SUMS`.
2. Picks QEMU's OVMF with Secure Boot, Microsoft's keys and SMM, and checks that it refuses an unsigned EFI binary (the negative control).
3. Edits the ISO's `grub.cfg` only (the serial console, `autoinstall`, `ds=nocloud`), so the signed boot chain stays as it is, and checks that the boot records did not change.
4. Installs Ubuntu with autoinstall from a NoCloud seed: encrypted ZFS (`layout: {name: zfs, password: ...}`), as the graphical installer lays it out.
5. Boots the installed disk with Secure Boot on, answers the unlock prompt on the serial console, reaches the system over SSH.
6. Proves Secure Boot was on (`mokutil`, the `SecureBoot` variable, the kernel's line, the QEMU command line) and checks the layout (bpool, rpool with its LUKS keystore, the boot environment, USERDATA, encrypted swap).
7. Searches the installed system, the artifacts and the disk image for the run's secrets; uploads the artifacts only when none is found.

`probe` mode stops after step 3 and reads the ISO instead: the installer snap's subiquity, cloud-init's datasource configuration, whether `openssh-server` is in the live. It takes about ten minutes.

The installed system is changed in three ways, all by autoinstall's `late-commands`: GRUB and the kernel also use the serial console (`/etc/default/grub.d/90-zark-bench.cfg`, without `quiet splash`), and the bench user may use `sudo` without a password (`/etc/sudoers.d/bench`). Nothing touches the boot chain's binaries, the pools or the keystore.

## Secrets

The disk passphrase and the user's password are made by each run, in memory, and masked in the job's log. They are written to one file only, the seed image, which the run deletes when it ends; they never appear in a command line (the run refuses to start QEMU with one). No repository secret is used.

## Running it

By hand, from a checkout with `gh` signed in:

```bash
gh workflow run bench.yml --ref <branch> -f host=both -f mode=probe
gh workflow run bench.yml --ref <branch> -f host=ubuntu-24.04 -f mode=install
gh run list --workflow bench.yml --limit 5
gh run view <run id> --log
gh run download <run id>
```

`host` is `ubuntu-24.04`, `ubuntu-26.04` or `both`; `mode` is `install` or `probe`; `iso` names one ISO of the 26.04 directory instead of the newest. The workflow also runs every Monday at 03:17 UTC on `main`, on both hosts.

GitHub disables scheduled workflows in a public repository after 60 days without activity in it; the Actions tab then offers to enable it again (`gh workflow enable bench.yml`).

### On a machine of your own

The same scripts run on an Ubuntu machine with KVM, without registering it as a runner (a self-hosted runner on a public repository would run code from anyone's pull request). The preparation script installs packages and writes a udev rule, so read it first; on your own machine you may prefer to install the packages it lists by hand and keep `/dev/kvm` as it is (being in the `kvm` group is enough). Then, from the checkout:

```bash
python3 -m bench probe --out /var/tmp/bench-out --work /var/tmp/bench-work
python3 -m bench install --out /var/tmp/bench-out --work /var/tmp/bench-work
```

The work directory needs about 25 GB; the guest uses 8 GiB of RAM and 4 vCPUs.

## Reading a run

The artifact `bench-<host>-<mode>` holds:

- `steps.json`: each step, its exit status and seconds;
- `iso/`: the ISO chosen, `gpgv`'s status, `grub.cfg` before and after, the boot records before and after;
- `serial-*.log`, `console-installed.log`, `screens/`: what the machines printed and showed;
- `qemu-*.argv`: every QEMU command line;
- `negative-control.json`, `secure-boot.json`, `layout.json`: the verdicts, failures first;
- `guest/`: the installed system's listings (`zpool get all`, `zfs get`, `findmnt`, `lsblk`, crypttab), its kernel journal and `/var/log/installer`;
- `probe/` in probe mode.

A run whose leak check fails, or cannot run, uploads nothing; its job log says why.

## What it does not cover

A virtual machine is not a real computer: USB-SATA bridges, a real firmware's quirks, power loss and real disk timings are outside it. `zark selftest` on real hardware and the certificates it produces cover what they can.
