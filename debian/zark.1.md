% ZARK(1) zark @VERSION@ | User Commands
%
% May 12, 2026

# NAME

zark - full bare-metal ZFS backup and recovery for Ubuntu with encrypted root

# SYNOPSIS

**zark** \[*--help* | *-h* | **help**\]

**zark** \[*--version* | *-v*\]

**zark** *command* \[*options*\]

# DESCRIPTION

**zark** is a portable Python suite that performs bare-metal backup and disaster
recovery of Ubuntu systems installed on encrypted ZFS root pools (the
**rpool** + **bpool** + LUKS-keystore-zvol layout used by the Ubuntu desktop
installer when "encrypt the new Ubuntu installation for security" is selected
together with the experimental ZFS option).

Backups are written to a dedicated external drive with raw **zfs send**
streams, so native ZFS encryption is preserved end-to-end: the backup drive
holds ciphertext only, and the original passphrase is the only thing that can
unlock it. Every backup adds one restore point to the drive and removes
none; the only thing **zark** keeps in the source pool between backups is a
bookmark per drive.

A full restore - partitioning, **rpool** + **bpool** recreation, LUKS keystore
reconstruction, GRUB and initramfs regeneration - runs from an Ubuntu Live USB
in roughly one minute and yields a system that is structurally identical to
the original installation. No custom artefacts are introduced and the
recovered system continues to receive normal **apt**(8) upgrades.

**zark** can be installed system-wide (the Debian package places it at
*/usr/share/zark/* with a */usr/bin/zark* symlink) or run in place from a USB
pendrive. Both invocation styles use exactly the same code; the only
difference is the default location of the configuration file and log.

# GLOBAL OPTIONS

**-h**, **--help**, **help**
:   Print a summary of available commands and exit. Does not require root.

**-v**, **--version**
:   Print the **zark** version and exit. Does not require root.

**--debug**
:   When supplied as an argument to any *command*, a Python traceback is
    printed if the command aborts with an unhandled exception. Without
    **--debug**, only the **\[FATAL\]** message is shown.

All other commands must be run as **root** (typically via **sudo**(8))
because every action involves ZFS pool import/export, partition table
manipulation, **cryptsetup**(8), or chrooting into a target system.

As a convenience, **zark** *command* and **zark** **--***command* are
equivalent: passing **--backup** is treated as **backup**.

# COMMANDS

The fourteen commands are grouped here by purpose. Within each group the
commands appear in the order they are typically used. See **WORKFLOWS** below
for the canonical end-to-end sequences.

## Inspection

**explore**
:   Non-destructively scan all connected block devices for ZFS pools and
    classify each one against the registry in **known_drives.json**:

    - *known* - name and pool GUID match a registered drive;
    - *GUID changed* - pool name matches but the GUID differs (typically
      after a re-prepare);
    - *renamed* - the GUID matches but the pool has been renamed;
    - *unknown* - the drive is not registered.

    For drives in the last three categories, **explore** prints the exact
    JSON snippet that should be added to **known_drives.json** to register
    them.

**monitor**
:   Live progress dashboard intended to be run in a second terminal while a
    backup is in flight. Reports pool health, whether a **backup** or
    **prepare** is running and for how long, the snapshot count on the
    drive and its newest snapshot by creation time.

