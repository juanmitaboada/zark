# Drive rotation and retention

How zark anchors each backup drive, what each drive keeps, how space is reclaimed, and the status zark reports.

## Drive rotation and retention policy

zark supports rotating multiple backup drives - one at home, one off-site, an archival copy in a desk drawer - with no limit on how long a drive stays disconnected.

### Anchors

After each backup, every `rpool` dataset gets a bookmark `#zark_<pool GUID>_<UTC>` for that drive (a few hundred bytes, holding no data and untouched by sanoid's pruning). The next backup to that drive starts from it even if the source no longer has any snapshot in common with the drive. `bpool` never gets bookmarks (GRUB must keep reading it); there zark keeps one snapshot per drive instead. Between backups nothing else of zark's stays in the source, except the point of a backup that was interrupted, kept only on the datasets whose partial transfer needs it to resume, until the next complete backup or prepare removes it. However a run ends, the newest snapshot the drive holds of every `rpool` dataset keeps a bookmark of that drive, and every backup point names its drive (`org.zark:point-for`), so whatever removes a point left in the source anchors it for that drive first (a point that names no drive, restored by a recover or left by an early 2.0.0 development build, is removed without an anchor). Before anything is removed in the source, zark re-reads the drive and checks that the dataset is there at the new point and anchored. `zark registry forget <name>` removes a lost drive's anchors; `zark purge` and `zark prepare` remove those of a drive they wipe or replace, and `zark registry list` shows any drive GUID that still holds anchors without an entry, which `zark registry forget <GUID>` removes.

### Retention windows

`zark setup` writes two sanoid templates to `/etc/sanoid/sanoid.conf` for the source's own history:

| Template | Datasets | Retention |
|---|---|---|
| `template_production` | `rpool/ROOT/<ubuntu>`, `rpool/USERDATA`, `bpool/BOOT` | hourly=24, daily=7, weekly=4, monthly=3 |
| `template_minimal` | `rpool`, `rpool/ROOT`, `rpool/var`, `bpool`, anything new | daily=14, weekly=8, monthly=3 |

The sanoid snapshots taken between two backups travel to the drive with the next one, as extra restore points. Drives written by zark 1.x are anchored only on shared snapshots: the first zark 2 backup to such a drive asks once how to continue when the drive holds snapshots newer than anything the source still has (abort, archive and resend, or roll back), and from then on the drive is bookmark-anchored. A drive with no anchor in origin (a zark 1.x drive before that run, or any drive after a recover, which does not restore bookmarks) can only continue from snapshots it shares with the source. After every complete backup zark records which ones the drive shares, and `zark status` and backup's report check them against the source: they say until about when the source keeps them (sanoid prunes a snapshot only when it is older than its bucket's count of periods and more than that count of its type exist), or that the next backup resends some datasets in full. A zark 2 drive's newest snapshot is its backup point, which the source keeps only as an anchor, so continuing from a shared snapshot means leaving the newer points behind: the next backup asks to keep them in archived lineages and continue from the newest shared snapshot (nothing destroyed and nothing resent), or to roll each dataset back to it (the drive's backup points after it go). `recover` starts each dataset from its newest monthly sanoid snapshot before the chosen point when it fits, so the recovered system shares that snapshot with every drive in rotation. A drive with no such record yet gets the guaranteed horizon as a condition: if it still shares snapshots with the source, its next backup can continue from them for at least 62 days after its last backup with these templates (incrementally on a 1.x drive, after a rollback on a zark 2 one). When the source cannot be read, both say so in one line instead.

### Retention on the drive

Each drive has its own retention, independent of sanoid's in the source. After every complete backup:

- every snapshot inside the window (30 days by default) stays;
- outside it, the newest complete backup point of each calendar month stays, with no limit in time; everything else outside the window goes;
- with a maximum age set (off by default), nothing older stays;
- the newest complete backup point and the newest snapshot of every dataset other than an archived lineage or a kept orphan (the base of its next incremental) always stay, and so do clone origins, held snapshots, snapshots with other names and history from before the drive's first zark 2 backup point (recorded on the drive as `org.zark:first-point`);
- a month's point is the newest one no dataset skipped since (a rollback or a hand destroy can leave a point partial); with none left, its newest point whose run was complete, so a month never loses every point;
- an archived lineage or kept orphan left with no snapshot, by the policy, by the space check or by `zark prune`, is destroyed whole, named first.

