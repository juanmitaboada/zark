# /boot on ext4: fixtures

Shaped as Ubuntu 26.10's installer leaves an encrypted ZFS install on UEFI
(eli, M12's L0 `c1-2610`): `fstab`, `crypttab` and `tune2fs -l` of `/boot`.
Every UUID, PARTUUID and the boot environment's id are replaced by fixed
values; nothing else is changed.
