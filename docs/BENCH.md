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

After step 3 every run reads the ISO without booting it: the installer snap's version and subiquity, cloud-init's datasource configuration, whether `openssh-server` is in the live, and which of the packages `zark recover` needs the live carries. `probe` mode stops there; it takes about ten minutes.

## Mode full: zark itself

`full` does all of the above, then runs zark as a user would, with every question answered through its terminal:

1. Builds zark's `.deb` and release tarball (`make deb`) from the commit checked out, before the ISO is fetched.
2. On the installed system: turns apt's daily timers off and waits for apt to be idle, writes 1 MiB of random data to `/root` and `/home/bench`, installs the `.deb` with apt, runs `zark setup`, plugs in a USB disk (QEMU's `usb-storage` on an xHCI controller, with an invented serial), runs `zark prepare` on it and then `zark backup`.
3. Boots the 26.04 live (its `grub.cfg` edited again, with no `autoinstall`) on the same machine with a new blank disk and no old one; a NoCloud seed gives it the bench user and installs `openssh-server`, which the live lacks, with `runcmd` (the live's cloud-init runs no packages module), then puts the package's own `sshd_config` in place of the one-line file cloud-init's `ssh_pwauth` left there (sshd would otherwise run without PAM and refuse the bench user, whose password is locked), password logins off in a drop-in; cloud-init's output and the install's go to the serial console too, and a live that never answers SSH leaves those lines in `live-ssh-timeout.txt` and the job log. Secure Boot is proved in the live as on the installed system. The release tarball goes into `/tmp`, and `./zark recover` restores the backup onto the new disk.
4. Boots the recovered disk with Secure Boot, answers the one unlock prompt, proves Secure Boot and checks the layout again, checks the random data and the drive's anchors that recover left on every restored dataset, runs `zark finish`, plugs the USB disk in again and runs `zark backup`, which must send nothing in full and roll nothing back.

zark has no non-interactive flags. The bench runs each command under a pty over SSH and reads the question zark publishes in `/run/zark/progress.json` (`docs/STATUS-FILES.md`); the answer comes from a table in `bench/drive.py`, written from zark's source. A question the table does not hold ends the command with end-of-file, as if nobody were there, and fails the step. The passphrase is typed only once zark's `Passphrase:` prompt is on the screen and the line has been quiet.

The live gets `openssh-server` from the archive, so its apt lists are filled before `zark recover` runs: a missing tool recover installs with apt is installed here where a user's fresh live may have no lists. `probe/probe.json` and `guest-live/tools.txt` say which of them the live had.

The installed system is changed in three ways, all by autoinstall's `late-commands`: GRUB and the kernel also use the serial console (`/etc/default/grub.d/90-zark-bench.cfg`, without `quiet splash`), and the bench user may use `sudo` without a password (`/etc/sudoers.d/bench`). Nothing touches the boot chain's binaries, the pools or the keystore.

## Secrets

The disk passphrase and the user's password are made by each run, in memory, and masked in the job's log. They never appear in a command line (the run refuses to start QEMU or a command with one). They are written to the seed image and, until the seed is built, to the seed's user-data file, which is removed right after; the run deletes the seed when it ends. The passphrase also goes, typed, to the serial console at the unlock prompt and, in mode `full`, to zark's `Passphrase:` prompts through a pty. In mode `full` the run also reads rpool's raw key from the installed system into memory, and searches for it as bytes in the artifacts and the disk images. No repository secret is used.

## Running it

By hand, from a checkout with `gh` signed in:

```bash
gh workflow run bench.yml --ref <branch> -f host=both -f mode=probe
gh workflow run bench.yml --ref <branch> -f host=ubuntu-24.04 -f mode=install
gh run list --workflow bench.yml --limit 5
gh run view <run id> --log
gh run download <run id>
```

`host` is `ubuntu-24.04`, `ubuntu-26.04` or `both`; `mode` is `install`, `full` or `probe`; `iso` names one ISO of the 26.04 directory instead of the newest. The workflow also runs every Monday at 03:17 UTC on `main`, on both hosts, in mode `full`; a fork that enables Actions does not.

GitHub disables scheduled workflows in a public repository after 60 days without activity in it; the Actions tab then offers to enable it again (`gh workflow enable bench.yml`).

### On a machine of your own

The same scripts run on an Ubuntu machine with KVM, without registering it as a runner (a self-hosted runner on a public repository would run code from anyone's pull request). The preparation script installs packages and writes a udev rule, so read it first; on your own machine you may prefer to install the packages it lists by hand and keep `/dev/kvm` as it is (being in the `kvm` group is enough). Then, from the checkout:

```bash
python3 -m bench probe --out /var/tmp/bench-out --work /var/tmp/bench-work
python3 -m bench install --out /var/tmp/bench-out --work /var/tmp/bench-work
python3 -m bench full --out /var/tmp/bench-out --work /var/tmp/bench-work
```

The work directory needs about 25 GB, 35 GB in mode `full` (which also needs `make deb`'s tools: `devscripts debhelper dh-python fakeroot lintian pandoc`, as the preparation script installs them); the guest uses 8 GiB of RAM and 4 vCPUs. Mode `full` builds in a `git worktree` under the work directory and removes it at the end.

## Reading a run

The artifact `bench-<host>-<mode>` holds:

- `steps.json`: each step, its exit status and seconds;
- `iso/`: the ISO chosen, `gpgv`'s status, `grub.cfg` before and after, the boot records before and after;
- `serial-*.log`, `console-installed.log`, `screens/`: what the machines printed and showed;
- `qemu-*.argv`: every QEMU command line;
- `negative-control.json`, `secure-boot.json`, `layout.json`: the verdicts, failures first;
- `guest/`: the installed system's listings (`zpool get all`, `zfs get`, `findmnt`, `lsblk`, crypttab), its kernel journal and `/var/log/installer`;
- `probe/`: what the ISO read found;
- in mode `full`: `full.json` (every verdict of steps 4 to 6), `zark/` (the build, each zark command's transcript and the questions it asked with the answers given, the passphrase shown as `<passphrase>`), `guest-installed/`, `guest-live/`, `guest-recovered/` (zark's log, `status.json`, the registry, the snapshots and bookmarks, each guest's own leak search).

A run whose leak check fails, or cannot run, uploads nothing; its job log says why.

Every wait in a run is bounded. The steps run inside a budget of their own, 215 minutes of the job's 240: when it runs out, or when a step's own timeout fires (the install's is an hour), the machine is stopped and the leak check and the upload still run. A timed-out install leaves its screens, its serial log and `install-timeout.txt` (the VM's state and the work directory's free space).

## What it does not cover

A virtual machine is not a real computer: USB-SATA bridges, a real firmware's quirks, power loss and real disk timings are outside it. `zark selftest` on real hardware and the certificates it produces cover what they can.
