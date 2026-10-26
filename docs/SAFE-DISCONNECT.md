# Safe USB disconnect

Why zark flushes and verifies a backup drive before it can be unplugged, and which USB bridges to avoid.

## Safe USB disconnect

ZFS issues writes with FUA (Force Unit Access) on critical metadata - uberblocks and the four redundant pool labels - meaning "do not acknowledge until this byte is on persistent media, not in volatile cache." Many cheap USB-SATA bridge chipsets ignore FUA and acknowledge from internal DRAM. ZFS believes the write is committed and `zpool export` reports success while the bridge still holds dirty pages; if the operator unplugs at that moment the kernel emits a last-ditch `SYNCHRONIZE CACHE` over the disconnecting cable, it fails with `DID_ERROR`, and the pending writes - possibly including the labels - are lost. Result: the pool comes back **FAULTED** on next import with `failed to unpack label 0/1/2/3`. Unrecoverable; not even `zpool import -FX` brings it back.

zark protects against this in two layers at every point where a command finishes with a pool exported:

1. **`sync(2)` + 2-second pause** - pushes the kernel page cache and dirty block-device buffers to the device, then gives the bridge firmware time to drain its internal queue. Always runs, no prompt.
2. **Interactive `eject` prompt** - `eject(1)` issues SCSI `SYNCHRONIZE CACHE (0x35)` followed by `START STOP UNIT (stop=1)`. SYNCHRONIZE CACHE is the device's most authoritative flush primitive (the bridge sees it as a distinguished operation, distinct from inline FUA - most chipsets that cheat on FUA still honour it). STOP UNIT then powers the controller down.

The eject is **never automatic**. After the success banner, zark asks:

```
Eject drive 'backup' now? (powers the device down) [Y/n]:
```

with a command-specific default chosen to match the typical next step:

| Command            | Default eject | Why |
|--------------------|---------------|-----|
| `backup`           | yes           | Typical: done backing up, unplug. |
| `umount`           | yes           | Operator signalled intent to disconnect by running `umount`. |
| `purge`            | yes           | Drive is being retired or repurposed. |
| `recover`          | yes           | Next step is unplug + reboot. |
| `prepare`          | no            | Canonical follow-up is `backup` against the same drive. |
| `repair-boot`      | n/a           | No removable drive involved; no prompt. |

If the prompt is answered without input (script, cron, systemd timer), the default applies. There is no `--eject` / `--no-eject` flag - the prompt with a sensible default is the only knob.

zark then emits one of two banners depending on the answer:

```
╔══════════════════════════════════════════════════════════╗
║  💾  Safe to unplug drive 'backup'                       ║      ← after eject
╚══════════════════════════════════════════════════════════╝

╔══════════════════════════════════════════════════════════╗
║  🔌  Drive 'backup' flushed, still attached              ║      ← after declining eject
╚══════════════════════════════════════════════════════════╝
```

The colours and icons are deliberately disjoint so the two states are never confused.

**Side-effect of STOP UNIT**: after a successful eject the drive **disappears from `/dev`** until physically replugged. This is intentional and lines up with the workflow. If a follow-up command on the same drive is planned, answer `n` and the drive stays in `/dev` - the kernel-side flush has still run, so a clean `umount` or another zark command later will prompt again.

`repair-boot` does not prompt: it touches only the internal rpool/bpool on the system disk, never a removable drive, and refuses to run with external pools imported.

### Read-back verification

A clean `zpool export` is not proof that the pool survived. The same FUA-lying bridges can also lose **spacemaps** while the labels persist, so the pool scans as `ONLINE` but fails a real open deep in `vdev_load` with `metaslab_init failed [error=52]` - discovered only when you finally need the backup. To catch this immediately, `backup` re-imports the pool **read-only** after export (dropping the page cache first so the read comes from the device, not RAM) and requires `ONLINE` before declaring the backup safe. If the re-import fails, or the pool comes back with errors, it prints **`BACKUP NOT VERIFIED`** and stops short of the safe-to-unplug prompt: the data is not trustworthy even though the transfer and the export reported success. This is always on.

### Known-bad enclosures and the UAS quirk

The bridge chipsets that cause all of the above are documented, with their USB IDs, exact kernel signatures, and the system-level `usb-storage` quirk that forces the conservative transport, in [**`docs/HARDWARE.md`**](HARDWARE.md). If you hit `BACKUP NOT VERIFIED`, an import that fails with `insufficient replicas` on a healthy-looking drive, or `Synchronize Cache(10) failed: DID_ERROR` under load, read that first.

To check a drive's risk factors up front without writing anything, run `zark health [/dev/sdX]`. It is fully interactive: it asks whether you want a read-only check (default) or a destructive write-and-verify test, gathers any further choices, then runs unattended. The read-only check shows the bridge's reported cache semantics and the active USB transport (UAS vs usb-storage) as information, warns when the bridge model is on the known-problematic list, and reports the health of any pool on the drive without importing it. The destructive test creates a throwaway pool (under zark's own altroot, destroyed read-write afterwards), writes with transaction churn (fast ~2 GB, medium ~15 GB, or whole-disk "surface"), and re-imports to confirm the bridge actually persisted the data; an optional cold pass powers the device down and has you physically reconnect it before re-importing, so the read-back comes strictly from NAND. `prepare` runs the same read-only check before doing any work and asks for confirmation when the bridge is a known-problematic one. Whenever a risk or test failure is found, a diagnostic report is written to `/tmp` with instructions for filing a GitHub issue. Note that a non-destructive check can only flag *risk*: it cannot prove a bridge is honest, since that only shows under write load. The authoritative proof is the read-back that `prepare`, `backup`, and the destructive test perform after writing.
