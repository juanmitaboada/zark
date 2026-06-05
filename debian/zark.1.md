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

Backups are written to a dedicated external drive using **syncoid**(8) raw
sends, so native ZFS encryption is preserved end-to-end: the backup drive
holds ciphertext only, and the original passphrase is the only thing that can
unlock it.

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
    backup is in flight. Reports pool health, the current **syncoid**(8)
    transfer rate and snapshot counts.

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

**prepare** \[*device*\]
:   Initialise a brand-new external drive as a **zark** backup target. The
    drive may be specified as a positional argument (for example
    */dev/disk/by-id/usb-Vendor_Model_Serial-0:0*); if omitted, **zark**
    presents an interactive list of unprepared drives.

    Creates an unencrypted ZFS pool on the drive (encryption is provided by
    the raw send from the encrypted source pool — adding a second layer
    here would just hide the failure of the inner one), performs the
    initial raw transfer of **rpool** and **bpool**, copies over the
    **rpool/keystore** zvol, and registers the new pool in
    **known_drives.json**.

    Before doing any work, **prepare** runs the same non-destructive risk
    check as **health** and, if a risk factor is present, warns and asks for
    confirmation. After the transfer it performs a read-back verification
    (identical to **backup**'s): because the initial raw send is a full
    write-under-load, a successful re-import here proves the bridge survives
    real load. If the read-back fails the drive is **not** registered.

**backup** \[**--no-snapshot**\]
:   Run an incremental backup to the connected, registered backup drive.
    Auto-detects which known drive is plugged in, imports the pool,
    triggers a fresh **sanoid**(8) snapshot pass on the source, and
    uses **syncoid**(8) raw send to transfer the new snapshots. Then
    synchronises **bpool**, exports the backup pool cleanly and (if the
    desktop environment is running) emits a **notify-send**(1)
    summary.

    **Read-back verification.** After exporting, **backup** drops the
    kernel page cache and re-imports the pool read-only by its exact
    device, requiring an **ONLINE** state before reporting the backup
    as safe. This guards against USB-SATA bridges that misreport cache
    flushing (FUA): such a bridge can let **zpool export** succeed over
    a pool that is no longer importable, with labels intact but
    spacemaps lost (the on-disk symptom is **metaslab_init failed
    [error=52]** on the next open). When the read-back fails, **backup**
    prints a **BACKUP NOT VERIFIED** banner and stops before the
    safe-to-unplug prompt — the transferred data is not trustworthy even
    though **syncoid** and **export** reported success. The check is
    always on. See the enclosure notes in the project's
    **docs/HARDWARE.md**.

    **Snapshot policy.** **sanoid**(8) takes snapshots automatically
    via its systemd timer (enabled by **zark setup**), typically
    hourly. To make sure the backup drive holds the most current
    state of the source pool, **backup** runs `sanoid
    --take-snapshots` itself before each replication, regardless of
    when the timer last fired. This is cheap (no I/O on the backup
    drive, idempotent within sanoid's retention windows) so it is
    the default.

    **--no-snapshot** skips the sanoid stage and replicates whatever
    snapshots already exist. Useful for re-runs after a transient
    failure, or for invocations that have already taken snapshots
    by other means.

    **Drive staleness reporting.** A **last_backup_at** field per
    drive in **known_drives.json** records the ISO-8601 UTC timestamp
    of every successful backup. Reporting is purely informative —
    **backup** does not refuse to run on a drive that has not been
    backed up in a long time, because the actual divergence threshold
    depends on **sanoid** retention (which the operator can change)
    and a backup that has crossed the threshold may still succeed if
    some shared snapshot remains. When **syncoid** does abort, the
    existing divergence handling in **repair-divergent** takes over.

    The retention horizon is read at runtime from
    **/etc/sanoid/sanoid.conf** and computed as **max(daily,
    weekly\\*7, monthly\\*30)** over templates actually used by
    **\[rpool\\*\]** or **\[bpool\\*\]** sections. After a successful
    backup, two informative messages may appear after the **BACKUP
    COMPLETED** banner:

    1. If the selected drive was already past the retention horizon
       when this run started, a **WARN** explains the situation and
       points at **purge** + **prepare** (the only remediation that
       fully reinitializes a drive that has aged past its anchor),
       with an explicit note that **repair-divergent** does *not* fix
       staleness — it only fixes divergent datasets after a
       **syncoid** abort, which is a different problem.
    2. An **INFO** list shows other known drives whose age has
       reached the danger zone (**\\>= retention - 30** days), so the
       operator knows which drive to grab next.

    The field auto-populates on the first successful backup. Drives
    that have never been backed up since the field was introduced
    are silently skipped by the reporting. A failure to persist the
    timestamp at the end of a backup is a warn, not fatal — the
    backup data itself is already on the target.

    **--no-snapshot anchor check.** When **--no-snapshot** is in
    effect, **backup** does not take a fresh **sanoid** pass and
    relies on whatever snapshots already exist in source. If no
    recent source snapshot is found, **backup** emits a **WARN**
    that **syncoid** may abort with no shared anchor, then proceeds
    so **syncoid**'s own (more authoritative) error wins if it does
    fail.

    **--no-sync-snap.** **backup** invokes **syncoid** with
    **--no-sync-snap** for both **rpool** and **bpool** transfers,
    avoiding the rotation-warning cascade that occurred when more
    than one backup drive shared a source: **syncoid**'s default
    **pruneoldsyncsnaps** cleanup destroys the source's previous
    **\@syncoid_\<host\>_\*** snapshot after each transfer, but that
    snapshot may still be the anchor for another drive. Without
    creating its own anchor snapshots, **syncoid** uses the most
    recent existing snapshot in source — typically the
    **autosnap_\*** snapshots from step 6.

    **backup** refuses to run from an Ubuntu Live USB: the live filesystem
    is not the system the user means to back up, and confusing the two
    would destroy good data on the backup drive.

**purge** \[*device*\]
:   Securely retire a managed backup drive. Destroys the ZFS pool,
    overwrites the start and end of the device with random data to defeat
    casual recovery, wipes filesystem signatures with **wipefs**(8) and
    zaps the partition table with **sgdisk**(8). The device may be
    specified as a positional argument, otherwise **zark** asks
    interactively. The drive is also removed from **known_drives.json**.

## Recovery workflow

**recover**
:   Full bare-metal restoration of an encrypted Ubuntu-on-ZFS system from
    a backup drive. Must be run from an Ubuntu Live USB with the backup
    drive connected.

    The procedure scans for backup drives, imports the chosen pool,
    prompts for the rpool passphrase, partitions the internal disk as
    EFI + bpool + rpool, recreates **rpool** *with* native encryption to
    match the Ubuntu installer, raw-receives **ROOT** and **USERDATA**,
    restores the LUKS keystore zvol last (which is mandatory — restoring
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
    a */dev/disk/by-id/* path); when omitted, ZFS auto-scans for the pools.
    **chroot** refuses to run when **rpool** is already imported — if that
    is the running system you are already inside it, and if it is a
    leftover from a previous run, **clean** releases it first.

**repair-divergent**
:   Interactively review and repair backup datasets whose snapshot
    history has diverged from the source. Divergence usually appears
    when the backup drive has been disconnected for longer than the
    source pool's **sanoid**(8) retention policy, so the snapshot
    that was once shared between source and target has been pruned
    on the source. **backup** auto-resolves divergence silently when
    the affected dataset is under 64 MB (almost always system
    metadata that can be recreated from the next initial
    replication); larger datasets are left alone and **backup**
    aborts with a pointer to this command. No source data is ever
    touched.

    For every divergent dataset above 64 MB, **repair-divergent**
    prints a context block with the size, snapshot count and date
    range on the target, the most recent snapshot suffix shared
    with the source counterpart (or *none*), the child datasets
    summary and a one-line hint, and asks the operator to choose
    among **destroy**, **skip**, or **abort all**. Datasets above
    1 GiB additionally require typing the literal string **DESTROY**
    (case-sensitive) at a second prompt before being touched. If a
    **zfs destroy** invocation fails mid-flight (busy zvol, lock
    contention), the operator is asked once how to handle the rest
    of the run — the choice (**continue**, **abort**, or **keep state
    and abort**) sticks for the remainder of the session.

**finish**
:   Post-recovery finalisation, intended to be run *from inside the
    recovered system* on its first boot. Resets the hostid, refreshes the
    ZFS cachefile, ensures the ZFS systemd services are enabled and runs
    a final **update-grub**(8) and initramfs regeneration without the
    backup drive present, so the resulting **grub.cfg** is clean.

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

## Maintenance

**mount** \[*target*\]
:   Mount a backup pool for inspection, **chroot**(1) entry or manual
    recovery work. Imports the chosen pool with an alternate root of
    */mnt/zark/<poolname>/* and mounts every dataset there. Asks
    interactively whether to mount read-only (recommended, and the
    default) or read-write.

    With no argument, scans for connected backup drives. With the
    *target* **local** (aliases **system**, **rpool**) it instead mounts
    the **installed system's** top-level **rpool**/**bpool** from a live
    USB — useful for inspecting the local disk without a full **chroot**.

    The complementary command is **umount**.

**umount** \[*target*\]
:   Unmount a previously **mount**-ed backup pool. Walks the dataset tree
    in reverse, closes the LUKS keystore and exports the pool cleanly.

    With the *target* **local** (aliases **system**, **rpool**) it exports
    the installed system's pools mounted by **mount local**. As a safety
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
3.12, **zfsutils-linux**, **sanoid** (which provides **syncoid**),
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

**zfs**(8), **zpool**(8), **syncoid**(8), **sanoid**(8),
**cryptsetup**(8), **dracut**(8), **grub-install**(8),
**update-grub**(8)