**health** \[*device*\]
:   Interactive drive analysis and debugging tool. With a *device* it checks
    that device; without one it scans connected non-system drives and lets
    you pick. It first asks whether to run a *read-only* check (default) or a
    *destructive* write-and-verify test, gathers any further choices up front,
    then runs unattended except for a single pause.

    The read-only check inspects kernel/sysfs state: whether the USB-SATA
    bridge reports DPO/FUA support and whether it runs the UAS transport
    (both shown as information: most USB bridges report no FUA, and UAS is
    the normal driver), and the bridge's USB VID:PID against a
    known-problematic list, the one risk it warns about. It also reports the
    health of any pool on the drive without importing it: an imported pool
    from its error counters, an exported one from the state **zpool import
    -d** reports, with the commands that read its counters. It writes
    nothing and flags *risk* only; it cannot prove a bridge honest, which
    shows solely under load.

    The destructive test (after explicit confirmation, on a blank drive)
    creates a throwaway pool on the drive's by-id name, never written to
    */etc/zfs/zpool.cache*, and writes with transaction churn (*fast*,
    ~2 GB; *medium*, ~15 GB; or *surface*, whole disk, capped), then
    re-imports to confirm the writes persisted. An optional *cold* pass
    powers the device down, waits for you to physically reconnect it, and
    re-imports, so the read-back comes strictly from NAND rather than the
    bridge's cache. Time estimates are shown from a measured write speed. The
    throwaway pool lives under */run/zark/altroot*, so **clean** releases
    one a killed run left imported. Afterwards it is destroyed read-write
    (a read-only import is exported and imported read-write first: a
    read-only pool's destroy writes nothing to its labels), and a pool that
    could not be destroyed is reported as an error, with the command that
    lists what may remain, and in the diagnostic report. Then only the disk
    its by-id name resolves to is wiped, and only when its label is the
    test pool's or there is none. A cold pass needs a by-id name and fails
    before writing without one; after the reconnect it waits for udev and
    up to 10 seconds for the disk's by-id links, and a partition link that
    never appears is its own failure.

    Whenever a risk (a warning) or test failure is found, a self-contained diagnostic
    report is written to */tmp* (environment, drive, bridge VID:PID, findings,
    dmesg tail) with instructions for filing a GitHub issue; you may choose to
    obfuscate serials/GUIDs. See the enclosure notes in **docs/HARDWARE.md**.
    The read-only check also runs at the start of **prepare**.

**selftest** \[**run**\] \[**--stick** *device*\] \[**--only** *ID*\[,*ID*...\]\] | **status** | **report** \[*file*\] | **certificate** \[**--into** *file*\] *file*... | **warnings** **on**|**off**
:   Check that this machine's ZFS, util-linux, systemd, kernel and Python
    behave as **zark** relies on. **run** (the default) runs the level-1
    checks on pools backed by sparse files in */dev/shm*, with random names
    no other pool has, imported under */run/zark/altroot* and never written
    to */etc/zfs/zpool.cache*; no disk is touched and no system pool is
    imported, changed or exported. It needs 1 GiB free in */dev/shm* and
    1.5 GiB of available memory. With **--stick**, it then runs the level-2
    checks on that whole disk, which it erases: the disk must be external,
    unregistered, blank (**purge** wipes one) and named under
    */dev/disk/by-id*, and its kernel name must be typed to go on; it is
    left blank as **purge** leaves a drive, both ends overwritten. **--only**
    repeats some checks; such a run certifies nothing, but a check it passes
    no longer shows as failed on this machine. Each check has an ID and a
    revision (*POOL-01.1*); a FAIL says what was expected, what came and
    what in **zark** depends on it, with the commands' output in zark.log
    and never in the report. A signal stops the run like any command, its
    pools destroyed and its directory removed, and the run is recorded as
    stopped. Exit status 0 when no check failed (a check that cannot run
    here, a missing tool, is a SKIP), 1 otherwise.

    Every run writes a JSON report (*/var/lib/zark/selftest/*): the
    platform's versions and the result of each check, with no host, pool,
    disk, serial or GUID in it. **report** prints a link that opens a GitHub
    issue form with the report filled in; **zark** sends nothing itself.
    **status** shows how much of the current checklist is covered on this
    platform (same Ubuntu, kernel, ZFS, util-linux and systemd series, and
    the same installed or live session) by the certificates shipped with
    **zark** and by this machine's own runs. **warnings on** adds one line at
    the start of **setup**, **backup** and **recover**, and to the MOTD,
    when no certificate covers this platform, when a check failed here, or
    when the loaded ZFS module and its tools are different releases;
    **warnings off** (the default) removes them. **certificate** turns
    reports into entries of the certificates file (for the maintainer).

## Backup workflow

**setup**
:   One-time installation of the runtime dependencies and configuration of
    **sanoid**(8) snapshot policies. Installs the packages listed in the
    **DEPENDENCIES** section, drops a **sanoid** template tuned for
    Ubuntu-on-ZFS into */etc/sanoid/*, and registers the **sanoid** systemd
    timer so snapshots are taken automatically. Also installs the apt guard
    and the update-grub guard (see **NOTES**; a copy that differs is
    replaced, a current one left alone) on the running system, so a background kernel or GRUB
    upgrade cannot half-apply while a backup drive is connected, and the
    space and backup-age alerts (see **alerts**).

    **setup** checks that **rpool** has **feature\@bookmark_v2** enabled:
    **backup** anchors each drive on a bookmark, and a raw incremental send
    from a bookmark needs it. When it is disabled, **setup** explains that
    enabling it cannot be undone (GRUB never reads **rpool**) and asks; a
    pool whose *compatibility* property does not list the feature is left
    as it is. **backup** and **prepare** refuse to run without it.

**prepare** \[*device*\]
:   Initialise a brand-new external drive as a **zark** backup target. The
    drive may be specified as a positional argument (for example
    */dev/disk/by-id/usb-Vendor_Model_Serial-0:0*); if omitted, **zark**
    presents an interactive list of unprepared drives.

    Every question (risk factors, stale registry entries, pool name,
    auto-eject, the drive's retention window and maximum age; see
    **Retention** under **backup**) is asked before anything is written. **prepare** then
    creates an unencrypted ZFS pool on the drive (encryption is provided by
    the raw send from the encrypted source pool - adding a second layer
    here would just hide the failure of the inner one) on its by-id name
    with an alternate root, so it is never written to
    */etc/zfs/zpool.cache*, sends one backup point of **rpool** and
    **bpool** (the point only, not the source's snapshot history) the way
    **backup** does, copies over the **rpool/keystore** zvol, and records
    the drive's metadata (see **backup**). A drive without a by-id name is
    refused rather than registered as *\<unknown\>*; registry entries left
    for the same drive are offered for replacement.

    The pool is created with **compatibility=openzfs-2.2**, so that a live
    session with OpenZFS 2.2 (Ubuntu 24.04) or later can read the drive and
    no feature newer than that ever activates on it; without that feature
    set installed, or when the source pools use **longname** or
    **large_microzap** (which zark's streams would carry), it is created
    with every feature, and **prepare** says so.

    Before doing any work, **prepare** runs the same non-destructive risk
    check as **health** and, when the bridge is on the known-problematic
    list, warns and asks for confirmation. After the transfer it performs a read-back verification
    (identical to **backup**'s): because the initial raw send is a full
    write-under-load, a successful re-import here proves the bridge survives
    real load. The drive is registered, with *last_backup_at* set, only
    when every dataset and the keystore landed and the read-back is
    **ONLINE**; otherwise it is **not** registered and **prepare** exits 1.

**backup** \[**--no-snapshot**\]
:   Back up this system to the connected, registered backup drive.
    Auto-detects which known drive is plugged in and imports it by its
    exact device with **-N** and an alternate root, so nothing it holds is
    mounted and it never enters */etc/zfs/zpool.cache*. A drive whose
    metadata names another system (**org.zark:origin-machine-id** differs
    from */etc/machine-id*) is refused right after it is imported, before
    the health check, the passphrase and any question; nothing on it is
    changed (the import and the export stay in the pool's history), and the
    message names the drive's host and last date. A drive whose metadata
    cannot be read is refused the same way. A drive without that property
    (written by **zark** 2.0.0-rc2 or earlier, or not by **zark**) is
    accepted only when at least one dataset has something in common with
    this system (a snapshot or bookmark to continue from, a rollback, a
    transfer to resume); this is read before any key, question or change,
    and the run that accepts it records this system as its owner. With
    nothing in common it is refused; **purge** and **prepare** make it this
    system's drive.

    Before the transfer, **backup** checks the health of the source pool
    and of the drive. A pool that is DEGRADED, or ONLINE with non-zero
    read, write or checksum error counters, is captured (see
    **zark-forensics** under **FILES**), its counters and first permanent
    errors are shown, and the run continues only on a yes (default no).
    Any other state stops the run.

    **backup** and **prepare** refuse to run while a pool named **bpool**
    can be imported (**zpool import** lists it) but is not. The refusal
    lists each such **bpool** with its id and devices: the one on
    **rpool**'s disk is this system's and is imported by id (**sudo zpool
    import** *id*); one on any other disk must not be imported, since it
    would mount over */boot*: disconnect that disk. Exporting **bpool** on a
    running system is never the way out: service mount namespaces hold
    copies of */boot*, so the export fails as busy after unmounting the
    host's */boot*. **backup**
    stops before any question when the drive holds a **bpool** backup and
    the source's **bpool** is not imported: a backup without it would leave
    the kernels out.

    **Backup point.** **backup** pauses the **sanoid** timer (waiting, up
    to 10 minutes and saying so, for a running snapshot or prune pass; a
    timer that an earlier **zark** run stopped and could not restart is
    started again at the end), takes one snapshot
    **zark_YYYY-MM-DD_HH:MM:SSZ** (UTC) of every replicated dataset - one
    **zfs snapshot** per pool, so all of **rpool** is captured in the same
    transaction group - and brings every dataset on the drive to it with
    **zfs send** \| **zfs receive**. The base of each transfer is the
    drive's newest snapshot of that dataset: when the source still has it,
    the transfer is **-I** and carries the **sanoid** snapshots taken since
    (they become extra restore points on the drive); when the source no
    longer has it, this drive's bookmark of it is used instead. Receives
    are resumable (**-s**) and never forced (**-F**), so no snapshot on the
    drive is destroyed by a transfer. Afterwards the dataset gets a new
    bookmark **\#zark\_\<pool GUID\>\_\<UTC\>** (on **bpool**, which never
    gets bookmarks, the point is kept as a snapshot of that name instead),
    the drive's older anchors go, the point is destroyed in the source and
    the timer is resumed. Each removal in the source is checked first
    against the drive: a point or an old anchor goes only when the dataset
    is verified on the drive at the new point and anchored, and every
    decision is logged with its reason. The structural containers
    (**rpool**, **bpool**, **rpool/ROOT**, **rpool/USERDATA**,
    **bpool/BOOT**) are not replicated, since **recover** creates them anew:
    **backup** creates them on the drive when missing, and lists one that
    holds data or can mount as not backed up. Datasets on the drive are
    received with **canmount=noauto** and, when new, no mountpoint of their
    own (a mountpoint an older **zark** set on the drive stays, but cannot
    mount anything with **canmount=noauto**); the source's values
    are recorded as **org.zark:canmount** and **org.zark:mountpoint** and
    used by **recover** and **mount**. Volumes other than the keystore are
    not backed up and are listed as such.

    **Questions.** Everything is asked before the first byte is sent, and
    nothing on the drive is destroyed unless chosen here (typed
    confirmations for destroys):

    - a dataset that exists only on the drive: ask again next time (the
      default), keep it (not asked again), destroy it, or - when the
      source renamed it - rename it on the drive too;
    - datasets whose only snapshot in common with the source is older than
      the drive's newest (the first run on a drive written by an older
      **zark**, or a source recovered from an older point): one prompt
      listing the snapshots at stake (three per dataset on the terminal,
      every one in the log), to abort (the default), keep the drive's newer
      snapshots and continue from the common one (offered when the
      transfer fits), or roll them back. Keeping them renames each dataset
      to an archived lineage, puts a clone of its common snapshot in its
      place and promotes it, and moves its children back under it: nothing
      is destroyed or sent again in full, and the archive holds only the
      newer snapshots;
    - a dataset with nothing in common at all: skip it this time (the
      default), archive it and send it again in full, or destroy it and
      send it again. Several such datasets are asked about together first:
      skip them all (the default), archive them all, destroy them all, or
      decide one by one;
    - datasets written on the drive since their newest snapshot (a
      read-write **mount** does this to every dataset it mounts, even with
      nothing written): one prompt listing them, to skip them this time
      (the default; the backup is then incomplete) or discard those
      changes, rolling each one back to its newest snapshot without
      destroying any (typed **DISCARD**). A dataset that also holds an
      interrupted transfer is listed there too: it could never resume onto
      a written dataset, so discarding also drops that partial transfer
      (**zfs receive -A**) before the rollback, and the dataset is sent
      again in the same run.

    An archived dataset is renamed to *\<name\>.archived-YYYYMMDD* on the
    drive and keeps all its snapshots; archiving or destroying a dataset
    takes the datasets below it along, which are not asked about and are
    sent again in full (keeping a rolled-ahead dataset's newer snapshots
    does not: its children stay). Before the typed **DESTROY**, every dataset the
    destroy takes on the drive is listed, archived lineages and datasets
    kept as only on the drive included.

    **Space.** The estimated transfer, the full sends above included, is
    checked against the drive's available space before any choice is
    applied. When the drive would end with less free space than its margin
    (10 % of its size by default), what its retention removes anyway goes
    first, without a question; if that is not enough, the oldest monthly
    backup points are listed with what each frees and removed only on a
    yes (the default is no). The margin is a goal: when only the transfer
    itself fits, the backup goes on and says so; it stops only when the
    transfer does not fit.

    **Retention.** After every complete backup the drive's retention is
    applied and each removal is listed and logged: every snapshot inside
    the window (30 days by default) stays; outside it, only the newest
    complete backup point of each calendar month (UTC), with no limit in
    time; with a maximum age set (off by default), nothing older. The
    newest complete point and the newest snapshot of every dataset other
    than an archived lineage or one kept as only on the drive (the base of
    its next incremental) always stay, and so do a clone's origin, a held
    snapshot, names neither **zark** nor **sanoid** wrote, and history from
    before the drive's first **zark** 2 backup point (recorded on the drive
    as **org.zark:first-point**, so it does not move when retention removes
    the first points; see **prune**). A
    month's point is the newest one no dataset skipped since; with none
    left, its newest point whose run was complete. An
    archived dataset or one kept as only on the drive goes whole once
    nothing of it is kept, also when the space check or **prune** empties
    it. Snapshots are destroyed by their exact names, never by a range,
    grouped per dataset (at most 100 per command; when ZFS refuses a group
    its names are destroyed one by one), and the drive's keys are not
    needed. **prepare** asks
    the window and the maximum age; **registry retention** changes them.

    **Clones.** A dataset new in the source that is a clone of a snapshot
    the drive already holds is sent as an incremental from it and received
    as a clone of the drive's copy, so only what the clone changed travels.

    **Verdict.** **BACKUP COMPLETED** only when every dataset reached the
    point and the read-back is **ONLINE**; a per-dataset table is printed
    and logged, with the error of each dataset that failed (from both sides
    of the transfer when both report one), and the banner lists what is not
    backed up by design.
    Otherwise **BACKUP INCOMPLETE** or **BACKUP NOT VERIFIED**, exit 1, and
    *last_backup_at* is not updated; **BACKUP NOT VERIFIED** after a run
    that reached the point says the drive already records it, since its
    metadata is written before the export the read-back needs. A dataset in
    the source whose name has a component containing *.archived-* is not
    backed up (the name is reserved for archived lineages) and is listed
    with the reason. A listing of the source or the drive
    that fails stops the run before the questions, or makes it incomplete
    afterwards, and then nothing is anchored or removed in the source. An
    interrupted transfer resumes at the next **backup**; its partial state
    is discarded only when the source no longer has what it needs, which is
    checked before the questions. **BACKUP INCOMPLETE** groups the datasets
    that missed the point by what the next **backup** does with them. When
    every dataset reached the point, its snapshots on the drive are marked
    **org.zark:point-complete=**\<number of datasets\>, which **recover**
    reads from each snapshot itself: a point that later loses part of its
    snapshots (a retention removal, a rollback) is no longer complete.

    **Read-back verification.** After exporting, **backup** drops the
    kernel page cache and re-imports the pool read-only by its exact
    device, requiring an **ONLINE** state before reporting the backup
    as safe, and exports it again. This guards against USB-SATA bridges
    that misreport cache flushing (FUA): such a bridge can let
    **zpool export** succeed over a pool that is no longer importable,
    with labels intact but spacemaps lost (the on-disk symptom is
    **metaslab_init failed [error=52]** on the next open). The verdict
    says whether the pool could not be re-imported or re-imported with
    errors (with the **zpool status -v** lines). An export that fails is a
    failed backup too. See the enclosure notes in the project's
    **docs/HARDWARE.md**.

    **Drive metadata.** The drive's root dataset records
    **org.zark:format**, **org.zark:version**, **org.zark:origin-host**,
    **org.zark:origin-rpool-guid**, **org.zark:origin-machine-id** and, for
    a complete backup,
    **org.zark:last-point** and **org.zark:last-backup-at**; they are
    readable without the passphrase, and **recover** and **mount** show
    them after importing the drive.

    After a successful backup, **backup** lists how many days have passed
    since every other registered drive's last backup, and records in the
    registry which **sanoid** snapshots (and **syncoid_\*** residues) this
    drive shares with the source, with the version that wrote it. For a
    drive with no anchor in the source (written by **zark** 1.x and not
    backed up by **zark** 2 yet, or any drive after a **recover**) it checks
    that record against the source: until about when the source keeps
    those snapshots (**sanoid** prunes one only once it is older than *n*
    periods of its type; *n* is the smallest count over the templates in use),
    or how many datasets share nothing and are resent in full. A **zark** 2
    drive's newest snapshot is its backup point, which the source keeps
    only as an anchor, so continuing from a shared snapshot means a
    rollback: the next backup asks to roll each dataset back to the newest
    shared snapshot (the drive's backup points after it go) or to resend it
    in full. Without a
    record it gives the guaranteed horizon as a condition: (*n* − 1)
    periods of the longest bucket of the shortest template in use, 62 days
    with **zark**'s templates, if the drive still shares snapshots with the
    source (incrementally on a **zark** 1.x drive, after a rollback on a
    **zark** 2 one). When the source cannot be read it says so in one line
    instead. It also says when **prune** can remove legacy residues.

    **--no-snapshot** is accepted and has no effect: the backup point is
    taken by **zark** itself.

    **backup** refuses to run from an Ubuntu Live USB: the live filesystem
    is not the system the user means to back up, and confusing the two
    would destroy good data on the backup drive.

**purge** \[*device*\]
:   Securely retire a managed backup drive. Destroys the ZFS pool,
    overwrites the start and end of the device with random data to defeat
    casual recovery, wipes filesystem signatures with **wipefs**(8) and
    zaps the partition table with **sgdisk**(8). The device may be
    specified as a positional argument, otherwise **zark** asks
    interactively. The drive is identified by the pool GUID on its ZFS
    label and by its by-id name, and every **known_drives.json** entry that
    describes it is removed. On the installed system the wiped pool's
    anchors in the source go too; elsewhere the banner gives the command to
    run there. Partitions and disks holding the running system, a mounted
    filesystem or active swap are refused.

    The pool's state is captured before it is destroyed (see
    **zark-forensics** under **FILES**). A failed **zpool destroy** stops
    before anything is overwritten; a pool found imported read-only (a
    finished read-only **mount**) is exported and imported read-write
    first, and a failed export or re-import stops there too. Every wipe
    step is checked, and all of them run even after one fails. The ZFS
    labels on the first partition are read right after the overwrite, with
    the disk locked against **udev**(7), which would otherwise re-read the
    partition table and remove the partition (and again after
    **wipefs**(8), if it left the table); then any partition left and any
    signature **blkid -p** finds are checked. Any failure ends **PURGE INCOMPLETE** with exit status 1, and
    the registry entries and the anchors in the source are kept, since the
    disk may still hold the pool. The banner lists every step and check as
    done, failed or not done, so it shows what state the disk is in.

**registry** \[**list** | **forget** *name*|*GUID* | **fix** \[*name*\] | **retention** *name* \[**window=***N*\] \[**max_age=***N*|**off**\] \[**margin=***N*\]\]
:   Inspect and repair **known_drives.json** without editing it by hand.
    **list** (the default) shows every entry, whether its disk is connected
    and whether the pool GUID on the disk matches; on the installed system
    also every drive GUID that holds anchors in the source without an
    entry, and how many backup points were left there by interrupted runs
    or by a run whose anchor failed.
    **forget** removes, on the installed system, a drive's anchors in the
    source (its bookmarks on **rpool** and snapshots on **bpool**) and then
    its entry; the drive itself is not touched. Given a pool GUID with no
    entry, it removes only that GUID's anchors. When no drive is left
    registered it offers to remove the backup points left in the source. **fix** rewrites *drive_id* from the
    connected disk that carries the registered pool GUID and writes every
    missing key. **retention** shows or changes a drive's retention: the
    window in days (1-3650), the maximum age in days (**off**, or more than
    the window) and the free-space margin in percent of the drive (0-50).
    Every write is validated and atomic; a malformed file is
    reported with its line and column and never overwritten.

## Recovery workflow

**recover**
:   Full bare-metal restoration of an encrypted Ubuntu-on-ZFS system from
    a backup drive. Must be run from an Ubuntu Live USB with the backup
    drive connected.

    The procedure scans for backup drives and, before importing anything,
    lists the chosen drive with **zpool import -d**: when it uses features
    the live session's ZFS cannot read, **recover** stops, naming each
    feature, the OpenZFS release that has it and the one running. It then
    imports the chosen pool read-only by its exact device, prompts for the rpool passphrase (when
    the drive holds several boot environments, a menu lists each with its
    newest snapshot's time, the newest the default) and
    offers the restore points found on the drive, ordered by snapshot
    creation time: backup points are labelled as such, one entry per backup
    point, those of a backup that did not reach every dataset as
    **INCOMPLETE**, and the **sanoid** snapshots carried between them as
    carried points, each with how many datasets it holds. The default is
    the newest complete backup point. A dataset is not restored for a point
    when it no longer existed then (a complete backup point at or before it
    lacks the dataset and nothing of it follows); when a backup simply did
    not reach it, its newest earlier snapshot is used and shown with its
    offset. Datasets archived
    by **backup** serve the points older than the archive. Each dataset is
    received from its newest **sanoid** monthly snapshot before the point,
    then the point incrementally from it, so the recovered system shares
    that snapshot with every drive in rotation and their next backups
    continue instead of sending everything again; when those snapshots do
    not fit on the disk, or a dataset has none, the point alone is
    restored, and a line says so. It then shows a table with
    the snapshot every dataset will be restored from - never one newer
    than the point - and the mount properties it will get. Only after a
    full pre-flight (sizes of that point, keystore, bpool) and a typed
    **YES** does it partition the internal disk as EFI + bpool + rpool,
    recreate **rpool** *with* native encryption to match the Ubuntu
    installer, raw-receive every first-level dataset tree (not only
    **ROOT** and **USERDATA**), restore the LUKS keystore zvol last (which is mandatory - restoring
    it earlier triggers a kernel udev crash documented in the Debian
    **zfs-linux** issue tracker), reinstates **encryptionroot** with
    **zfs change-key -i**, repopulates **bpool**, chroots into the
    recovered system, reinstalls a Secure-Boot-capable GRUB chain via the
    **dpkg-reconfigure**(8) sequence described in **NOTES**, regenerates
    the initramfs (**dracut**(8) on Ubuntu 25.04+, **initramfs-tools**(8)
    on 24.04) and exports both pools cleanly.

    The recovered system boots without **zark** present and receives
    normal **apt**(8) upgrades thereafter. **zark** is needed only for
    backup and recovery; once the disaster is over it can be unplugged
    along with the live USB.

**repair-boot**
:   Fix a broken boot chain on a system whose ZFS pools are intact.
    Imports **rpool** and **bpool** under an alternate root, mounts the
    affected system, regenerates **grub.cfg** and the initramfs and
    cleanly exports the pools. The most common reasons to need this are
    that **update-grub**(8) ran while a **zark** backup drive was
    connected (polluting **grub.cfg** with backup-pool UUIDs), or that a
    GRUB or shim package upgrade has left a **grub.cfg** referencing
    drive paths or UUIDs that no longer match the current firmware
    layout.

    The pools are imported by */dev/disk/by-id*, so the *zpool.cache* it
    writes records stable device names. A system whose pools still record
    kernel names (*/dev/sdb4*) can fail to boot when a USB disk plugged in
    at power-on takes that name; **backup** and **finish** warn about it,
    and one **repair-boot** fixes it. While a system pool's vdevs still
    carry kernel names after the import, **repair-boot** ends **BOOT REPAIR
    INCOMPLETE** naming them, since the *zpool.cache* it writes records them.

**chroot** \[*device*\]
:   Open an interactive **chroot**(1) into the installed ZFS system from a
    live USB. Imports **rpool** and **bpool** under an alternate root,
    unlocks the keystore, mounts the boot environment (with several, a menu
    lists each with its newest snapshot's time; the same choice applies to
    **mount local** and **repair-boot**), sets up the
    **/proc**, **/sys**, **/dev**, **/dev/pts**, **/run**, **efivars** and
    ESP bind mounts a chroot needs, and starts a login shell inside the
    system. Inside, ordinary tools (**apt**(8), **update-grub**(8),
    **dpkg-reconfigure**(8)) behave as on a booted system. On exit the
    command unmounts everything and exports both pools cleanly, so the next
    real boot imports them without **-f**.

    **rpool** and **bpool** are looked for in */dev/disk/by-id* first; the
    optional *device* (for example */dev/nvme0n1*) is a hint tried next,
    and a full scan comes last. An import that did not go through
    */dev/disk/by-id* is named in a warning, since the pool then records
    those names.
    **chroot** refuses to run when **rpool** is already imported - if that
    is the running system you are already inside it, and if it is a
    leftover from a previous run, **prune**
:   Reclaim space, interactively, in the source or on a connected registered
    drive. Nothing goes without a confirmation; every snapshot is destroyed
    by its exact name and logged, and what each choice frees is measured
    first. In the source: each pool's fill level and, per kind of
    snapshot, how many there are, the oldest and what removing them frees;
    then **sanoid**'s snapshots older than a number of days (typed
    **PRUNE**; */etc/sanoid/sanoid.conf* is not changed), the legacy
    residues (**syncoid_\*** snapshots left by **zark** 1.x on datasets
    where every registered drive has an anchor, and the keystore's
    **prepare_\*** snapshots; yes/no), the backup points left in the source
    by interrupted runs or by a run whose anchor failed (typed **PRUNE**;
    a point names its drive, **org.zark:point-for**, and is anchored for
    that drive before it goes, so the drive keeps its base; a point that
    names no drive (one from before 2.0.0-rc5, or one a **recover**
    restored) is removed without an anchor, and a drive that received
    part of it rolls those datasets back at its next backup, after asking;
    either way a drive
    whose transfer was interrupted sends it again) and a drive's **bpool** anchors
    (typed **PRUNE**; that drive's next backup then asks to roll those
    datasets back to a shared **sanoid** snapshot, which removes its newest
    backup point on them, or to archive them and resend them in full).
    Backups impose no floor: the drives' anchors on **rpool** are
    bookmarks, which no snapshot removal touches. On a drive (no passphrase
    needed): what its retention removes now (yes/no), then the oldest
    monthly backup points that still have something to remove and the
    history from before its first **zark** 2 backup point (typed **PRUNE**).

**status**
:   How full **rpool** and **bpool** are (a warning from 80 %, critical
    from 90 %), the days since every registered drive's last backup, what
    a drive with no anchor in the source still shares with it (see
    **backup**) and the legacy residues **prune** can remove. **status --refresh** writes
    */run/zark/status.motd*; with **--notify** it also sends a desktop
    notification for a pool at a warning or critical level, at most once a
    day per pool and level. Read-only.

**alerts** **install**|**remove**
:   Install or remove the status alerts: */etc/update-motd.d/97-zark-status*,
    which prints */run/zark/status.motd* at login (nothing is computed
    then), and **zark-status.timer**, which runs **status --refresh
    --notify** every hour and two minutes after boot, with the **zark**
    that installed it. **setup** installs them; removing the Debian package
    removes them.

**clean** releases it first.

**finish**
:   Post-recovery finalisation, intended to be run *from inside the
    recovered system* on its first boot. Resets the hostid, refreshes the
    ZFS cachefile, ensures the ZFS systemd services are enabled and runs
    a final **update-grub**(8) and initramfs regeneration without the
    backup drive present, so the resulting **grub.cfg** is clean. If
    **update-grub** fails (the grub guard refuses while an external pool
    is visible) or produces no kernel entries, the previous **grub.cfg**
    is kept, the banner reads FINISH INCOMPLETE and the exit status is 1;
    the same applies to a failed initramfs update or a pool not ONLINE.
    Disconnect the backup drive and run **finish** again.

**simulate** \[*device*\] \[**--rw**\] \[**--display** *WxH*\]
:   Boot a recovered (or live) disk in **qemu-system-x86_64**(1) under
    OVMF UEFI firmware to verify the boot chain without rebooting the
    physical machine. With no arguments, presents an interactive menu
    of eligible disks (the host's in-use disks are filtered out for
    safety); pass a **/dev/...** path to skip the menu.

    **Read-only is the default.** QEMU is started with **-snapshot**
    so any writes are discarded at shutdown and the underlying disk
    is never modified - the recommended mode for verifying that a
    freshly restored system can actually boot.

    **--rw** opts into read-write mode. QEMU writes to the physical
    disk just like a real boot would. This is occasionally useful (for
    example, to allow first-boot self-healing of the hostid issue
    described in **NOTES**) but requires interactive confirmation -
    the operator must re-type the target device path verbatim.

    **--display** *WxH* sets the QEMU display resolution. Default is
    *2560x1440*, tuned for typical zark-on-Ubuntu hardware (4K-class
    developer machine). Common overrides: *1920x1080*, *3840x2160*.
    Both lower- and upper-case **x** are accepted; values above 8K
    are rejected as probable typos.

    When the host has both a GPU render node (*/dev/dri/renderD\**)
    and **virtio-vga-gl** support in **qemu-system-x86_64**, simulate
    automatically uses GL-accelerated rendering with a resizable GTK
    window. Otherwise it falls back to software rendering with a
    warning naming the missing capability.

    For backwards compatibility **--ro** is silently accepted as a
    no-op (read-only is the default now).

    Requires the **qemu-system-x86** and **ovmf** packages, both of
    which are listed in *Recommends* and which **simulate** offers
    to install on first use.

**fix-rpool-mountpoint**
:   Give a restored **rpool** back the Ubuntu installer's
    *mountpoint=/* (with *canmount=off*). **recover** up to 1.0.12
    created **rpool** with *mountpoint=none*: datasets created later
    directly under **rpool** then never mount, and drives prepared
    from that system cannot be browsed with **mount**. **backup**,
    **prepare** and **finish** warn when they detect it.

    Runs from a live USB only. It sets the **zvol_inhibit_dev** module
    parameter before importing **rpool** (**-N**, under an altroot, no
    key loaded), so no zvol device exists while the mountpoint is
    changed, and refuses to start while another pool's zvol devices
    exist; lists the datasets that inherit their mountpoint from
    **rpool** with their *canmount*, and for each one that is not
    *off* or *noauto*, which starts mounting at the next boot, asks
    whether to keep it (typing its name), set *canmount=off* or
    abort; asks for *YES*. It exports **rpool** and restores the
    parameter on a normal end, an error, Ctrl-C, SIGTERM or SIGHUP
    (also several at once), and when its output goes to a closed
    terminal or a dead pipe; not on SIGKILL.

## Maintenance

**mount** \[*target*\]
:   Mount a backup pool for inspection, **chroot**(1) entry or manual
    recovery work. Imports the chosen pool by its exact device with an
    alternate root of */mnt/zark/<poolname>/*. Asks interactively whether
    to mount read-only (recommended, and the default) or read-write.

    Read-only imports the pool read-only and rebuilds the backed-up
    system's tree under */mnt/zark/<poolname>/* the way **recover** would
    restore it: the boot environment at the top (with several on the drive,
    chosen from a menu, the newest the default), */home*, */boot* and every
    other dataset at the origin's mountpoint (from the origin's
    *zfs-list.cache* inside the backup, else the Ubuntu layout). Nothing
    is written to the drive; a dataset whose mountpoint directory does not
    exist, or whose path resolves outside */mnt/zark/<poolname>/* (through
    a symbolic link or *..* inside the backup), is listed as not mounted.
    Read-write mounts the same tree, writable; mounting changes every
    mounted dataset, even with nothing written, and the next **backup**
    asks whether to skip those datasets or discard the changes. Archived
    datasets (*\<name\>.archived-YYYYMMDD*) are not part of the tree; the
    banner lists them with the command to browse one.

    With no argument, scans for connected backup drives. With the
    *target* **local** (aliases **system**, **rpool**) it instead mounts
    the **installed system's** top-level **rpool**/**bpool** from a live
    USB - useful for inspecting the local disk without a full **chroot**.

    The complementary command is **umount**.

**umount** \[*target*\]
:   Unmount a previously **mount**-ed backup pool. Unmounts only the tree
    under */mnt/zark/<poolname>/*, closes the LUKS keystore and exports the
    pool. If the export fails because a copy of the tree stays mounted in
    another mount namespace (a service's or a snap's, including those
    **snapd** pins in */run/snapd/ns*), it unmounts the tree there with
    **nsenter** (only copies of its own tree: a dataset of the pool, or
    a mount with the same type and source as one the tree had here) and
    tries the export again; what still cannot be released is named with
    the namespace's process, and it exits 1.

    With the *target* **local** (aliases **system**, **rpool**) it exports
    the installed system's pools mounted by **mount local**, unmounting
    only the tree under their alternate root. As a safety
    measure it refuses to export any pool whose alternate root is not
    under */mnt/zark/* - that is the guard against exporting the running
    system's own **rpool**.

**clean**
:   Release what an interrupted **zark** run left mounted or imported, and
    nothing else. It lists what **zark** owns: mounts below */mnt/zark*,
    */mnt/recover*, */mnt/repair*, */run/zark/altroot* and
    */run/zark/probe*; pools imported with an alternate root below them;
    keystore mappings */dev/mapper/zark_ks_\**, and a mount on top of
    */run/keystore/rpool* that comes from one; a **sanoid** timer left
    stopped by an interrupted **backup**; **zvol_inhibit_dev=1** left by
    **fix-rpool-mountpoint**. It also names the imported pools it leaves
    alone (a pool without an alternate root, such as the running system's
    **rpool** and **bpool** or one imported by hand, is never touched).
    After a yes (default no) it unmounts, closes and exports, without
    forcing anything, and removes only the directories left empty, the
    roots themselves included, once nothing is mounted below them and no
    pool of its own is still imported (removing a directory would detach a
    copy mounted there in another mount namespace). Anything that stays (a
    mount in use, or
    held in another mount namespace that could not be released there) ends
    **CLEANUP INCOMPLETE** with exit status 1, naming it. When a pool does
    not export, the copies other mount namespaces keep below the trees it
    unmounted are unmounted there and the export is tried again. The same rules apply on an installed system and on a live
    session.

