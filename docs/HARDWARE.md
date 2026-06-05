# Hardware notes

This document records hardware behaviours that materially affect ZFS backup
integrity, and the mitigations zark relies on. It exists because some USB-SATA
enclosures misreport cache semantics in ways that can silently corrupt a pool
on a path that otherwise looks completely successful.

## USB-SATA bridges that lie about cache flushing (FUA)

### Symptom

Some USB-SATA bridge chipsets advertise that they do **not** support FUA
(Forced Unit Access) / DPO, and do not honour cache-flush semantics reliably.
The kernel reports this at attach time:

```
sd X:0:0:0: [sdX] Write cache: enabled, read cache: enabled, doesn't support DPO or FUA
```

Such a bridge can acknowledge a cache-flush command (and therefore let
`zpool export` return success) while the data backing that flush never actually
reached the SSD's NAND. The pool's labels and uberblocks — written at fixed
positions — may survive, but the **spacemaps** written during the closing burst
of a transfer do not. The pool then looks exported-OK but is no longer
importable.

### Failure signature

A later import fails deep inside `vdev_load`, after the uberblock and MOS have
loaded successfully:

```
spa_load(<pool>, config trusted): using uberblock with txg=<N>
...
disk vdev '...': metaslab_init failed [error=52]
disk vdev '...': vdev_load: metaslab_init failed [error=52]
spa_load(<pool>, config trusted): FAILED: vdev_load failed [error=52]
```

`zpool import` (scan only, reading labels) reports the pool **ONLINE**, which
is misleading — the labels are intact. The real failure only appears on a full
open. `error=52` is `EBADE` ("Invalid exchange"); `zdb -e <pool>` surfaces the
same as `can't open '<pool>': Invalid exchange`.

Because **every** uberblock in the ring references the same corrupt
`metaslab_array`, rewind recovery (`-F`, `-FX`, explicit `-t <txg>`) cannot
help: there is no earlier consistent transaction to roll back to. The pool must
be recreated from the source.

This is distinct from the catastrophic "all four labels lost" mode. Here the
labels are fine and only the allocation metadata is gone.

### A second trap: the bogus shared WWN alias

The same class of bridge can expose a generic, non-unique WWN to all drives it
hosts, e.g. `wwn-0x5000000000000001`. Two different SSDs in two such enclosures
then present the **same** `wwn-0x...` / `scsi-3...` identifier. Consequences:

- `zpool import` scanning a directory may resolve the vdev through this alias,
  which points at the **whole disk** rather than the labelled `-part1`, and so
  fails to find the pool.
- Two drives sharing one WWN cannot be told apart by that identifier if both
  are connected.

zark avoids this by always importing via the **exact** device path
(`/dev/disk/by-id/<model>_<serial>-...-part1`), which embeds the unique serial
and targets the correct partition. See `pool_import` in `lib/zfs.py`.

### Distinguishing healthy from broken at disconnect

The kernel always attempts a final cache flush when a USB device leaves the
bus. On these bridges that final flush often *fails* — but the `hostbyte`
distinguishes harmless from harmful:

| Context | Transport | `hostbyte` at flush | Meaning |
|---|---|---|---|
| During operation, under write load | UAS | `DID_ERROR` (+ `uas_eh_*` resets) | Transport stalled with dirty data in flight — this is what corrupts |
| At unplug, pool already exported | usb-storage (quirked) | `DID_NO_CONNECT` | Device simply gone; nothing dirty pending — harmless |

`Synchronize Cache(10) failed` on its own is **not** a usable health signal: it
appears on every clean disconnect too. The only reliable signal is whether the
pool **reads back**. zark therefore re-imports the pool read-only after export
and requires `ONLINE` before reporting the backup safe (see
`verify_exported_pool_readback` in `lib/zfs.py`).

## Mitigation 1 — read-back verification (always on)

After exporting, zark drops the page cache and re-imports the pool read-only,
no-mount, by the exact device, and requires health `ONLINE`. If the re-import
fails (the `error=52` signature) the backup is reported **NOT VERIFIED** and
the operator is told not to rely on it. This converts a silent, deferred loss
into an immediate, visible failure. It does not *prevent* the bridge lying — it
*detects* it while you can still act.

## Mitigation 2 — force usb-storage instead of UAS (system-level)

For a known-bad bridge you can disable the UAS driver for that specific
VID:PID, forcing the more conservative `usb-storage` transport. This is a
**system** change, outside zark's portable scope, but it is the most effective
prevention.

Observed offending bridge: `idVendor=0634 idProduct=5604`
(seen on Micron CT2000X10PROSSD9 in USB-SATA enclosures).

```sh
# /etc/modprobe.d/uas-quirk.conf
options usb-storage quirks=0634:5604:u
```

Then reload the modules (with the enclosure disconnected):

```sh
sudo modprobe -r uas
sudo modprobe -r usb_storage
sudo modprobe usb_storage
sudo modprobe uas
```

Reconnect and confirm the kernel chose usb-storage:

```
usb X-X: UAS is ignored for this device, using usb-storage instead
usb-storage X-X:1.0: Quirks match for vid 0634 pid 5604: 800000
scsi hostN: usb-storage
```

If you instead see `scsi hostN: uas`, the quirk did not take — recheck the file
and that the modules were reloaded after the device was unplugged.

With the quirk in place, the bridge stops stalling under write load: the
disconnect flush degrades from `DID_ERROR` to a harmless `DID_NO_CONNECT`, and
post-export read-back succeeds. **Without** the quirk, a backup can still be
corrupted under load, which is exactly why Mitigation 1 runs unconditionally.

### Replace the enclosure if it keeps happening

A bridge that lies about FUA is fundamentally unsafe for backup storage. If a
particular enclosure repeatedly produces `error=52`, the durable fix is to
retire that enclosure (or move the SSD to a direct SATA connection), not to
keep relying on the quirk alone.

### Evaluating an enclosure with `zark health`

To assess an enclosure before trusting it, run `zark health` and choose the
read-only check for a quick risk read, or the destructive write-and-verify
test on a blank drive to reproduce the failure deliberately. The destructive
test writes with transaction churn and re-imports the pool; its **cold** pass
(power down, physically reconnect, re-import) is the most faithful — it forces
the read-back to come from NAND rather than the bridge's DRAM, which is where
a lying bridge would otherwise hide a not-yet-persisted write. A `FAIL` there
is strong evidence the enclosure should not be used for backups. When a risk
or failure is found, `zark health` writes a diagnostic report to `/tmp` with
the bridge's USB VID:PID and instructions for filing an issue so the bridge
can be catalogued in future versions.
