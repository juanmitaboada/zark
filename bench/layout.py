# Copyright 2026 Juanmi Taboada
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
The layout check: the installed system is the one Ubuntu's installer lays
out with encrypted ZFS (bpool, rpool with its LUKS keystore, encrypted
swap), read from the guest's own commands.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from bench.report import Check

# The guest's commands, by the name their output is filed under. Every one
# reads only; -H -p for tab-separated exact values.
COMMANDS: dict[str, str] = {
    "zpool-list": "zpool list -H -p -o name,health",
    "zpool-get": "zpool get -H -p all rpool bpool",
    "zfs-list": (
        "zfs list -H -p -t filesystem,volume -r "
        "-o name,type,mountpoint,encryptionroot,keystatus,keylocation,keyformat rpool bpool"
    ),
    "zfs-get": "zfs get -H -p -s local,received all",
    "findmnt": "findmnt -J -o TARGET,SOURCE,FSTYPE",
    "lsblk": "lsblk -J -o NAME,TYPE,FSTYPE,MOUNTPOINTS",
    "cryptsetup": "cryptsetup status keystore-rpool",
    "crypttab": "cat /etc/crypttab",
}

KEYSTORE = "rpool/keystore"
KEYSTORE_MOUNT = "/run/keystore/rpool"


@dataclass(frozen=True)
class Dataset:
    """One line of the zfs listing."""

    name: str
    type: str
    mountpoint: str
    encryptionroot: str
    keystatus: str
    keylocation: str
    keyformat: str


def parse_zfs_list(text: str) -> dict[str, Dataset]:
    """COMMANDS["zfs-list"] by dataset name."""
    out: dict[str, Dataset] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) == 7:
            out[fields[0]] = Dataset(*fields)
    return out


def parse_pools(text: str) -> dict[str, str]:
    """COMMANDS["zpool-list"] as {pool: health}."""
    return dict(ln.split("\t")[:2] for ln in text.splitlines() if ln.count("\t") >= 1)


def _walk(nodes: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])


def parse_mounts(text: str) -> dict[str, tuple[str, str]]:
    """findmnt's JSON as {target: (source, fstype)}; the last mount wins."""
    doc = json.loads(text)
    return {n["target"]: (n["source"], n["fstype"]) for n in _walk(doc.get("filesystems", []))}


def _mount_of(path: str, mounts: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    best = ""
    for target in mounts:
        inside = path == target or path.startswith(target.rstrip("/") + "/")
        if inside and len(target) > len(best):
            best = target
    return mounts.get(best) if best else None


def _check_pools(pools: dict[str, str]) -> Check:
    state = {p: pools.get(p, "absent") for p in ("rpool", "bpool")}
    ok = all(h == "ONLINE" for h in state.values())
    return Check("pools-online", ok, ", ".join(f"{p} {h}" for p, h in state.items()))


def _check_boot_environment(mounts: dict[str, tuple[str, str]]) -> list[Check]:
    root = mounts.get("/", ("", ""))
    boot = mounts.get("/boot", ("", ""))
    esp = mounts.get("/boot/efi", ("", ""))
    root_m = re.fullmatch(r"rpool/ROOT/ubuntu_(\w+)", root[0])
    boot_m = re.fullmatch(r"bpool/BOOT/ubuntu_(\w+)", boot[0])
    same = bool(root_m and boot_m and root_m.group(1) == boot_m.group(1))
    return [
        Check("root-dataset", bool(root_m) and root[1] == "zfs", f"/ is {root[0]} ({root[1]})"),
        Check("boot-dataset", bool(boot_m) and boot[1] == "zfs", f"/boot is {boot[0]} ({boot[1]})"),
        Check("boot-environment-pair", same, "the root and /boot carry the same id"),
        Check("esp", esp[1] == "vfat", f"/boot/efi is {esp[0]} ({esp[1]})"),
    ]


def _check_userdata(mounts: dict[str, tuple[str, str]], home: str) -> list[Check]:
    checks = []
    for path in ("/root", home):
        mount = _mount_of(path, mounts)
        source = mount[0] if mount else ""
        ok = source.startswith("rpool/USERDATA/") and mount is not None and mount[1] == "zfs"
        checks.append(Check(f"userdata {path}", ok, f"{path} is on {source or 'nothing'}"))
    return checks


def _check_encryption(datasets: dict[str, Dataset]) -> list[Check]:
    rpool = [d for d in datasets.values() if d.name.split("/")[0] == "rpool"]
    wrong = [
        d.name
        for d in rpool
        if d.name != KEYSTORE and (d.encryptionroot != "rpool" or d.keystatus != "available")
    ]
    top = datasets.get("rpool")
    keylocation = top.keylocation if top else ""
    keystore = datasets.get(KEYSTORE)
    return [
        Check(
            "rpool-encrypted",
            bool(rpool) and not wrong,
            "every rpool dataset under encryption root rpool with its key loaded"
            if not wrong
            else "not under rpool's key: " + ", ".join(sorted(wrong)),
        ),
        Check(
            "rpool-key-from-keystore",
            keylocation.startswith(f"file://{KEYSTORE_MOUNT}/"),
            f"keylocation {keylocation or 'absent'}, keyformat {top.keyformat if top else '-'}",
        ),
        Check(
            "keystore-zvol",
            keystore is not None and keystore.type == "volume",
            f"{KEYSTORE} is {keystore.type if keystore else 'absent'}",
        ),
    ]


def _check_keystore_mapping(cryptsetup: str, mounts: dict[str, tuple[str, str]]) -> Check:
    active = "is active" in cryptsetup
    mounted = KEYSTORE_MOUNT in mounts
    return Check(
        "keystore-mapping",
        active and mounted,
        f"keystore-rpool {'active' if active else 'not active'}, "
        f"{KEYSTORE_MOUNT} {'mounted' if mounted else 'not mounted'}",
    )


def _check_swap(lsblk: str, crypttab: str) -> Check:
    names = {ln.split()[0] for ln in crypttab.splitlines() if ln.strip() and ln[0] != "#"}
    swaps = [
        n["name"]
        for n in _walk(json.loads(lsblk).get("blockdevices", []))
        if n.get("type") == "crypt" and "[SWAP]" in (n.get("mountpoints") or [])
    ]
    ok = any(s in names for s in swaps)
    return Check("swap-encrypted", ok, f"crypt swap devices {swaps or 'none'}")


def check(outputs: dict[str, str], user: str) -> list[Check]:
    """Every layout verdict from the guest's outputs (keys of COMMANDS)."""
    mounts = parse_mounts(outputs["findmnt"])
    datasets = parse_zfs_list(outputs["zfs-list"])
    return [
        _check_pools(parse_pools(outputs["zpool-list"])),
        *_check_boot_environment(mounts),
        *_check_userdata(mounts, f"/home/{user}"),
        *_check_encryption(datasets),
        _check_keystore_mapping(outputs["cryptsetup"], mounts),
        _check_swap(outputs["lsblk"], outputs["crypttab"]),
    ]
