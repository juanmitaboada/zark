# /boot on ext4: fixtures

Shaped as Ubuntu 26.10's installer leaves an encrypted ZFS install on UEFI
(eli, M12's L0 `c1-2610`): `fstab`, `crypttab` and `tune2fs -l` of `/boot`.
Every UUID, PARTUUID and the boot environment's id are replaced by fixed
values; nothing else is changed.

`grub.cfg` is `/boot/grub/grub.cfg` reduced to the lines zark reads and
rewrites (the header's and two entries' `search` lines, their `linux`
lines), plus one `40_custom` entry that searches another file system's
UUID and must stay as it is; `esp-grub.cfg` is the ESP's
`EFI/ubuntu/grub.cfg` (the installer leaves a space at the end of its first line,
left out here for the repository's whitespace hook).

`fstab-selectors` and `crypttab-selectors` are `fstab` and `crypttab` with
one line added each, not from `c1-2610`: a file system at `/srv` named by
`/boot`'s old UUID, and a second encrypted device by `PARTUUID=` without
`swap`. The rewrites must leave both lines as they are (M13R-9).
