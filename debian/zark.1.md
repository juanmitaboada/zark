% ZARK(1) zark @VERSION@ | User Commands
%
% May 12, 2026

# NAME

zark — full bare-metal ZFS backup and recovery for Ubuntu with encrypted root

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

A full restore — partitioning, **rpool** + **bpool** recreation, LUKS keystore
reconstruction, GRUB and initramfs regeneration — runs from an Ubuntu Live USB
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

    - *known* — name and pool GUID match a registered drive;
    - *GUID changed* — pool name matches but the GUID differs (typically
      after a re-prepare);
    - *renamed* — the GUID matches but the pool has been renamed;
    - *unknown* — the drive is not registered.

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

    The read-only check inspects kernel/sysfs state for risk factors: a
    USB-SATA bridge that reports it does not support DPO/FUA, the UAS
    transport on a bridge that may mishandle it, and the bridge's USB VID:PID
    against a known-problematic list. It writes nothing and flags *risk*
    only — it cannot prove a bridge honest, which shows solely under load.

    The destructive test (after explicit confirmation, on a blank drive)
    creates a throwaway pool and writes with transaction churn — *fast*
    (~2 GB), *medium* (~15 GB), or *surface* (whole disk, capped) — then
    re-imports to confirm the writes persisted. An optional *cold* pass
    powers the device down, waits for you to physically reconnect it, and
    re-imports, so the read-back comes strictly from NAND rather than the
    bridge's cache. Time estimates are shown from a measured write speed. The
    throwaway pool is always destroyed afterwards, leaving the drive blank.

    Whenever a risk or test failure is found, a self-contained diagnostic
    report is written to */tmp* (environment, drive, bridge VID:PID, findings,
    dmesg tail) with instructions for filing a GitHub issue; you may choose to
    obfuscate serials/GUIDs. See the enclosure notes in **docs/HARDWARE.md**.
    The read-only check also runs at the start of **prepare**.

## Backup workflow

