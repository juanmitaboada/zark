# Changelog

All notable changes to **zark** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

zark 2.1 is being built as milestones on `main`, versioned `2.1.0-rcN`
(`lib/config.py`) and `2.1.0~rcN-1` UNRELEASED (`debian/changelog`).
Nothing is uploaded to the PPA until the final 2.1.0.

### M7 - the status writer (2.1.0-rc1)

#### Added

- **Status files for other programs** (`docs/STATUS-FILES.md`, schema
  version 1, with JSON Schemas in `docs/schema/` and fixtures for every
  state in `tests/fixtures/status/`). zark is not a daemon, so a GNOME
  Shell extension, `zark monitor` or a script learns what it does from two
  files it writes, readable without root:
  - `/run/zark/progress.json` (`lib/progress.py`) while a command that
    changes state runs: its numbered step, whether it waits for an answer
    and for what, and for `backup` the drive with its space, the backup
    point and the transfer (datasets planned, done and failed, the one
    being sent, the estimated stream size, the bytes sent and their
    rate). Written at once, on every event and from a heartbeat every 2 s
    inside the zark process, at most once a second; removed when the
    command ends, after its result is in `status.json`. A file left by a
    killed run is an orphan the next command records as `interrupted`.
  - `/var/lib/zark/status.json` (`lib/status_file.py`): the source pools'
    space (`zfs` and `zpool` figures, read together) and datasets, every
    registered drive with its retention, last backup, last run and space
    as last measured, and the MOTD's lines as structured notices. Written
    at the end of every command that changes state, by `backup` when its
    result is known (before the eject prompt), and by `zark status` by
    hand and from the alerts timer; never from a live session.
- **Every backup ending is recorded** as the drive's `last_run`:
  `completed`, `incomplete`, `not_verified`, `aborted` (nothing was sent)
  or `interrupted` (a receive began), with the reason.
