# Upgrading zark

What changes when a system and its drives move from zark 2.1 to 2.2, from zark 2.0 to 2.1, and from zark 1.x to zark 2, and what to know before upgrading Ubuntu from 24.04 to 26.04 on a system zark backs up.

## Upgrading from zark 2.1

zark 2.2 changes one thing in `/etc/zark/known_drives.json`: an entry may carry `enclosure_id`, and `drive_id` may be the disk's own `ata-` name instead of the enclosure's `usb-` name. Nothing on a drive changes. On Ubuntu 24.04 and 26.04 no `zark setup` run is needed after the package upgrade; on a 26.10 system with `/boot` on ext4, run `sudo zark setup` once to create `rpool/ROOT/<be>/zark-boot` (see **Ubuntu 26.10.** below).

**Why.** A `usb-` name is made by the enclosure's bridge. Two disks that go through one enclosure share it, and 2.1 took the second for the first: `purge` and `prepare` could remove the other drive's entry and its anchors in the source. 2.2 recognises a drive by the pool label on the disk, or by the disk's own name, and treats an enclosure's name as evidence about the enclosure only.

**What happens to your entries.** Nothing at the upgrade. An entry is corrected the first time its drive is connected and its pool GUID is verified by a command that may write the registry: `zark backup`, or `zark registry fix`. The entry then holds `drive_id` (the disk's own name, when the disk has one) and `enclosure_id` (the `usb-` name it is seen under). A drive behind an enclosure that hides the disk's name keeps its `usb-` name as `drive_id` and gets no `enclosure_id`. No entry is dropped and no anchor is touched. `zark registry list` shows both names.

**What asks now.** `purge` and `prepare` used to treat an entry that shared only the enclosure's name as the disk's own. Such an entry (every entry 2.1 wrote, until it is corrected) is now asked about, and kept by default. `zark explore` no longer calls two drives that share an enclosure's name duplicates.

**The way back.** 2.1.1 reads and rewrites a registry that has `enclosure_id` and keeps the field. On its next backup it sets `drive_id` back to the enclosure's name, as it does for any entry; 2.2 corrects it again at the next verified connection.

**Ubuntu 26.10.** zark 2.2 also adds Ubuntu 26.10's layout, `/boot` on ext4 with no bpool. A point of a 26.10 system is recovered with zark 2.2.0 or later only: zark 2.1.1 and earlier take it for a bpool point, erase the target disk and leave the restored system without `/boot`. Recover a 26.10 system from a live USB with a 2.2 portable tree, or with the 2.2 package installed in the live session. Points of 24.04 and 26.04 systems are recovered by either version.

## Upgrading from zark 2.0

zark 2.1 keeps 2.0's drives, registry and backup points as they are: no format on a drive or in `/etc/zark/known_drives.json` changed, and every 2.0 drive goes on with its next backup as before.

**What the package upgrade does by itself.** `sudo apt upgrade` replaces the code under `/usr/share/zark/` and the self-test's certificates there and byte-compiles it (the bytecode is the package's, removed with it), creates `/run/zark` (from `/usr/lib/tmpfiles.d/zark.conf`, also at every boot) and installs the status files' description under `/usr/share/doc/zark/`. `/etc/zark/`, `/etc/sanoid/sanoid.conf`, the guards and the alerts are left as they are.

**Then, once per system:** `sudo zark setup`. It rewrites the alerts timer, which now also fires on every wall-clock hour (`OnCalendar=hourly`), so the status is refreshed right after a laptop resumes. As in 2.0, it shows any difference between its snapshot policy and `/etc/sanoid/sanoid.conf` and asks "Apply these changes?" before writing it. Until `setup` runs, the timer installed by 2.0 keeps working: it runs the new code every hour of uptime.

**What 2.1 adds:**