**setup**
:   One-time installation of the runtime dependencies and configuration of
    **sanoid**(8) snapshot policies. Installs the packages listed in the
    **DEPENDENCIES** section, drops a **sanoid** template tuned for
    Ubuntu-on-ZFS into */etc/sanoid/*, and registers the **sanoid** systemd
    timer so snapshots are taken automatically. Also installs the apt guard
    (see **NOTES**) on the running system, so a background kernel or GRUB
    upgrade cannot half-apply while a backup drive is connected.

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
    auto-eject) is asked before anything is written. **prepare** then
    creates an unencrypted ZFS pool on the drive (encryption is provided by
    the raw send from the encrypted source pool — adding a second layer
    here would just hide the failure of the inner one) on its by-id name
    with an alternate root, so it is never written to
    */etc/zfs/zpool.cache*, sends one backup point of **rpool** and
    **bpool** (the point only, not the source's snapshot history) the way
    **backup** does, copies over the **rpool/keystore** zvol, and records
    the drive's metadata (see **backup**). A drive without a by-id name is
    refused rather than registered as *\<unknown\>*; registry entries left
    for the same drive are offered for replacement.

    Before doing any work, **prepare** runs the same non-destructive risk
    check as **health** and, if a risk factor is present, warns and asks for
    confirmation. After the transfer it performs a read-back verification
    (identical to **backup**'s): because the initial raw send is a full
    write-under-load, a successful re-import here proves the bridge survives
    real load. The drive is registered, with *last_backup_at* set, only
    when every dataset and the keystore landed and the read-back is
    **ONLINE**; otherwise it is **not** registered and **prepare** exits 1.

**backup** \[**--no-snapshot**\]
:   Back up this system to the connected, registered backup drive.
    Auto-detects which known drive is plugged in and imports it by its
    exact device with **-N** and an alternate root, so nothing it holds is
    mounted and it never enters */etc/zfs/zpool.cache*.

    **Backup point.** **backup** pauses the **sanoid** timer (waiting for a
    running snapshot or prune pass), takes one snapshot
    **zark_YYYY-MM-DD_HH:MM:SSZ** (UTC) of every replicated dataset — one
    **zfs snapshot** per pool, so all of **rpool** is captured in the same
    transaction group — and brings every dataset on the drive to it with
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
    the timer is resumed. Datasets on the drive are received with
    **canmount=noauto** and no mountpoint of their own; the source's values
    are recorded as **org.zark:canmount** and **org.zark:mountpoint** and
    used by **recover** and **mount**. Volumes other than the keystore are
    not backed up and are listed as such.

    **Questions.** Everything is asked before the first byte is sent, and
    nothing on the drive is destroyed unless chosen here (typed
    confirmations for destroys):

    - a dataset that exists only on the drive: ask again next time (the
      default), keep it (not asked again), destroy it, or — when the
      source renamed it — rename it on the drive too;
    - datasets whose only snapshot in common with the source is older than
      the drive's newest (the first run on a drive written by an older
      **zark**, or a source recovered from an older point): one prompt
      listing the snapshots at stake, to abort (the default), archive those
      datasets and send them again in full, or roll them back;
    - a dataset with nothing in common at all: skip it this time (the
      default), archive it and send it again in full, or destroy it and
      send it again.

    An archived dataset is renamed to *\<name\>.archived-YYYYMMDD* on the
    drive and keeps all its snapshots. The estimated transfer is checked
    against the drive's free space before any choice is applied.

    **Verdict.** **BACKUP COMPLETED** only when every dataset reached the
    point and the read-back is **ONLINE**; a per-dataset table is printed
    and logged. Otherwise **BACKUP INCOMPLETE** or **BACKUP NOT VERIFIED**,
    exit 1, and *last_backup_at* is not updated. An interrupted transfer
    resumes at the next **backup**.

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
    **org.zark:origin-rpool-guid** and, for a complete backup,
    **org.zark:last-point** and **org.zark:last-backup-at**; they are
    readable without the passphrase, and **recover** and **mount** show
    them after importing the drive.

    After a successful backup, **backup** lists how many days have passed
    since every other registered drive's last backup.

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
    describes it is removed. Partitions and disks holding the running
    system, a mounted filesystem or active swap are refused.

**registry** \[**list** | **forget** *name* | **fix** \[*name*\]\]
:   Inspect and repair **known_drives.json** without editing it by hand.
    **list** (the default) shows every entry, whether its disk is connected
    and whether the pool GUID on the disk matches. **forget** removes one
    entry and, on the installed system, that drive's anchors in the source
    (its bookmarks on **rpool** and snapshots on **bpool**); the drive
    itself is not touched. **fix** rewrites *drive_id* from the
    connected disk that carries the registered pool GUID and writes every
    missing key. Every write is validated and atomic; a malformed file is
    reported with its line and column and never overwritten.

## Recovery workflow

**recover**
:   Full bare-metal restoration of an encrypted Ubuntu-on-ZFS system from
    a backup drive. Must be run from an Ubuntu Live USB with the backup
    drive connected.

    The procedure scans for backup drives, imports the chosen pool
    read-only by its exact device, prompts for the rpool passphrase and
    offers the restore points found on the drive, ordered by snapshot
    creation time (the newest is the default): backup points are labelled
    as such and the **sanoid** snapshots carried between them as carried
    points, each with how many datasets it holds. A dataset without a
    backup point's snapshot did not exist then and is not restored for that
    point; datasets archived by **backup** serve the points older than the
    archive. It then shows a table with
    the snapshot every dataset will be restored from — never one newer
    than the point — and the mount properties it will get. Only after a
    full pre-flight (sizes of that point, keystore, bpool) and a typed
    **YES** does it partition the internal disk as EFI + bpool + rpool,
    recreate **rpool** *with* native encryption to match the Ubuntu
    installer, raw-receive every first-level dataset tree (not only
    **ROOT** and **USERDATA**), restore the LUKS keystore zvol last (which is mandatory — restoring
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
    and one **repair-boot** fixes it.

**chroot** \[*device*\]
:   Open an interactive **chroot**(1) into the installed ZFS system from a
    live USB. Imports **rpool** and **bpool** under an alternate root,
    unlocks the keystore, mounts the boot environment, sets up the
    **/proc**, **/sys**, **/dev**, **/dev/pts**, **/run**, **efivars** and
    ESP bind mounts a chroot needs, and starts a login shell inside the
    system. Inside, ordinary tools (**apt**(8), **update-grub**(8),
    **dpkg-reconfigure**(8)) behave as on a booted system. On exit the
    command unmounts everything and exports both pools cleanly, so the next
    real boot imports them without **-f**.

    The optional *device* is an import hint (for example */dev/nvme0n1* or
    a */dev/disk/by-id/* path), tried before anything else, so **rpool**
    then records the names it was found under. When omitted, **rpool** is
    looked for in */dev/disk/by-id* first and by a full scan only when that
    fails.
    **chroot** refuses to run when **rpool** is already imported — if that
    is the running system you are already inside it, and if it is a
    leftover from a previous run, **clean** releases it first.

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
    is never modified — the recommended mode for verifying that a
    freshly restored system can actually boot.

    **--rw** opts into read-write mode. QEMU writes to the physical
    disk just like a real boot would. This is occasionally useful (for
    example, to allow first-boot self-healing of the hostid issue
    described in **NOTES**) but requires interactive confirmation —
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
    restore it: the boot environment at the top, */home*, */boot* and every
    other dataset at the origin's mountpoint (from the origin's
    *zfs-list.cache* inside the backup, else the Ubuntu layout). Nothing
    is written to the drive; a dataset whose mountpoint directory does not
    exist, or whose path resolves outside */mnt/zark/<poolname>/* (through
    a symbolic link or *..* inside the backup), is listed as not mounted.
    Read-write mounts the same tree, writable. Archived datasets
    (*\<name\>.archived-YYYYMMDD*) are not part of the tree; the banner lists
    them with the command to browse one.

    With no argument, scans for connected backup drives. With the
    *target* **local** (aliases **system**, **rpool**) it instead mounts
    the **installed system's** top-level **rpool**/**bpool** from a live
    USB — useful for inspecting the local disk without a full **chroot**.

    The complementary command is **umount**.

**umount** \[*target*\]
:   Unmount a previously **mount**-ed backup pool. Unmounts only the tree
    under */mnt/zark/<poolname>/*, closes the LUKS keystore and exports the
    pool. If the export fails (a copy of a dataset can stay mounted in
    another mount namespace, such as a service's or a snap's), it says so
    and exits 1; on a live USB, rebooting the live session releases it.

    With the *target* **local** (aliases **system**, **rpool**) it exports
    the installed system's pools mounted by **mount local**, unmounting
    only the tree under their alternate root. As a safety
    measure it refuses to export any pool whose alternate root is not
    under */mnt/zark/* — that is the guard against exporting the running
    system's own **rpool**.

**clean**
:   Emergency cleanup. Forcibly unmounts everything under */mnt/zark/*,
    closes any open LUKS mappings and exports every imported backup
    pool. Intended as a "get me back to a clean state" command after a
    previous operation has been interrupted.

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
    */etc/zark/known_drives.json.example*.

    Each top-level key is a pool name with these fields: **guid** (pool
    GUID, decimal; required), **drive_id** (stable */dev/disk/by-id/*
    identifier without the *-part1* suffix; required), **last_backup_at**
    (ISO-8601 UTC of the last successful backup; auto-written; optional),
    and **autoeject** (boolean; optional, default false). When
    **autoeject** is true the eject prompt for that drive shows a
    10-second countdown and then applies the command's default
    automatically — any keypress cancels it and restores the normal
    blocking prompt. **prepare** asks whether to enable it; it can also
    be toggled by editing the file.

*/var/log/zark.log*
:   Log file for system installs. Portable installs write
    *zark.log* alongside the entry-point script instead, so the log
    follows the pendrive.

*/mnt/zark/*
:   Alternate-root mountpoint base used by **mount** and (transiently) by
    **recover** and **repair-boot**.

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

**130**
:   The user pressed **Ctrl-C** (SIGINT). **zark** registers a cleanup
    handler that exports any pools it imported and closes any keystore
    it opened before exiting, so interrupting is safe at any point.

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
this mode.

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

The **recover**, **repair-boot** and **finish** commands install
*/etc/grub.d/09_zfs_backup_guard*, which aborts **update-grub**(8) when a
registered **zark** backup drive is connected. This prevents the most
common cause of post-update boot failure: **update-grub** picking up the
backup pool and writing its UUIDs into **grub.cfg**.

## apt guard

**setup** (on the running system) and **recover**/**finish** (on a
recovered system) install a complementary, earlier line of defence:
*/usr/local/lib/zark/apt-zfs-backup-guard*, wired in as a
**DPkg::Pre-Install-Pkgs** hook via
*/etc/apt/apt.conf.d/09zark-zfs-backup-guard*. APT runs it before
**dpkg**(1) unpacks anything; if a boot-critical package (**linux-image**,
**linux-headers**, **grub**, **shim**, **zfs**, …) is being installed
*while an external ZFS pool is connected*, the hook aborts the whole
transaction. This stops a background **unattended-upgrades**(8) run from
half-applying a kernel upgrade — new kernel unpacked, **update-grub**
blocked by the guard above, old kernel autoremoved — which would leave
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