- **bytes_done measured by the kernel**, outside the data path: the
  `wchar` count of each `zfs send` (its process group, live; the exited
  shell's, exactly, at the end). The send, its stderr and the pipe are
  unchanged.
- **`/run/zark` from the package's `tmpfiles.d` entry** at boot and on
  install, so a reader can watch it before zark first runs.
- **Self-test checks SND-13, PRC-05, SYS-02 and POOL-14** (level 1 has
  55): the send's count, field 22 of `/proc/<pid>/stat`, a tmpfiles.d
  directory's mode, and a pool's figures and dataset shares.
- **CI on named Ubuntu series** (RELP.8): tests and lint on 24.04; the
  `.deb` is built on 24.04 and installed on 24.04 and 26.04 on a tag and
  when run by hand (`workflow_dispatch`).

#### Changed

- The MOTD file is refreshed at the end of every command that changes
  state, a failed backup included; 2.0.0 refreshed it after a successful
  backup and a prune only. The MOTD and `status.json` come from one read.
- A source pool whose datasets cannot be listed has a MOTD line of its
  own.
- `/run/zark` and `/var/lib/zark` are 0755 whatever the umask, and made
  so when an earlier zark left them narrower.
- The alerts timer also fires on every wall-clock hour
  (`OnCalendar=hourly` next to `OnUnitActiveSec=1h`): the monotonic timer
  pauses while the machine sleeps, the calendar one catches up on resume. The next
  `zark setup` or `zark alerts install` rewrites an installed unit.
- `zark status` writes `status.json` too.
- While `known_drives.json` cannot be read or parsed, no command writes
  `status.json` (a warning names the cause), so every drive's last run,
  space and point stay as they were until the registry is fixed (M7R-1).
  An absent registry is no drive, written as before.

#### Known limitations

- `wchar` also counts what `zfs send` writes to stderr (a few bytes, an
  error message). On an architecture without large kernel stacks the
  stream would be written from a taskq thread and not counted; zark is
  `amd64` only, and SND-13 certifies the count on the running platform.
- With BSD process accounting on (atop's `atopacct`, `acct`), the kernel
  writes a 64-byte record from every process that exits, so `bytes_done`
  counts 64 bytes more per exited process of a send (a few hundred bytes
  a run); SND-13 measures those records and certifies the rest exactly.
- Reading another process's `/proc/<pid>/io` needs root: zark runs as
  root; a reader never needs it.
- `transfer` is reported for `backup` only; `recover` moves as much data
  without it.
- `status.json` survives `apt purge zark`.

#### Rejected approaches

- `zfs send -vP` parsed from stderr for the byte count: it changes the
  command and its log line, its counter restarts at each intermediate
  snapshot of `-I`, and its lines would fill the stderr pipe zark reads
  only at the end. A counting relay, `pv` or `mbuffer` between send and
  receive: in the data path. `-V`: rounded figures.
- A signal timer in the main thread for the heartbeat: a handler can run
  in the middle of anything, the cleanup included. A thread waits instead.
- A resident service, or readers running `zark status --json`: zark keeps
  the files itself, with no resident process.
- `jsonschema` as a test dependency: the suite stays standard library
  only, with a validator for exactly the keywords the schemas use.
- A 15-minute timer: four times the reads of every snapshot in the
  source, for figures readers can read live themselves.

## [2.0.0] - 2026-10-23

zark 2 replaces the replication of zark 1.x and adds the commands that
manage space, alerts, exclusions and the platform it runs on. This
section is for 1.x users; the development history below it (M1 to M6,
published only as `2.0.0-rc1` to `2.0.0-rc11` on `main`) has every change
in detail, with the approaches that were rejected.

### For zark 1.x users

#### What changed, and why

- **zark's own replication instead of syncoid.** syncoid based each
  incremental on the newest snapshot common to both sides and received
  with `-F`, so every backup destroyed the drive's snapshots newer than
  that base, the previous backups' restore points, once the source had
  pruned them. zark 2 takes one atomic backup point (`zark_<UTC>`) of
  every replicated dataset, sends from the drive's newest snapshot, found
  in the source as a snapshot or, once pruned there, through this drive's
  bookmark (`#zark_<pool GUID>_<UTC>`), and never forces a receive.
  Anything that would destroy data on a drive is a question asked before
  the transfer starts. `bpool` takes no bookmarks (GRUB must keep reading
  it); zark keeps one snapshot per drive there instead.
- **An honest verdict.** BACKUP COMPLETED only when every dataset expected
  reached the point; otherwise BACKUP INCOMPLETE, exit 1, and the drive's
  `last_backup_at` is not written. Each run prints and logs a table per
  dataset.
- **Retention on the drive.** Per drive: everything of the last 30 days,
  then the newest complete point of each month (`window`, `max_age`, off
  by default, and a free-space `margin` of 10%, in the registry). Before a
  transfer that would not leave the margin free, zark applies the policy
  and offers the oldest monthly points.
- **A drive is never mounted over the running system.** Backup pools are
  imported with nothing mounted under an alternate root, and every
  receive is unmounted: 1.x could mount a drive's `bpool` over `/boot`
  during a backup. The APT and update-grub guards also count an imported
  zark drive.
- **recover** shows its plan per dataset, never mixes points of different
  times, orders points by creation, checks the drive's features against
  the live's ZFS before touching anything, creates both pools with the
  features the source had at that point, and anchors what it restored for
  the drive it read.
- **One zark run at a time** (exit 75 for a second one), and a stopped run
  (Ctrl-C, SIGTERM, a closed terminal) stops its child first and then
  cleans up once.

#### The first backup of a 1.x drive

It can ask once, when the drive holds snapshots newer than anything the
source still has ("first run of this zark on a drive written by an older
one"): the case 1.x resolved by destroying them silently. Keeping them in
archived lineages and continuing from the common snapshot destroys and
resends nothing. After that backup the drive is anchored by its own
bookmarks and its next backups are incremental whatever sanoid prunes.
README's "Upgrading from zark 1.x" and the manpage's WORKFLOWS have the
whole sequence: `zark setup`, one backup per drive, then `zark prune` for
1.x's residues.

#### Commands

- New: `status` (the source pools' fill and each drive's backup age),
  `alerts` (the MOTD line, an hourly timer and desktop notifications for
  them), `prune` (reclaim space in the source or on a drive: sanoid
  snapshots by age, 1.x's `syncoid_*` and keystore `prepare_*` residues,
  points left by interrupted runs, a drive's oldest points or its 1.x
  history), `registry list|forget|fix|retention`, `exclude list|add|remove` (a
  dataset left out of backups, of sanoid's snapshots, or both),
  `selftest` (checks on this platform what zark relies on; `status`,
  `report`, `warnings on|off`), `fix-rpool-mountpoint` (gives a restored
  rpool back `mountpoint=/`, from a live).
- Changed: `backup` (above; `--no-snapshot` is accepted and does
  nothing), `prepare` (every question before the transfer; the drive's
  pool with `compatibility=openzfs-2.2`; registered only after a verified
  transfer), `recover` (above; from an Ubuntu 26.04 or newer live),
  `purge` (by the drive's identity, every step verified, PURGE INCOMPLETE
  otherwise), `health` (one entry per disk, the pool's state, exit 1 when
  the destructive test fails), `clean` (lists what an interrupted run left
  and asks; never forces an export, never deletes a file), `setup` (the
  guards, the alerts, `feature@bookmark_v2`; `--remove` undoes them, and
  removing the package runs it), `finish` (does not export the running
  system's pools; names only what `setup` would still do), `mount`,
  `umount`, `chroot`, `mount local` and `repair-boot` (read-only mounts
  are read-only imports; targets resolved inside the tree).
- Removed: `repair-divergent` (its auto-repair destroyed drive datasets
  without asking; backup's own questions replace it). syncoid is no longer
  used; sanoid still takes the source's snapshots.

#### Ubuntu series

2.0.0 is validated on Ubuntu 24.04 LTS (noble) and 26.04 LTS (resolute),
installed with the installer's encrypted ZFS layout, and published in the
PPA for both. A system is recovered from an Ubuntu 26.04 or newer live.
The package is `Architecture: amd64` (1.x's was `all`). Ubuntu 26.10 and
later are not validated yet.

#### Known limitations a 1.x user meets

- **After a confirmed destroy of a dataset that exists only on the
  backup**, the backup points that held it read PARTLY REMOVED in
  recover's menu; such a point still restores everything but the
  destroyed dataset (M5bV-4). The destroy is refused by ZFS when an
  archived lineage of that dataset is a clone of one of its snapshots: the
  run ends COMPLETED with an ERROR line whose text is wrong ("Failed:
  destroyed ..."), and nothing is destroyed (M5bV-3). Both are fixed in
  2.1.
- **A drive written by 1.x keeps the mount properties 1.x sent with its
  datasets** (`bpool`'s boot environment has `mountpoint=/boot`): zark
  imports it with nothing mounted, but a `zpool import` by hand without
  `-N` could mount it over the running `/boot`. Import a drive through
  zark.
- **The history 1.x wrote on a drive stays** until `zark prune` on that
  drive removes it; retention never touches it.
- **Recovering a point 1.x wrote** (it records no pool features) creates
  `rpool` with every feature of the live's ZFS, with a warning: a 24.04
  system restored that way may not import `rpool` with its own OpenZFS
  once one of them becomes active.
- **A 24.04 system is recovered from a 26.04 or newer live**; an older live
  gets a warning. recover selects shim's `.latest` variant; on 24.04 with
  Secure Boot, a kernel the new shim revokes then cannot boot, where
  shim-signed's own postinst would have chosen `.previous`.
- **On 24.04, OpenZFS 2.2.2's `zfs receive`** refuses streams that carry
  the `longname` or `large_microzap` flags: such a dataset fails and the
  backup is INCOMPLETE (only a drive or a pool a newer OpenZFS made can
  have them).
- Volumes other than the keystore are not backed up (listed every run). A
  directory cannot be excluded; it has to become a dataset first.
- A drive's keystore is the copy `prepare` made: after `cryptsetup
  luksChangeKey` or `zfs change-key` in the source, `zark purge` and
  `zark prepare` each drive.
- Desktop notifications need `notify-send` (libnotify-bin). `zark monitor`
  shows `zpool list`'s figures. recover's `zfs mount -a` on the restored
  tree does not resolve symlinks inside it.
- The self-test does not check what needs a boot, a package operation or
  a cable: GRUB, Secure Boot, dracut, the guards, a USB bridge's cache
  flushes (`zark health` tests an enclosure).

### Development history: M1 to M6

The milestones as they went into `main`, newest first, each with its
known limitations at the time (several fixed by a later milestone) and
the approaches rejected.

### M6: Ubuntu 24.04 (noble), and origin's pools on the drive (2.0.0-rc11)

#### Added

- **Each backup point records how origin's pools were** (M6-5 5a):
  prepare and every backup write `org.zark:pools` on every snapshot of
  the point, in the same `zfs set` as its exclusion record: for rpool
  and bpool each feature that is enabled or active, with its state (one
  the tools do not know as `unsupported@<guid>`), the pool's
  `compatibility`, and origin's Ubuntu release and OpenZFS tools and
  module, as one JSON value well under a user property's 8191 bytes. A
  pool that cannot be read is a warning and the point is written without
  the record (M6-9 9a).
- **recover creates rpool and bpool as origin had them** (M6-6 6a):
  `zpool create -d` with only the recorded features this live's ZFS
  knows, and origin's `compatibility` when the live has its file (left
  off and named otherwise). Decided right after the plan, before the
  target is chosen: a feature origin had active that the live lacks is a
  FATAL naming the pool, the feature and the release it needs ("Nothing
  was erased"); one origin only had enabled is left out with an INFO
  line. bpool created from the record does not get the dracut extended
  features. A point without the record keeps the old creation, with a
  WARN naming the live's ZFS and the import risk (M6-7 7a).
- **recover warns on a live older than Ubuntu 26.04** (M6-8 8a), naming
  its release and OpenZFS, and goes on: the drive's feature check and the
  pre-flight stop what that live cannot do.
- **Self-test check POOL-13** (M6-10): `zpool create -d` with a feature
  list enables only those and their dependencies, and `zpool get -H -p
  all`, read by the record's parser, shows each in its state.

#### Changed

- **ROLLBACK's keep (`Run.branch`) clones without `-u`** (M6-20, N4):
  OpenZFS 2.2.2 (noble) has no `zfs clone -u`, so the option failed on
  its first command there. The clone's `canmount=noauto` keeps either
  release from mounting it; the self-test's CLN-01, SND-09, SND-10,
  DST-02 and KEY-03 set up their clones the same way.
- **recover adds no keystore hook to an initramfs-tools system** (M6-4
  4a, N1): on noble the hook's local-premount script ran before
  zfs-initramfs imported rpool, so it never acted; noble's own zfs script
  opens the keystore after the import (part 0's L0 found `cryptroot` in
  the installer's initrd). dracut systems are unchanged.
- **The self-test's reboot warning** (M6-3 3a, N3) compares the loaded
  ZFS module with the one installed for the running kernel (`modinfo -F
  version zfs`), revision included, instead of the module with the tools:
  noble's HWE 7.0 kernel ships a 2.4.1 module under 2.2.2 tools by
  design.
- **POOL-06 and DST-02 read OpenZFS 2.2.2's texts** too (M6-20, N5, N6):
  `zpool import`'s listing indented one space more, and "dataset is
  busy" for the destroy of a held snapshot.
- **setup's `.previous` shim text** names both causes (M6-16 16a): the
  installer, or shim-signed's own postinst choosing `.previous` under
  Secure Boot for a kernel the new shim's SBAT revokes.
- **The package is `Architecture: amd64`** (M6-11): `zark_<v>-1_amd64.deb`.
- **recover gives both pools' roots the installer's properties** (M6R-1,
  M6F-2 2a): rpool's root now gets `compression=lz4` and `devices=off`,
  bpool's `relatime=on`, as subiquity sets them, so every restored
  dataset inherits `devices=off` (before, `on`: a device node in any
  dataset could be opened). `compression` was lz4 already through `on`,
  and `relatime` is on by default; both are now `local`. A system
  recovered before this (by zark 1.x or an earlier rc) gets the same
  with `zfs set devices=off compression=lz4 rpool` and `zfs set
  relatime=on bpool`: `devices` takes effect at each dataset's next
  mount, the next boot at the latest, and a dataset that needs device
  nodes (a chroot or container tree) keeps them with a local
  `devices=on`, which backups carry (M6F-3 3a).
- **recover checks that a restored initramfs-tools system can open its
  keystore** (M6R-2, M6F-4 4a): noble's zfs initramfs script opens it
  with `cryptsetup-initramfs`'s `cryptroot`, so a restored root without
  `/usr/share/initramfs-tools/scripts/local-top/cryptroot` or
  `/usr/share/initramfs-tools/hooks/cryptroot` is an error in the
  RECOVERY banner naming the package and the repair (`zark chroot`, then
  `apt install cryptsetup-initramfs && update-initramfs -u -k all`),
  where the boot would stop in the initramfs shell. An installer's root
  always has it; dracut systems are unchanged.
- **The no-record warning names the live when it is the live's fault**
  (M6R-4, M6F-6 6a): a whole record on a live whose `zpool upgrade -v`
  lists no feature now says so ("This live's zpool upgrade -v lists no
  feature, so origin's recorded pool features cannot be checked against
  it") instead of calling the point written before rc11 or incomplete;
  the pools are created as for a point without a record, as before.
- Comments that cite sanoid 2.3.0 or systemd v258/v259.5 also give
  noble's lines where zark relies on the behaviour; the sanoid defaults
  path in `lib/sanoid_retention.py` is `/usr/share/sanoid/` (M6-18,
  M6-19).

#### Tests

- The record: `zpool get`'s states, its size bound, a pool that cannot be
  read, the read-back (none, a disagreement, a value that is not a
  record), the one `zfs set`, prepare's and backup's points carrying it;
  `create_spec` and its options, compatibility present and missing.
- recover with a record (both `zpool create` lines and the plan's
  lines), a FATAL before any wipe, a point without a record, the bpool
  extended features only without it, the live warning on 24.04 only, the
  initramfs-tools root left without a hook.
- The fake refuses `zfs clone -u` as 2.2.2 does; the reboot warning on a
  stale module and not on noble's design; setup's two causes.
- A failed read of the point's record is recover's FATAL before any wipe;
  a record of another format, or with a feature state other than enabled
  or active, is refused on every snapshot alike (M6R-3, M6F-5).
- Both `zpool create` lines carry the installer's root properties, with
  and without origin's record (M6R-1).
- An initramfs-tools root with and without `cryptsetup-initramfs`'s
  script and hook (M6R-2).
- A whole record on a live whose `zpool upgrade -v` lists nothing
  (M6R-4).
- An initramfs-tools root with only one of `cryptsetup-initramfs`'s two
  files, each way: one error naming the missing file alone (M6V-1).

#### Known limitations

- **A 24.04 system is recovered from a 26.04 or newer live** only; an
  older live gets a warning and may stop at the drive's features or the
  pre-flight (M6-8).
- **recover forces shim's `.latest`** (M6-17 17a): on noble, when Secure
  Boot is on and an installed kernel is revoked by the new shim's SBAT,
  shim-signed's postinst would have chosen `.previous`; the restored
  system can then not boot that kernel.
- **sanoid 2.2.0 (noble) defers a prune** while `ps` shows a `zfs send`
  or `receive` naming the dataset (M6-18): zark's own sends stop
  `sanoid.timer` first, so only another tool's send defers it; a drained
  snapshot exclusion then loses its old snapshots one run later.
- **A drive with OpenZFS 2.3 or 2.4 features** passes recover's feature
  check on noble (its 2.4.1 module imports it), but noble's 2.2.2 tools
  cannot receive streams with the longname or large_microzap flags
  (M6-20, N8). Only a drive a 2.3+ system prepared without compatibility
  has them. **On the origin side** (V3, M6F-7 7a): a noble rpool that a
  recover from a newer live made (zark 1.x's, or an rc's before rc11)
  has those two features enabled, and a dataset where one becomes active
  (longname: a name of 256 bytes or more under `longname=on`, a property
  only a newer `zfs` than noble's sets; large_microzap: a microzap past
  128 KiB, with the `zap_micro_max_size` module parameter raised) sends
  streams that noble's 2.2.2 `zfs receive` refuses ("stream has
  unsupported feature"): that dataset fails and the run is INCOMPLETE.
  The other six features of 2.3 and 2.4 change no stream.
- A point written before 2.0.0-rc11 has no record: recover gives rpool
  every feature of its live's ZFS, as before, with a warning (M6-7).

#### Rejected approaches

- **One drive-level record rewritten at every backup** (M6-5 5b, 5c): it
  would give an old point the pools of a later `zpool upgrade`.
- **bpool's recorded features intersected with GRUB's `grub2` set**
  (M6-6 6b): origin's bpool was readable by the GRUB being restored.
- **Refusing a point without a record, or deriving a compatibility from
  the boot environment's OpenZFS** (M6-7 7b, 7c): no v2 drive outside the
  maintainer's has such points, and the derivation is guesswork.
- **A question for an old live** (M6-8 8b), and **a FATAL when origin's
  pools cannot be read** (M6-9 9b): the exclusion record's failure is a
  warning too.
- **Dropping the module warning, or leaving it** (M6-3 3b, 3c); **keeping
  the initramfs-tools hook** (M6-4 4b); **following shim's postinst
  choice in recover** (M6-17 17b).

### M5b: dataset exclusions, and M5a's leftovers (2.0.0-rc10)

#### Added

- **`zark exclude list|add|remove <dataset> backups|snapshots|both`**
  (D7, D8, M5b-2): an exclusion covers the dataset and its subtree and is
  a ZFS user property set on it in the source (`org.zark:replicate=no`,
  `org.zark:snapshot=no`; only `-s local` values count, M5b-3, M5b-4).
  Boot environments, the boot pool, the keystore, the structural
  containers and a dataset whose subtree holds one are refused, and a
  value found on them is ignored with a warning at every run (D10,
  M5b-5). `list` takes no lock; `remove` says what each registered
  drive's next backup does (M5b-11).
- **backup and prepare leave the excluded subtrees out** without making
  the run incomplete; the plan and the banner list them with their size,
  the source's `referenced` (D9, D12, M5b-12). Each point records what it
  left out, with each dataset's canmount and mountpoint, on its snapshots
  on the drive (`org.zark:excluded`, M5b-7, M5b-8). A record longer than
  one user property (8191 bytes) is written in parts,
  `org.zark:excluded.1` to `.n`, in the same `zfs set` as a pointer that
  names their count and the sha256 of the whole record; a missing part or
  a digest that does not match reads as an unreadable record (M5bR-1,
  M5bF-6). Past 64 parts the run stops before the point (M5bF-7).
- **A drive that still holds an excluded subtree** (D11, M5b-9, M5b-10):
  the copy stays live with its anchors in the source; the first backup to
  that drive asks once to keep it (the default, `org.zark:excluded-kept`)
  or destroy it (typed `DESTROY`), and `zark prune` on the drive offers
  the destroy (typed `PRUNE`). After a destroy the points that held it
  are recorded without it, so they stay complete, and that drive's
  anchors on it are removed from the source.
- **Included again**, a dataset continues from the copy a drive kept
  (an incremental from origin's bookmark) and the kept mark is cleared
  (M5b-11).
- **recover recreates what the point left out** as empty datasets, after
  `change-key -i` and before the backup pool is imported again, with
  `zfs create -u`, their canmount and mountpoint and, on the top, the
  exclusion itself; never from an earlier snapshot. A dataset excluded
  from snapshots only gets its exclusion set again after the receive: a
  raw stream carries no user property (M5b-13).
- **mount and status name the exclusions** (M5b-13b); the MOTD does not.
- **Self-test checks SNP-07** (a user property: local where set,
  inherited below, listed with `-s local` only there) and **KEY-04**
  (`zfs create -u` under an encrypted parent: its key, its properties,
  not mounted), level 1 (M5b-16); **SNP-08** (user properties of 8191
  bytes, several in one `zfs set` on snapshots, read back as a list; 8192
  refused), level 1, for the record in parts (M5bF-10).

#### Changed

- **setup writes a snapshot-excluded subtree in sections of its own**
  that take no snapshots and let sanoid prune the ones it took, outside
  every recursive rule: sanoid 2.3.0 applies a recursive section over its
  children's own sections in hash order (M5b-6). backup and status warn
  when sanoid.conf is not setup's.
- **health**: "DESTRUCTIVE TEST DID NOT FINISH" says to purge the drive
  first when its test pool could not be removed; the disk's USB id is
  read once, before the eject, and both kernel names count at the unplug
  (M5aR-2, M5aR-3).
- **mount's banner keeps its border** with long archived-lineage names,
  listed after it; **purge's eject question** names the drive (M5aP.7,
  M5aP.8).
- **The PPA series** come from one Makefile table (M5aP.4).

#### Fixed

- **The level-2 self-test's teardown** reads the vdev lines of `zpool
  list -v` only, never the pool line (M5aR-5).
- **A point an interrupted run left on a dataset that is then excluded**
  is anchored for the drive it names, this drive or another, before it
  is removed, as on any other dataset (M5bR-2, M5bV-1). A point the drive
  never received there is anchored too; the first verified run after the
  dataset is included again supersedes it.
- **setup reads the exclusions strictly**: a failed read stops setup's
  sanoid step (and `zark exclude`'s) before it writes anything, and the
  sanoid.conf check of backup, status and finish says it could not read
  them (M5bR-4).
- **A destroy of a drive's held copy** rewrites a record in parts as it
  was written, and destroys nothing when the record of a point it would
  rewrite cannot be read (M5bR-1, M5bF-9).

#### Tests

- backup's freeing wait and the banner's re-read are driven from
  `run()`, so the review's mutations R6, R8 and R9 are killed (M5aR-1);
  the lock test no longer creates `/run/zark` on the host (M5aR-4).
- one test for each mutation of the M5b review's probe that no test
  noticed (M5bR-5); recover's need for `rpool/USERDATA` is a function of
  its own so its test can reach it.
- the record's digest alone refuses a part changed alike on every
  snapshot of a point, and a rewrite past the bound on the parts writes
  nothing and leaves the point PARTLY REMOVED (M5bV-2).

#### Known limitations

- A directory cannot be excluded: it has to become a dataset first, by
  hand, as the manpage's `exclude` entry describes (M5b-15).
- `zark status` has no line per drive: the registry does not know what
  each drive holds (M5b-13b).
- The point's exclusion record and its completion mark are two `zfs set`
  calls, the record first: a run stopped between them leaves an
  incomplete point that carries its record. A complete point lacks the
  record only when its own `zfs set` failed, which the run warns about;
  recover then restores that point as if nothing were excluded (M5bF-4,
  M5bR-3).
- Parts a rewrite after a destroy no longer needs stay on the point's
  snapshots; the pointer's count means they are never read (M5bF-8).
- backup's destroy of an orphan is refused by ZFS ("filesystem has
  dependent clones") when an archived lineage of the orphan, kept next
  to it after a ROLLBACK (M4b-9 9a), is a clone of one of its snapshots.
  The confirmation lists what is below the orphan, not that archive; the
  run ends COMPLETED with an ERROR line ("Failed: destroyed ..."),
  nothing is destroyed and the question comes back at the next run
  (M5bV-3; the fix is 2.1's). Either answer "keep", or destroy the
  archive by hand while no zark command runs: `zpool import -N -R
  /mnt/zark-fix -d <the drive's by-id>-part1 <pool>`, `zfs destroy -r
  <pool>/<orphan>.archived-<date>`, `zpool export <pool>`; the next
  backup's DESTROY then succeeds. The archive's snapshots go with it,
  and a backup point that held one of them reads PARTLY REMOVED
  afterwards (run on eli).

#### Rejected approaches

- **An `exclusions.json` next to the registry** (M5b-3 3b): it would not
  move with the dataset or survive a recover; **excluding one dataset
  with its children still sent** (M5b-4 4b): a second kind of empty
  container on drives, and recover would need both.
- **A child section with `autosnap = no` under sanoid's recursive rule**
  (M5b-6 6b): sanoid 2.3.0 processes its sections in hash order, so the
  recursive parent's settings may overwrite it; **keeping the old
  snapshots until `zark prune`** (6c): the excluded dataset is usually
  the one that eats space.
- **Asking about a drive's held copy at every run, or only in `zark
  prune`** (M5b-10 10b, 10c): keeping is the safe default, and the user
  would not learn the drive still holds the data.
- **A full send on re-inclusion** (M5b-11 11b): it throws away the base
  the design keeps on purpose.
- **A directory in place of the recreated dataset** (M5b-13 13b): the
  exclusion would be lost and the next backup would send its contents
  with the parent; **`zark exclude convert`** (M5b-15 15b): it moves user
  data on the running system, a new destructive path before the final
  v2.
- **A self-test check for the chase.c:648 crash** (M5b-14 14b): a race
  passes only by chance, and provoking it would crash the host's udev;
  the rule stays an invariant of the code.
- **Questions in setup, or both a command and questions** (M5b-2 2b,
  2c): setup is a one-off step and an exclusion a later decision per
  dataset; two ways would say the same thing.
- **D10's three items only as the blacklist** (M5b-5 5b):
  `org.zark:replicate=no` on `rpool` would exclude everything but the
  keystore.
- **The current set of exclusions on `<pool>/rpool`, or a manifest in an
  unencrypted `<pool>/zark`** (M5b-7 7b, 7c): no history, so recover of
  an older point would recreate the wrong datasets; a dataset on the
  drive that orphans, retention and mount would each have to skip.
- **A held copy drained by retention** (M5b-9 9b): the points before the
  exclusion would turn PARTLY REMOVED and the base for a return would go.
- **`used`, or the stream's estimate, as the banner's size** (M5b-12
  12b, 12c): it counts history the drive would never hold; one more
  command per exclusion and run for an informational figure.
- **No new self-test check** (M5b-16 16b): gate 1 asks for one where a
  bench pool reaches the behaviour, and it does.
- **A pointer without a digest** (M5bF-6 6b): a part left from another
  record would go unnoticed; **names relative to the top, inherited
  values left out** (6c): it moves the bound without removing it, and a
  second format to read.
- **A bound of 14 parts**, so one `zfs set` fits execve's floor of 128
  KiB (M5bF-7 7b): a large docker host can reach it, and no default stack
  limit is that low.
- **Removing stale parts with `zfs inherit`** (M5bF-8 8b): one more
  command per part and point, each a way for a rewrite to end half done.
- **Rewriting a point whose record cannot be read as if it had none**
  (M5bF-9 9b): the point would lose what it left out, and recover could
  restore older copies of it.
- **`zark exclude add` refusing a subtree past the bound** (M5bF-11
  11b): the bound moves with the subtree's growth after the exclusion.
- **Shortening the parts in zark.log** (M5bF-12 12b): every command is
  logged by one path; the log keeps what recover will read.

### M5a: hygiene, the M4d review's findings and the checklist's cosmetics (2.0.0-rc9)

#### Changed

- **Banners and boxes keep their right edge when a line has a newline**
  (M5a-4), and a heavy banner keeps a column free on its right so a
  full-width line never touches the border (M5a-24).
- **prune's age-cut warning** names the drive and up to three of the
  datasets, one warning per drive (M5a-5).
- **`zark setup --remove` with any other argument** is a usage error,
  exit 2, and removes nothing (M5a-6).
- **`zark chroot`, `mount local` and repair-boot** do not mount other
  boot environments' subtrees, with one info line; bpool's import gets
  the device hint (M5a-7, M5a-12). chroot mounts the ESP of rpool's disk,
  not the first NVMe disk's (M5a-19).
- **Tips and status lines name the command as run** (M5a-9).
- **A backup waits up to 60 s for the drive to finish freeing** before the
  transfer when its plan removed something (prune uses the same wait), and
  its banner's "Available:" is read after retention (M5a-10, M5a-11).
- **health**: an unconfirmed reconnect pause (EOF) ends the cold pass as
  FAILED; a failed export before the eject stops it; kernel lines are
  matched by whole disk name and follow the disk's new name after the
  reconnect; the kernel's USB reset lines of the disk's bridge count; the
  directory of an undestroyed test pool stays (M5a-13, M5a-17, M5a-18).
  **simulate** installs nothing on a closed stdin (M5a-13).
  That ending says "DESTRUCTIVE TEST DID NOT FINISH", the drive was not
  judged, and it no longer blames the bridge or invites a hardware report
  (M5aF-2).
- **The empty altroot directory** of an exported pool goes (M5a-15).
- **The critical CAP notification** is critical (stays until dismissed)
  (M5a-22).
- **finish** reads the alerts' file mode too: a MOTD script left 0644 is
  not current (M5a-23).
- **repair-boot's banner** says whether the grub guard was written; on a
  live session the installer probe's stop is logged once per boot
  (M5a-24).
- **Release flow**: questing (25.10, EOL) is out of the PPA series;
  `release-guard` refuses an UNRELEASED top changelog entry; `make
  fulltest` runs the pinned lint only through tox (M5a-21, M5a-25).

#### Fixed

- **A level-2 self-test stopped between its pool's create and its GUID**
  leaves no pool on the stick (M4dR-1, M5a-3).

#### Tests

- The stop harness's host directories are behind a lock, so the three
  Pythons can run at once (M4dR-10); the review's surviving mutations
  (prune's filter and mask order, setup --remove's exit status, narrow
  boxes, a disabled Cleanup, recover's monthly cases) are each killed by a
  test; the fake parses `-r` and writes a bookmark token as `#name`
  (M5a-2, M5a-8, M5a-20).
- The tests that run the Makefile skip, with the reason, on a host without
  `make` (eli has none) instead of failing (M5aF-1).

#### Known limitations

- recover's `zfs mount -a` on the restored tree still does not resolve its
  symlinks (M5a-14).
- `zark monitor` still shows `zpool list`'s alloc and free (M5a-16).
- A destroy returns before its blocks are free; the waits are read from
  OpenZFS source and were not run on a pool (no self-test check, M5a-27).

#### Rejected approaches

- **Leaving the host directories of the stop tests in place, or a unique
  directory per run** (M5a-2 2b, 2c): the first leaves three empty
  directories in `/var` and `/run` after a root run; the second is
  impossible, the paths are the ones zark itself uses.
- **Destroying whatever pool `pool_on_stick` finds** (M5a-3 3b): wider than
  the finding; it could destroy a pool the run did not create.
- **Removing the embedded newlines from the three refusals** (M5a-4 4b):
  the next caller meets the same box.
- **One prune warning line per drive and dataset** (M5a-5 5a): 28 lines for
  a 1.x drive.
- **Rejecting every unknown flag in `setup`** (M5a-6 6b): wider than the
  finding; no other flag is documented.
- **The same warning for other boot environments at debug level** (M5a-7
  7b): keeps the children of a boot environment that cannot be mounted.
- **The absolute path of the checkout in the MOTD and the notifications**
  (M5a-9 9b): one more rule to keep.
- **The banner's Available read again without the wait, or the banner left
  with "(before retention)"** (M5a-10 10b, 10c): a lagging figure, or a
  figure that is known to be wrong.
- **No wait before the transfer, or a retry of the receive on ENOSPC**
  (M5a-11 11b, 11c): the first leaves the case to the operator; the second
  adds code to the engine's resume paths for a case the wait prevents.
- **Leaving bpool's import without the device hint** (M5a-12 12b).
- **Only logging the answers of the four bare prompts** (M5a-13 13b): the
  unattended QEMU install would remain.
- **Per-dataset mounts in recover's `zfs mount -a`** (M5a-14 14a): the
  benefit is a case Ubuntu's layout does not have, the risk a change in the
  boot path that no hardware run checks.
- **Leaving the empty altroot directories to `zark clean`** (M5a-15 15b).
- **`zark monitor` reading `zfs get`** (M5a-16 16a): a behaviour change on
  a screen nothing tests; M4d-8 rejected the same change.
- **Only the two health fixes that can leave the system in a state**
  (M5a-17 17b): the other two miscount transport errors.
- **Dropping the dead `reset SuperSpeed` pattern, or matching it on any
  `usb` line** (M5a-18 18b, 18c): the first gives up the check, the second
  counts another device's resets.
- **Keeping chroot's ESP guess as a fallback** (M5a-19 19b): it can mount
  the wrong one.
- **Only the PPA series, leaving the UNRELEASED entry a ritual note**
  (M5a-21 21b).
- **Leaving the notification urgency alone** (M5a-22 22b); **leaving
  `files_current` on content alone** (M5a-23 23b).
- **Asking `snap services` before logging the probe's stop** (M5a-24 24a,
  alternative): it relies on that command's output format.
- **A `tox-lint` target before the tests** (M5a-25 25b): the lint and the
  types would run twice.
- **M4d's `.orig` block with the versions guessed** (M5a-26 26b): the pool's
  own listing names every file.
- **A level-1 check for `freeing`** (M5a-27 27a): it would change the
  checks and need certificates for rc9.
- **Splitting the docs across the code commits** (M5a-28 28b).

### M4d: status, alerts, purge and health, the self-test, cosmetics, docs, tests and packaging (2.0.0-rc8)

#### Added

- **`zark setup --remove`** (M4d-7): removes what setup installed, the
  alerts, the APT guard (its `apt.conf.d` entry before the hook, so APT
  never points at a hook that is gone), the external-pool MOTD script and
  the update-grub guard. A file goes only when it holds zark's content or
  carries zark's header; anything else is left and named. The package's
  prerm runs it on `remove`, without the lock, and a file it leaves is
  named. The only way to undo setup for a portable zark.
- **KEY-03** (M4d-20), a level-1 check: an encryption root, a child that
  inherits its key and a clone of the child; only the root takes a key,
  and loading it makes all three available. Level 1 has 47 checks.
- **`make release-guard`** (M4d-28), a prerequisite of `deb-ppa`: no
  uncommitted or untracked file, and HEAD is the tag `v<version>`.

#### Changed

- **backup's notifications through `lib/notify`** (M4d-6): they reach
  Wayland desktops (the `who` line they looked for exists on X11 only),
  every active user session, never a greeter, and a hung session bus
  stops a notification after 10 s instead of the backup. "Backup
  completed" goes out before the eject question, which waits for the user
  (M4dF-3).
- **One residue line** (M4d-8): backup, `zark status` and the MOTD say
  "In origin: N snapshot(s) no registered drive needs (`syncoid_*` left
  by zark 1.x, keystore `prepare_*`): sudo zark prune removes them".
  BACKUP COMPLETED shows the pool root's zfs used and available, the
  figures the space plan uses, not `zpool list`'s alloc and free (up to
  the slop space, 128 GiB, apart). `zark status` prints the critical CAP
  level as `[ERROR]`. A second `zark setup` says the alerts are already
  current and runs no enable.
- **purge writes nothing when it cannot hold the disk from udev for
  10 s** (M4d-9): PURGE INCOMPLETE, exit 1, registry and anchors kept;
  purging again then checks the labels, which a run without the lock
  could not. DRIVE PURGED lists every step and check, and a label check
  that did not run (the drive reset during the overwrite) is warned.
- **purge's advice follows what failed** (M4d-10): only a step with every
  check clean and no pool left is "Purge again to run the failed step.";
  no label and a check failed is "Something is left on the disk"; a disk
  another program holds is "Nothing was overwritten: another program
  holds <disk>", with the `fuser` line to find it (M4dF-4). The empty
  `/run/zark/altroot/<pool>` goes after the destroy.
- **health ends 1 when the destructive test fails or its test pool is
  not destroyed** (M4d-11), after the report is written, as every other
  command ends a red verdict. This reverses M4aF-10's choice to keep 0.
  In a cold pass, the kernel's errors between the eject and the reconnect
  are listed as at the unplug and are not transport errors (M4d-12).
- **The self-test** (M4d-13, M4d-15, M4d-16): its masks replace whole
  names only (a short serial no longer changes `SND-01`); a level-2 bench
  names no `/dev/shm` directory; purge's steps inside it are unnumbered;
  on a live session only `selftest run` stops the installer's probe. A
  run stopped right after a `zpool create` of one of its pools ended,
  before the pool's GUID was read, no longer leaves that pool and the
  bench's directory behind: the teardown destroys it when it sits on the
  run's own file (M4dF-1). `--stick` with an empty value is refused (exit
  2) instead of running level 1 only (M4dF-2).
- **recover: a dataset whose point failed after its monthly arrived**
  (M4d-17) is named as left at that monthly, mounted (bpool) and anchored
  there for the drive; for bpool, each kernel the restored root has and
  `/boot` lacks is named with the command that reinstalls it.
- **`zark chroot`, `mount local` and repair-boot resolve a symlink of the
  installed tree inside it** (M4d-18), as the backup mount does since
  M4c-22: a dataset under the tree's `/var/run` mounts on the tree's
  `/run`, never the live session's.
- **Keys: only encryption roots are loaded** (M4d-19): the same keys as
  before, without the failed attempts on every inheriting child.
- **One frame for the banners and the question boxes** (M4d-21): wide
  enough for the text, up to the terminal's width, longer lines wrapped
  at words with their indent and colour; a by-id path or other long word
  stays whole. The question boxes have a right edge.
- **Tips name the command as it runs** (M4d-22): `sudo zark` installed,
  `sudo ./zark` from a checkout or a stick, also in the sentences that
  tell to run one (chroot, setup, repair-boot, recover, the eject; M4dF-5).
  The eject line names the drive, and purge names an unregistered disk by
  its label or model.
- **setup counts sanoid's snapshots on both pools** (M4d-23) and stops
  when `rpool/ROOT` has no boot environment, instead of asking for a name
  no answer could make right; repair-boot shows one step for importing,
  unlocking and mounting.
- **registry list tells a point recover restored** (M4d-24) from one an
  interrupted run left; the passphrase prompt follows its box through
  stdout, in order under `| tee`.
- **M2V-7's items and M3R-15** (M4d-25): purge drops the anchors of every
  registry entry it removes; the strict read of a drive takes a root as
  missing only when the pool's listing lacks it; the resume check reads
  libzfs in English; a written dataset whose discarded partial leads to a
  decision says it is asked again next time.
- **CHROOT NOT CLOSED** (M4d-26): `zark chroot` ends 1 when a pool stays
  imported; a Ctrl-C with nothing to clean up says so.
- **prune** (M4d-32, M4d-33): before an age cut that touches a dataset
  where a registered drive has no zark 2 anchor (a 1.x drive), it names
  that drive, whose base there is a sanoid snapshot the cut can remove;
  after a removal it shows the pools' capacity once they have freed it
  (up to about a minute), or says they are still freeing.
- **Packaging** (M4d-28, M4d-30): `make deb` builds the binary from the
  source package (tracked files only), so nothing untracked reaches the
  .deb; setup and recover no longer install syncoid, pv, mbuffer, lzop or
  bc, and recover installs only the packages of the tools the live lacks.
- **Docs** (M4d-27): the keystore's FILES entry, chroot's lost sentence
  and the prune entry's term restored in the manpage, the update-grub
  guard's `grub.cfg.new`, and the source notes on dash and on the
  clone's `-o origin=`.
- **Tests** (M4d-2 to M4d-5, M4d-14, M4d-31): the review gaps closed with
  tests that fail without what they guard; `zark selftest` stopped by a
  signal end to end, in a mount namespace of its own; the self-test's
  reads checked to name only the bench's objects; quiet output, a ticking
  clock, and `Path.exists` answered per path.

#### Known limitations

- purge cannot read the labels of a disk that loses its partition table
  during the overwrite (a drive reset with the lock held): the check is
  listed as not done.
- `zark monitor` still shows `zpool list`'s alloc and free.
- recover's `zfs mount -a` on the restored tree does not resolve its
  symlinks in the tree as M4d-18 does for the installed one.

#### Rejected approaches

- **Counting purge's skipped label check as a failure** (M4d-9 9b): the
  first run's `sgdisk --zap-all` removed the table, so purging again has
  no partition to check either.
- **The test model's udev re-reading the table at the lock's release**
  (M4d-4 4a): systemd 259.5 gives up that re-read while the disk is held
  and does not retry it.
- **Keeping health's exit status 0** (M4d-11 11b): the one command whose
  red verdict ended 0.
- **Only `Synchronize Cache` lines in the unplug window** (M4d-12 12b),
  or none apart (12c).
- **Dropping `BEYOND_2_2_STREAM` instead of moving it** (M4d-13 13b): the
  feature module is the table the test checks it against.
- **Refusing every dataset whose path crosses a link** (M4d-18 18b): a
  relative link inside the tree is harmless.
- **`textwrap` for the frames** (M4d-21 21b): it measures with `len()`,
  so colours and wide characters break the edges.
- **Keeping setup's prompt for the boot environment** (M4d-23 23b): with
  `rpool/ROOT` empty no name is right.
- **Blocking prune's age cut until every drive has a zark 2 backup**
  (M4d-32 32b): the user may give a 1.x drive up knowingly, and PRUNE is
  typed anyway; **the capacity line without the wait** (M4d-33 33b).
- **A pylint bump** (M4d-29 29a): 4.1.3 finds nothing 4.0.8 does not.

### M4c: recover, finish and the boot chain (2.0.0-rc7)

#### Changed

- **`finish` no longer exports and re-imports the pools** (M4c-4). On the
  running system `zpool export rpool` unmounts every dataset that is not
  busy and then fails on `/`; both exit statuses were ignored and "Pool
  hostid synced" was printed, although a pool's labels take the importing
  system's hostid at every import, the boot's included. In its place a
  read-only line per pool: the hostid in its label (`zdb -l`) against
  `/etc/hostid`.
- **`finish` rebuilds the initrds before `update-grub`, and checks the
  result** (M4c-5 to M4c-8, M4c-10, M4c-11). zpool.cache first (the
  initrds carry it), then the initrd of every installed kernel, then
  `update-grub`, which writes no entry for a kernel without one. grub.cfg
  counts as regenerated only with an entry for every installed kernel
  (`10_linux_zfs` exits 0 even when it stops half way). The ESP fixed in
  fstab is the one on rpool's disk (it was the first NVMe disk's), and
  `/boot/efi` must be that ESP, mounted, with the signed shim and GRUB.
  Every `canmount=on` dataset left unmounted is mounted once, or named.
  "Run setup" is printed only when something setup guarantees is missing,
  and says what.
- **dracut is given the initrd's path** (M4c-5): `dracut --force
  /boot/initrd.img-<v> <v>`, as Ubuntu's own wrapper runs it, for recover,
  repair-boot and finish. With `--kver` alone, an ESP with a
  `loader/entries` directory received the initrd, where GRUB never looks.
- **`recover` anchors what it restored for the drive it read** (M4c-12,
  M4bF-2): a bookmark of every restored rpool dataset, and the restored
  bpool snapshot renamed to that drive's bpool anchor, so that drive's next
  backup continues instead of resending in full. Another drive's run names
  such a point "not on <pool>; it names no drive" when it removes it.
- **`recover` records the ESP in debconf for real** (M4c-9): the line
  lacked its owner and type, and `debconf-set-selections` skipped it with a
  warning and exit 0. It is read back now. A missing signed EFI binary is
  an error of the recovery (M4c-7), the kernel count no longer counts the
  `vmlinuz` links (M1P.8), a dataset left at its monthly base by a failed
  point is named so (M4c-13), the keystore's `@prepare_*` snapshot is
  removed from the recovered system (M4c-14), a backup point that lost
  datasets since its run is PARTLY REMOVED in the menu (M4c-16), the delta
  column counts days from 48 hours on (M4c-17), and the drive's metadata
  is read strictly (M4c-18).
- **`backup` stops when the keys' status cannot be read** (M4c-19): it was
  taken for "Key already loaded".
- **`mount local` read-only writes nothing to the installed pools**
  (M4c-21): they are imported `readonly=on` and the keystore opened
  read-only, instead of a read-write import remounted read-only.
- **`zark mount` read-only resolves a symlink of the backup inside the
  tree** (M4c-22): `/var/run -> /run` is the tree's `/run`, and the dataset
  under it is mounted there instead of being skipped.
- **`zark mount` checks the drive's features before its import** (M4c-23),
  as recover does; in read-write mode, a drive this ZFS can only read is
  refused with the advice to mount it read-only.
- **The read-back's import belongs to the run's cleanup** (M4c-25): a stop
  between the read-back's import and its export exports the drive.
- **A self-test killed with SIGKILL is cleaned up** (M4c-26): the next run
  removes its directory in `/dev/shm`, and `zark clean` closes its
  keystore mapper.
- **The self-test resolves the stick's vdevs to their disks** (M4c-27)
  before it blanks the stick.
- **Three new self-test checks** (M4c-28): SND-12 (a recovered snapshot's
  bookmark, and a renamed one, are bases of the drive), POOL-12 (the label
  carries the importing system's hostid), MNT-04 (a read-only import
  mounts and cannot create a mountpoint). Level 1 has 46 checks.
- **`lib/engine.py` and `commands/recover.py` are split** (M4c-1),
  without changing what they do: `lib/survey.py`, `lib/drive_marks.py`,
  `lib/anchors.py`, `lib/reclaim.py`, `lib/restore_plan.py` and
  `lib/boot_chain.py`.
- **`make fulltest` runs the pinned ruff** (M4c-2), from its own tox env,
  not the one in PATH.
- **No em dash, en dash or U+2026 in the tree** (M4c-3), the old entries
  of this file and of `debian/changelog` included; a test keeps it so. The
  scripts zark installs change with it: the next `setup` rewrites the APT
  hook, its MOTD reminder, the grub guard and the alerts' MOTD script once,
  and says so.

#### Known limitations

- A drive's keystore is the copy `prepare` made of the source's keystore
  (M4c-20). A later `cryptsetup luksChangeKey` in the source leaves the
  drive asking the old passphrase; a later `zfs change-key` of rpool makes
  the next backup rewrap the drive's datasets to the new key, after which
  the drive's keystore no longer opens them, the following backup stops at
  the key load and recover from that drive cannot load its keys. After
  either change: `zark purge` and `zark prepare` of each drive.
- After the child of a stopped command has exited, zark waits for its
  process group by polling it every 20 ms (M4c-24, M4bV-1): a new process
  group that took the same number inside one of those gaps (a pid wrap,
  `pid_max` is 4194304 on Ubuntu 26.04) would be waited for and killed, and
  a zombie counts as a member until its parent reaps it.
- Not checked by the self-test, as each would change the running system's
  `/boot`, ESP, NVRAM or debconf: debconf's `install_devices`,
  `grub-multi-install`, the signed packages' postinsts, `update-grub` and
  `10_linux_zfs`, dracut and the kernel's `/boot` links. eli's recover,
  finish and boot verify them.
- `mount local` read-only does not see the last writes of an unclean
  shutdown (the intent log is not replayed), and lists as not mounted a
  dataset whose mountpoint directory is missing.

#### Rejected approaches

- **Re-exports from `lib/engine.py`** (M4c-1): the callers would not show
  where a name lives; **one public `quote` in `lib/sh.py`** instead of each
  module's two-line `_q`: it changes every caller's AST, which the move's
  check compares.
- **Dropping ruff from fulltest and relying on `tox -e lint`** (M4c-2 2b):
  ruff would report after the suite and the Debian checks;
  **`uvx` with the pin read from pyproject.toml** (2c): `uv` is in tox-uv's
  environment, not necessarily in PATH.
- **Leaving the old CHANGELOG and debian/changelog entries as they were**
  (M4c-3 3b): a test with exceptions keeps the characters.
- **Dropping finish's sync step without a replacement** (M4c-4 4b): the
  removed line pretended to show something the hostid line shows.
- **Keeping `--kver` and skipping the `mkdir` only without an ESP**
  (M4c-5 5b): an ESP restored with `loader/entries` would still get the
  initrd.
- **One entry for any kernel enough** (M4c-6 6b): finish rebuilds every
  initrd first, so a missing entry is a real failure.
- **`org.zark:point-for` on the restored points as well** (M4c-12 12b):
  redundant with the bookmark.
- **Leaving the keystore's `prepare_*` snapshot** (M4c-14 14b).
- **A warning and a wait before recover's passphrase in a graphical
  session** (M4c-15 15b): the dialog's cause, the installer's probe, is
  stopped since rc4 (M4aF-6).
- **No more passphrase and key loading on drives prepared by rc2 or
  later** (M4c-19 19b): the passphrase is the recurring proof that the
  drive's keystore still opens the drive's datasets.
- **zark as a child subreaper** (M4c-24 24a): every orphan of every child
  would become zark's to reap, on every path.
- **Documenting the self-test's leftovers only** (M4c-26 26b).

### M4s: platform self-test (2.0.0-rc6)

#### Added

- **`zark selftest`** (M4s-1, M4s-2, M4s-3). Level 1 checks, on pools
  backed by sparse files in `/dev/shm` with random names no other pool
  has, every behaviour of OpenZFS, util-linux, systemd, the kernel, CPython
  and sanoid that zark relies on (43 checks: pools, snapshots, send and
  receive, destroy, clone, keys, mounts and namespaces, signals and locks,
  systemd units, sanoid's pruning). Nothing it runs touches a disk or a
  system pool, nor sets a mount property (the chase.c:648 rule). Level 2,
  with `--stick <device>`, checks on a blank scratch stick what needs a
  real disk (8 checks: prepare's pool on a whole disk, udev's partition
  re-read and the lock that holds it off, purge's wipe); the stick must
  be external, unregistered, blank and named under `/dev/disk/by-id`, its
  kernel name is typed, and it is left blank as purge leaves a drive. Each
  check has an ID and a revision; a FAIL prints what was expected, what
  came and what in zark depends on it, with the commands' output in
  zark.log only. A signal stops the
  run like any command: its pools are destroyed (only the ones whose GUID
  it recorded), its directory removed, and the run recorded as stopped.
- **Reports, record and certificates** (M4s-5 to M4s-9, M4s-14). Every run
  writes a JSON report under `/var/lib/zark/selftest/` with the platform's
  exact versions, the series they belong to (Ubuntu, kernel, OpenZFS,
  util-linux, systemd; installed or live, and the live's ISO) and each
  check's result; the run's own names, the hostname, GUIDs, serials and
  by-id names are replaced before it is written. `zark selftest report`
  prints a link to a GitHub issue form (`.github/ISSUE_TEMPLATE/selftest.yml`)
  with the report filled in; zark sends nothing. `zark selftest status`
  shows the coverage of this platform by the certificates shipped with zark
  (`certificates/selftest.json`, filled at each rc from the maintainer's
  machine) and by this machine's own runs.
- **Warnings by subscription** (M4s-15). `zark selftest warnings on` adds a
  line at the start of `setup`, `backup` and `recover` and to the MOTD
  when no certificate covers this platform, when a check failed here, or
  when the loaded ZFS module and its tools are different releases (Ubuntu's
  revision ignored, M4s-8). Off by default; stored in `settings.json` next
  to `known_drives.json`.

#### Changed

- **`prepare` creates new drives with `compatibility=openzfs-2.2`**
  (M4s-10). A drive prepared with every feature of OpenZFS 2.4 could not
  be read by an older live session once `dynamic_gang_header` activated
  on it (a ganged write on a nearly full drive); with 2.2's set, Ubuntu
  24.04's live and later read it. When that set is not installed, or the
  source pools use `longname` or `large_microzap` (which zark's streams
  would carry), the drive gets every feature and prepare says so. Drives
  prepared before are unchanged.
- **`recover` names what a live session cannot read before importing
  anything** (M4s-11). It lists the drive with `zpool import -d`; when the
  drive uses features this ZFS lacks for reading, it stops with each
  feature's name, the OpenZFS release that has it and the one running;
  features it lacks only for writing are one line (recover imports
  read-only).
- **The test model of ZFS follows OpenZFS where it did not** (M4s-12):
  `written` is a number on every filesystem, a `zfs set` naming several
  targets applies to the ones it can open, `zfs destroy -r ds@s` takes
  that name on every descendant, a held snapshot's refusal says so, a
  clone sent from its origin is attached to that origin by GUID, and a
  resume token whose snapshot was recreated is "no longer the same
  snapshot".
- **The review's fix round (M4sR-1 ... M4sR-4, M4sR-6).**
  - A stopped run's report is masked as a complete one's: the masks of
    the run's names (its directory and pools, the stick's by-id names and
    serial) are taken when the benches are made, before the first check
    (M4sR-1, M4sF-1 1a).
  - A FAIL keeps what the check states (what it expected, a count, an
    exit status, a value it read from its own pools) apart from the
    command output behind it, which is printed under the FAIL and written
    to zark.log but never to the report; an unexpected exception reports
    its type only. Before, up to 300 characters of a command's output
    reached the report, a system dataset in sanoid's stderr or the host's
    mounts in MNT-03's listing among them (M4sR-1, M4sF-2 2a).
  - The level-2 teardown wipes the stick with purge's own wipe, both ends
    of the disk overwritten first: `wipefs` alone left part1's ZFS labels,
    which `blkid -p` does not see on the whole disk. Nothing is written
    while a pool is still imported from the stick (M4sR-2, M4sF-4 4a).
  - A stick with no `/dev/disk/by-id` name is refused before the typed
    question, as `prepare` refuses one (M4sR-3, M4sF-5 5a).
  - A check that passes in a later run, `--only` included, leaves the
    failures recorded for this platform, so "failed on this machine" goes;
    coverage still needs a complete run (M4sR-4, M4sF-6 6a).

#### Known limitations

- The self-test does not check what needs a boot, a package operation, a
  change to the running system or a hand on a cable: GRUB reading bpool
  and the feature set it accepts; `update-grub`'s `10_linux_zfs` and the
  grub guard; the APT hook; the Secure Boot chain; dracut; a USB bridge's
  cache flushes (`zark health`'s destructive test is the tool for an
  enclosure); chase.c:648 (its re-evaluation is M5); `zvol_inhibit_dev`;
  the pause of `sanoid.timer` and the timer's cadence; logind ending a
  session; `snap stop` of the live installer's probe; `zpool export bpool`
  on an installed system and the initramfs importing every cached pool; a
  pool last used by another host; notify-send and `pam_motd`; the uevents
  after a USB power-down (health COLD).
- Checked by unit tests only: a drive with features a live's ZFS cannot
  read, a ZFS module and tools of different releases, each check's FAIL
  path, a report too long for the link, the warning lines in `setup` and
  `recover`, a stick with no by-id name.
- On a live session the record and reports live in RAM: the report is
  copied off before shutting down.
- A run killed with SIGKILL (outside the stops the self-test handles)
  leaves its pools imported under `/run/zark/altroot`, which `zark clean`
  exports, and its sparse files in `/dev/shm/zark-selftest-*` until a
  reboot; killed during KEY-02, its LUKS mapper `<pool>-ks` keeps `zark
  clean` from exporting that pool until it is closed by hand (M4sR-5, M4c).
- `zark mount` imports a drive without recover's feature check (M4c).
- A `--only` run certifies nothing: where no shipped certificate covers
  the platform, "no certificate covers this platform" stays until a
  complete run.

#### Rejected approaches

- **`zark check` or `zark verify` as the command, or an option of
  `health`** (M4s-1 1b, 1c): `check` reads as "check my backup", and
  `health` is about a drive while level 1 needs none.
- **A tmpfs the run mounts under `/run/zark/selftest`** (M4s-3 3b): one
  more mount that `/run`'s shared propagation copies into every service
  namespace. **A directory the operator names** (3c): one more way to put
  the vdevs on a pool.
- **Level 2 on a disk that is not blank, its content listed first**
  (M4s-4 4b): a run that erases a disk zark never looked at.
- **The record and the subscription in `known_drives.json`** (M4s-5 5b),
  or the record next to it (5c): settings mixed with drives, and a
  portable zark carrying one machine's record to another.
- **One certificates file per platform series** (M4s-6 6b); **eli's
  results in the next milestone's first commit** (M4s-7 7b): `main` would
  carry an rc without its own entries.
- **Comparing the exact ZFS version strings** (M4s-8 8b): a live, whose
  module comes from the ISO, would always warn.
- **A plain-text report in the issue's body** (M4s-9 9b): no field, and
  the entries copied by hand.
- **recover's check alone, without a `compatibility` in prepare** (M4s-10
  10b): a full drive could activate `dynamic_gang_header` and become
  unreadable by any live older than OpenZFS 2.4. **The check in
  `import_backup_pool` for every command** (M4s-11 11b): only recover and
  mount meet an older ZFS.
- **Only the framework against the test model of ZFS** (M4s-12 12b): the
  checks that run against it are what found eleven divergences.
- **The live's kernel release alone in the report** (M4s-14 14b): it
  does not say which ISO the live booted, which `/cdrom/.disk/info` does.
- **The MOTD line for a failure only** (M4s-15 15b).
- **Filtering the report alone** (M4sF-2 2b), replacing every `came` that
  is not one short token: the checks would still compose texts that are
  not what they observed. **The source pools' names among the masks**
  (M4sF-3 3b): a mask replaces a pool's name, not the dataset path after
  it, and short names would be replaced inside ordinary words.
- **`zpool labelclear` before the teardown's `wipefs`** (M4sF-4 4b): it
  needs part1's node, which a stopped DSK-04, DSK-06 or DSK-07 has
  already removed.

### M4b: interrupted runs and the engine's bases (2.0.0-rc5)

#### Changed

- **One way to stop for every command** (H12, R2 8.4, M4b-1). zark
  installs one handler for SIGINT, SIGTERM and SIGHUP. The first signal
  ignores all three (logind sends SIGTERM and SIGHUP together when it
  ends a session), stops the running child and the processes below it
  (each child runs in a process group of its own: `sh -c` stays the
  parent of its command, so a signal to the shell alone would not reach
  `zfs`), waits up to 30 seconds and then SIGKILLs what is left, and only
  then lets the command unwind. The command's cleanup runs once with the
  three signals ignored, whether a signal stopped the run (then from
  `atexit`) or the command ended normally and runs it itself; after a
  normal end the handler is back for what follows (a backup's read-back
  and eject prompt). The interruption line names what it stops
  ("Interrupted by SIGTERM: stopping zfs send | zfs receive, then
  cleaning up"). Before, SIGTERM ran the cleanup from inside the handler
  while the child kept running and the command went on (a backup could
  export its pool under a running receive), and SIGHUP killed zark with
  no cleanup at all.
- **Output whose reader went away stops the command through its
  cleanup** (V-6, M1P.23, M4b-2). SIGPIPE stays ignored, as Python
  starts. The first write to stdout or stderr that finds no reader ends
  the command as a hangup does (child stopped, cleanup, exit 141); from
  then on both go to `/dev/null`, so neither the cleanup nor the exit
  flush fails, and zark.log records why the run stopped. During the
  cleanup a missing reader only silences the output.
- **`zark chroot` and `simulate`** run their interactive child (the shell,
  QEMU) in zark's own process group, so a Ctrl-C is the child's; SIGTERM
  or SIGHUP stop it (SIGHUP to the shell, SIGTERM to QEMU) and then the
  cleanup unmounts what it had. A chroot whose terminal closed used to
  leave its binds mounted.
- **`umount` and `clean` release the copies other mount namespaces keep**
  (W-1, M4b-3). The kernel skips a propagated unmount whose mount has a
  child of its own (Linux 7.0 `fs/pnode.c`), so a systemd service or a
  snap kept a copy of the tree and the pool stayed busy. When the export
  fails, zark unmounts the tree in every other mount namespace that still
  lists it (those of `/proc/*/ns/mnt` and the ones snapd pins in
  `/run/snapd/ns`), with `nsenter --mount=<ns> umount -R`, then retries
  the export. Only copies of zark's tree are touched there: a ZFS mount
  from one of the pools being released, or a mount with the same type
  and source as one the tree had here (a chroot's binds); anything else
  at those paths, in a namespace with a root of its own, is left alone.
  What still cannot be released is named with the namespace's process
  (or the snap whose namespace snapd pins). Datasets are told from binds
  by their source, a ZFS mount from one of the pools being released
  (W-2); the mount table is read again after the unmount, and what is
  still mounted here is named (R2 11.4a).
- **Anchors whatever the run's ending** (8b, M2R-2, M2P.8, M4b-4). The
  drive's newest snapshot of every rpool dataset keeps a bookmark of that
  drive in the source, however the run ended: at the end of every run
  and in the cleanup of one that was interrupted, zark bookmarks the
  newest snapshot the drive holds when the source still has it (a
  dataset that reached the point, a `-I` stream cut after a carried
  snapshot). After an interruption the point stays only where a partial
  receive needs it to resume, or where its anchor failed; zark.log names
  every point snapshot removed or kept, with its reason, and any destroy
  that failed.
- **Every backup point names its drive** (`org.zark:point-for`, set by
  the same `zfs snapshot` that takes it). When a backup of another drive
  or `zark prune` removes a point a run left in the source, it is first
  anchored for the drive it names (a bookmark on rpool, a rename on
  bpool), so that drive continues without a rollback. A point that cannot
  be anchored stays. `zark prune`'s warning says what remains: points
  from before rc5 name no drive, and a partial receive still needs the
  snapshot itself.
- **A failed anchor is tried again** after a pause (M3R-5, M2P.7); an
  anchor already there for the same snapshot (a run stopped between the
  bookmark and the destroy) counts as made.
- **ROLLBACK keeps the drive's newer points** (M3R-1, M4b-5). A drive whose
  newest snapshot the source no longer has, but which shares an older
  one, used to choose between rolling back (its newer points go) and
  archiving the lineage to resend it in full. The second option is now
  "keep the newer snapshots in archived lineages and continue from the
  common snapshot": the lineage is renamed aside, a clone of its common
  snapshot takes its place and is promoted, its children go back under
  it, and the next transfer is the usual incremental. Nothing is
  destroyed and nothing is resent; the archive holds only the newer
  snapshots. It works with the keys unloaded (staged on eli).
- **recover starts each dataset from a shared monthly** (M3P.13, M4b-6).
  Each restored dataset is received from its newest sanoid monthly before
  the chosen point (in full), then the point with `-i` from it. The
  recovered system then shares that monthly with every drive in rotation,
  and their next backup continues (with the ROLLBACK above) instead of
  resending everything. The size check counts the extra blocks; when they
  do not fit, the point alone is restored, as before, with a line saying
  so, and so is a dataset with no monthly before the point.
- **One question for several diverged datasets** (M3P.27, M4b-11): skip
  them all (the default), archive them all (when everything fits),
  destroy them all (typed DESTROY, after listing every dataset each
  destroy takes) or decide one by one. A single one keeps its question.
- **Retention and `zark prune` destroy per dataset** (M3P.26, M4b-10):
  `zfs destroy <ds>@a,b,c`, at most 100 exact names per command, each
  logged with its reason first. ZFS destroys none of a group when one
  fails; that group's names are then destroyed one by one, so what is
  kept and what failed stay exact. Never a `%` range.
- **`zark prune` on a drive names what it keeps** (held snapshots, clone
  origins), as backup's retention does (M3X.5).
- **Where 1.x history ends is recorded** (`org.zark:first-point`, M3P.25,
  M4b-9): prepare writes its point; a drive without it gets its oldest
  backup point at the start of the next backup, before that run's
  retention. The boundary no longer moves when the policy removes the
  first points.
- **A failed parent holds back only NEW children** (M2X.1, M4b-12); a
  child the drive already holds gets its incremental.
- **A source dataset named like an archive is not backed up** (M2R-13,
  M4b-7): a name component containing `.archived-` is reserved for
  archived lineages; such a dataset is listed as NOT BACKED UP with the
  reason ("rename it"). Before, it was sent and then ignored on the drive,
  so no backup could complete.
- **BACKUP NOT VERIFIED says what the drive already records** (M2X.2,
  M4b-13): `last-point` and the completion marks are written before the
  export the read-back needs, so the drive records the point while the
  registry's last backup is not updated.
- **The installer's probe service is stopped on a live session** (M4aF-6,
  M4b-14). Before recover, mount, umount, chroot, repair-boot,
  fix-rpool-mountpoint, clean, purge, prepare, health and explore, zark
  runs `snap stop ubuntu-desktop-bootstrap.subiquity-server` (for this
  session only; nothing changes for the next boot): it probes the disks
  on every udev event and writes a crash report after each pool zark
  imports. A missing snap or service is not an error.
- The propagation harness checks it has a mount namespace of its own
  before its first mount (X-3); the propagation and `clean` real-mount
  tests assert that nothing is left here or in the other namespace (X-1,
  X-2).
- **The review's fix round (M4bR-1 ... M4bR-8).**
  - `undo_point` logs every removal of the point and what it keeps, with
    the reason, and warns when a destroy fails, as every other removal in
    the source does; it lists the source first and removes the point only
    where it exists (M4bR-1, M4bF-4 4a).
  - The normal-end cleanup of a command ignores the three exit signals
    too, so a Ctrl-C during it no longer cuts it short; the handler is
    back once it is over (M4bR-2, M4bF-5 5a).
  - `umount` and `clean` release, in another mount namespace, only copies
    of zark's tree, told by source and type, never by path alone
    (M4bR-3, M4bF-6 6a).
  - The interruption line names the child it stops (M4bR-8, M4bF-7 7a);
    NOT UNMOUNTED names the process of a namespace that still holds a copy
    (M4bR-8, M4bF-8 8a).
  - The stop wait of a child is read when the child is stopped, so the
    SIGKILL fallback can be tested; a test for each of the review's other
    survivors (M4bR-7). A stop waits for the child's whole process group
    and SIGKILLs what is left of it, so a command that ignores the signal
    behind a shell that did not never outlives the cleanup (M4bF-10 10a).
  - The clone that takes a kept lineage's place is made with
    `canmount=noauto` and the `org.zark:canmount` / `org.zark:mountpoint`
    marks (eli F1.6 showed it with `canmount=on`, the default, and no
    marks until its next receive: a clone has a property set of its own,
    and `canmount` is not inherited). The properties are set by the kernel
    with the clone, as `receive -o` sets them, never by `zfs set`
    (M4bR-4, M4bF-9 9a).
  - `prune` and the retention's "kept" lines call a snapshot from before
    the drive's first backup point by its date, not "1.x history": the same
    rule catches a dataset that reaches a zark 2 drive with older sanoid
    snapshots (eli F2.4; M4bF-12 12a).

#### Known limitations

- A dataset received as a clone keeps its origin snapshot on the drive;
  retention keeps it, and a destroy or rollback of the origin's dataset
  fails while the clone exists (M3P.4). Nothing is lost; the run reports
  it.
- Mountpoints set locally on drives written by zark 2.0.0-rc1 stay (O2):
  clearing them needs a `zfs set` with the drive's keystore zvol present.
- A backup of one drive can remove a point that another drive's partial
  receive needs to resume: that drive then discards the partial receive
  and sends the dataset again from its anchor.
- A backup point that names no drive, such as the one a recover
  restores, is removed by another drive's verified run as if that drive
  held it (M4bF-2). The drive the recover read then shares with the
  source only what recover's monthly base gave it, and its next backup
  resends the rest in full. M4c anchors what recover restored for the
  drive it read.
- On bpool, which takes no bookmarks, a `-I` stream cut after a carried
  snapshot still needs a ROLLBACK at the next run (about 150 MB).

#### Rejected approaches

- **Running the cleanup from the signal handler** (rc4's model): it runs
  while the child it would undo is still writing, and a second signal
  interrupts it halfway.
- **Restoring SIGPIPE to its default**: zark would die at the first print
  to a closed pipe, before any cleanup.
- **Leaving copies in other namespaces and telling the user to reboot**
  (W-1 3a): the busy pool then blocks every later command.
- **Re-importing the drive read-write after the read-back** to write the
  metadata only then (M4b-13 13b): another import and export for a mark
  whose meaning can be stated instead.
- **Marking archives with a property** instead of reserving the name
  (M4b-7 7a): it touches the planner, recover, mount and retention, and
  archives made before rc5 would still need the name.
- **One `zfs destroy` with a `%` range**: it takes whatever lies between
  the two names, not only what retention decided.

### M4a: guards and destructive commands (2.0.0-rc4)

#### Added

- **One lock for every command that changes state** (M4a-5). The entry
  point takes an exclusive `flock(2)` on `/run/zark/zark.lock`, without
  waiting, before it loads the command. A second run is refused at once,
  naming the run that holds it ("zark backup (pid N, since T)"), with exit
  status 75. The kernel releases the lock on any death of the process,
  SIGKILL included, so it is never left stale; child processes do not
  inherit it. When the lock file cannot be opened the message says so
  ("Cannot take the lock ..."), with the same exit status (M4aR-9).
  `status`, `explore`, `monitor` and `registry` with no subcommand or
  `list` run without it. Two backups at once could remove
  each other's points, and `zark prune` could run during a backup.
- **Forensic capture before remediation** (I7, M4a-9). Before anything
  changes or loses a pool's state, zark writes
  `zark-forensics-<pool>-<UTC>.txt` next to `zark.log` (mode 0600): `zpool
  status -vP`, `zpool events -v` for the pool, the end of `zpool history
  -il`, the kernel log of the last two hours, `lsblk` and the USB bridge
  ids of the pool's disks. It runs, without asking, when the read-back
  after a backup or prepare fails or is not ONLINE, when backup's
  pre-check finds a pool that is not clean, and in `purge` before `zpool
  destroy`. black's checksum errors of 2026-09-25 could not be explained
  afterwards because purge destroyed the only `zpool status -v`.
- **Pool health in read-only `zark health`** (I11, M4a-11), without
  importing anything: an imported pool on the disk is read from its
  counters (`zpool status -vp`); an exported one from the state `zpool
  import -d <partition>` reports, with the commands that read its
  counters under zark's own altroot.

#### Changed

- **`zark clean` touches only what zark mounted or imported** (H6, M4a-1,
  M4a-2). It used to run `umount -R` and then `rm -rf` on `/mnt/zark` and
  `/mnt/recover`, so a filesystem still mounted there lost its files; it
  ran `zfs unmount -a`, unmounted `/run/keystore/rpool` whatever was
  mounted there, and exported with `-f` every imported pool, rpool and
  bpool included when rpool was imported. It now builds an inventory of
  zark's own state
  (mounts below `/mnt/zark`, `/mnt/recover`, `/mnt/repair`,
  `/run/zark/altroot` and `/run/zark/probe`; pools imported with an
  altroot below them; `zark_ks_*` keystore mappings and a keystore mount
  on top of `/run/keystore/rpool` that comes from one; `sanoid.timer`
  left stopped by an interrupted backup; `zvol_inhibit_dev=1` left by
  fix-rpool-mountpoint), shows it, lists the imported pools it leaves
  alone, and asks (default no). Mounts are unmounted without `-l`, pools
  exported without `-f`, and only directories left empty are removed
  (`rmdir`), the roots themselves included (M4aF-8), after every mount
  below them is gone and while no zark pool is still imported: `rmdir`
  of a directory that is a mountpoint only in another mount namespace
  detaches that copy (M4aR-6, M4aF-7). Anything still there
  ends **CLEANUP INCOMPLETE**, exit 1, naming it. The same rules apply on
  an installed system and on a live session.
- **The guards see an imported backup pool** (H18, D3, M4a-3). The APT
  hook, its MOTD reminder and the grub guard looked only at `zpool
  import`. An imported pool other than rpool/bpool now counts as external
  when it has an altroot or carries `org.zark:format` (a drive written by
  zark 1.0.12 or rc1 imported by hand without an altroot puts its kernels
  in the running system's `grub.cfg`: `10_linux_zfs` takes, in every
  imported pool, the filesystems whose mountpoint is the pool's altroot or
  `/`). A permanent data pool imported without an altroot does not block.
  The advice says to let a running backup finish or end a mount with `zark
  umount`, then disconnect; it no longer suggests `zpool export`.
- **`setup` installs both guards** (M4a-4), overwriting a copy that
  differs; re-running it changes nothing. The grub guard used to reach a
  system only through recover, repair-boot or finish.
- **The keystore zvol is found by its dataset name** (H11): each whole
  `/dev/zdN` is asked its name with `zvol_id`, and `/dev/zvol/<pool>/keystore`
  is the fallback. The previous code picked by position in a text-sorted
  list (`zd112` before `zd16`) and used a single zvol of any pool with a
  warning.
- **System pools are imported by-id first** (R2-9): `-d /dev/disk/by-id`,
  then a device hint and its directory, then an auto-scan, each named in
  a warning when it is not by-id. repair-boot ends **BOOT REPAIR
  INCOMPLETE** while a system pool has kernel-named vdevs, since the
  `zpool.cache` it writes records them.
- A disk with no medium (size 0) is refused before anything is asked
  (M1P.12).
- **Several boot environments** (M4a-8): on a drive, a menu lists each
  with its newest snapshot's time, the newest the default; on the
  installed system the one mounted at `/` is taken; on a live session the
  same menu. recover, `zark mount`, `mount local`, `chroot` and
  repair-boot used the alphabetically first.
- **Destructive health test** (H19): the throwaway pool is created and
  imported by its by-id name with `cachefile=none`; a cold pass without a
  by-id name fails before writing; at the end only the disk that by-id
  resolves to now is wiped, and only when its label is the test pool's or
  there is none. The picker lists one physical disk once (I10). The pool
  is created and imported under `/run/zark/altroot/<pool>` (`-R`), so
  `zark clean` finds one a killed run left imported (M4aR-3). It is
  destroyed read-write: the read-back's read-only import is exported and
  imported read-write first, since `zpool destroy` of a read-only pool
  returns 0 and writes nothing to its labels; a test pool that could not
  be destroyed is reported, with the command that lists what may remain,
  and goes into the diagnostic report (M4aR-1, M4aF-10). After the cold
  reconnect it waits for udev (`udevadm settle`) and up to 10 s for the
  disk's by-id links before importing; a partition link that never
  appears is its own failure (M4aR-8, M4aF-11).
- **backup's pre-check shows the damage** (I8, M4a-10): a DEGRADED pool,
  or an ONLINE one with non-zero READ/WRITE/CKSUM counters (which passed
  silently), is captured, its counters and first 20 permanent errors
  shown, and asked about with default no.
- **FUA and UAS findings are INFO** (M2P.12, M4a-12): most USB bridges
  report no DPO/FUA, and UAS is the normal driver. WARN, prepare's
  question and health's report file come from the known-bad bridge list
  and, in read-only health, a damaged pool.
- **`purge` checks every step and verifies the wipe** (M4a-13). A failed
  `zpool destroy` stops before anything is overwritten; every wipe step
  is checked, and all run even after one fails. The ZFS labels are read
  on the old first partition right after the two overwrites, with the disk
  held from udev (`flock`, systemd's block device locking): udev re-reads
  the partition table as soon as the first overwrite closes the disk, and
  the partition would be gone (M4aR-2, M4aF-14, seen on eli). They are
  read again after `wipefs` if it left the partition table; then no
  partition left and `blkid -p` finding nothing. A pool purge finds imported read-only (a finished read-only
  `zark mount`) is exported and imported read-write before its destroy,
  which on a read-only pool writes nothing to the labels; a failed export
  or re-import stops purge before anything is overwritten (M4aR-1,
  M4aF-9).
  Any failure ends **PURGE INCOMPLETE**, exit 1, and the registry entries
  and the pool's anchors in the source stay. The banner lists every step
  and check with ✓ (done), ✗ (failed) or - (not done, and why), so it
  says what state the disk was left in; its last line names what is kept,
  or says there is nothing to keep for an unregistered disk with no pool
  label (M4aF-4, after eli P). The "IRREVERSIBLE OPERATION" line of
  purge's opening banner is yellow (M4aF-5).
- **A drive with no owner recorded is accepted only when it shares
  something** (M2R-7, M4a-7). Before keys, questions or the point, a
  read-only survey with the run's own planner; with nothing in common
  (no resume, incremental, bookmark, rollback, written or at-point
  dataset) it is refused: "no owner recorded and nothing in common with
  this system: nothing was changed". The owner is written by prepare and
  by a run that accepted the drive, never by a refusal.
- **The bpool refusal tells this system's bpool from another disk's**
  (M2V-2, M4a-6). It lists every importable bpool with its id and devices:
  one with a disk in common with rpool is this system's and is imported by
  id; any other must not be imported (it would mount over `/boot`) and its
  disk disconnected; one whose devices cannot be read is named as such,
  with no advice about its disk (M4aR-4, M4aF-12); with none on rpool's
  disk, the system has no bpool of its own. The advice
  never includes exporting bpool: on a running systemd system the export
  fails as busy (service mount namespaces hold copies of `/boot`) after
  unmounting the host's `/boot` (P6 on eli).
- recover puts a failed `zfs change-key -i` in its verdict (**RECOVERY
  COMPLETED WITH ERRORS**); prepare's keystore snapshot is destroyed by the
  cleanup when the run dies in the send (M2R-16, M4a-14).

#### Known limitations

- `zark clean` cannot release a mount held in another mount namespace (a
  service's or a snap's): it says so, and a reboot releases it.
- A drive with no owner recorded (written by zark 2.0.0-rc2 or earlier)
  and no snapshot or bookmark in common with this system (blank, or every
  anchor in origin gone) is refused; `prepare` it again (M4aR-7).
- A system whose disk holds no bpool is refused while another disk's
  bpool is importable; with none importable it backs up without one, as
  before.
- `clean` reuses umount's tree unmount unchanged; the propagation of
  unmounts to other namespaces (X-3, X-4) is M4b's.

#### Rejected approaches

- **A lock file with a pid** checked for liveness: a reused pid or a
  killed run leaves it wrong; `flock(2)` is released by the kernel.
- **`clean` by environment** (everything on a live session, only zark's
  on an installed system): the altroot already tells zark's imports apart
  in both, and a pool imported by hand on a live session is the operator's.
- **Forced or lazy unmounts and exports in `clean`** (`umount -l`,
  `zpool export -f`): a mount that disappears from view while still in
  use makes the directory below look empty to the removal that follows,
  and a forced export pulls a pool from under whoever is using it.
- **Asking before the forensic capture**: it changes nothing and the
  answer is always yes. Capturing after the export: the events and
  errors are gone by then.
- **Only warning for an ONLINE pool with error counters** (M4a-10 10b):
  checksum errors on an ONLINE pool are damage already done.
- **Importing the pool read-only in `health` to read its counters**
  (M4a-11 11b): a read-only check should not import, and importing a
  damaged pool is the step that can fail or hang.
- **Removing purge's registry entry and anchors after a failed step**
  (M4a-13 13b): it loses the only record of a pool that may still be on
  the disk.
- **A typed ADOPT for a drive with no owner and nothing in common**
  (M4a-7 7b): purge and prepare already give that path.
- **Supporting systems without bpool** (M4a-6 6b): backup, recover and
  finish would all need their own work; out of M4a's size.
- **purge refusing a pool it finds imported read-only** (M4aF-9 9b),
  instead of exporting and importing it read-write: one more step for the
  operator, for the same result.
- **health exiting 1 when its test pool was not destroyed** (M4aF-10
  10b): health exits 0 whatever the test found; one exception would
  surprise scripts more than it helps.
- **Only polling for the links after the cold reconnect** (M4aF-11 11b):
  `udevadm settle` first lets the events udevd already queued finish.

### M3 - space (2.0.0-rc3)

#### Added

- **Destination retention, per drive** (`lib/retention.py`, pure; the
  executor in `lib/engine.py`). After every complete backup: everything
  inside the window (30 days by default) stays; outside it, only the
  newest complete backup point of each calendar month (UTC, by
  `creation`), with no limit in time; an optional maximum age (off by
  default) removes anything older. Always kept: the newest complete
  backup point, and every dataset's newest snapshot on the drive, the
  base of its next incremental (I-E). A backup point is decided once for
  the whole drive, so it goes or stays whole. Never touched by the
  policy: a clone origin or a held snapshot (ZFS refuses both), names
  neither zark nor sanoid wrote, history from before the drive's first
  zark 2 backup point. Archived lineages and kept orphans are not covered
  by I-E and go whole once nothing of them is kept. A point counts as
  complete for retention when its run was (it carries a mark), even after
  part of its snapshots went, so a dataset destroyed on the drive later
  does not turn the older points into removable ones; a failed read of
  the marks skips retention instead of guessing. Every snapshot is
  destroyed by its exact name, one per command, and logged with its
  reason; what a plan frees is measured with `zfs destroy -nvp` over the
  exact names. No key is needed.
- **Space check before the transfer.** When the drive would end below its
  margin (10 % of its size by default), the policy's removals go first,
  without a question; then the oldest monthly points, listed with what
  each frees, on a yes (default no). The margin is a goal: the backup
  stops only when the transfer itself does not fit.
- **Per-drive settings** in `known_drives.json` (`retention`:
  `window_days`, `max_age_days`, `margin_pct`), validated with the rest
  of the entry; `prepare` asks the window and the maximum age with its
  other questions; `zark registry retention <name> [window=N]
  [max_age=N|off] [margin=N]` shows or changes them; `registry list`
  shows them.
- **`zark prune`**: interactive, in the source or on a connected drive.
  In the source: per kind, the count, the oldest and what removing it
  frees; sanoid's snapshots older than N days (typed PRUNE), legacy
  residues (yes/no), points left by interrupted runs and a drive's bpool
  anchors (typed PRUNE, after saying what the drive's next backup will
  ask). No floor imposed by the backups: rpool's anchors are bookmarks.
  On a drive: its retention now, then its oldest monthly points and its
  1.x history (typed PRUNE); an archived lineage or kept orphan left
  with nothing goes whole, as with the policy. A mistyped word removes
  nothing and says so.
- **Legacy residues** (`lib/origin_prune.py`): `rpool/keystore@prepare_*`
  always removable; a `syncoid_*` snapshot only on a dataset where every
  registered drive has an anchor, otherwise kept naming the drive that
  may depend on it. backup says when there are removable ones.
- **`zark status`, the MOTD and the timer.** CAP of rpool and bpool
  (warning from 80 %, critical from 90 %), the days since every drive's
  last backup (the RPO line, highlighted when the newest backup is older
  than 7 days), the horizon of drives anchored only on shared snapshots,
  the residues. `zark alerts install|remove`: an MOTD script that only
  prints `/run/zark/status.motd`, and `zark-status.timer` (hourly and
  after boot) running `zark status --refresh --notify`, which also sends a
  desktop notification for a pool at a level once a day (sessions from
  loginctl, notify-send as each session's user). `zark setup` installs
  them; the Debian package's prerm removes them.
- **Clone-aware sends (O3).** A dataset new in the source that is a clone
  of a snapshot the drive holds is sent with `-I` from that origin and
  received with `-o origin=` as a clone of the drive's copy.
- **What a drive shares with the source** (decision 5e, from eli's
  validation). Every complete backup, and `prepare`, records in the
  registry per dataset the snapshots the drive shares with the source
  besides its anchors (the newest of each sanoid type and the newest
  `syncoid_*` residue: `shared`), and the versions that prepared and last
  wrote it (`prepared_at`, `prepared_version`, `last_backup_version`;
  `org.zark:prepared-version` on the drive). Entries written before
  pick up the preparation fields from the drive at its next complete
  backup.

#### Changed

- **Completion marks** record how many datasets the point had
  (`org.zark:point-complete=<n>`) and are read from each snapshot itself
  (`zfs get -s local`), never inherited from a filesystem (M2V-3). A
  point that loses part of its snapshots (retention, a rollback, a
  destroy) is no longer complete, with nothing to rewrite (M2V-4). Marks
  `yes` written by 2.0.0-rc2 are read as complete and rewritten with
  their count at the next complete run.
- **M2V-1** (decision 12a, confirmed on eli): a dataset with an
  interrupted transfer whose copy on the drive was written since its
  newest snapshot (a read-write `zark mount` in between) was resumed on
  every run; ZFS refuses the resumed stream at its end and keeps the
  state, so every backup ended INCOMPLETE saying the next one would
  resume it. It is now listed with the other written datasets; DISCARD
  drops the partial transfer (`zfs receive -A`), rolls the dataset back to
  its newest snapshot and sends it again in the same run.
- **Hallazgo 21.** A drive with no anchor in the source is judged by the
  snapshots it was recorded to share, checked against the source now
  (decision 5e): "no anchor in origin; it still shares snapshots with it
  until about <date> (<dataset>@<snapshot>)", or "N dataset(s) share no
  snapshot with it: its next backup resends them in full", which is what
  a recover leaves (the source keeps only the restored point). sanoid
  prunes a snapshot only once it is older than its type's count of
  periods (1 h, 1 d, 7 d, 31 d, 365.25 d; `frequent_period` minutes), and
  the date takes the smallest count over the templates in use. Without a
  record the horizon stays as a condition: (n − 1) periods of a
  template's longest bucket, the smallest over the templates in use, 62
  days with zark's templates (not 90), if the drive still shares
  snapshots with the source. `zark status`, the MOTD and backup's RPO
  lines report it.
- `zark prune` on a drive lists beyond the policy only monthly points
  with something it can remove, as backup's space plan does (decision 6a).
- backup estimates a new clone sent from its origin with `-i`: OpenZFS
  2.4.1 sizes `zfs send -nvP -I <origin> <clone>@<point>` as the whole
  clone (`libzfs_sendrecv.c:1237-1255`); the send itself stays `-I`
  (decision 7a).
- `zark prune`'s warning for points left by interrupted runs says both
  costs: a drive that received part of the point rolls those datasets
  back at its next backup, after asking, and a drive whose transfer was
  interrupted sends it again (decision 8a).
- **G4a.** The 1%-of-source free-space guard is gone, and so is the
  "Target is N% full. Continue?" question; step 4 shows the root
  dataset's used and available space only, the figures the space check
  uses.
- recover names another drive's bpool anchor carried to this drive as
  that drive's backup point (M2X.5).
- backup's out-of-space advice no longer suggests destroying snapshots by
  hand.
- **The review's fix round (M3R-1 ... M3R-14).**
  - The "no anchor" lines, the H21 fallback and `zark prune`'s warning
    before removing a drive's bpool anchors describe what the next backup
    asks: a zark 2 drive's newest snapshot is its backup point, so it never
    stays incremental on a shared sanoid snapshot; it rolls the drive back
    to the newest shared one (the drive's backup points after it go) or
    resends in full. Without a record the line covers a 1.x drive
    (incremental) and a 2.x one (rollback) in one text (M3R-1, M3F-4 4a).
  - `zark status`, the MOTD and backup's final report read the source
    strictly; when it cannot be read they say so in one line instead of
    calling every registered drive unanchored (M3R-2).
  - The space check before a transfer and `zark prune`'s beyond-policy
    points destroy whole an archived lineage or kept orphan they leave
    empty, as the policy does (7a), name it before the question and count
    its space in what the plan frees (M3R-3).
  - Retention prefers, as a month's point, one no dataset skipped since
    (a snapshot before and after it, none of it: a rollback or a hand
    destroy left it partial); a month with no whole point left keeps its
    newest run-complete one, so nothing empties a month (M3R-4, M3F-5 5b).
  - `zark prune`'s "backup points left in origin" also covers the point a
    complete run kept because its anchor failed; removing it costs that
    drive a rollback of a complete point, and the warning says so (M3R-5).
  - The pins (clone origins, holds) are read strictly: retention is
    skipped and `zark prune` stops when they cannot be read (M3R-8).
    recover reads the marks strictly and stops when it cannot (M3R-9).
  - `zark alerts remove` also removes `/run/zark/notified` (M3R-10,
    M3F-7 7a). `zark alerts install` refuses a zark path with a quote, a
    backslash or a control character, which systemd refuses in `ExecStart`
    (M3R-14, M3F-9 9a). The timer stays `OnUnitActiveSec=1h`: systemd
    counts it from the timer's last trigger, condition or not (M3F-6 6a).
  - A subtree destroyed whole names, from the drive's own listing, every
    dataset it takes, those with no snapshot and partial receives included,
    and is left alone when that listing fails (M3R-11, M3F-8 8a).
  - Tests pin the window rule, the descendant check of a subtree destroy,
    the space plan's newest complete point, the 1.x history guards, the
    `[y/N]` defaults, the clone origin's guid match, the active-session
    filter, prepare's retention answers and prune's pins (M3R-6, M3R-7,
    M3R-12, M3R-13).

#### Removed

- `drives_in_danger_zone()` (never called) and
  `worst_case_retention_days()`, replaced by
  `guaranteed_overlap_days()`.

#### Known limitations

- History from before a drive's first zark 2 backup point is never
  removed by the policy; on a drive written by 1.x it stays until
  `zark prune` removes it or the drive is prepared again.
- The rc2 marks are rewritten with the number of datasets that hold
  their point when the next complete run sees them; a point that had
  already lost part of its snapshots then reads as complete.
- Desktop notifications need `notify-send` (libnotify-bin) in the
  session's user environment.
- The date a drive without anchors keeps sharing snapshots until is
  conservative: it takes the smallest count of each sanoid type over the
  templates in use, so a dataset with a longer template may keep its
  snapshots longer than the date says.
- An interrupted run anchors nothing: removing its point in the source
  costs the drives that received part of it a rollback of those datasets.
  A complete run whose anchor could not be created keeps its point in the
  source as that drive's base too, and `zark prune` lists it with them
  (M3R-5); the anchoring retry is M4's.
- The registry keeps, per dataset, only the newest shared snapshot of
  each sanoid type (decision 11a). sanoid prunes oldest first, so that
  one is the longest-lived; but if it is destroyed by hand while older
  ones of its type remain, a drive without anchors is reported as
  resending that dataset in full where a rollback to an older shared
  snapshot is still possible. The report errs towards the larger cost.
- A zark 2 drive that loses its anchors in the source (a recover does
  not restore bookmarks; `zark prune` can remove bpool's) continues at
  its next backup only after a rollback: on every dataset the drive's
  newest backup point goes, since the base is the newest shared sanoid
  snapshot, which is older than the point. A dataset whose sanoid
  template keeps no hourly snapshots shares at most a daily one, so that
  rollback also removes the drive's backup points of that day on it (eli
  S1.5: 10 points on `rpool/var`). An engine that could continue from an
  older base without destroying the newer destination snapshots is M4's.
- The space check before a transfer never offers a drive's 1.x history;
  `zark prune` on the drive does.
- A dataset received as a clone keeps its origin snapshot on the drive
  (ZFS refuses to destroy a clone's origin): retention keeps it, and a
  `zfs destroy -r` or `zfs rollback -r` of the origin's dataset on the
  drive (DESTROY, ROLLBACK) fails while the clone exists; nothing is lost,
  the run reports the failure.

#### Rejected approaches

- **Calendar buckets with fixed tails** (all points for 90 days, then
  monthly for 24 months, then yearly for 10 years): history should end
  where the drive's space ends, not at a date.
- **Copying sanoid's template to the drive**: sanoid's counts fit a
  regular local history; applied to backup points they would drop the
  drive's monthly history after about 93 days.
- **Clearing the completion mark on what is left of a point** before
  removing part of it: every path that removes snapshots, hand destroys
  included, would have to remember it, and an interrupted removal would
  leave a wrong mark. The count makes the check self-contained.
- **Removing legacy residues automatically at the end of a backup**:
  deleting dozens of snapshots in the source is not something a backup
  should do unasked.
- **A fixed free-space margin that stops the backup**: a drive the size of
  its source could never keep it.
- **A `ZARK_NOW` override to stage old points on eli**: a test hook in
  production code that could not spread points across months anyway;
  eli's wall clock does.
- **Deciding a drive's horizon by the version that wrote it** (a zark 2
  drive "keeps only points"): wrong, since sends via a bookmark carry
  sanoid's intermediates; what matters is what the drive and the source
  still share, which the recorded snapshots tell.
- **Mapping each dataset to its own sanoid template** for the date:
  copying sanoid's section resolution for a date that is approximate
  anyway.

### M2 - replication engine (2.0.0-rc2)

#### Added

- `lib/replication.py` (pure planner) and `lib/engine.py` (executor):
  zark's own send/receive loop for `backup` and `prepare`, replacing
  syncoid. Per run: `sanoid.timer` paused (waiting, with one line saying so,
  for a running take or prune) and resumed on every ending only if the
  run stopped it - `/run/zark/sanoid-paused` marks a timer zark stopped,
  so a run that finds it with the timer inactive restarts the timer an
  earlier run left stopped; one backup point
  `zark_YYYY-MM-DD_HH:MM:SSZ` (UTC) taken with one `zfs snapshot` per pool
  (a call cannot span pools); every dataset brought to it, parents first,
  with `zfs send [-w] ... | zfs receive -s -u -o canmount=noauto
  -x mountpoint -o org.zark:canmount=... -o org.zark:mountpoint=...`, never
  `-F`. The base is always the destination's newest snapshot: `-I` from it
  when origin still has it (the sanoid snapshots in between travel as
  carried restore points), or `-i` from this drive's bookmark of it and
  then `-I` (`-I` cannot start at a bookmark) when origin pruned it. A
  partial receive is resumed first (`send -t`). Its state is discarded
  with `receive -A`, which touches no snapshot, only when
  `zfs send -nvP -t` says the resume can never work (its snapshot or
  incremental source no longer exists in origin), checked before the
  questions so that what the dataset then needs is asked in the same run;
  after any other failure it is kept for the next run.
  After the transfers every dataset that holds the point is anchored -
  a bookmark `#zark_<pool GUID>_<UTC>` on rpool; on bpool, which never
  gets bookmarks, the point renamed to `@zark_<pool GUID>_<UTC>`. Every
  removal in origin (this run's point, points left by earlier runs, the
  drive's older anchors) is then checked against a fresh read of both
  sides: it goes only when the dataset is verified on the backup at the
  new point, by guid, and anchored; whatever may still be a base stays,
  nothing goes when a side cannot be read, the backup pool is no longer
  imported or a dataset vanished from it, another drive's anchors are
  never candidates, and every verdict is logged with its reason. Both
  sides are read strictly throughout: a listing that fails stops the run
  before the questions, or leaves it incomplete afterwards; it never
  shrinks what origin is expected to have or what the drive holds only
  for itself. A structural container is never offered for destruction.
  (D14-D16, redesign §4, I-A...I-G)
- The structural containers (`rpool`, `bpool`, `rpool/ROOT`,
  `rpool/USERDATA`, `bpool/BOOT`) are not replicated: recover creates them
  anew, so after a recover their snapshots never match the backup's, and
  they hold no data. The engine creates the missing ones on the
  destination (`canmount=off`, `mountpoint=none`, unencrypted) and lists
  one that holds data (> 8 MiB) or can mount as not backed up.
  (decision 13)
- backup's questions, all asked before the first byte is sent: datasets
  only on the drive (keep - remembered as `org.zark:orphan=kept@<date>`,
  destroy, ask next time, or follow a rename in origin found by guid);
  datasets whose only common snapshot is older than the drive's newest
  (first run on a drive written by zark ≤ 2.0.0-rc1, or an origin
  recovered from an older point) - one prompt per drive listing the
  snapshots at stake (three per dataset on the terminal, every name in
  zark.log): abort, archive and resend, or roll back (typed `ROLLBACK`);
  diverged datasets - skip, archive as `<name>.archived-YYYYMMDD` and
  resend, or destroy and resend (typed `DESTROY`); datasets written on the
  drive since their newest snapshot, as a read-write `zark mount` leaves
  every dataset it mounts - one prompt listing them: skip them (default,
  the backup is INCOMPLETE) or discard those changes with `zfs rollback`
  to each newest snapshot, without `-r`, so no snapshot is destroyed
  (typed `DISCARD`). Before a typed
  `DESTROY`, every dataset it takes on the drive is listed from the
  drive's own listing, archived lineages and kept orphans included. Archiving or destroying acts on the top of a subtree only:
  the datasets below move or go with it, are not asked about, and are
  resent in full. A typed word that does not match is no answer: the same
  question is asked again. A kept orphan that is back in origin (after a
  recover) loses its mark and is replicated again. The estimated transfer
  (`zfs send -nvP`, including those full resends and the incrementals that
  follow a rollback) is checked against the drive's free space before any
  choice is applied. (D17, redesign §7, M2 decisions 7, 8, 18)
- Drive metadata on the pool's root dataset, readable without the
  passphrase: `org.zark:format` (2), `version`, `origin-host`,
  `origin-rpool-guid`, `origin-machine-id`, `prepared-at`, and for a
  complete run `last-point` and `last-backup-at`; `recover` and `mount`
  show them after importing a drive. (decision 17a)
- Completion marks: a run in which every expected dataset reached its
  point sets `org.zark:point-complete=yes` on every snapshot of that point
  on the drive (a user property, never set on a filesystem, so no
  snapshot inherits it by accident). A drive written before the marks is
  marked once by its first complete run, from `last-point`. recover reads
  the marks. (review M2R-1, decision M2F-1)
- backup refuses, right after importing it and before the health check,
  the keys and any question, a drive whose `org.zark:origin-machine-id` is
  not this system's `/etc/machine-id`, or whose metadata cannot be read:
  it names the drive's host and last date, and the way out (the system's
  own drive, or purge and prepare). Nothing on the drive changes; the
  import and export stay in the pool's history. machine-id, not the rpool
  GUID, because recover creates rpool anew while `/etc/machine-id` lives
  in the restored boot environment. A drive without the property is
  accepted as before. (decided 2026-10-08)
- backup and prepare refuse while a pool named bpool can be imported
  (`zpool import` lists it) but is not, and backup stops before any
  question when the drive holds bpool's backup and origin's bpool is not
  imported. (review M2R-4, decision M2F-2)
- `zark setup` checks `feature@bookmark_v2` on rpool and offers to enable
  it (default no; it cannot be undone; GRUB never reads rpool); `backup`
  and `prepare` refuse without it. (decision 4a)
- `tests/fake_zfs.py`: an in-memory model of the zfs/zpool commands the
  engine runs, keeping the kernel rules it relies on and refusing `-F`;
  the engine, `backup` and `prepare` are tested end to end against it.

#### Changed

- **backup.** Success means every expected dataset reached the point and
  the read-back is ONLINE (I-D); otherwise BACKUP INCOMPLETE or BACKUP NOT
  VERIFIED, exit 1, and `last_backup_at` is not written. A per-dataset
  table goes to the terminal and zark.log, with the whole ZFS error of a
  failed dataset, send's and receive's when both report one; zvols other
  than the keystore are listed as NOT BACKED UP, in the table and in the
  BACKUP COMPLETED banner; BACKUP INCOMPLETE groups the datasets that
  missed the point by what the next backup does with them (resume, ask,
  try again, send, or free space first). An export that fails is a failed backup
  (H17). The banner's duration covers the whole run (P0-2) and its space
  figures are read after the transfer (P0-4). After a backup it lists the
  days since each other drive's last backup instead of retention-horizon
  warnings (decision 15). The pre-backup `sanoid --take-snapshots` is
  gone; `--no-snapshot` is accepted and has no effect. (H2/I1, I2, I3,
  H4/D5, H13, H23/F4, I4)
- **Read-back.** `verify_exported_pool_readback` returns what happened:
  could not re-import, or re-imported with health X (with `zpool status
  -v` lines), without blaming the bridge; its own re-export is checked and
  logged. (I6, I12)
- **prepare.** Every question is asked before the pool is created (the
  auto-eject one used to come after the long send). The initial send is
  the backup point only, through the engine; no `zfs set` of mount
  properties with zvols imported, no `syncoid_*` residues, nothing of
  another drive pruned; the keystore snapshot is destroyed once sent. The
  drive is registered only when every dataset and the keystore landed and
  the read-back is ONLINE, with `last_backup_at` set. (I13, I14/F9, H16,
  R2§8.5/M1P.2, M1P.14, incident §8, decisions 2 and 14)
- **Destination mount properties.** Every dataset on a drive is received
  with `canmount=noauto` and, when new, no mountpoint of its own
  (`-x mountpoint`; a mountpoint already set locally on the destination is
  kept: libzfs skips the exclusion then, lib/libzfs/libzfs_sendrecv.c:
  4313-4326); origin's values are recorded as `org.zark:*`, which `recover`
  and `mount` already read. A manual `zpool import` of a drive, or a boot
  with it attached, mounts nothing.
  (P0-11/F11/I15/D6, redesign §9, decision 1)
- **recover.** Backup points are labelled as such, one entry per point
  name however close two backups were, those of an incomplete run as
  INCOMPLETE, and other runs as carried points, each with how many
  datasets it holds; the default is the newest complete backup point. A
  dataset missing from the chosen point is left out when a complete backup
  point at or before it lacks the dataset and nothing of the dataset
  follows (destroyed in origin, never brought
  back, also for a carried point) and otherwise, when a run simply did not
  reach it, restored from its newest earlier snapshot; the table says
  which ("destroyed in origin before this point" or "no snapshot at or
  before this point"). Archived lineages serve the points older than them
  (rows marked "from archive"). The final banner's last step is `finish`,
  which regenerates grub.cfg itself. (redesign §4.2, decisions 14, 18, 19)
- **mount.** Read-write mounts the same origin tree as read-only,
  explicitly (`mount -t zfs -o zfsutil`), and says that the next backup
  will ask about every mounted dataset; archived lineages are listed
  apart.
- **registry forget** also destroys the drive's anchors in origin, by the
  pool GUID they carry, before it removes the entry; it accepts a pool
  GUID too, and for a GUID without an entry removes only its anchors.
  **registry list** shows the GUIDs that hold anchors without an entry and
  the points left by interrupted runs, which `forget` offers to remove
  once no drive is registered. **purge** removes the wiped pool's anchors,
  and **prepare** those of the entries it replaces and its own when it
  ends without registering the drive. (D16, M1 decision 4; review M2R-6,
  decisions M2F-3, M2F-4, M2F-5)
- **monitor.** "Latest" is the newest snapshot by creation (M1P.3); a run
  in progress is a `zark backup`/`prepare` process.
- The unit-suite runner counts a `SystemExit` raised by a test as a
  failure, fails the run on anything else that escapes, and always prints
  its summary. (M1P.26)

#### Removed

- `zark repair-divergent` and `lib/repair.py`: the < 64 MB auto-repair
  destroyed destination datasets without asking (against I-A) and the
  command picked the common snapshot by name order (M1P.1). backup's own
  questions replace both.
- syncoid from `backup` and `prepare` (sanoid stays for local snapshots),
  `syncoid_exclude_flag()`, `ZFS.unique_snap_names()`.

#### Known limitations

- A drive written by zark ≤ 2.0.0-rc1 asks once, at its first zark 2
  backup, how to continue when it holds snapshots newer than anything the
  source still has.
- Destination retention, origin prune and the CAP/RPO MOTD are M3:
  backup points and archived lineages accumulate on the drive until then,
  and kept orphans too.
- Volumes other than the keystore are not backed up (listed every run).
- On a drive written before the completion marks, a backup point at or
  before `last-point` is marked complete by name, unless some dataset has
  snapshots on the drive both before and after it but not the point
  itself. That rule errs both ways: a dataset that was gone from origin
  for a while and came back on the same lineage (destroyed and restored)
  leaves such a gap, so the points taken meanwhile stay unmarked and
  recover calls them incomplete; and one whose lineage on the drive starts
  after a point (sent again in full) shows none, so a legacy point that
  missed it may be marked complete; and a point whose snapshot was
  destroyed on the drive later (a rollback, by hand) while its dataset
  stayed is left unmarked too. Points taken from now on are marked by the
  run that took them.
- An anchor that could not be created leaves that dataset on its previous
  anchor and point (reported in the table and the log); the next backup
  continues from the point.
- On a drive written by zark ≤ 2.0.0-rc1, mountpoints set locally by the
  old code stay (`<pool>/rpool` at `/`, `bpool/BOOT/<be>` at `/boot`):
  `canmount=noauto` keeps the datasets from mounting, and `<pool>/rpool`,
  encrypted, cannot mount without its key. Clearing them needs a
  `zfs set` with the drive's keystore zvol present (M4). The same holds
  for `canmount=on` on datasets received by zark ≤ 2.0.0-rc1, including
  when they end up in an archived lineage: they are encrypted and mount
  only with their key loaded and on explicit request.

#### Rejected approaches

- **Keep syncoid and anchor on a real snapshot per drive kept in origin
  (redesign 1b).** Pins origin space per drive, a lost drive blocks
  pruning, and syncoid prefers any older common snapshot over a bookmark,
  so its `-F` still rolls the destination back.
- **`receive -F` when the base is the destination's newest.** It destroys
  nothing in that case, but without `-F` the kernel itself refuses any
  other base of a raw stream (ETXTBSY), and destroys no snapshot for a
  plain one (bpool), so a wrong base can never destroy points.
  Explicit, confirmed `zfs rollback -r` is the only rollback left.
- **Bookmarks on bpool.** `bookmark_v2`/`bookmark_written` are not in
  GRUB 2.12's read-only-compatible set; one snapshot per drive is kept
  there instead (bpool/BOOT already keeps three monthlies).
- **One `zfs snapshot` for rpool and bpool together.** Impossible: a
  snapshot ioctl is bound to one pool (EXDEV).
- **Replicating zvols with `volmode=none`.** recover would still not
  restore them; listing them as not backed up is the honest state until
  it does.
- **Metadata inside the keystore's LUKS volume.** Needs the passphrase and
  a read-write open of the keystore zvol on every backup.
- **recover receiving the containers instead of creating them.** It would
  keep `rpool/ROOT`, `rpool/USERDATA` and `bpool/BOOT` on the backup's
  lineage, but not the pool roots, which `zpool create` makes and a forced
  receive cannot replace on an encrypted root; the containers hold nothing
  to back up anyway.
- **Exact membership for backup points.** It kept destroyed datasets out of
  later points, but a run interrupted before reaching a dataset left a
  point that restored without it (an unbootable system if that was part of
  the boot environment); the lineage-end rule keeps the first property
  without the second, provided it takes its evidence from complete backup
  points only, which is why completion is recorded on the drive.
- **`canmount=noauto` on an archived lineage at archive time.** It needs a
  `zfs set` of a mount property while the drive's keystore zvol is present
  (chase.c:648); lineages received by zark 2 already carry `noauto`.
- **Grouping backup points by time like sanoid runs.** Two backups a
  minute apart became one entry and the older could not be chosen; a
  backup point is one atomic snapshot call with a unique name.
- **Identifying the drive's system by the rpool GUID.** recover creates
  rpool anew, so every recovered system would look foreign to its own
  drive; `/etc/machine-id` travels in the boot environment.
- **Keeping `repair-divergent` as a separate command.** Its decisions now
  belong before the transfer, where backup asks them with the space
  figures at hand.
- **A list of complete points on the pool root.** One user property holds
  at most 8191 bytes, about 315 point names: trimming it would make old
  complete points read as incomplete, and one property per month would
  need its own pruning in M3. A mark on each point's snapshots needs no
  bound and goes with the snapshots.
- **Telling bpool is missing by `/boot` being a ZFS mount.** With bpool not
  imported `/boot` is not mounted at all. `blkid` labels and
  `/etc/zfs/zfs-list.cache/bpool` were also rejected: ZFS's own `zpool
  import` listing needs no other tool, and a stale cache file could block
  every backup.
- **A separate `registry drop-anchors <GUID>` command.** `forget` already
  removes anchors by GUID; pool names cannot start with a digit, so the
  same command takes either.

### M1 - recover integrity, device identity and registry (2.0.0-rc1)

#### Added

- `lib/identity.py`: one device identity for every command. Any device
  argument (kernel name, partition, by-id, by-path) is resolved once with
  `realpath` + `lsblk` to the whole disk, its preferred by-id name (bogus
  WWN aliases last), hardware facts and the ZFS label on the disk
  (`zdb -l`). Registry entries are matched by the pool GUID on disk, then
  by `drive_id`. (I20, I25)
- `lib/registry.py`: strict parsing of `known_drives.json` (line/column of
  JSON errors, schema validation), every key always written, unknown keys
  preserved, atomic replacement. (I23, I24)
- `zark registry list|forget|fix`. (I24)
- `lib/restore_points.py`, `lib/mount_props.py`, `lib/initrd.py` (below).
- `tests/fixtures/phase0-stick-manifest.txt`: the frozen Phase 0 stick,
  used by the recover planner tests.
- `zark fix-rpool-mountpoint` (live USB only) gives a restored rpool back
  `mountpoint=/`. It sets the `zvol_inhibit_dev` module parameter before
  importing rpool (`-N`, altroot, no key loaded), so no zvol device exists
  while the mountpoint changes (chase.c:648); it lists the datasets that
  inherit from rpool with their `canmount` (a failure to list them is
  fatal), and for each one that is not `off` or `noauto` (it starts
  mounting at the next boot, over whatever the boot environment has there)
  asks whether to keep it (typing its name), set `canmount=off` in the same
  inhibited import (before the mountpoint) or abort; then it asks for YES,
  judges success by the stored property, and lists in its verdict the
  datasets it set to `canmount=off`. It exports rpool and restores the
  parameter on a normal end, an error, Ctrl-C, SIGTERM and SIGHUP (a
  closed terminal), several of them arriving together (logind sends
  SIGTERM and SIGHUP at once when it ends a session), and with stdout on
  a closed terminal or a pipe whose reader went away (a closed terminal
  with `| tee`; under `sudo` a Ctrl-C does not kill the reader, X-5);
  not on SIGKILL. The first of those signals ignores all three before
  leaving for the teardown; during the teardown SIGINT, SIGTERM, SIGHUP
  and SIGPIPE are ignored, by the `zpool export` child as well. It refuses
  to start while any `/dev/zd*` exists and names the pools to unmount or
  export first.
  `backup`, `prepare` and `finish` warn when rpool has the local
  `mountpoint=none` of the Ubuntu layout.

#### Changed

- **recover - restore points (P0-8, hallazgo 3).** Snapshots are grouped
  into runs per name family (`autosnap_`, `syncoid_<host>_`, `prepare_`,
  `zark_`, manual) by `creation`, which receive preserves. A run is split
  only when a dataset reappears more than five minutes after its first
  snapshot in it. Only runs that include the boot environment are offered,
  newest by default. Each dataset resolves to its snapshot in the run, or
  its newest snapshot not after the run; with none, it is listed as NOT
  RESTORED and named in the final banner. The keystore resolves on its
  own (newest). On the frozen stick this yields exactly four points
  (15:52:42, 16:00:01, 16:12:50, 17:00:01 UTC) instead of 14 names.
- **recover - whole first level (hallazgo 1, D6, P0-11).** Every
  filesystem under `<pool>/rpool` is restored. canmount/mountpoint are set
  at receive time (`zfs receive -u -o ...`, applied in the kernel with no
  mount), from, in order: `org.zark:*` on the destination (written by M2),
  origin's `/etc/zfs/zfs-list.cache` read from the boot environment's
  snapshot, the Ubuntu layout, and structural inference (an empty snapshot
  root with children was never mounted → `canmount=off`). Values stored on
  a 1.0.12 destination are never copied. recover no longer runs `zfs set`
  of mount properties, including the bpool one it used to run with the
  restored keystore zvol present.
- **recover - before the disk is touched (hallazgos 9, 10, 14, 15).** The
  plan is shown as a table (snapshot, offset from the point, size, mount
  properties and source). The backup pool is imported read-only,
  device-exact, under an altroot; the keystore opens read-only
  (`cryptsetup --readonly`, `mount -o ro,noload`). The hostid comes from
  the point's boot-environment snapshot mounted in a private directory,
  not from a mount over `/`. The size check sums `referenced` of the chosen
  snapshots and the keystore against the real rpool partition (bpool
  against 2G) and fails closed if any size is unknown. Target candidates
  exclude the backup drive and any disk with a mounted filesystem, active
  swap or imported vdev (the live USB), and show serial and transport.
  The whole pre-flight runs before `YES`, including the check that nothing
  is mounted on or anywhere below `/mnt/recover`, read from the whole mount
  table (it used to run after the wipe, as a refusal of `zpool create`).
  Step 6 removes `/mnt/recover` with
  `--one-file-system`, so bind mounts left by a killed run are never
  descended into. The copy of `system.key` is created 0600 in a private
  directory that Cleanup removes on every exit it handles (it used to stay
  in `/tmp` on every failure path). Both exports of the backup pool are
  verified; a failed second import is fatal.
- **Invariant I-G (P0-10).** Every backup-pool import (backup, purge,
  mount, recover, repair-divergent, read-back verification) is
  `zpool import -N -R <altroot> [-o readonly=on] -d <exact partition>`, with
  no directory or by-name scan; `prepare` creates the pool with `-R`. A
  backup pool is never in `/etc/zfs/zpool.cache` and never mounts over the
  running system. Every syncoid call left until M2 receives with
  `--recvoptions=u`.
- **purge** identifies the disk by its ZFS label and by-id, destroys the
  labelled pool through a device-exact altroot import (never a system pool
  name, never a same-named pool of another disk), and removes every
  matching registry entry. The confirmation asks for the kernel name.
  (I21)
- **prepare** refuses a disk without a by-id name instead of registering
  `<unknown>`, offers to replace registry entries of the same drive,
  explains a name clash with another drive, and creates the pool on the
  by-id vdev. (I16, I22, I23)
- **backup** rewrites a placeholder or stale `drive_id` once the pool GUID
  matched.
- **mount** imports once, under `/mnt/zark/<pool>`, read-only in read-only
  mode, instead of import / export / re-import.
- **validate_external_block_device** refuses partitions and any disk
  holding an imported pool's vdev, a mounted filesystem or active swap; the
  "nvme" substring rule is gone. (hallazgo 7)
- **repair-boot** mounts through `mount_system_pools`, so every mount is
  tracked and `/boot` is unmounted before rpool is exported; the banner is
  built from what Cleanup actually exported; the ESP is taken from the
  disk holding rpool. (P0-6)
- **initrd** (recover, repair-boot): one `dracut --force --kver=<v>` per
  kernel that has both a vmlinuz and modules, exit status honoured.
  (P0-7)
- Passphrase prompts re-ask up to three times on a typo. (I9)
- Verdict banners, prompts and answers are written to `zark.log`;
  passphrases never are. (I19) This includes the typed confirmations that
  read the terminal directly: recover's `YES` and `IUNDERSTAND`, purge's two
  confirmations and repair-boot's "continue without external pools"; found
  missing in the eli validation. A closed terminal at those prompts aborts.
  An answer is written verbatim only when it is one of the words that
  prompt accepts (or its default); anything else, such as a passphrase
  typed at the wrong prompt, is logged as its length. `zark.log` is
  created with mode 0600, and an existing one is tightened to it.
- `known_drives.json` now always carries `last_backup_at` (null until the
  first backup) and `autoeject`.
- A malformed `known_drives.json` stops every command that writes it and
  is reported loudly by the others; it is never overwritten.
- The Makefile names Debian artefacts with the tilde version
  (`2.0.0~rc1`) while the upstream tarball keeps `2.0.0-rc1`.

#### Fixed

- The four "out of space" remediations in backup pointed at `zark purge`,
  which erases the whole drive. (hallazgo 8)
- repair and repair-divergent treated an unreadable size (`used=-1`) as
  small and destroyed the dataset. (hallazgo 5)
- `ask_choice` looped forever when stdin was closed. (hallazgo 22)
- repair-boot ran `zfs set mountpoint` with the keystore zvol imported.
- recover created rpool with `mountpoint=none` (since the first import of
  the code, no recorded reason) where the Ubuntu installer uses `/`. On a
  restored system, datasets created later directly under rpool never
  mounted, and `prepare`'s mountpoint sync (it reads only rpool's own
  value) copied nothing, so drives prepared there had every dataset at
  `none` and `zark mount` mounted 0 (G4 on eli). recover now creates rpool
  with origin's value from the boot environment's `zfs-list.cache`, or `/`
  when that is absent or `none` (a `none` there is what the old recover
  left). It is set at `zpool create` (the backup pool and its keystore zvol
  are imported, so no later `zfs set`) and shown in the plan table.
- `zark mount` read-only mounted nothing useful. M1 imports the pool
  `readonly=on`, but the old path mounted each dataset at its stored
  mountpoint and relied on ZFS creating the directories; on a backup
  `<pool>/rpool`, `ROOT` and `USERDATA` are `canmount=on` (raw sends carry
  no properties), so the empty `<pool>/rpool` covered the altroot and no
  child could get a directory. Read-only mode now rebuilds origin's tree:
  the boot environment at `/mnt/zark/<pool>`, then every dataset at
  origin's mountpoint (`org.zark:*` → origin's zfs-list.cache in the
  boot environment → Ubuntu layout → structure, as in recover) with
  `mount -t zfs -o ro,zfsutil`, parents first; containers, `canmount=off`,
  other boot environments, datasets whose directory does not exist and
  datasets whose path resolves outside `/mnt/zark/<pool>` (an absolute
  symbolic link or `..` inside the backup would otherwise lead onto the
  running system) are listed as not mounted; the resolved path is what
  gets mounted. Nothing is written to the drive.
- recover created bpool and rpool on kernel names (`/dev/sda2`,
  `/dev/sda4`), and every later import recorded whatever names the disk
  had then. On eli a USB stick plugged in at power-on took `sda`; the
  initramfs import read the stick's 5 MB `sda2` as bpool's vdev, libzfs
  aborted with "internal error: Value too large for defined data type"
  instead of rescanning, and the boot stopped in the emergency shell.
  recover now creates the pools on the internal disk's
  `/dev/disk/by-id/...-partN` (kernel name only when the disk has no by-id
  name, with a warning); `chroot`, `mount local` and `repair-boot` import
  the system pools scanning `/dev/disk/by-id` first, so the zpool.cache
  repair-boot writes records stable names; `backup` and `finish` warn when
  rpool/bpool still record kernel names. Two exceptions remain: a device
  hint (`zark chroot <device>`) is tried before `/dev/disk/by-id` for
  rpool, and when the by-id scan finds nothing the import falls back to a
  plain scan without saying so.
- `zark umount` ran `zfs unmount -a`, which also tries every dataset of
  the running system; it now unmounts only `/mnt/zark/<pool>` (and
  `zpool export` unmounts whatever of the pool is left). `umount local`
  does the same with the system's altroot, so a backup mounted alongside
  it stays mounted, and it requires that altroot, resolved with
  `realpath`, to be below `/mnt/zark/`. Before `umount -R`, both branches
  make each non-ZFS mount under the tree (a chroot's or an operator's
  binds of `/proc`, `/sys`, `/dev`, `/run`, the ESP) private and unmount
  it: under systemd's shared propagation such a bind would otherwise
  carry the unmount to whatever the host mounted there since. The ZFS
  datasets are unmounted without changing their propagation, because
  their copies in other mount namespaces (systemd services, snaps, the
  namespaces snapd pins in `/run/snapd/ns`) are released only by a
  propagated unmount; making the whole tree private (an intermediate
  version) left all three pools busy on eli's live session. When an
  export still fails, `umount local` ends in SYSTEM NOT UNMOUNTED and the
  backup branch in BACKUP NOT UNMOUNTED, both with exit 1 (`umount local`
  used to print "exported cleanly" regardless). After a failed
  export, umount no longer deletes empty directories under
  `/mnt/zark/<pool>`: with the pool still mounted there, `find -delete`
  removed empty directories of the backup itself.
- repair-boot ran `update-grub` before regenerating the initrds, so with
  missing initrds (found on eli by deleting them) `10_linux_zfs` listed no
  kernel; it then restored a `grub.cfg.pre-repair` left by an earlier run
  and still reported "grub.cfg regenerated ✓". The initrds and the grub
  guard now come first; only this run's copy is ever restored; a grub.cfg
  without kernel entries ends in BOOT REPAIR INCOMPLETE (exit 1).
- finish ignored the exit status of `update-grub`: with the backup drive
  still plugged in, the grub guard refused ("External ZFS pool(s)
  detected") and finish still printed "update-grub completed" and
  "GRUB config updated ✓". finish now shares repair-boot's grub.cfg check
  (`lib/grub_cfg.py`: non-zero exit or no kernel entries is a failure,
  reported by the guard's ERROR line, and only this run's
  `grub.cfg.pre-finish` is restored); a failed update-grub,
  update-initramfs or a pool not ONLINE ends in FINISH INCOMPLETE (exit 1).
- recover ran `zpool destroy -f rpool bpool` before partitioning. With
  the new target filter those pools can only belong to another disk, so
  recover now refuses to start while they are imported instead.
- recover mounts the restored root explicitly before bpool (a
  `canmount=noauto` boot environment is skipped by `zfs mount -a`, and a
  bpool mounted first is shadowed by the root) and stops if either
  mount fails.

- Log lines (`ok`, `info`, `warn`, `error`, `dbg`, `raw`) ignore an
  `OSError` from the terminal (EIO once it is closed, `BrokenPipeError` on
  a dead pipe when SIGPIPE is ignored): the line still reaches `zark.log`
  and the command carries on. This applies to every command. Banners,
  `fatal` and the prompts still raise, and with zark's SIGPIPE at SIG_DFL a
  dead pipe still kills a command outright, except inside
  fix-rpool-mountpoint's teardown.

#### Known limitations (to M4)

- finish runs `update-grub` before `update-initramfs`, so a missing
  initrd ends in "no kernel entries" with advice that cannot fix it
  (`repair-boot` does); the results of the two EFI `dpkg-reconfigure`
  calls are not part of its verdict. (review 2, R2-7)
- repair-boot and finish decide "regenerated" by a `vmlinuz` substring
  in grub.cfg, and a grub.cfg with invalid UTF-8 ends in a generic fatal
  instead of their verdict. (R2-8)
- Cleanup's SIGTERM handler runs the cleanup and lets the command go on,
  SIGHUP is not handled, and with SIGPIPE at SIG_DFL a command whose
  output reader goes away (a closed terminal with `| tee`; under `sudo` a
  Ctrl-C does not kill the reader, X-5) dies before `atexit` runs, leaving
  the pools of recover, repair-boot or chroot imported. (hallazgo 12;
  fixed in M4b)
- `umount` leaves dataset copies in other mount namespaces (systemd
  services, snaps, the namespaces snapd pins in `/run/snapd/ns`) when a
  non-ZFS mount under the tree has a mount of its own below it: a `zark
  chroot` that died without its cleanup (`/dev` with `/dev/pts`, `/sys`
  with `efivars`), or a bind whose host directory received a mount after
  it was bound. The pool stays busy and `umount` ends in SYSTEM / BACKUP
  NOT UNMOUNTED with exit 1. Rebooting the live session releases it; the
  installed system may then stop at an emergency shell, where `zpool
  import -N -f rpool; exit` continues the boot (and `zpool import -N -f
  bpool` if it asks for it; seen on eli). (review 2, W-1; with hallazgo
  12)

#### Removed

- `lib/mount.MountedPool` (no callers; imported backup pools by name).
- recover's name-sorted restore-point menu, `_raw_send`'s silent
  fallback to another snapshot, and the hard-coded
  `_apply_root_children_canmount` pass.

#### Rejected approaches

- *Copy canmount/mountpoint from the destination.* A 1.0.12 destination
  holds `canmount=on` for containers that are `off` in origin
  (`rpool/var`, `rpool/var/lib` on the frozen stick): restoring them would
  mount an empty dataset over the boot environment's `/var`.
- *`zfs set` after the backup pool is exported.* Works for rpool but not
  for bpool, which is received after the keystore zvol exists; `receive
  -o` avoids the constraint for every dataset.
- *A fixed time window to group a run.* A syncoid replication spans
  minutes (11 minutes for the stick's prepare) while distinct points are 7
  minutes apart; grouping per family with a per-dataset repeat gap
  separates both.
- *`createtxg` across datasets.* On a destination it is receive order:
  `bpool/BOOT@16:00:01` has txg 452 while a syncoid snapshot 12 minutes
  newer has txg 59.
- *Read the hostid by mounting the backup root at its mountpoint.* That
  mount lands over `/` (or the altroot) and read the live USB's hostid; the
  snapshot is mounted read-only in a private directory instead.
- *`zpool import -d <part>` to read a disk's pool.* It cannot see a pool
  that is already imported; `zdb -l` reads the label either way.
- *Drop the export/re-import around step 9 now that no `zfs set` of mount
  properties remains.* Kept: the keystore zvol must still be the last ZFS
  operation, and the export is now verified.
- *Registering `<unknown>` and fixing it later.* Every later command had
  to guess; a disk without a by-id name is refused up front.

### Release tarball ships tracked files only

#### Fixed

- `make dist` copied the whole working tree (`cp -a .`) and removed only a
  list of caches, so anything else untracked went into the upstream
  tarball and the `.orig.tar.gz`: the dev `.venv/` (nearly all of a 29 MB
  tarball; the tracked tree is 1.2 MB), `PROJECT-NOTES.md`, `.python-version` and the private
  `etc/known_drives.json` with real drive GUIDs and by-id names. `dist-check`
  could not notice, because `dpkg-source -b .` compared the tarball with
  the same tree. `make dist` now archives `git ls-files` (tracked files, with
  their working-tree edits), and fails outside a git checkout.
- The expected untracked files of a developer tree are listed in
  `debian/source/options`; any other untracked file now makes `dist-check`
  and `deb-source` fail with "unexpected upstream changes" instead of
  leaking.
- `dist-check`'s "no debian/ in the tarball" guard looked for
  `zark-<version>/debian/`, but the tarball's top directory is `zark/`, so
  it could never fire.

#### Rejected approaches

- *`git archive HEAD`.* It drops uncommitted edits, so `fulltest` before a
  commit would check a different tree than the one being committed, and
  `deb-ppa` would upload a `.orig` that does not match the tree `dpkg-source`
  diffs against.
- *Running `dpkg-source`/`debuild -S` in a clean export.* Also closes the
  leak, but moves `deb-ppa`'s per-series `dch` edits out of the working
  tree; the ignore list plus fail-closed diff reaches the same guarantee
  without changing the upload ritual.

### Development tooling (no behaviour change)

#### Changed

- Consolidated the linting stack on **ruff**: `black`, `blackdoc`, `isort`,
  `flake8` (plus `flake8-pyproject`), `pyupgrade` and `add-trailing-comma`
  are gone, replaced by `ruff check` (rules `E`, `F`, `W`, `B`, `I`, `UP`,
  `COM`, `RUF027`) and `ruff format`. The separate RUF027-only invocation
  in the Makefile, tox and pre-commit is folded into the main one.
- Moved the 638-line `.pylintrc` into `[tool.pylint.*]` in `pyproject.toml`
  and deleted the file. Verified equivalent: the output of
  `pylint --generate-toml-config` is identical before and after, so only
  the ~45 settings that actually deviate from pylint's defaults survive.
- Added a `[project]` table so `requires-python = ">=3.12"` is declared
  once. Only ruff deduces its target version from it - mypy and pylint do
  not, and keep explicit `python_version` / `py-version` values that must
  be kept in sync. There is still no `[build-system]`, which is what keeps
  the tree non-installable; `debian/rules` documents why that is safe.
- `tox.ini` declares `requires = tox-uv`. `make tox` no longer needs
  python3.12, 3.13 and 3.14 installed by hand: tox provisions the plugin
  and uv supplies any missing interpreter.
- Bumped mypy 1.19.1 → 2.3.1, pylint 3.3.9 → 4.0.8, ruff 0.15.12 → 0.16.8,
  and the matching pre-commit revs.
- `make format` now runs `ruff check --fix` + `ruff format`; new
  `make format-check` (read-only, used by `make lint` and `make fulltest`)
  and `make basedpyright`.

#### Added

- **basedpyright**, configured under `[tool.pyright]` and run by
  `make lint`, `make fulltest`, `tox -e types` (in `envlist`) and CI. Its
  own strict preset is mostly noise here - those rule families are off,
  and the 42 findings that remained were fixed rather than silenced (see
  *Fixed*). It stays out of pre-commit: it checks the whole project rather
  than the files being committed.
- **codespell** as a pre-commit hook, and `check-toml` alongside the other
  standard hooks.
- `check_untyped_defs` for mypy. Every test is an unannotated
  `def test_x(self):`, so mypy had been skipping all 346 test bodies -
  which is why the two mismatched test doubles below were invisible to it.

#### Fixed

- `SanoidRule` was declared `total=False` purely so that `_classify`'s
  skip case could `return {}`, which made the type lie about the seven
  populated returns that every consumer indexes unconditionally. It is now
  total, and skipping is `None`. Behaviour is unchanged; the checker now
  verifies each populated return sets all three keys.
- `_diff_rules` returned `dict[str, list]` for five fixed keys holding
  three different element types. Replaced by a `SanoidDiff` TypedDict,
  which immediately caught a test building its diff as an unchecked dict
  literal.
- The bare `dict` / `list` / `set` annotations in `lib/drives.py`,
  `lib/zfs.py` and `commands/setup.py` are parameterized; the helpers
  simply had not carried down the types their only caller already declared.
- `FakeZFS` and `_FakeKeystore` are now typed by the `ZFSQuery` and
  `KeystoreLike` protocols rather than being passed where the concrete
  classes were declared. `_FakeKeystore` gained the `umount()` that
  `Cleanup.track_keystore` would have called on it.
- `test_strict_mode` raised its "should have raised" guard inside the very
  `try` whose `except AssertionError` was meant to catch the expected
  failure. Moved to an `else` clause.
- `datetime.timezone.utc` → `datetime.UTC` (17 occurrences) and
  `assert False` → `raise AssertionError(...)` in three tests, where
  `python -O` would have removed the assertion and let the test pass
  silently.
- Spelling of *eligible* in a `commands/simulate.py` comment, found by the
  new codespell hook.

## [1.0.12] - 2026-06-04

### Exact-device import + post-export read-back verification - surviving a second FUA-lie corruption mode

v1.0.11 addressed the *catastrophic* form of USB-SATA bridge corruption: all four pool labels lost after unplug. Field use surfaced a second, subtler mode on the same `0634:5604` Micron enclosures, and a device-identity trap that turned a recoverable situation into an hour of misdiagnosis. This release fixes the import path and adds a verification step that converts a silent, deferred backup loss into an immediate, visible failure.

#### The incident

A `zark backup` to drive `black` had completed successfully on a previous run (`[OK] Pool black exported`), yet the next backup failed to import it:

```
zpool import black
cannot import 'black': insufficient replicas
        Destroy and re-create the pool from a backup source.
```

Forensics told a layered story. `zpool import` (label scan) reported the pool **ONLINE** with all four labels present (`zdb -l ... labels = 0 1 2 3`, 32 valid uberblocks, txg 58702→58918 all within a 2-second window - the signature of a syncoid closing burst). But a real open failed deep in `vdev_load`:

```
spa_load(black): using uberblock with txg=58918
...
disk vdev '...': metaslab_init failed [error=52]
spa_load(black): FAILED: vdev_load failed [error=52]
zdb: can't open 'black': Invalid exchange
```

`error=52` is `EBADE`. The labels and uberblocks (fixed-position writes) persisted; the **spacemaps / `metaslab_array`** (written during the closing burst) did not. The bridge had acknowledged a flush whose data never reached NAND - the same FUA lie as v1.0.11, but landing on allocation metadata rather than labels. Because *every* uberblock in the ring references the same corrupt `metaslab_array`, rewind recovery (`-F`, `-FX`, explicit `-t <txg>`) had no consistent transaction to roll back to. The pool was unrecoverable and had to be recreated from source (`rpool` was intact - `black` is a backup *target*).

Two contributing traps were identified and are fixed here.

#### Fix 1 - Import by the exact device, not by scanning a directory

The bridge exposes a **generic, non-unique WWN** (`wwn-0x5000000000000001`) identical across enclosures - both `blue` and `black` presented the same identifier. `commands/backup.py` already computed the precise device path (`...-part1`), but `ZFS.pool_import` discarded it: it passed `os.path.dirname(device)` to `-d`, turning a precise device into a directory scan. ZFS then resolved the vdev through the generic WWN alias, which points at the **whole disk** rather than the labelled partition, contributing to the `insufficient replicas` confusion.

`ZFS.pool_import` now builds an **ordered** candidate list and tries the **exact device first** (without `-f`, then with), falling back to directory scan and finally auto-scan only if the exact device fails:

1. `-d <exact device>` - the precise `...-part1` the caller supplied
2. `-d <device directory>` - fallback
3. `-d /dev/disk/by-id/` - fallback
4. auto-scan (no `-d`) - last resort

The previous loop ordering (all directories without force, then all with force) is replaced by per-candidate `(plain, force)` so that an exact-device import needing only `-f` is still preferred over a directory scan that could latch onto the wrong alias. The public signature is unchanged; callers that import without a device (`recover`, `mount`, `repair-divergent`, all passing `device=None`) are unaffected and keep the fallback-only behaviour.

This was validated live: importing by `...-part1` makes the vdev appear under its unique serial (`usb-Micron_..._<serial>-0:0`) instead of the bogus WWN.

#### Fix 2 - Post-export read-back verification (always on)

The hard lesson: **on these bridges, a `zpool export` that returns 0 does not prove the pool is reimportable.** The only reliable signal is to read it back. A new `ZFS.verify_exported_pool_readback(name, device)` runs after `Cleanup.run()` has exported and flushed, and before the success banner:

1. `sync`, then drop the kernel page cache (`/proc/sys/vm/drop_caches`) - so the read-back comes from NAND, not from RAM still holding what was just written. Without this we would verify against cache and a silently-corrupt pool would pass.
2. Re-import **read-only, no-mount, by the exact device** (reusing Fix 1).
3. Require health `ONLINE`.
4. Re-export, restoring the on-entry state.

If the re-import fails - the `error=52` signature - `backup` now emits a red **`BACKUP NOT VERIFIED`** banner pointing at `docs/HARDWARE.md`, fires a failure notification, and returns *without* reaching the "safe to unplug" prompt. The operator is told the backup is not trustworthy even though syncoid and export both reported success. This is **detection, not prevention**: it cannot stop the bridge lying, but it surfaces the loss while the operator can still act, instead of at restore time months later.

The `backup` flow is renumbered 1/10...10/10 (verification is step 10). The check is unconditional - no flag - because the failure is silent and the cost (one read-only import/export cycle) is seconds. Per-FUA-lie scrubbing was considered and rejected (see below). Note the v1.0.11 "no rapid export/import cycles" rule applies only to the live-USB overlay during `recover`, not to external backup targets, so the extra cycle here is safe.

#### Fix 3 - `zark health` and risk checks in `prepare`

A new non-destructive command, `zark health [device]`, inspects kernel/sysfs state for the conditions correlated with bridge-induced corruption: a bridge that reports `doesn't support DPO or FUA`, the UAS transport on a USB device, and the bridge's VID:PID against a known-problematic list (`0634:5604` and `0634:5607`, the two Micron CT2000X10* USB-SATA enclosures observed to misbehave). It writes nothing, so it flags **risk**, never asserting a drive is safe - that can only be proven under write load. The checks live in `lib/health.py` as a small declarative list (`_check_fua`, `_check_transport`, `_check_known_bridge`) designed to grow as new hardware is identified; `KNOWN_BAD_BRIDGES` is a module constant.

`prepare` now runs the same readonly check before doing any work and, if a risk factor is present, warns and asks for confirmation (default no) rather than aborting - because with the usb-storage quirk such a bridge works correctly, so a hard refusal would block a usable drive. `prepare` also performs a read-back verification at the very end: since `prepare` writes the entire rpool raw send (real write-under-load with transaction churn), its post-export re-import is itself a destructive confirmation that the bridge survives load. If the read-back fails, the drive is **not registered** in `known_drives.json`, even though every prior step reported success.

`zark health` is fully interactive (no flags): it gathers all choices up front, then runs unattended except for one pause. Beyond the read-only checks it offers a **destructive** mode that creates a throwaway pool, writes ~2/15 GB (or whole-disk "surface", capped) with transaction churn, exports, and re-imports to verify the bridge persisted the writes. An optional **cold** pass powers the device down (`eject`), waits for the operator to physically reconnect, and only then re-imports - the only check that reads strictly from NAND rather than possibly from the bridge's own DRAM. Profiles show pessimistic time estimates derived from a measured write speed. The throwaway pool is always destroyed and the device wiped (`wipefs` + `sgdisk --zap-all`) on exit, leaving it genuinely blank rather than carrying orphan `zfs_member` partitions that would look like real data. Whenever any risk or test failure is found, a self-contained diagnostic report (environment, drive, bridge VID:PID, findings, dmesg tail) is written to `/tmp`, with instructions for filing a GitHub issue and an option to obfuscate serials/GUIDs (offered in both read-only and destructive modes).

The destructive test also scans the kernel log for **transport errors during the run** (`DID_ERROR`, UAS aborts/resets, non-clean failed cache flushes), captured between a pre-test dmesg marker and a scan taken after the re-import. This closes a blind spot found on real hardware: a bridge under UAS can stumble at the bus level (logging `DID_ERROR`) yet still happen to persist the data, so the read-back alone reports a misleading PASS. When the data survives but transport errors were logged, the result is surfaced as **"passed, but the enclosure is not trustworthy"** (a WARN, included in the report) rather than a clean OK. A clean `DID_NO_CONNECT` disconnect is explicitly excluded, consistent with the `hostbyte` distinction below. The device picker also lists bare connected USB disks (not just pool-bearing/registered drives), so a blank enclosure - the device a destructive evaluation actually targets - is selectable without passing an explicit path.

#### Per-drive auto-eject (timed eject prompt)

The eject prompt can now time out for drives that opt in. A new per-drive `autoeject` boolean in `known_drives.json` (default false, asked by `prepare`, editable by hand) makes the prompt show a 10-second countdown (`EJECT_TIMEOUT_SECONDS`, a fixed constant - the per-drive flag is the only knob) and then **eject** the drive automatically. Auto-eject always ejects on expiry: the command's own no-eject default (used by `prepare`/`repair-divergent` so the manual path leaves the drive attached for a following backup) is overridden once the flag is on, because a field named "auto-eject" must do what it says. Any keypress cancels the countdown and falls back to the normal blocking prompt, so a present operator always keeps control; with no TTY (cron/systemd) the drive is ejected immediately. This is implemented as `Log.ask_timeout` using `select.select` (no signal handlers, no threads). When `autoeject` is false or absent - the default for every existing drive - behaviour is unchanged. The `known_drives.json` schema (including this field) is now documented in the README.

#### Disconnect noise is not a health signal

The kernel always attempts a final cache flush at USB disconnect; on these bridges it routinely "fails". An A/B test on the same hardware showed the `hostbyte` - not the message - is what matters: under UAS with dirty data in flight it is `DID_ERROR` (the corrupting path); at clean unplug of an exported pool it is `DID_NO_CONNECT` (harmless "device gone"). zark therefore must **not** treat `Synchronize Cache failed` as a fault indicator - it appears on every clean disconnect. The authoritative signal is the read-back, which is exactly what Fix 2 implements. This reasoning is documented in `docs/HARDWARE.md`.

#### Documentation

New `docs/HARDWARE.md` records: the offending bridge `idVendor=0634 idProduct=5604`; both corruption modes (lost labels vs. `metaslab_init error 52`); the bogus shared-WWN trap; the `hostbyte` distinction at disconnect; the read-back mitigation; and the system-level `usb-storage quirks=0634:5604:u` quirk that forces the conservative transport (with the caveat that read-back runs unconditionally regardless, because a drive may be used on a machine without the quirk).

#### Rejected approaches and their flaws

- **Rewind recovery (`-F` / `-FX` / `-t <txg>`)** to save the corrupt pool: every uberblock referenced the same broken `metaslab_array`; there was no consistent transaction to return to. Confirmed dead end via `zdb -e`.
- **`zpool import -m`** (allow missing log device): the corruption was in the main vdev's spacemaps, not a separate log; failed identically.
- **`zpool scrub` as part of read-back verification:** correct in spirit but far too costly (hours on a 1.8 TB target) for an always-on post-backup step. A read-only re-import already surfaces structural corruption like `error=52` at open time, which is what we need. Left out; could be an opt-in flag later.
- **Relying on the UAS quirk alone** as the fix: the quirk is a system-level change outside zark's portable, zero-install scope, and does not protect a drive used on another machine. It is documented as a recommended mitigation, but the read-back is the in-tool guarantee.
- **Treating `Synchronize Cache failed` in `dmesg` as the failure signal:** it fires on every clean disconnect too (`DID_NO_CONNECT`); using it would produce a false positive on every successful backup. Discarded in favour of the read-back.

### `repair-boot` hardening - guaranteed clean export and automatic forced-import fallback

A kernel update applied in the background by `unattended-upgrades` while a backup drive was connected left the system unbootable: the `09_zfs_backup_guard` correctly aborted `update-grub` (external pool visible), but the kernel transaction had already swapped `/boot` contents, so `grub.cfg` ended up referencing a kernel that autoremove had pulled. Recovery with `repair-boot` then hit two latent bugs in the command itself.

#### Bug 1 - `repair-boot` never registered its `Cleanup`, so a mid-run fatal left the pools imported (and forced)

`commands/repair_boot.py` constructed a `Cleanup` but, unlike every other command, never called `cleanup.register()`. `Cleanup.run()` was only invoked explicitly at the final step. Any `log.fatal()` before that point - e.g. the import step failing - exited without exporting `rpool`/`bpool`. A pool left imported is marked in-use by the live host, so the next real boot's initramfs import (which carries no `-f`) failed: `repair-boot` could *manufacture* the very forced-import-at-boot problem it exists to fix. Now `cleanup.register()` runs immediately after construction, so `atexit`/`SIGTERM` guarantee a clean export on every exit path. A clean export is precisely what lets the next boot import without `-f`.

#### Bug 2 - the import lacked a `-f` fallback, so an unclean pool aborted the repair

Step 2 issued a raw `zpool import -R /mnt/repair -N <pool>` with no force fallback and `log.fatal()` on failure. But the operator reaches `repair-boot` *because* the system shut down uncleanly, which is exactly when a pool is left "in use" and a plain import fails - forcing the operator to know to run `zpool import -f` by hand outside zark. The import now routes through `ZFS.pool_import(pool, altroot=..., no_mount=True)`, which already tries a clean import first and only then `-f` (and is regression-tested). The forced state is transient: the guaranteed clean export from Bug 1's fix consumes it, so the next boot imports without `-f`. The end-of-run banner adds a note that a one-off forced-import prompt at the next boot is benign.

#### Bug 3 - tautological live-USB guard

The environment check read `Path("/").stat().st_dev == os.stat("/").st_dev` - comparing a value with itself, always true - and then tested `zfs get ... mounted rpool/ROOT`, a container dataset that is never mounted. The guard never did anything. Replaced with `sh.is_live_usb()` plus an explicit confirmation prompt: if we are not on live media, warn and ask before continuing (pointing at `zark finish` for the installed system), rather than either silently proceeding or hard-aborting. The confirmation, not a hard refusal, avoids locking the operator out on exotic/remastered live media where casper markers are absent.

`is_live_usb()` is promoted to `lib/sh.py` as the single source of truth (routed through `run` so it is mock-testable) and `recover.py`'s private copy now delegates to it, removing the duplicated detection logic.

#### Rejected approaches

- **Adding a `--force` flag to `repair-boot`:** violates zark's "explicit confirmation, no `--force`/`--yes`" design, and is unnecessary - the clean-import-then-`-f` fallback inside `pool_import` already does the right thing without operator ceremony, and the guaranteed export neutralises the forced state.
- **Hard-aborting when not on live media** (the apparent intent of the old guard): rejected for the remastered-media false-negative; a confirmation prompt is safer and still steers the operator to `zark finish`.
- **Leaving `is_live_usb()` duplicated** in `recover` and `repair-boot`: rejected on the project's no-duplication principle; a single mock-testable helper in `lib/sh.py` is cheaper to keep correct.

### New `zark chroot` command + local-system mount/umount

The recovery toolkit could mount and chroot a *backup* pool (`zark mount` prints chroot instructions for the nested `<pool>/rpool`), but offered no first-class way to chroot the **installed system itself** from a live USB - the exact thing needed to fix a broken boot by hand (run `apt`, `update-grub`, `dpkg-reconfigure` inside the real system). Operators were left assembling `zpool import` / keystore-unlock / bind-mount / `chroot` by hand, getting the import-without-`-f` and clean-export details wrong in precisely the way that breaks the next boot.

#### `zark chroot [device]`

A new interactive command that, from a live session where `rpool`/`bpool` are not imported:

1. Imports both pools under an altroot (`/mnt/zark/chroot`) via `ZFS.pool_import`, inheriting the clean-then-`-f` fallback.
2. Unlocks the keystore (passphrase) and loads all dataset keys.
3. Mounts the boot environment and its children, then `bpool/BOOT/<be>` at `/boot`.
4. Sets up the bind mounts a chroot needs (`/proc`, `/sys`, `/dev`, `/dev/pts`, `/run`), `efivars`, and the ESP at `/boot/efi`.
5. Drops the operator into `/bin/bash --login` inside the system.
6. On shell exit (or Ctrl-C, or any fatal) tears everything down and **exports both pools cleanly**, so the next real boot imports without `-f`.

Safety: it refuses when `rpool` is already imported (you are either already inside the running system, or a previous run left it imported - the message points at `zark clean`), and warns+confirms when not on live media. The interactive shell is the one place zark deliberately does not route through `sh.run` (which captures output and would break a live TTY); it execs `chroot ... /bin/bash` with the controlling terminal attached, via a fixed argument vector (no shell, no string interpolation).

#### `zark mount local` / `zark umount local`

The "generalised mount/umount" the operator asked for, kept deliberately light: `mount` and `umount` now accept an explicit `local` (alias `system`/`rpool`) target to operate on the installed system's pools rather than a removable backup drive. `zark mount local` imports + unlocks + mounts the system read-only (default) or read-write for inspection and leaves it mounted; `zark umount local` exports it again. The no-argument behaviour of both commands is unchanged (scan backup drives).

`umount local` carries the critical safety discriminator: zark always imports the system under an altroot beneath `/mnt/zark`, whereas the *running* system's `rpool` has `altroot = -`. `umount local` refuses to export any pool whose altroot is not under `/mnt/zark`, so it can never pull the root filesystem out from under a running machine. `umount local` also closes the rpool keystore (the LUKS mapping over the keystore zvol) before exporting: `zfs unmount -a` and `unload-key` do not tear down the cryptsetup device, so without an explicit close the zvol keeps a device-mapper holder and `zpool export rpool` hangs indefinitely in `taskq_wait`. This was found on real hardware (live-USB session) and fixed by reusing `Keystore.attach_to_pool()` + `umount()`; `attach_to_pool()` now detects an already-open mapper and marks the instance mounted so the close actually runs (previously it early-returned, silently leaving the zvol held - the same latent gap that affected any caller closing an externally-opened keystore).

#### Shared system-mount helper

The top-level `rpool`/`bpool` mounting logic (distinct from the nested backup-pool layout `MountedPool.mount_rpool` handles) is centralised in `lib/mount.py` as `mount_system_pools()` + `find_system_root_dataset()`, used by both `zark chroot` and `zark mount local`. It mounts via altroot + each dataset's *stored* mountpoint and **never runs `zfs set mountpoint`**, honouring the absolute rule against mountpoint changes while the keystore zvol is imported (`zfs mount <ds>` mounts `canmount=noauto` boot environments without touching properties). `repair-boot`'s own inline mount path is intentionally left as-is - it interleaves grub regeneration and is field-validated - rather than retrofitted in the same change that just hardened it.

#### Rejected approaches

- **Overloading `zark mount` to auto-detect the system pool** (no explicit `local` keyword): rejected - the default scan-backup-drives behaviour must stay predictable, and silently importing the system pool because no backup drive was found would be surprising and risky. An explicit keyword is clearer.
- **A single generic mount path for both backup (nested) and system (top-level) layouts:** rejected as premature unification; the keystore location, dataset hierarchy and root-dataset discovery differ enough that one branchy function would be harder to reason about than two focused ones.
- **Refusing `zark chroot` outright when not on live media:** rejected for the same reason as `repair-boot` - a confirmation prompt handles remastered media without locking the operator out - though chroot additionally hard-refuses when `rpool` is already imported, which unambiguously means the running system.

### Test-suite lint cleanup (pylint 10.00/10)

Pulled the `lib.health` imports in the health test classes up to module level and collapsed three identical inline `FakeZFS` helpers into one module-level `_FakeHealthZFS` (with a docstring). These were the last `import-outside-toplevel` (C0415) and `missing-class-docstring` (C0115) findings; with them gone the aggregate `pylint lib commands tests zark` is a clean 10.00/10. No behavioural change - test-only.

### New apt guard - block kernel/GRUB upgrades with an external pool attached

The boot-loss incident this release addresses had a root cause upstream of `repair-boot`: a background `unattended-upgrades` kernel upgrade ran *while a zark backup drive was connected*. The grub guard (`09_zfs_backup_guard`) correctly aborted the `update-grub` step, but by then the kernel package transaction had already swapped `/boot`, and autoremove had pulled the previous kernel - so `grub.cfg` ended up referencing a kernel that no longer existed. The grub guard fires too late in the package lifecycle to prevent this; it can only stop `grub.cfg` regeneration, not the kernel unpack that precedes it.

The new guard moves the defence earlier, to before dpkg unpacks anything.

#### Layer A - `DPkg::Pre-Install-Pkgs` hook (the load-bearing defence)

A new installer module `lib/apt_guard.py` (mirroring `lib/grub_guard.py` as the single source of truth) deposits three standalone files:

- `/usr/local/lib/zark/apt-zfs-backup-guard` - a POSIX `sh` hook. APT runs it before dpkg unpacks (Version 1 protocol: `.deb` paths on stdin); a non-zero exit aborts the entire transaction. The hook collects the boot-critical packages in the transaction first (so ordinary installs exit before paying for any device scan), and only then - if any `linux-image-*` / `linux-headers-*` / `linux-modules-*` / `grub-*` / `shim-*` / `zfs-*` / `zfsutils-*` package is present - checks for an external (non rpool/bpool) pool via `zpool import`. External pool + boot-critical package ⇒ refuse, with a message naming the pool(s) and the `zpool export` fix.
- `/etc/apt/apt.conf.d/09zark-zfs-backup-guard` - registers the hook.
- (Layer B, below.)

The hook is **standalone by design**: it detects pools with `zpool` directly and never calls `zark`. This matters because the recovered system runs without zark present - a zark-dependent hook would be inert exactly where the protection is needed. It also helps any ZFS-on-root system, not just zark installs.

A `ZARK_INTERNAL=1` environment escape lets zark's own flows that import the backup pool or run apt deliberately (`recover`, `finish`, `repair-boot`, and `setup` - whose dependency install must not be refused by a guard a previous setup installed) bypass the hook. It is set centrally in the dispatcher for those commands only; routine operations (`backup`, `mount`, `chroot`, `prepare`) are left unmarked so the guard still protects against an accidental upgrade while a drive is connected.

#### Layer B - login-time MOTD reminder (secondary defence)

`/etc/update-motd.d/99-zark-external-pool` prints a one-line warning at login when an external pool is visible, so an interactive operator sees that boot updates are currently blocked even outside an apt run. A full `wall`/`notify-send`-on-attach path (which would need a udev/systemd trigger and fragile graphical-session detection) was deliberately deferred - the apt hook is what actually closes the vector; the MOTD line is a cheap, reliable reminder.

#### Installation

`zark setup` installs the guard on the running productive system (this is the system most exposed to the `unattended-upgrades` vector); `recover` and `finish` install it into the recovered system (`finish` non-clobbering, like the grub guard). `install()` is idempotent: when the three files already hold the canonical content it writes nothing (no mtime churn), so re-running `zark setup` is a true no-op.

#### Rejected approaches

- **Relying on the grub guard alone:** rejected - it fires after the kernel unpack, so it cannot prevent the half-applied state; it can only refuse to regenerate `grub.cfg`, which is what produced the brick in the first place.
- **A `DPkg::Pre-Invoke` hook (no package list):** rejected - it would have to block *all* apt operations whenever a drive is attached, since it cannot see which packages are involved. `Pre-Install-Pkgs` lets the guard be surgical: only boot-critical operations are refused.
- **Requesting the Version 2/3 hook protocol:** rejected as unnecessary - the Version 1 `.deb` file list is all the hook needs, and keeps the parsing trivial.
- **Implementing the hook in Python and invoking `zark`:** rejected for the standalone reason above - the recovered system has no zark, and an apt hook must not depend on a binary that may be absent.
- **A `systemd`/udev `wall`+`notify-send` notice now (Layer B2):** deferred - added machinery and a fragile graphical-session lookup for marginal gain over the apt hook plus the MOTD line.

## [1.0.11] - 2026-05-21

### Safe USB disconnect - `sync` + sleep + interactive `eject` + visible banner

Hardware validation of v1.0.10 uncovered a class of pool-corruption events that was not zark's fault but happened because zark gave the operator no visible signal of when the underlying device had actually flushed. A backup to drive `black` completed with `[OK] Pool black exported`, the operator extracted the USB drive shortly after, and the pool came back FAULTED on next import - all four redundant labels reported `failed to unpack label 0/1/2/3`. Forensic `dmesg` trace at unplug:

```
[27338.563] usb 2-1: USB disconnect
[27338.582] sd 0:0:0:0: [sda] Synchronizing SCSI cache
[27338.748] sd 0:0:0:0: [sda] Synchronize Cache(10) failed: hostbyte=DID_ERROR
```

and on attach, the smoking gun:

```
[27277.040] sd 0:0:0:0: [sda] Write cache: enabled, read cache: enabled,
                              doesn't support DPO or FUA
```

ZFS issues writes with FUA (Force Unit Access) on critical metadata - uberblocks and the four redundant pool labels - meaning "do not acknowledge until this byte is on persistent media, not in volatile cache." Many cheap USB-SATA bridge chipsets (including the one in the Micron CT2000X10* enclosures used here) silently ignore FUA: they acknowledge from their internal DRAM buffer before the data has reached NAND. ZFS believes the write is committed and `zpool export` reports success while the bridge still holds dirty pages. The kernel emits a final `SYNCHRONIZE CACHE` SCSI command on USB disconnect, but if the cable is already out it fails with `DID_ERROR` and the pending writes - including possibly the labels - are lost. This is a well-known failure mode of ZFS on USB-SATA; zark is *for* portable backup on pendrive and must therefore make this safe.

v1.0.11 introduces a kernel-side flush at every point where zark exits with a pool exported, plus an **interactive eject prompt** with command-specific defaults so the typical operator workflow - including `prepare` immediately followed by `backup` on the same drive - is preserved.

#### Layer 1 - kernel-side flush, always

A new module-level helper `flush_device_cache(log)` in `lib/cleanup.py` performs:

1. **`sync(2)`** - kernel-side flush. Blocks until the kernel has emitted all dirty buffers to the device. Protects against losing the kernel's write-back cache.
2. **2-second pause** (`USB_FLUSH_DELAY_SEC`, module-level constant, patchable in tests). Gives the bridge firmware a window to drain its internal write queue.

This happens unconditionally after every successful `zpool export`, via `Cleanup.run()` for commands that go through `Cleanup` (`backup`, `recover`, `repair-divergent`, `repair-boot`) and via direct calls inline for the rest (`prepare`, `purge`, `umount`). Failures are non-fatal: by the time it runs, the on-disk data is already durable; what this layer protects is the operator's option to physically remove the drive a few seconds later without losing what the firmware has not yet confirmed.

#### Layer 2 - interactive eject prompt

A new module-level helper `eject_device(device, log)` issues SCSI `SYNCHRONIZE CACHE (0x35)` followed by `START STOP UNIT (stop=1)` via `eject(1)` (core `util-linux`, no new dependency). SYNCHRONIZE CACHE is the device's most authoritative flush primitive - the bridge sees it as a distinguished operation ("host is asking for a global flush right now") and most chipsets that cheat on inline FUA still honour it. STOP UNIT then powers the controller down so any cache concern is moot.

**The eject is never automatic.** A new `prompt_eject_or_attach()` helper asks the operator after the operation's success banner. The default Y/N reflects the typical workflow:

| Command            | Default eject | Rationale |
|--------------------|---------------|-----------|
| `backup`           | **yes**       | Typical end-of-session: one Enter unplugs. Operators rotating multiple backups answer `n` to keep the drive attached for the next run. |
| `umount`           | **yes**       | The operator already signalled intent to disconnect by running `umount`. |
| `purge`            | **yes**       | A purged drive is being retired or repurposed - disconnect is the next step. |
| `recover`          | **yes**       | The printed next-steps tell the operator to unplug the live USB and reboot - they're done with the backup drive too. |
| `prepare`          | **no**        | Canonical next step is `backup` against the freshly-prepared drive - auto-ejecting would force a pointless unplug/replug cycle. |
| `repair-divergent` | **no**        | Canonical next step is `backup` to validate the fix. |
| `repair-boot`      | n/a           | Touches only internal rpool/bpool on the system disk; no removable drive involved. No prompt. |

EOF on stdin (cron / systemd timer / scripted run) uses the default. There is no `--eject` / `--no-eject` flag - the prompt with a sensible default is the only knob, consistent with zark's "explicit confirmation by design" policy.

#### Two banners, distinct intent

- **Green `banner_safe_unplug(drive)`** - `💾 Safe to unplug drive 'X'`. Emitted *after* a successful eject. The unambiguous end-of-output signal that the operator may now disconnect.
- **Cyan `banner_drive_attached(drive)`** - `🔌 Drive 'X' flushed, still attached`. Emitted when the operator declined the eject. Tells them the kernel-side flush is done but the drive is *still in `/dev`* and a physical unplug now is not recommended - they should run `zark umount` (or accept the eject in a later command) first.

Operators must never see one banner when the other was meant - the colour, icon, and wording are deliberately disjoint.

#### Architecture: `Cleanup` is device-agnostic

The classifier "external vs. internal pool" used to live inside `Cleanup` in earlier drafts of this change. It was moved out: `Cleanup` now does only what its name says - exports pools, unmounts mounts, closes keystores - and always pairs a successful export with `flush_device_cache()`. The eject decision is the calling command's, because only the command knows what the operator is likely to do next. Rejected approaches and their flaws:

- **Auto-eject in `Cleanup`** (the test1 prototype): broke the `prepare → backup` flow by ejecting the drive between the two commands. Recoverable only by unplug/replug. Discarded.
- **`hdparm -W 0`** to disable write cache permanently: ~3× slower backups, behaviour varies across enclosures, and the setting persists.
- **`sg_sync` / `sdparm --command=sync`** to flush without STOP UNIT: would let the device stay attached after a flush, but neither is core on a minimal Ubuntu install. `eject` is in `util-linux` - always available - and the STOP UNIT side-effect is fine *because the operator just told us they were about to unplug*.
- **Detecting USB-only and applying different code**: the FUA-broken behaviour is a property of misbehaving firmware, not USB specifically. The protection is unconditional.

#### Tests

Twenty new test cases in `tests/test_unit.py` across three classes: `TestCleanup` (export/sync/forced-export ordering, no-eject-from-Cleanup invariant, no-flush-on-failed-export, `exported_pools()` query API), `TestFlushAndEjectHelpers` (sync issued, never ejects, sync failure handling, eject_device return contract), `TestPromptEjectOrAttach` (yes/no branches emit the correct banner, default-on-EOF for both `True` and `False` cases, unresolvable device falls through to attached banner without prompting), plus the `banner_safe_unplug` / `banner_drive_attached` assertions in `TestLog`. `USB_FLUSH_DELAY_SEC` is a module-level constant precisely so tests can patch it to 0; the full unit suite (299 tests) runs in under 2 seconds.

#### Operational notes

- After answering "yes" to the eject prompt, the bridge is powered down. The drive **disappears from `/dev`** until physically replugged. This is intentional - the workflow is "we're done, disconnect" - but worth knowing if a follow-up command needs the same drive without replug.
- After answering "no", the drive stays in `/dev`, the kernel-side flush has run, and the next zark command against the drive will work normally and prompt again at its end.

## [1.0.10] - 2026-05-12
---

## [1.0.9] - 2026-05-12

### Snapshot retention - `template_minimal` raised to ~3 months

The original `template_minimal` policy in `zark setup` retained `daily=2` (two days, no weekly, no monthly) for `rpool`, `rpool/ROOT`, `rpool/var`, and `bpool` - every "container" dataset that barely changes. This was incompatible with any realistic drive-rotation schedule: a backup drive disconnected for more than 48 hours would lose its anchor in source and diverge on every container dataset, leaving the leaves (`rpool/ROOT/<ubuntu>`, `rpool/USERDATA`, all `production`-template) intact while syncoid aborted with `Cowardly refusing to destroy your existing target` on the rest.

- **`template_minimal` is now `daily=14 weekly=8 monthly=3`.** ~3 months of guaranteed overlap, ~25 snapshots per dataset. Container datasets weigh KB to a few MB so the metadata cost of the extra snapshots is negligible. `template_production` is unchanged - it already had ~3 months of headroom on the active leaves.
- **`zark setup` detects the migration.** The existing diff machinery now compares `[template_minimal]` against the expected values. When they disagree, the diff is shown alongside any other rule changes through the same "Apply these changes?" prompt - no separate flow. Saying "no" leaves the file untouched and re-running `zark setup` will offer the migration again (the choice is not persisted, by design - every setup run gets a fresh chance). Saying "yes" regenerates `sanoid.conf` with `.backup.<ts>` written first, preserving manual sections the user has added under non-managed prefixes (e.g. `[tank/games]`).

### `--no-sync-snap` for syncoid: end of the rotation-warning cascade

When a user rotated between two backup drives, syncoid emitted a long cascade of warnings on the second-to-second-drive run:

```
could not find any snapshots to destroy; check snapshot names.
WARNING:  zfs destroy 'blue/rpool'@syncoid_host_<ts> failed: 256 at /usr/sbin/syncoid line 1596.
```

Repeated for every dataset, every alternation, every backup. The replication itself worked, but the operator was buried in apparent errors. Tracked down to the [`pruneoldsyncsnaps` mechanism in syncoid](https://discourse.practicalzfs.com/t/managing-syncoid-snapshots-with-multiple-destinations/4452): syncoid creates `@syncoid_<host>_<ts>` snapshots on source/target before each transfer and tries to garbage-collect older ones afterwards. With multiple destinations, the run to drive B destroys the source snapshot that drive A still needs as its anchor - the next run to drive A then warns loudly when the matching target snapshot has nothing to delete.

- **`zark backup` now passes `--no-sync-snap` to syncoid for both rpool and bpool transfers.** Syncoid no longer creates its own anchor snapshots; instead it picks the most recent existing snapshot in source as the anchor. Step 6 already takes a fresh `sanoid --take-snapshots` pass before each backup, so a clean anchor is guaranteed.
- **No filtering of stderr; the bug is fixed at the source.** Future warnings from syncoid (real ones) keep showing up unmuted.
- **`@syncoid_host_*` snapshots that already exist on drives are left in place.** They're inert - syncoid no longer references or recreates them. They die naturally when the drive is purged.
- **`--no-snapshot` + missing anchor surfaces a WARN.** When the operator passes `--no-snapshot`, `zark backup` no longer takes a fresh sanoid pass. If sanoid hasn't run independently, source has no recent anchor; the WARN tells the operator that syncoid may abort, but the run continues so syncoid's own (more authoritative) error wins if it does fail.

### Drive staleness reporting

A new `last_backup_at` field per drive in `etc/known_drives.json` records the ISO-8601 UTC timestamp of every successful backup. Reporting is purely informative - there is **no FATAL** based on staleness, because the actual divergence threshold depends on sanoid retention (which the operator can change) and a backup that has crossed the threshold may still succeed if some shared snapshot happens to remain. When syncoid does abort, the existing divergence handling in `lib/repair.py` and `commands/repair_divergent.py` already takes over.

- **Retention horizon is read at runtime from `/etc/sanoid/sanoid.conf`.** New `lib/sanoid_retention.py` parses the file and returns `worst_case_retention_days = max(daily, weekly×7, monthly×30)` over templates actually used by `[rpool*]` or `[bpool*]` sections. Adding the buckets together would double-count: the monthly snapshot, taken once per month, replaces older daily/weekly snapshots once they age past their bucket - so the longest single bucket defines the horizon.
- **End-of-backup banner gains two informative messages.** After `BACKUP COMPLETED`, if the selected drive was already past the retention horizon when the run started, a WARN explains the situation and points at `sudo ./zark purge && sudo ./zark prepare` (the only thing that actually fixes a fully-aged drive) plus an explicit note that `zark repair-divergent` does **not** fix staleness - it only fixes divergent datasets after a syncoid abort, which is a different problem. Below, an INFO list shows other known drives whose age has reached the danger zone (`≥ retention - 30` days).
- **`zark repair-divergent` shows the same staleness note** at the end of a run that found nothing divergent but selected a drive in the danger zone - the operator who came expecting a fix is told why this command can't help.
- **The field auto-populates on first successful backup.** Existing `known_drives.json` files keep parsing without modification; the field is emitted on disk only when populated, keeping the JSON minimal. Failure to persist the timestamp at the end of a backup is a WARN, not fatal - the backup itself succeeded.
- **No FATAL gate on staleness.** Earlier in development the design called for a 60-day FATAL guard. Discarded after testing because the actual horizon is whatever sanoid retains (90+ days with the new `template_minimal`), and bailing out before syncoid loses information that syncoid itself would surface clearly anyway.

### `zark repair-divergent` - interactive review

The previous `repair-divergent` only handled the "all divergent datasets are ≤ 64 MB" case (auto-destroyed silently) and aborted with FATAL on anything larger, telling the user to "use `zfs destroy` directly." That's the right safety posture for a non-interactive flow but leaves the operator with no zark-supported way to repair real divergence. The command is now a guided review:

- **Per-dataset context block.** For every divergent dataset above 64 MB, prints `used`, snapshot count and the date range on the target, the most recent snapshot suffix shared with source (or `none`), child-dataset count and aggregated `used`, plus a one-line hint classifying the pattern (orphan, container with intact leaves, leaf rotated past target).
- **Three-choice action prompt: destroy, skip, or abort all.** Default is `skip` - the only fully reversible choice. `abort` walks out without touching anything else, preserving the rest for a later session.
- **Typed `DESTROY` confirmation above 1 GiB.** When the operator picks `destroy` on a dataset over 1 GiB (`DOUBLE_CONFIRM_BYTES`), a second prompt requires typing the literal string `DESTROY` (case-sensitive). Anything else cancels the destroy. The threshold is hardcoded by explicit user request - no flag.
- **Mid-flight failure policy, asked once.** When a `zfs destroy` fails (busy zvol, lock contention), the operator is asked once how to handle the rest of the run: `continue`, `abort`, or `keep state and abort` (the third stops immediately and leaves the pool exactly as it is, no further cleanup-driven changes). The choice is sticky for the rest of the session - subsequent failures don't reprompt.
- **Final summary banner** lists the destroyed and skipped datasets and points at `sudo ./zark backup` (which recreates destroyed datasets via initial replication) plus a re-run hint when anything was skipped.

The 64 MB silent threshold matches `zark backup`'s auto-repair path so the two flows stay symmetric.

### Documentation

- **README: new "Drive rotation and retention policy" section** between Requirements and Testing. Explains the divergence mechanic, the two retention templates with their windows, the staleness reporting model, and the new interactive `repair-divergent`.
- **README: Apport popup screenshot** added inline at the top of the existing "System program problem detected" troubleshooting section. The image lives at `docs/images/apport-popup.png` (a new tree under the project root, not shipped in the deb).

### Process plumbing

- **`lib/sanoid_retention.py`** (new module) parses `/etc/sanoid/sanoid.conf` with the standard `configparser` and computes the worst-case retention horizon over templates referenced by managed sections. Returns `None` and emits a visible WARN when the file is absent - backup itself does not require sanoid.conf, so staleness reporting becomes unavailable rather than blocking.
- **`lib.config.now_utc_iso()` and `parse_utc_iso()`.** Single source of truth for the timestamp written to `last_backup_at`, including parse tolerance for both `Z`-suffix and explicit `+00:00`. Tests can monkeypatch the clock without reaching into a command module.
- **`DriveInfo.last_backup_at: str | None = None`.** Added field with a `None` default so existing call sites that construct `DriveInfo` keep working unchanged.
- **`drive_staleness_days(info)`, `is_drive_stale(info, threshold_days)`, `drives_in_danger_zone(...)`** in `lib.drives` wrap the timestamp arithmetic and the danger-zone selection. All accept an optional `now` argument so tests can pass a fixed clock.

### Tests

- **281 unit tests**, ruff clean, pylint 10.00/10. New coverage:
  - `TestConfigTimestamps` (5 tests): now_utc_iso shape, parse round-trip, Z and +00:00 suffix handling, garbage input.
  - `TestConfigKnownDrivesTimestamp` (5 tests): legacy file load, modern file with field, save omits None, save preserves, ignores non-string field.
  - `TestDrivesStaleness` (7 tests): None when absent or malformed, integer-day delta when present, threshold boundary at exactly N days (not stale), beyond N (stale), False when field absent.
  - `TestDrivesInDangerZone` (6 tests): empty, skip without field, include at-or-beyond threshold, exclude below threshold, exclude named drive, sort age-desc.
  - `TestSetupTemplateDiff` (5 tests): no-diff when matches, diff with old values, missing section, expected constants match generator, _print_diff includes template section.
  - `TestBackupStalenessReporting` (5 tests): silent when retention unknown, WARN when expired at start, no warn when fresh, lists other drives in danger zone, excludes just-backed-up drive.
  - `TestSanoidRetention` (6 tests): per-template horizon math, returns largest used template, smaller-when-only-minimal-used, None when file missing, None when no managed sections, ignores zvol sections without use_template.
  - `TestRepairDivergentHints` (3 tests), `TestRepairDivergentDoubleConfirm` (6), `TestRepairDivergentActionPrompt` (7), `TestRepairDivergentLoop` (7), `TestRepairDivergentSnapshotHelpers` (4): full coverage of interactive flow.

[1.0.9]: #109--2026-05-12

## [1.0.8] - 2026-05-08

### Out-of-space handling - backup

Two complementary safeguards for `zark backup`. The preventive guard avoids burning operator time on a backup we can prove will fail; the reactive handler catches the cases the preventive guard misses (the backup grew during the window, the threshold was tuned wrong, etc.) and turns them into clean fatal errors instead of silent fall-through.

- **Preventive ENOSPC guard.** Before kicking off `syncoid`, backup now compares `target.avail_bytes` against a threshold of `max(source.used_bytes / 100, 1 GiB)`. Below that, fatal with cause/solution lines (purge or larger drive). The 1% / 1 GiB choice is deliberately lax: it only fires on a target that is essentially full, where any incremental is mathematically guaranteed to ENOSPC mid-stream. A separate coherence check warns (does not fatal) when the target pool is physically smaller than the source pool - eventually fatal but the operator may know what they're doing (testing on a small loop, compression headroom, etc.).
- **Reactive ENOSPC detection in three call sites.** Each `syncoid` invocation in `backup` (rpool, bpool incremental, bpool from-scratch resync after `no snapshots matching`) now checks the captured stdout/stderr for ENOSPC markers and emits a fatal with ENOSPC-specific cause/solution lines. The order matters: the rpool check runs **before** the existing "syncoid had warnings but transferred N datasets" branch, otherwise an ENOSPC after the first dataset fell through as "partial success" and continued into the bpool sync, which would also ENOSPC, producing a confusing chain of generic warnings instead of one clear fatal at the source. bpool ENOSPC deliberately does **not** auto-recreate (the previous `no snapshots matching` path destroys the target bpool and re-sends from scratch); destroying a stale bpool when the drive is full would leave the user with no boot backup at all.

### Out-of-space handling - recover

Mirrors the backup story but adapted to recovery's invariants (target NVMe is wiped and re-partitioned, capacity comparison is against a physical disk rather than a pool).

- **Preventive disk-size check.** Before the `YES` confirmation prompt, `recover` measures the target disk via `lsblk -bdn -o SIZE` and compares against `backup.rpool.used_bytes × 1.05` (5% overhead margin for ESP, bpool, swap, ZFS metadata). Below that, fatal - the operator is not allowed to commit to wiping a disk we already know is too small. Silently deferred to the reactive handler if either size measurement returns 0 (transient `lsblk` or `zfs list` failure), so a probe glitch doesn't block a recovery that would otherwise succeed.
- **Reactive ENOSPC detection at every send/receive site.** `_raw_send` (every dataset except keystore), `_bpool_send_recv` (bpool send/receive), and the keystore final send each check stderr/stdout for ENOSPC markers and fatal with a recovery-specific message. Unlike backup, recover has no "stale data is better than nothing" tradeoff - partial recovery is unbootable, so every ENOSPC is fatal regardless of which dataset hit it. The `_raw_send` path preserves its non-ENOSPC `return False` for callers that intentionally tolerate individual dataset failures (`_ = _raw_send(...)` at the rpool/ROOT/USERDATA loops).

### Process plumbing

- **`lib.sh.run_pipe()` now reports a non-zero return code if either side of the pipeline failed.** The previous implementation returned only `p2.returncode`, which silently hid `p1` failures: `zfs send` of a deleted snapshot followed by a clean EOF on `zfs receive` was reported as success, the receiving dataset was incomplete, and downstream code proceeded as if the transfer had worked. The fix waits explicitly for `p1`, captures both stderr streams, concatenates them so callers (and `is_enospc()`) see the combined error output, and picks the most informative non-zero return code (`p2` takes precedence when both fail, since downstream errors are usually what the caller cares about). The specific case of ENOSPC during `zfs send | zfs receive` was already detected correctly even before this change - `zfs receive` itself emits ENOSPC and exits non-zero - so this is an improvement for the *other* failure modes that were hidden, not a fix to ENOSPC handling.
- **New `lib.sh.is_enospc(text)` helper** with case-insensitive match against four real-world markers observed on Ubuntu 24.04 / 25.04 / 25.10: `"no space left on device"`, `"out of space"`, `"enospc"`, `"disk quota exceeded"`. The last is technically `EDQUOT`, not `ENOSPC`, but the user-visible remediation is the same and treating it as ENOSPC avoids falling through to a generic "syncoid failed" message on quota-bound storage.
- **New `lib.sh.humanize_bytes(n)` helper** for IEC-formatted sizes (`512B`, `4.0M`, `1.9T`). Used in ENOSPC fatal messages so the operator sees both the human size from `zfs list` and the numeric byte count from the threshold computation in coherent units.
- **New `lib.zfs.dataset_used_bytes(name)`** wrapping `zfs list -H -p -o used`. Numeric companion to the existing `PoolInfo.size_bytes` (also new this release), used by recover's preventive guard to compare against the physical target disk in bytes.

### Backup - fresh snapshots before every replication

- **`zark backup` now invokes `sanoid --take-snapshots` automatically before running `syncoid`.** Previously, backup relied entirely on the `sanoid` systemd timer to populate the source pool with snapshots - typically hourly. That meant an interactive `zark backup` could be replicating state that is up to ~1 hour old. Since taking a snapshot is cheap (seconds, no I/O on the backup drive, idempotent within sanoid's retention windows), backup now takes a fresh pass itself before each replication. Result: the backup drive always holds the most current state of the source pool.
- **`--no-snapshot` flag** as escape hatch. Skips the sanoid stage and replicates whatever snapshots already exist. Realistic uses are narrow (re-running after a transient failure when fresh snapshots aren't wanted, or for scripts that have already triggered sanoid by other means) but it lets explicit-intent invocations declare their intent in one place.
- **A failure of `sanoid --take-snapshots` is non-fatal.** Sanoid sometimes emits warnings about a few datasets while still snapshotting the rest, and we'd rather proceed with backup (replicating slightly-stale state if necessary) than abort because of a noisy edge case. The operator sees the warning either way.
- **Backup is now a 9-step process** (was 8). The new step 6 ("Taking fresh snapshots before backup") sits between "Loading encryption key" and "Running syncoid". When `--no-snapshot` is passed, the step still appears in the log with an `--no-snapshot: replicating existing snapshots` line, so the step counter stays consistent.

### Simulate

- **GL acceleration auto-detect.** `zark simulate` now probes the host for `/dev/dri/renderD*` and for `virtio-vga-gl` in `qemu-system-x86_64 -device help`. When both are present, QEMU is launched with `-device virtio-vga-gl,edid=on,xres=W,yres=H -display gtk,gl=on`, giving the guest a real virtio-gpu DRM device, a synthetic EDID advertising the requested resolution, and an OpenGL-scaled GTK framebuffer that resizes with the window. When either probe fails, falls back to `virtio-vga + edid + plain gtk` with a warning that names the missing capability. Auto-detection (no flag) was chosen over an explicit `--gl` opt-in to keep the CLI uncluttered: there is no legitimate use case for the slower path when GL is available.
- **`--display WxH` flag.** Lets the operator override the default 2560×1440 EDID-advertised resolution. Validated with positive bounds checks (≤8K) and a `WxH` parse with both lower- and upper-case `x`. Combinable with the existing `disk` and `--rw` arguments in any order. Default tuned for the typical zark-on-Ubuntu hardware (4K-class developer machine); on a 1366×768 laptop the QEMU GTK frontend scales/scrolls the window so it doesn't overflow.

### Tests

- **214 unit tests**, ruff clean, pylint 10.00/10. New coverage:
  - `TestShRunPipe` (5 tests): both-succeed, p2-fail, p1-fail-with-clean-p2, stderr combination, p2-precedence-when-both-fail.
  - `TestShIsEnospc` (7 tests): empty input, unrelated error, each of the four markers individually, mixed stderr from a piped invocation.
  - `TestShHumanizeBytes` (8 tests): zero, raw bytes (no decimal), KiB boundary at 1024, KiB/MiB/GiB/TiB ranges, negative values.
  - `TestZfsDatasetUsedBytes` (3 tests): ok, lsblk-style failure, unparseable output.
  - `TestBackupCheckTargetSpace` (8 tests): plenty-of-space, below 1 GiB floor, below 1% of source, just above threshold, smaller-target warn, three "silent skip" paths (None src/dst, zero used_bytes).
  - `TestBackupParseArgs` (3 tests): default takes snapshots, `--no-snapshot` disables, unknown flags ignored.
  - `TestRecoverDiskSize` (9 tests): three for `_disk_size_bytes`, six for `_check_target_disk_size` covering pass/fatal/silent/boundary just-above and just-below.
  - `TestSimulateArgs` extended for `--display`: bad format, zero dimension, exceeds 8K, default when flag absent, combined with disk + --rw, both `x` and `X` separators.
- **New `make test-enospc-real` target** (`tests/test_enospc_real.sh`). Empirical calibration of `lib.sh._ENOSPC_MARKERS` against the actual ZFS / kernel installed on the machine: creates two PID-tagged loop-backed pools with `cachefile=none`, provokes a real out-of-space `zfs send | zfs receive`, captures stderr, and verifies that `is_enospc()` would catch it. Opt-in (root-only, not invoked by `make fulltest`, not in CI). Run after upgrading `zfsutils-linux` or moving to a new distro / kernel.

[1.0.8]: #108--2026-05-08

## [1.0.7] - 2026-05-06

### Backup model

- **Removed `--use-hold` from all syncoid invocations.** Earlier iterations experimented with ZFS holds to keep the syncoid anchor snapshot alive across long backup gaps, but holds retain blocks on the *source* pool until manually released - meaning a forgotten backup drive could silently keep tens of GB unreclaimable on `rpool` for months. The new model is simpler and honest: source retention is governed entirely by sanoid policy; the destination accumulates whatever it receives. If a drive sits offline longer than the source policy retains (~3 months on `production`), the next backup auto-resolves the resulting divergence.
- **`zark backup` now auto-repairs divergent datasets under 64 MB silently.** When syncoid aborts with `Cowardly refusing to destroy your existing target`, backup invokes the same logic as `repair-divergent` for any dataset under 64 MB (the threshold syncoid itself uses to flag "did you mistakenly run zfs create on the target?") and re-runs syncoid. Anything larger aborts with a clear pointer to interactive `zark repair-divergent` - no user data ever destroyed silently.
- **`bpool/BOOT` snapshot policy moved from `minimal` (2 days) to `production` (~3 months).** Kernel and initramfs history deserves the same rollback horizon as user data; ZFS deduplicates the extra hourly snapshots since `/boot` doesn't change between hours.

### Secure Boot

- **`zark recover` now pins the `.latest` signed shim/grub variant before reinstalling.** Subiquity has been observed leaving Ubuntu installations pointing at the older `.signed.previous` shim (15.4-0ubuntu9), which gets revoked by the next SBAT level update from `fwupd`, leaving the system unbootable with `Verifying shim SBAT data failed: Security Policy Violation`. Recover now switches the alternative to `.latest` before `dpkg-reconfigure`, so the recovered system uses the up-to-date binary regardless of what the original installation chose. No-op on releases that don't ship the `.latest`/`.previous` split.
- **`zark setup` adds a Secure Boot pre-check (step 5).** Inspects the live system's alternatives for both shim and grub signed binaries; if either points to anything other than `.latest`, prompts (default NO - boot chain changes warrant explicit consent) before switching and re-running the postinst. Helps the user fix the subiquity bug before it propagates to backups.
- **bpool features pruned to GRUB-compatible subset.** Earlier versions of zark activated `head_errlog` and `vdev_zaps_v2` on bpool for "Ubuntu 25.04+ dracut systems," based on the assumption that dracut implied a more capable GRUB. This was wrong: GRUB2's bundled ZFS reader (used during boot to load the kernel) does not support either feature even in GRUB 2.14, the version shipped in Ubuntu 26.04. With those features active, GRUB rejects bpool as unreadable and boot fails with `file '/BOOT/.../vmlinuz-...' not found` followed by `prohibited by secure boot policy` for every menu entry. Existing recovered systems hit by this need a fresh `zark recover` with v1.0.7+ (features cannot be disabled once active). The recover code now activates only features documented in `/usr/share/zfs/compatibility.d/grub2` on bpool. rpool is unaffected - it's read by the full ZFS kernel module and continues to use whatever features the running ZFS supports.
- **`fix_grub_bpool_uuid` now rewrites every `--fs-uuid` reference, including those with `--hint-*` options.** The previous regex required only whitespace between `--set=<name>` and the 16-hex UUID, which silently skipped Ubuntu's standard menu-entry lines (`search --fs-uuid --set=root --hint-bios=... --hint-efi=... --hint-baremetal=... <UUID>` - the only ones that actually run on grub 2.12+ when `feature_platform_search_hint` is set). The result was that the `if`-branch of every menu entry kept the source machine's stale UUID after recovery, while only the `else`-branch got rewritten. This bug was invisible whenever the recovered disk happened to match the source's BIOS index (e.g. always recovering the same physical machine), but produced `error: no such device: <old-uuid>` followed by `disk 'hdN,gptN' not found` and `you need to load the kernel first` on cross-host recovery (different machine, different drive enumeration). The new regex is permissive about what may appear between `--fs-uuid` and the UUID.
- **README troubleshooting section** documents two recurring scenarios users may hit: (1) the harmless "System program problem detected" Apport popup that appears during disk-intensive operations on the live USB (caused by unrelated GNOME/udev daemons reacting to rapid disk activity), and (2) the SBAT rescue procedure for users hit by the shim revocation issue with a pre-1.0.7 recover (boot live USB → temporarily disable Secure Boot → switch alternatives → reboot with Secure Boot back on).

### Refactor

- Moved divergence detection (`find_divergent`, `auto_repair_under_64mb`, `is_divergence_error`) from `commands/repair_divergent.py` to a new `lib/repair.py`, shared between `backup` (silent path) and `repair-divergent` (interactive path).
- Removed `lib.zfs.release_all_holds` and `lib.zfs.has_syncoid_holds` (no longer needed; both were broken anyway because `zfs holds -r <dataset>` only accepts snapshots).
- Renamed syncoid flag `--exclude` to `--exclude-datasets` *automatically* per installed syncoid version. Initially we hard-coded `--exclude-datasets` in test15, assuming Ubuntu's `2.2.0-2` package backported the rename. It didn't: only sanoid `2.3.0` upstream knows `--exclude-datasets`, and on Ubuntu that version is 26.04+ only. On 22.04, 24.04, 25.04, and 25.10 (all sanoid `2.1.0` or `2.2.0-2`), `--exclude-datasets` aborted syncoid with "Unknown option" and left the destination pool with keystore + bpool but **no rpool** - a silently broken backup. The new `syncoid_exclude_flag()` helper inspects `syncoid --help` at runtime and picks `--exclude-datasets` (2.3+) or `--exclude` (2.2-) accordingly. Both `prepare` and `backup` use it.

### Tests

- 147 unit tests, ruff clean, pylint 10.00/10.

### Breaking change

- Pools backed up with v1.0.6 or earlier may have leftover `syncoid_host_*` snapshots in source `rpool` and `bpool` (with no useful function). Cleanup is optional and one-shot:
  ```
  sudo zfs list -H -o name -t snapshot -r rpool | grep '@syncoid_host_' | xargs -r -n1 sudo zfs destroy
  sudo zfs list -H -o name -t snapshot -r bpool | grep '@syncoid_host_' | xargs -r -n1 sudo zfs destroy
  ```

[1.0.7]: #107--2026-05-06

## [1.0.6] - 2026-05-03

### Distribution

- Added a `zark(1)` manpage. The source lives at `debian/zark.1.md` (Markdown) and is converted to roff at build time via `pandoc + sed + awk` in `debian/rules` - the same pipeline is exposed as `make manpage` / `make manpage-view` / `make manpage-clean` for local iteration. Output passes `mandoc -Tlint` and `groff -ww -z` cleanly.
- Fixed `dpkg-source: error: aborting due to unexpected upstream changes` triggered when local tool caches (`.mypy_cache/`, `.ruff_cache/`, `.tox/`, ...) lived in the working tree but not in the `.orig.tar.gz`. Added `debian/source/options` with `tar-ignore` + `extend-diff-ignore` covering all such paths; kept in sync with the `find ... -delete` invocations in `make dist`.
- Fixed `lintian W: no-debian-changes`: `make dist` was bundling `debian/` inside the `.orig.tar.gz`, violating `3.0 (quilt)`'s upstream/packaging split. The dist target now strips `debian/` when assembling the upstream slice; the resulting tarball is upstream-pure (≈118 KB, down from ≈130 KB).
- Made `lib/config.py` (`VERSION`) the single source of truth for the version string everywhere it can be substituted at build time. The manpage source carries `@VERSION@` in its header, replaced by both `make manpage` and `debian/rules` before pandoc runs (substituting *after* would have to match pandoc's `\[at]` escape - fragile). The `bug_report.yml` issue template placeholder and the version examples in `debian/README.packaging.md` were moved to a generic `X.Y.Z` form so they no longer rot. Net effect: bumping a release now means editing `lib/config.py` + the changelogs only.

[1.0.6]: #106--2026-05-03

## [1.0.5] - 2026-05-01

### Security

- `recover` now aborts when the backup is missing its keystore zvol, instead of silently producing a system that boots into an emergency shell.
- The two unsafe fallbacks (embedding the raw key in the rootfs, switching `rpool` to `keylocation=prompt`) were considered and rejected for silently degrading the security model the user set up.

### Tooling

- Added `tox.ini` (py3.12/3.13/3.14 + lint env), with mypy/pylint/ruff versions declared in `pyproject.toml` under `[dependency-groups].dev` (PEP 735) as a single source of truth.
- Added Ruff's `RUF027` rule to pre-commit to catch stripped `f`-string prefixes - the v1.0.4 root cause.
- Added GitHub Actions CI: tests matrix (py3.12/3.13/3.14, fail-fast off) and a lint job; integration tests (QEMU) deliberately remain local.

### Distribution

- Added Debian/Ubuntu packaging (`debian/`, `3.0 (quilt)`) installing under `/usr/share/zark/` with `/usr/bin/zark` as a symlink - `lib/config.py`'s existing path search handles the layout without code changes.
- Added `make deb` / `make deb-source` / `make deb-ppa` driving `debuild`, with PPA uploads iterating over Ubuntu 24.04 → 26.04; see `debian/README.packaging.md` for the maintainer flow.
- Added a CI `package` job gated on `refs/tags/v*` that builds an unsigned `.deb` and attaches it to the GitHub Release; signed PPA upload stays local.

[1.0.5]: #105--2026-05-01

## [1.0.4] - 2026-04-30

Initial public release of zark under its current name.

zark is a portable Python-based bare-metal ZFS backup and recovery suite
for Ubuntu systems with full-disk encryption, UEFI Secure Boot, and
zero-installation portability (runs from a USB pendrive).

For the design philosophy, command set, and supported environments, see
the project README.

[1.0.4]: #104--2026-04-30
