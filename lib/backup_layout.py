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
What a backup pool holds, read the same way by ``recover`` and ``mount``.

A backup pool mirrors origin under ``<pool>/rpool`` and ``<pool>/bpool``;
the helpers here list it, find its boot environment and read the mount
properties recorded on it (``org.zark:*``, M2+). The values that decide
where each dataset goes come from :mod:`lib.mount_props`.
"""

from lib.sh import run


def list_datasets(pool: str) -> dict[str, str]:
    """{relative name: type} under <pool>/rpool and <pool>/bpool."""
    out: dict[str, str] = {}
    for root in (f"{pool}/rpool", f"{pool}/bpool"):
        r = run(f"zfs list -Hp -o name,type -r {root}")
        for line in r.lines if r.ok else []:
            fields = line.split("\t")
            if len(fields) == 2 and fields[0].startswith(f"{pool}/"):
                out[fields[0][len(pool) + 1 :]] = fields[1]
    return out


def find_be(pool: str) -> str:
    """Boot environment name: the first child of <pool>/rpool/ROOT."""
    for line in run(f"zfs list -H -o name -r {pool}/rpool/ROOT").lines:
        ds = line.strip()
        if ds.count("/") == 3 and "@" not in ds and ".archived-" not in ds:
            return ds.split("/")[-1]
    return ""


def zark_props(pool: str) -> dict[str, tuple[str, str]]:
    """org.zark:canmount / org.zark:mountpoint recorded on the destination (M2+)."""
    found: dict[str, dict[str, str]] = {}
    for root in (f"{pool}/rpool", f"{pool}/bpool"):  # one query each: bpool may be absent
        r = run(
            "zfs get -Hp -s local -o name,property,value "
            + f"org.zark:canmount,org.zark:mountpoint -r {root}",
        )
        for line in r.lines if r.ok else []:
            fields = line.split("\t")
            if len(fields) == 3 and fields[0].startswith(f"{pool}/"):
                found.setdefault(fields[0][len(pool) + 1 :], {})[fields[1]] = fields[2]
    return {
        ds: (p["org.zark:canmount"], p["org.zark:mountpoint"])
        for ds, p in found.items()
        if "org.zark:canmount" in p and "org.zark:mountpoint" in p
    }


def root_is_empty(source: str, probe_dir: str, *, zfsutil: bool) -> bool | None:
    """True when the root directory of ``source`` has no entries; None if unreadable.

    ``source`` is a snapshot (mounted as legacy) or, with ``zfsutil``, a
    filesystem; it is mounted read-only at ``probe_dir`` and unmounted.
    """
    _ = run(f"mkdir -p {probe_dir}")
    opts = "ro,zfsutil" if zfsutil else "ro"
    if not run(f"mount -t zfs -o {opts} {source} {probe_dir}").ok:
        return None
    try:
        r = run(f"find {probe_dir} -mindepth 1 -maxdepth 1 -print -quit")
        return r.ok and not r.output
    finally:
        _ = run(f"umount {probe_dir}")
