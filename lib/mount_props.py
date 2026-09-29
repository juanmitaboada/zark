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
Where a restored dataset's canmount/mountpoint come from (D6, P0-11).

Raw sends carry no properties, and a 1.0.12 destination holds wrong
values outside ``rpool/ROOT/<be>`` (e.g. ``canmount=on`` on containers
that are ``off`` in origin), so recover never copies them from the
destination. Sources, highest priority first:

  1. ``zark``     — ``org.zark:canmount`` / ``org.zark:mountpoint`` user
                    properties on the destination (written by M2 backups).
  2. ``cache``    — origin's ``/etc/zfs/zfs-list.cache/<pool>`` as found in
                    the boot environment's snapshot at the restore point
                    (OpenZFS zedlet; enabled on Ubuntu 26.04).
  3. ``ubuntu``   — the Ubuntu installer layout for the boot environment,
                    its known children, ``home_*``/``root_*`` and bpool.
  4. ``inferred`` — structure of the snapshot itself: a dataset whose root
                    directory is empty and that has children was never
                    mounted, so ``canmount=off``; otherwise ``on``. A
                    first-level dataset gets ``mountpoint=/<name>``,
                    anything deeper inherits.

The values are applied at receive time (``zfs receive -u -o ...``), never
with ``zfs set`` while a pool with zvols is imported.
"""

from dataclasses import dataclass

# Ubuntu installer layout under rpool/ROOT/<be>/ (24.04 – 26.04).
UBUNTU_ROOT_CHILDREN_OFF = frozenset({"usr", "var"})
UBUNTU_ROOT_CHILDREN_ON = frozenset(
    {
        "usr/local",
        "var/lib",
        "var/log",
        "var/mail",
        "var/snap",
        "var/spool",
        "var/www",
        "var/games",
        "var/lib/apt",
        "var/lib/dpkg",
        "var/lib/AccountsService",
        "var/lib/NetworkManager",
        "srv",
    },
)

# Datasets recover creates itself (not received), with these effective values.
# rpool's own mountpoint comes from rpool_root_mountpoint().
CREATED_CONTAINERS = {"rpool/ROOT": "none", "rpool/USERDATA": "none"}
BPOOL_CONTAINERS = {"bpool": "none", "bpool/BOOT": "none"}

# The Ubuntu installer creates rpool with canmount=off, mountpoint=/.
UBUNTU_RPOOL_MOUNTPOINT = "/"


@dataclass(frozen=True)
class MountProps:
    """Target properties of one restored dataset."""

    canmount: str
    mountpoint: str  # effective value; "" = unknown (inherit)
    source: str  # "zark" | "cache" | "ubuntu" | "inferred"


def parse_list_cache(text: str) -> dict[str, tuple[str, str]]:
    """``zfs-list.cache`` lines → {dataset: (canmount, mountpoint)}."""
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) >= 3 and fields[0]:
            out[fields[0]] = (fields[2], fields[1])
    return out


def rpool_root_mountpoint(cache: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """(mountpoint, source) for the pool root recover creates.

    ``none`` in origin's cache is not trusted: zark recover up to
    2.0.0-rc1 created rpool with mountpoint=none, so a system restored by it
    carries that value into its own cache and every later backup.
    """
    mountpoint = cache.get("rpool", ("", ""))[1]
    if mountpoint and mountpoint not in ("none", "legacy", "-"):
        return (mountpoint, "cache")
    return (UBUNTU_RPOOL_MOUNTPOINT, "ubuntu")


def ubuntu_layout(rel: str, be: str) -> tuple[str, str] | None:  # pylint: disable=too-many-return-statements
    """(canmount, mountpoint) from the Ubuntu layout, or None when unknown."""
    root = f"rpool/ROOT/{be}"
    if rel == root:
        return ("on", "/")
    if rel.startswith(f"{root}/"):
        child = rel[len(root) + 1 :]
        if child in UBUNTU_ROOT_CHILDREN_OFF:
            return ("off", f"/{child}")
        if child in UBUNTU_ROOT_CHILDREN_ON:
            return ("on", f"/{child}")
        return None
    if rel.startswith("rpool/USERDATA/home_"):
        return ("on", "/home")
    if rel.startswith("rpool/USERDATA/root_"):
        return ("on", "/root")
    if rel == f"bpool/BOOT/{be}":
        return ("on", "/boot")
    return None


def inherited(parent_effective: str, leaf: str) -> str:
    """Effective mountpoint a child inherits from its parent."""
    if parent_effective in ("none", "legacy", "-", ""):
        return parent_effective or "none"
    return f"{parent_effective.rstrip('/')}/{leaf}"


def choose(  # pylint: disable=too-many-arguments
    rel: str,
    be: str,
    *,
    zark: dict[str, tuple[str, str]],
    cache: dict[str, tuple[str, str]],
    empty_with_children: bool,
) -> MountProps:
    """Pick the properties of ``rel`` from the highest-priority source."""
    if rel in zark:
        return MountProps(zark[rel][0], zark[rel][1], "zark")
    if rel in cache:
        return MountProps(cache[rel][0], cache[rel][1], "cache")
    layout = ubuntu_layout(rel, be)
    if layout:
        return MountProps(layout[0], layout[1], "ubuntu")
    canmount = "off" if empty_with_children else "on"
    parts = rel.split("/")
    mountpoint = f"/{'/'.join(parts[1:])}" if len(parts) == 2 else ""
    return MountProps(canmount, mountpoint, "inferred")


def receive_options(props: MountProps, parent_effective: str, leaf: str) -> list[str]:
    """``zfs receive`` ``-o`` options that give ``props`` under that parent.

    canmount is always set explicitly. mountpoint is set only when it
    differs from what the dataset would inherit, so inheritance survives.
    """
    opts = [f"-o canmount={props.canmount}"]
    if props.mountpoint and props.mountpoint != inherited(parent_effective, leaf):
        opts.append(f"-o mountpoint={props.mountpoint}")
    return opts


def effective_mountpoint(props: MountProps, parent_effective: str, leaf: str) -> str:
    """Effective mountpoint after receive with :func:`receive_options`."""
    return props.mountpoint or inherited(parent_effective, leaf)
