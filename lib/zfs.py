"""
ZFS pool and dataset operations.

Wraps zfs/zpool commands with structured output and error handling.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from lib.log import Log
from lib.sh import run


@dataclass
class PoolInfo:  # pylint: disable=too-many-instance-attributes
    """Information about a ZFS pool."""

    name: str
    guid: str = ""
    health: str = ""
    size: str = ""
    used: str = ""
    avail: str = ""
    used_bytes: int = 0
    avail_bytes: int = 0
    pct_used: int = 0


@dataclass
class DatasetInfo:  # pylint: disable=too-many-instance-attributes
    """Information about a ZFS dataset."""

    name: str
    mountpoint: str = ""
    used: str = ""
    refer: str = ""
    canmount: str = ""
    keystatus: str = ""
    encryption: str = ""
    type: str = ""  # "filesystem" or "volume"


class ZFS:
    """ZFS operations wrapper."""

    def __init__(self, log: Log):
        self.log = log

    # ── Pool operations ──────────────────────────────────────────────────

    def pool_exists(self, name: str) -> bool:
        """Check if a pool is currently imported."""
        return run(f"zpool list {name}").ok

    def pool_info(self, name: str) -> PoolInfo | None:
        """Get pool info. Returns None if not imported."""
        r = run(f"zpool list -H -o name,guid,health,size,alloc,free {name}")
        if not r.ok:
            return None
        parts = r.output.split("\t")
        if len(parts) < 6:
            return None

        # Get byte-level usage for percentage
        rb = run(f"zfs list -H -o used,avail -p {name}")
        used_b = avail_b = 0
        if rb.ok:
            bp = rb.output.split("\t")
            if len(bp) >= 2:
                used_b = int(bp[0].strip())
                avail_b = int(bp[1].strip())

        total = used_b + avail_b
        pct = (used_b * 100 // total) if total > 0 else 0

        return PoolInfo(
            name=parts[0],
            guid=parts[1],
            health=parts[2],
            size=parts[3],
            used=parts[4],
            avail=parts[5],
            used_bytes=used_b,
            avail_bytes=avail_b,
            pct_used=pct,
        )

    def pool_import(
        self,
        name: str,
        device: str | None = None,
        altroot: str | None = None,
        no_mount: bool = False,
        force: bool = False,
    ) -> bool:
        """
        Import a pool. Tries multiple strategies:
          1. Auto-scan (no -d) — ZFS searches all devices
          2. -d /dev/disk/by-id/ — search by-id directory
          3. -d <device_directory> — search specific device's parent dir
          4. Force variants of all above
        """
        del force  # currently unused, but could be added as a param to try force first if desired
        if self.pool_exists(name):
            self.log.dbg(f"Pool {name} already imported")
            return True

        def _build_cmd(dev_arg: str = "", use_force: bool = False) -> str:
            cmd = "zpool import"
            if use_force:
                cmd += " -f"
            if no_mount:
                cmd += " -N"
            if altroot:
                cmd += f" -R {altroot}"
            if dev_arg:
                cmd += f" -d {dev_arg}"
            cmd += f" {name}"
            return cmd

        # Build list of -d candidates to try
        dev_args = [""]  # empty = auto-scan (try first)
        if device:

            # Add the directory containing the device (not the device itself)
            dev_dir = os.path.dirname(device)
            if dev_dir and dev_dir not in dev_args:
                dev_args.append(dev_dir)
            # Also try /dev/disk/by-id/ explicitly
            if "/dev/disk/by-id" not in dev_dir:
                dev_args.append("/dev/disk/by-id/")

        # Try each strategy, first without force, then with force
        last_err = ""
        for use_force in (False, True):
            for darg in dev_args:
                cmd = _build_cmd(darg, use_force)
                r = run(cmd, log=self.log)
                if r.ok:
                    self.log.ok(f"Pool {name} imported")
                    return True
                last_err = r.stderr.strip()

        self.log.error(f"Cannot import pool {name}: {last_err}")
        return False

    def pool_export(self, name: str) -> bool:
        """Export a pool safely."""
        if not self.pool_exists(name):
            return True

        r = run(f"zpool set cachefile=none {name}")
        run("sleep 1")
        r = run(f"zpool export {name}", log=self.log)
        if r.ok:
            self.log.ok(f"Pool {name} exported")
            run("sync")
            return True

        # Force export
        r = run(f"zpool export -f {name}", log=self.log)
        if r.ok:
            self.log.ok(f"Pool {name} exported (forced)")
            run("sync")
            return True

        self.log.warn(f"Could not export {name}")
        return False

    def pool_guid(self, name: str) -> str:
        """Get pool GUID (decimal string)."""
        r = run(f"zpool get -H -o value guid {name}")
        return r.output if r.ok else ""

    def pool_health(self, name: str) -> str:
        """Get pool health status (e.g. "ONLINE", "DEGRADED")."""
        r = run(f"zpool list -H -o health {name}")
        return r.output if r.ok else "UNKNOWN"

    def write_zpool_cache(self, cache_path: str, pools: list[str]) -> None:
        """
        Persist the given pools into a zpool.cache file at ``cache_path`` and
        then disconnect the pools from the cache so a subsequent export won't
        erase the saved entries.

        The dance is required because ``zpool export`` removes a pool from the
        active cachefile, but if we set ``cachefile=none`` first we lose the
        entries entirely. Workflow used by both ``recover`` and ``repair-boot`` when
        preparing a target system to boot:

        1. Point both pools at ``cache_path`` so their entries land in it.
        2. Copy ``cache_path`` aside.
        3. Set ``cachefile=none`` on both pools (so future export is a no-op
           on the cache).
        4. Restore the saved copy back to ``cache_path``.

        ``cache_path``'s parent directory is created if it does not exist.
        """
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        backup = f"{cache_path}.recover"
        for pool in pools:
            run(f"zpool set cachefile={cache_path} {pool}")
        run(f"cp {cache_path} {backup}")
        for pool in pools:
            run(f"zpool set cachefile=none {pool}")
        run(f"mv {backup} {cache_path}")

    # ── Dataset operations ───────────────────────────────────────────────

    def list_datasets(self, root: str, recursive: bool = True) -> list[DatasetInfo]:
        """List datasets under a root."""
        flag = "-r" if recursive else ""
        r = run(
            "zfs list -H -o name,mountpoint,used,refer,canmount,type "
            + f"-t filesystem,volume {flag} {root}",
        )
        if not r.ok:
            return []

        result = []
        for line in r.lines:
            parts = line.split("\t")
            if len(parts) >= 6 and "@" not in parts[0]:
                result.append(
                    DatasetInfo(
                        name=parts[0],
                        mountpoint=parts[1],
                        used=parts[2],
                        refer=parts[3],
                        canmount=parts[4],
                        type=parts[5],
                    ),
                )
        return result

    def list_snapshots(self, root: str, pattern: str = "") -> list[str]:
        """List snapshot names under a root, optionally filtered."""
        r = run(f"zfs list -H -o name -t snapshot -r {root}")
        if not r.ok:
            return []
        snaps = r.lines
        if pattern:
            snaps = [s for s in snaps if pattern in s]
        return snaps

    def release_all_holds(self, root: str) -> int:
        """Release every hold on every snapshot under ``root`` (recursive).

        Used before ``zfs destroy -r`` on a dataset whose snapshots may have
        holds placed by ``syncoid --use-hold``. Without this, the destroy
        fails with "dataset is busy". Returns the number of holds released.

        Resilient to partial failures: if individual ``zfs release`` calls
        fail, the remaining ones are still attempted. The caller should
        retry the destroy and react to its result, not to this count.
        """
        # `zfs holds -r -H` outputs three tab-separated columns per held
        # snapshot: NAME, TAG, TIMESTAMP. One row per (snapshot, tag) pair.
        r = run(f"zfs holds -r -H {root}")
        if not r.ok or not r.lines:
            return 0

        released = 0
        for line in r.lines:
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            snap, tag = parts[0], parts[1]
            if run(f"zfs release {tag} {snap}").ok:
                released += 1
        return released

    def unique_snap_names(self, root: str, pattern: str = "autosnap") -> list[str]:
        """Get unique snapshot timestamps sorted."""
        snaps = self.list_snapshots(root, pattern)
        names = sorted({s.split("@")[1] for s in snaps if "@" in s})
        return names

    def dataset_exists(self, name: str) -> bool:
        """Check if a dataset exists. Note: this does not check if it's mounted."""
        return run(f"zfs list {name}").ok

    def get_property(self, dataset: str, prop: str) -> str:
        """Get a specific property value for a dataset. Returns empty string if not found."""
        r = run(f"zfs get -H -o value {prop} {dataset}")
        return r.output if r.ok else ""

    def set_property(self, dataset: str, prop: str, value: str) -> bool:
        """Set a specific property value for a dataset. Returns True if successful."""
        r = run(f"zfs set {prop}={value} {dataset}", log=self.log)
        return r.ok

    def load_key(self, dataset: str, keyfile: str) -> bool:
        """Load the encryption key for a dataset from a file. Returns True if successful."""
        r = run(f"zfs load-key -L file://{keyfile} {dataset}", log=self.log)
        return r.ok

    def datasets_needing_key(self, root: str) -> list[str]:
        """
        Return datasets under `root` whose key is not yet loaded.

        Reads `zfs get -H keystatus -r <root>` and selects every non-snapshot
        dataset (no '@' in the name) with keystatus == 'unavailable'. Used by
        every caller that needs to walk an encryption hierarchy and load keys.
        """
        r = run(f"zfs get -H keystatus -r {root}")
        if not r.ok:
            return []
        out: list[str] = []
        for line in r.lines:
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].strip() == "unavailable" and "@" not in parts[0]:
                out.append(parts[0].strip())
        return out

    def load_keys_recursive(self, root: str, keyfile: str) -> int:
        """Load keys for all encrypted datasets under `root`. Returns count loaded."""
        count = 0
        for ds in self.datasets_needing_key(root):
            if self.load_key(ds, keyfile):
                count += 1
        return count

    def mount_dataset(self, dataset: str, mountpoint: str) -> bool:
        """Mount a specific dataset at a specific path (safe, explicit)."""
        run(f"mkdir -p {mountpoint}")
        r = run(f"mount -t zfs {dataset} {mountpoint}", log=self.log)
        return r.ok

    def mount_all(self) -> bool:
        """Mount all mountable datasets. Use with care."""
        r = run("zfs mount -a", log=self.log)
        return r.ok

    def unmount_all(self) -> bool:
        """Unmount all datasets. Use with care (e.g. before exporting a pool)."""
        r = run("zfs unmount -a", log=self.log)
        return r.ok

    # ── Discovery (no pool import needed) ────────────────────────────────

    def scan_zfs_members(self) -> list[dict]:
        """
        Scan all ZFS member partitions via blkid.
        Returns list of {devname, uuid, label} dicts.
        """
        r = run("blkid -t TYPE=zfs_member -o export")
        if not r.ok:
            return []

        results = []
        current: dict = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                if current:
                    results.append(current)
                    current = {}
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                current[key.lower()] = val
        if current:
            results.append(current)

        return results

    def importable_pools(self) -> list[dict[str, str]]:
        """List pools available for import."""
        r = run("zpool import")
        if not r.ok:
            return []
        # Parse output — simplistic but functional
        pools: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("pool:"):
                if current:
                    pools.append(current)
                current = {"name": line.split(":", 1)[1].strip()}
            elif line.startswith("id:"):
                current["guid"] = line.split(":", 1)[1].strip()
            elif line.startswith("state:"):
                current["state"] = line.split(":", 1)[1].strip()
        if current:
            pools.append(current)
        return pools