When a backup would leave the drive with less free space than its margin (10 % of its size by default), the policy's removals go first, then zark lists the oldest monthly points with what each frees and removes them only if you say yes. If only the transfer itself fits, the backup still runs and tells you the drive is too small to keep the margin. Snapshots are removed by their exact names, grouped per dataset (at most 100 per `zfs destroy`, retried one by one when ZFS refuses the group), each logged; a backup point that loses part of its snapshots is shown as incomplete by `recover`.

### Reclaiming space and alerts

`zark prune` shows, for the source or for a connected drive, what each kind of snapshot holds and what removing it would free, and removes what you choose: sanoid snapshots older than N days, the `syncoid_*` and keystore `prepare_*` snapshots zark 1.x left behind (a `syncoid_*` one stays while a registered drive may still depend on it), backup points left in the source by interrupted backups or by a run whose anchor failed (each is anchored for the drive it names before it goes; one restored by a recover or left by an early 2.0.0 development build names none, and removing it costs the drive that depends on it a rollback at its next backup, which asks first), a drive's `bpool` anchors (that drive's next backup then asks to roll those datasets back to a shared sanoid snapshot or to resend them in full), and on a drive its oldest monthly points or the history from before its first backup point (whether zark 1.x wrote it or a dataset arrived with older snapshots). The backups impose no floor in the source: their anchors on `rpool` are bookmarks.

`zark setup` installs a login message and an hourly timer (`zark alerts remove` takes them away): the message says when `rpool` or `bpool` is 80 % full or more (90 % is critical, and a desktop notification is sent once a day), how many days have passed since each drive's last backup, and what `zark prune` could remove. `zark status` shows the same at any time.

### Status files for other programs

zark is not a daemon, so it writes what it is doing and how the backups stand to two JSON files anyone can read: `/run/zark/progress.json` while a command that changes state runs (its step, whether it waits for an answer, and for a backup the drive, the point and the bytes sent with their rate), and `/var/lib/zark/status.json` between runs (the pools' space and datasets, each drive's last backup and last run, the login message's lines). They are written by the running command itself, at the end of every command that changes state, and hourly by the alerts timer. Their format, schema version 1, what a reader may rely on and how to read them safely: [`docs/STATUS-FILES.md`](STATUS-FILES.md).

### Drive staleness reporting

`zark backup` records `last_backup_at` in `etc/known_drives.json` after every complete, verified run, and the drive's root dataset records the zark version, the source host, its machine-id and the last point (`org.zark:*`). A drive whose machine-id is not this system's is refused before anything is touched. A drive with no machine-id recorded (written by zark 1.x or an early 2.0.0 development build) is accepted only when it shares something with this system, a snapshot or bookmark to continue from, and that run records the owner; one with nothing in common (blank, or every anchor in the source gone) is refused, and `purge` plus `prepare` make it this system's. After a backup it lists how many days have passed since every other drive's last backup, and for a drive with no anchor in the source what it still shares and what its next backup will ask (a rollback to the newest shared snapshot, or a full resend).

### Divergence and datasets only on the drive

A dataset with nothing in common with the drive (recreated in the source, or its bookmark destroyed by hand) is asked about before the transfer: skip it this time, archive the drive's copy as `<name>.archived-YYYYMMDD` and send it again, or destroy it and send it again (typed `DESTROY`; a word that does not match asks the question again). Several of them are asked about together first (skip, archive or destroy them all, or decide one by one). A dataset in the source whose name contains `.archived-` is not backed up (the name is reserved) and is listed with the reason. A dataset that exists only on the drive can be kept (not asked again until it reappears in the source), destroyed, or renamed when the source renamed it. `recover` restores old points from archived copies too.

### Excluded datasets

`zark exclude add <dataset> backups|snapshots|both` leaves a dataset and everything below it out of the backups, out of sanoid's snapshots, or both; `zark exclude list` shows what is excluded and `zark exclude remove` undoes it. The exclusion is a ZFS user property on the dataset in the source (`org.zark:replicate=no`, `org.zark:snapshot=no`), so it moves with the dataset. Boot environments, the boot pool, the keystore and the structural containers can never be excluded. A backup lists what it left out with its size and records it on the drive; the first backup to a drive that still holds a copy asks once whether to keep it (the default) or destroy it, and `zark prune` offers the destroy later. Included again, a dataset continues incrementally from the copy a drive kept. `recover` recreates the top of each subtree the restored point left out as an empty dataset with its mountpoint and the exclusion, so the system boots with its paths in place; what was below it is not created, since it is the application's own layout (docker's datasets, for one). A directory has to become a dataset first, by hand (the manpage's `exclude` entry has the steps).
