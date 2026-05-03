% ZARK(1) zark @VERSION@ | User Commands
%
% May 3, 2026

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

The thirteen commands are grouped here by purpose. Within each group the
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

## Backup workflow

**setup**
:   One-time installation of the runtime dependencies and configuration of
    **sanoid**(8) snapshot policies. Installs the packages listed in the
    **DEPENDENCIES** section, drops a **sanoid** template tuned for
    Ubuntu-on-ZFS into */etc/sanoid/*, and registers the **sanoid** systemd
    timer so snapshots are taken automatically.

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

**backup**
:   Run an incremental backup to the connected, registered backup drive.
    Auto-detects which known drive is plugged in, imports the pool,
    triggers a **sanoid**(8) snapshot pass on the source, and uses
    **syncoid**(8) raw send to transfer the new snapshots. Then synchronises
    **bpool**, exports the backup pool cleanly and (if the desktop
    environment is running) emits a **notify-send**(1) summary.

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

**repair**
:   Fix a broken boot chain on a system whose ZFS pools are intact.
    Imports **rpool** and **bpool** under an alternate root, mounts the
    affected system, regenerates **grub.cfg** and the initramfs and
    cleanly exports the pools. The most common reason to need this is
    that **update-grub**(8) ran while a **zark** backup drive was
    connected, polluting **grub.cfg** with backup-pool UUIDs.

**finish**
:   Post-recovery finalisation, intended to be run *from inside the
    recovered system* on its first boot. Resets the hostid, refreshes the
    ZFS cachefile, ensures the ZFS systemd services are enabled and runs
    a final **update-grub**(8) and initramfs regeneration without the
    backup drive present, so the resulting **grub.cfg** is clean.

**simulate** \[*device*\] \[**--ro**\]
:   Boot a recovered (or live) disk in **qemu-system-x86_64**(1) under
    OVMF UEFI firmware to verify the boot chain without rebooting the
    physical machine. With no arguments, boots */dev/nvme0n1*; pass a
    different **/dev/...** path to override.

    With **--ro**, QEMU is started with **-snapshot**, so writes are
    discarded at shutdown and the underlying disk is never modified — the
    recommended mode for verifying that a freshly restored system can
    actually boot. Without **--ro**, QEMU writes to the physical disk
    just like a real boot would; this is occasionally useful (for
    example, to allow first-boot self-healing of the hostid issue
    described in **NOTES**) but should be reached for deliberately.

    Requires the **qemu-system-x86** and **ovmf** packages, both of which
    are listed in *Recommends* and which **simulate** offers to install
    on first use.

## Maintenance

**mount**
:   Mount a backup pool for inspection, **chroot**(1) entry or manual
    recovery work. Imports the chosen pool with an alternate root of
    */mnt/zark/<poolname>/* and mounts every dataset there. Asks
    interactively whether to mount read-only (recommended, and the
    default) or read-write.

    The complementary command is **umount**.

**umount**
:   Unmount a previously **mount**-ed backup pool. Walks the dataset tree
    in reverse, closes the LUKS keystore and exports the pool cleanly.

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

    sudo zark simulate --ro

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

*/var/log/zark.log*
:   Log file for system installs. Portable installs write
    *zark.log* alongside the entry-point script instead, so the log
    follows the pendrive.

*/mnt/zark/*
:   Alternate-root mountpoint base used by **mount** and (transiently) by
    **recover** and **repair**.

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

**recover** and **repair** must run from a live environment. Importing
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

The **prepare** command installs */etc/grub.d/09_zfs_backup_guard*, which
aborts **update-grub**(8) when a registered **zark** backup drive is
connected. This prevents the most common cause of post-update boot
failure: **update-grub** picking up the backup pool and writing its
UUIDs into **grub.cfg**.

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

Verify the recovery in QEMU before rebooting, without writing to disk:

    sudo zark simulate --ro

# BUGS

Report bugs at https://github.com/juanmitaboada/zark/issues.

# AUTHOR

Juanmi Taboada (juanmi@juanmitaboada.com)

# SEE ALSO

**zfs**(8), **zpool**(8), **syncoid**(8), **sanoid**(8),
**cryptsetup**(8), **dracut**(8), **grub-install**(8),
**update-grub**(8)
