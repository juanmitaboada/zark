# Upgrading from zark 1.x

What changes when a system and its drives move from zark 1.x to zark 2.

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