# ── grub.cfg helpers ─────────────────────────────────────────────────────

# In Ubuntu ZFS systems, every `search --no-floppy --fs-uuid --set=NAME HEX`
# line in /boot/grub/grub.cfg points to the bpool: rpool is referenced via
# the `root=ZFS=...` kernel param, ESP via /etc/fstab — neither uses fs-uuid
# in grub. So replacing all such hex values with the new bpool GUID is safe.
#
# `update-grub` (10_linux_zfs) emits `--set=root` on real Ubuntu hardware.
# Manual / test configs may use `--set=boot_fs`. The regex matches any
# `--set=<name>` so both work, and restricts to 16-hex-char strings (the
# fixed format of a ZFS pool GUID) to avoid catching unrelated tokens.
_GRUB_FS_UUID_RE = re.compile(r"(fs-uuid\s+--set=\w+\s+)([a-f0-9]{16})")


def fix_grub_bpool_uuid(grub_cfg: Path, new_bpool_hex: str, log: Log) -> bool:
    """
    Rewrite every `fs-uuid --set=<name> <hex>` line in grub_cfg so the hex
    matches the freshly-recreated bpool's GUID.

    The bpool is created from scratch during recovery, so it gets a new GUID,
    but the grub.cfg restored via bpool send/receive still references the
    OLD source bpool GUID. This helper rewrites every stale fs-uuid reference
    in the file to point at the new bpool.

    Returns True if grub.cfg existed and was processed (modified or already
    correct); False if grub.cfg was missing.
    """
    if not grub_cfg.exists():
        log.warn(f"{grub_cfg} not found — bpool content was not restored")
        return False

    content = grub_cfg.read_text()
    matches = _GRUB_FS_UUID_RE.findall(content)
    if not matches:
        log.warn(f"No fs-uuid --set= references in {grub_cfg.name} " "— bpool UUID not updated")
        return True

    old_uuids = {hex_ for _, hex_ in matches if hex_ != new_bpool_hex}
    if not old_uuids:
        log.dbg(f"grub.cfg bpool UUID already correct: {new_bpool_hex}")
        return True

    # Substitute only hex values that follow `fs-uuid --set=...`. Using
    # re.sub instead of str.replace keeps us from touching any unrelated
    # 16-hex tokens that might appear elsewhere in the file.
    def _sub(match: re.Match) -> str:
        prefix, old_hex = match.group(1), match.group(2)
        if old_hex == new_bpool_hex:
            return match.group(0)
        return f"{prefix}{new_bpool_hex}"

    grub_cfg.write_text(_GRUB_FS_UUID_RE.sub(_sub, content))

    old_list = ", ".join(sorted(old_uuids))
    log.ok(f"Fixed bpool UUID in grub.cfg: {old_list} → {new_bpool_hex}")
    return True