# WORKFLOWS

The following sequences cover the two normal end-to-end uses of **zark**.

## First-time setup of a new backup drive

    sudo zark setup
    sudo zark prepare /dev/disk/by-id/usb-...

After this, plugging in the drive and running **zark backup** is enough.

## Routine backup

    sudo zark explore        # confirm the right drive is plugged in
    sudo zark backup         # do the work
    sudo zark monitor        # in a second terminal, optional

## Bare-metal recovery

Boot the affected machine from an Ubuntu Live USB containing **zark** (or
download/extract the **zark** tarball after booting the live USB), then:

    sudo ./zark recover

After the recovery completes and **zark** has exported both pools, reboot
into the recovered system and run:

    sudo zark finish

To verify a recovery without rebooting:

    sudo zark simulate

# FILES

*/usr/share/zark/*
:   Installation directory used by the Debian package. The
    */usr/bin/zark* symlink points at */usr/share/zark/zark*.

*/etc/zark/known_drives.json*
:   Registry of known backup drives, used by every command that needs
    to identify a connected drive. When **zark** is run from a portable
    location (USB pendrive, **git** checkout, extracted tarball), the
    registry is looked up next to the entry-point script first and falls
    back to */etc/zark/known_drives.json* only if no local copy exists.
    A documented example is shipped at
    */etc/zark/known_drives.json.example*. Besides the keys an operator
    sets, **zark** keeps per drive *prepared_at*, *prepared_version*,
    *last_backup_version* and *shared* (the snapshots the drive shared with
    the source at its last complete backup).

    Each top-level key is a pool name with these fields: **guid** (pool
    GUID, decimal; required), **drive_id** (stable */dev/disk/by-id/*
    identifier without the *-part1* suffix; required), **last_backup_at**
    (ISO-8601 UTC of the last successful backup; auto-written; optional),
    and **autoeject** (boolean; optional, default false). When
    **autoeject** is true the eject prompt for that drive shows a
    10-second countdown and then applies the command's default
    automatically - any keypress cancels it and restores the normal
    blocking prompt. **prepare** asks whether to enable it; it can also
    be toggled by editing the file. An optional **retention** object holds
    the drive's **window_days**, **max_age_days** and **margin_pct**; see
    **registry retention**.

*/run/zark/status.motd*, */etc/update-motd.d/97-zark-status*, */etc/systemd/system/zark-status.service*, */etc/systemd/system/zark-status.timer*
:   The status alerts; see **alerts**.

*/var/lib/zark/selftest/*
:   **selftest**'s reports (the last ten, *report-\<UTC\>.json*) and
    *record.json*, this machine's newest complete and partial result per
    level and session type.

*settings.json*
:   Next to *known_drives.json*: **selftest_warnings** (true or false),
    set by **selftest warnings**.

*/usr/share/zark/certificates/selftest.json*
:   The self-test results certified for each platform series, shipped with
    **zark** (next to *lib/* in a portable copy).

*/var/log/zark.log*
:   Log file for system installs. Portable installs write
    *zark.log* alongside the entry-point script instead, so the log
    follows the pendrive.

*/mnt/zark/*
:   Alternate-root mountpoint base used by **mount** and (transiently) by
    **recover** and **repair-boot**.

*/run/zark/zark.lock*
:   The lock every command that changes state takes with **flock**(2)
    before it starts; **status**, **explore**, **monitor**, **registry**
    (list) and **selftest** (**status**, **report**, **certificate**) run
    without it. The kernel releases it when the run ends, however
    it ends. Its content (command, pid, start time) is only for the refusal
    message.

*zark-forensics-\<pool\>-\<UTC\>.txt*
:   Written next to *zark.log* (mode 0600) before a damaged pool is
    touched or destroyed: **zpool status -vP**, **zpool events -v**, the end
    of **zpool history -il**, the kernel log of the last two hours,
    **lsblk** and the USB bridge ids of the pool's disks.

*/run/keystore/rpool/system.key*
:   In-memory location of the unlocked rpool key on a running system.
    Created by the **dracut**(8) keystore module at boot.

# ENVIRONMENT

**ZARK_CONFIG_DIR**
:   If set, overrides the configuration directory search and forces
    **zark** to read **known_drives.json** from this path. Useful for
    integration tests and for keeping multiple registries (for example,
    one per laptop) on a single recovery pendrive.

**DEBEMAIL**, **DEBFULLNAME**
:   Used only by the packaging targets in the project's **Makefile**
    (**make deb-ppa**); not consulted by the **zark** runtime itself.

# EXIT STATUS

**0**
:   Success.

**1**
:   A fatal error occurred. The **\[FATAL\]** banner printed just before
    exit explains the cause and lists likely remediations. With
    **--debug**, a Python traceback is also printed.

**75**
:   Another **zark** run that changes state is in progress (it holds
    */run/zark/zark.lock*); the message names it. Run again once it ends.
    Also when the lock file cannot be opened; the message says so.

**130**, **143**, **129**, **141**
:   The run was stopped by **Ctrl-C** (SIGINT), SIGTERM, SIGHUP (a closed
    terminal), or because the reader of its output went away. **zark**
    stops the running command first (up to 30 seconds, then SIGKILL),
    ignores further signals, then runs its cleanup once: it exports any
    pools it imported, closes any keystore it opened and starts the
    **sanoid** timer again if it stopped it. The cleanup a command runs at
    its normal end ignores the three signals the same way, and handles
    them again once it is over. In **chroot** and **simulate**
    a Ctrl-C belongs to the shell or QEMU. A backup interrupted during its
    transfers anchors, for the drive, the newest snapshot the drive holds
    of every dataset, and keeps its point in the source only where a
    partial transfer needs it; the next backup resumes them and removes
    it. The log names every point snapshot removed or kept, with the
    reason.

# DEPENDENCIES

Hard runtime dependencies (declared by the Debian package): **python3** ≥
3.12, **zfsutils-linux**, **sanoid** (local snapshots),
**cryptsetup-bin**, **gdisk**, **grub2-common** and one of **dracut** or
**initramfs-tools**.

Soft dependencies (declared as *Recommends*): **dosfstools** (for
EFI partition formatting during **recover**), **grub-efi-amd64-signed**
and **shim-signed** (for Secure Boot installs), **qemu-system-x86** and
**ovmf** (for **simulate**).

# NOTES

## Secure Boot

**recover** never invokes **grub-install**(8) directly. The Ubuntu
Secure-Boot chain requires the Canonical-signed **grubx64.efi.signed** and
**shimx64.efi**; **grub-install** alone overwrites these with an unsigned
binary, breaking the chain. **zark** instead runs **grub-install** to lay
down the GRUB modules, then **dpkg-reconfigure grub-efi-amd64-signed**
followed by **dpkg-reconfigure shim-signed** to install the signed
binaries, and finally **update-grub**(8). This matches what the Ubuntu
installer does on a fresh install.

## Live ISO behaviour

**recover** and **repair-boot** must run from a live environment. Importing
and exporting pools repeatedly on a live USB is known to corrupt the
overlay filesystem (*/bin/sh* and similar essentials disappear), so
**zark** keeps such cycles to the minimum required by the recovery
sequence and writes its log to the pendrive, not to */var/log/*, while in
this mode. Before a command that imports pools, **zark** stops the
installer's **ubuntu-desktop-bootstrap.subiquity-server** for the session
(**snap stop**, nothing changes for the next boot): it probes the disks on
every change and pops up a crash report after each import.

## First-boot hostid

The Ubuntu installer (**subiquity**) does not seed */etc/hostid* before
generating the initramfs, so a freshly recovered system may drop into an
**emergency mode** shell on first boot. The fix from that shell is:

    zpool import -f -N rpool
    zpool import -f -N bpool

Subsequent boots self-heal. This affects all Ubuntu-on-ZFS installs, not
just **zark**-recovered systems; an upstream bug has been filed against
**subiquity** on Launchpad.

## update-grub guard

The **setup**, **recover**, **repair-boot** and **finish** commands install
*/etc/grub.d/09_zfs_backup_guard*, which aborts **update-grub**(8) while an
external pool is connected: one **zpool import** lists, or one imported,
other than **rpool** and **bpool**, with an alternate root or carrying
**org.zark:format** (a **zark** drive imported by hand). A permanent data
pool imported without an alternate root does not block it. This prevents the most
common cause of post-update boot failure: **update-grub** picking up the
backup pool and writing its UUIDs into **grub.cfg**.

## apt guard

**setup** (on the running system) and **recover**/**finish** (on a
recovered system) install a complementary, earlier line of defence:
*/usr/local/lib/zark/apt-zfs-backup-guard*, wired in as a
**DPkg::Pre-Install-Pkgs** hook via
*/etc/apt/apt.conf.d/09zark-zfs-backup-guard*. APT runs it before
**dpkg**(1) unpacks anything; if a boot-critical package (**linux-image**,
**linux-headers**, **grub**, **shim**, **zfs**, ...) is being installed
*while an external ZFS pool is connected*, the hook aborts the whole
transaction. An external pool is one **zpool import** lists, or an imported
one detected as the update-grub guard does. This stops a background **unattended-upgrades**(8) run from
half-applying a kernel upgrade - new kernel unpacked, **update-grub**
blocked by the guard above, old kernel autoremoved - which would leave
**grub.cfg** pointing at a missing kernel and the system unbootable.

The hook is standalone: it detects pools with **zpool**(8) directly and
does not require **zark** to be installed, so it keeps protecting a
recovered system after the live USB is gone. Bypass it once for a
deliberate operation with **ZARK_INTERNAL=1**; remove
*/etc/apt/apt.conf.d/09zark-zfs-backup-guard* to disable it entirely. A
login-time reminder (*/etc/update-motd.d/99-zark-external-pool*) warns when
an external pool is attached.

# EXAMPLES

Show what is plugged in:

    sudo zark explore

Prepare a new external SSD as a backup target:

    sudo zark prepare /dev/disk/by-id/usb-Micron_CT2000X10PROSSD9_2449E8CD1F15-0:0

Run the routine nightly backup:

    sudo zark backup

Mount yesterday's backup read-only to grep through */etc*:

    sudo zark mount
    grep something /mnt/zark/backup/etc/some.conf
    sudo zark umount

Recover a dead laptop from an Ubuntu Live USB:

    sudo ./zark recover
    # ... reboot into the recovered system ...
    sudo zark finish

Drop into the installed system from a live USB to fix it by hand:

    sudo ./zark chroot
    # inside the chroot:
    update-grub && exit
    # zark exports the pools cleanly on exit

Verify the recovery in QEMU before rebooting, without writing to disk:

    sudo zark simulate

# BUGS

Report bugs at https://github.com/juanmitaboada/zark/issues.

# AUTHOR

Juanmi Taboada (juanmi@juanmitaboada.com)

# SEE ALSO

**zfs**(8), **zpool**(8), **sanoid**(8),
**cryptsetup**(8), **dracut**(8), **grub-install**(8),
**update-grub**(8)