- The status files. zark itself keeps `/var/lib/zark/status.json` (the source pools, every registered drive with its last backup, its last run, its space and what it holds) and, while a command runs, `/run/zark/progress.json` (its step, what it waits for, a backup's transfer). Both are readable without root and described for other programs in [STATUS-FILES.md](STATUS-FILES.md) (installed as `/usr/share/doc/zark/STATUS-FILES.md.gz`, the JSON Schemas in `/usr/share/doc/zark/schema/`). The first `status.json` is written by the next command that changes state, by `sudo zark status`, or by the timer within the hour; nothing is written from a live session.
- `zark monitor` reads those files instead of polling, and runs without `sudo`.
- The GNOME Shell extension, on Ubuntu 26.04: `sudo apt install gnome-shell-extension-zark` from the same PPA. zark suggests it and does not install it.

**What behaves differently:**

- `recover` creates only the top of a subtree excluded from backups at the point it restores (empty, with its properties); the datasets below it are not created.
- `backup` and `prepare` refuse a system without a bpool of its own, naming where `/boot` is (Ubuntu 26.10 and later put it on ext4 on UEFI machines, Secure Boot on or off; support is planned for 2.2, [#5](https://github.com/juanmitaboada/zark/issues/5)); 24.04 and 26.04 LTS, as their installers lay out ZFS, are the supported series. zark 2.2 added that layout: see **Ubuntu 26.10.** under [Upgrading from zark 2.1](#upgrading-from-zark-21).
- For contributors: `make test-real` and 1.x's QEMU harness are gone; validation is the self-test and a checklist on real hardware.

**The way back.** 2.0.0 reads everything 2.1 writes on drives and in the registry. Going back is installing 2.0.0's package with `sudo apt install --allow-downgrades ./<its .deb>`, never `apt remove zark` (it removes the guards and alerts); the status files are then left as they are and no longer updated.

## Upgrading from zark 1.x

zark 2 replaces 1.x's syncoid with its own replication: each drive is anchored by its own bookmarks in the source, receives are never forced, and nothing on a drive is destroyed without a question (see [Why not syncoid?](DESIGN.md#why-not-syncoid) and [Anchors](RETENTION.md#anchors)). Drives written by 1.x keep working; each one needs one zark 2 backup to move over.

**What the package upgrade does by itself.** `sudo apt upgrade` (or `sudo apt install zark`) replaces the code under `/usr/share/zark/` and adds the self-test's certificates there. Nothing else changes: `/etc/zark/known_drives.json` is kept as it is, 1.0.12 has no maintainer script that runs on upgrade and 2.0.0's only makes sure `/etc/zark/` exists, `/etc/sanoid/sanoid.conf` is not touched, and the guards 1.x installed (the APT hook, the update-grub guard, the MOTD reminder) keep working until `setup` replaces them. The package is now `amd64` (it was `all`); apt upgrades it the same way. Removing the package (not upgrading it) also removes what `zark setup` installed, except sanoid's configuration.

**Then, once per system:**

1. `sudo zark setup`. It shows the snapshot policy and, when `/etc/sanoid/sanoid.conf` differs from it, the differences, then asks "Apply these changes?" (the old file is kept as `sanoid.conf.backup.<date>`; answer `n` to read it first and run `setup` again). It installs or replaces the APT and update-grub guards and installs the status alerts (a MOTD line, an hourly timer and desktop notifications). If rpool's `feature@bookmark_v2` is disabled it asks to enable it: backups need it for their anchors, and once enabled it cannot be disabled again (GRUB never reads rpool, and every live since Ubuntu 20.04 imports it).
2. One `sudo zark backup` to each drive, as usual. The first one to a drive written by 1.x can ask once:
   - "N dataset(s) on <drive> hold snapshots newer than anything origin still has: first run of this zark on a drive written by an older one.", then "How should this backup continue?". This is the case 1.x resolved by destroying those snapshots silently (`receive -F`). Choose **keep the N newer snapshot(s) in archived lineages and continue from the common snapshot**: nothing is destroyed and nothing is resent, and the archived snapshots stay restore points until retention or `zark prune` removes them. The option is offered when the transfer fits; otherwise abort, and free space first.
   - "N dataset(s) on <drive> changed since their newest snapshot": a read-write mount of the drive left writes there. Skipping them (the default) changes nothing and ends the backup INCOMPLETE; discarding them (typed DISCARD) rolls those datasets back to their newest snapshot.
   - "<dataset> exists only on the backup": a dataset 1.x sent that the source no longer has. "ask again next time" (the default) changes nothing.

   After that backup the drive is anchored by bookmarks, and `zark status` no longer lists it as having "no anchor in origin".
3. After every registered drive has had its first zark 2 backup: `sudo zark prune`, origin, "remove the legacy residues". It lists the `syncoid_*` snapshots 1.x left in the source and the keystore's `prepare_*` ones, and removes them after a `[y/N]`; a `syncoid_*` snapshot that a drive may still depend on is kept and named.

Until a drive has had its first zark 2 backup, do not use `zark prune`'s "remove sanoid snapshots older than N days" in the source: a sanoid snapshot is that drive's only base there (prune warns about it).

**1.x precautions that no longer apply:** stopping `apt-daily-upgrade.timer` around a backup (backup pools are imported with nothing mounted, and the APT guard refuses a boot-critical upgrade while a backup pool is attached); avoiding `zark clean`, `prepare` and `purge` (each touches only what is zark's, after a question); keeping 1.x's `syncoid_*` snapshots (each drive has its own bookmarks); checking `known_drives.json` by hand after an edit (`zark registry list|forget|fix`). **Still true:** never destroy snapshots by `%` range or by hand on a backup drive, and after a crash during a backup, unplug the drive before booting.

**The way back.** Until a drive's first zark 2 backup, 1.0.12 can still back up to it (keep a copy of its `.deb` and of `/etc/zark/known_drives.json`). A drive zark 2 has written must not be used with 1.x again: syncoid would roll back zark 2's backup points.

## Upgrading Ubuntu from 24.04 to 26.04

zark itself goes on working after the release upgrade: on the test machine, `zark status` and the self-test passed on the upgraded system. Two things in the upgrade need you, on a system installed with the installer's encrypted ZFS layout.

**Before it.** Back up with `sudo zark backup`, then unplug the drive before you start: zark's apt guard aborts any change to boot-critical packages (kernel, GRUB, shim, ZFS) while a backup drive is connected, and a release upgrade changes all of them.

**To avoid a stop halfway.** On a system like this the upgrade can stop halfway (next paragraph). It renames the key-loading unit that rpool's mounts depend on; if a unit with the new name is already active, systemd has nothing to stop. On the test machine, with the steps below, the graphical upgrade from the Software Updater went through with every dataset mounted (tested once). Run them in the same boot session, immediately before you start the upgrade, with no reboot in between: the unit lives in `/run`, and a reboot removes it.

1. Create the unit and start it:

   ```bash
   sudo install -d /run/systemd/system
   printf '[Unit]\nDescription=Key unit name of OpenZFS 2.4, active before the upgrade\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/true\n' | sudo tee /run/systemd/system/zfs-load-key@rpool.service
   sudo systemctl daemon-reload && sudo systemctl start zfs-load-key@rpool.service
   ```

2. Check that `systemctl is-active zfs-load-key@rpool.service zfs-load-key-rpool.service` prints `active` twice.
3. Upgrade as usual and reboot when it asks. After the reboot the unit is gone, and Ubuntu's own `zfs-load-key@rpool.service` takes its place.

Create it in `/run/systemd/system`, never in `/etc/systemd/system`: there it would hide Ubuntu's unit for good.

**If the upgrade stops halfway.** On the test machine the graphical upgrade from 24.04 to 26.04 stopped twice at the same point: while configuring packages, systemd unmounted part of rpool's datasets, the desktop session closed and the upgrader and dpkg stopped with it. The cause is in Ubuntu's ZFS packages, not in zark (the mount units of OpenZFS 2.2 and 2.4 depend on differently named key-loading units), so it can affect any 24.04 system with an encrypted ZFS root; it is reported to Ubuntu as [LP #2168897](https://bugs.launchpad.net/ubuntu/+source/zfs-linux/+bug/2168897) and has not been fixed at the time of writing. If it happens to you, because the steps above were not followed or for any other reason:

1. Switch to a text console with Ctrl+Alt+F3 and log in. The network may be down until the reboot.
2. Finish the interrupted upgrade with `sudo dpkg --configure -a`. If `sudo` is not found (it was missing on the test machine at that point), run `/usr/lib/cargo/bin/sudo dpkg --configure -a`.
3. Reboot. On the test machine the system then started as Ubuntu 26.04 with every dataset mounted.

**After it: zark's PPA.** The release upgrader disables third-party sources: it marks zark's PPA `Enabled: no` and keeps zark at its 24.04 build, which then gets no updates. To enable the PPA for 26.04 and move zark to its 26.04 build:

```bash
sudo sed -i -e 's/^Suites: .*/Suites: resolute/' -e '/^Enabled: no$/d' /etc/apt/sources.list.d/juanmitaboada-ubuntu-zark-*.sources
sudo apt update
sudo apt install zark
```

On 26.04 the GNOME Shell extension is also available from the same PPA: `sudo apt install gnome-shell-extension-zark`. On the test machine zark's guards and the snapshot timer were still in place after the upgrade, so the next step is another backup.
