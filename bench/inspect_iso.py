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
Reading the live ISO without booting it (probe mode): its casper layers,
the installer snap inside them, cloud-init's configuration and the live's
installed packages.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

SNAP_PREFIX = "var/lib/snapd/seed/snaps/ubuntu-desktop-bootstrap_"
SUBIQUITY_FILE = "subiquity/server/controllers/filesystem.py"
CLOUD_FILES = ("etc/cloud/cloud.cfg", "etc/cloud/cloud.cfg.d", "etc/cloud/ds-identify.cfg")
DPKG_STATUS = "var/lib/dpkg/status"
# The packages of the tools zark recover needs in the live (its
# commands/recover.py:174-181 at 66cce62); recover installs a missing one
# with apt-get install, without apt-get update (:184-190).
RECOVER_PACKAGES = ("zfsutils-linux", "gdisk", "cryptsetup-bin", "dosfstools", "parted")
# A snap keeps its metadata in meta/; snap/ holds hooks and the like.
SNAP_YAML = "meta/snap.yaml"


def squashfs_names(find_output: str) -> list[str]:
    """The layer file names in ``xorriso -find /casper -name '*.squashfs'``'s output."""
    names = set()
    for line in find_output.splitlines():
        path = line.strip().strip("'")
        if path.endswith(".squashfs"):
            names.add(path.rsplit("/", 1)[-1])
    return sorted(names)


def wanted_from_layer(path: str) -> bool:
    """A file the probe reads out of a live layer."""
    snap = path.startswith(SNAP_PREFIX) and path.endswith(".snap")
    return path == DPKG_STATUS or path.startswith(CLOUD_FILES) or snap


def wanted_from_snap(path: str) -> bool:
    """A file the probe reads out of the installer snap."""
    return path.endswith(SUBIQUITY_FILE) or path == SNAP_YAML


def layer_chain(top: str, available: list[str]) -> list[str]:
    """
    The casper layers a live session stacks, lowest first: for
    ``layerfs-path=minimal.standard.live.squashfs`` that is ``minimal``,
    ``minimal.standard`` and ``minimal.standard.live``, each present.
    """
    name = top.removesuffix(".squashfs")
    parts = name.split(".")
    chain = [".".join(parts[: i + 1]) + ".squashfs" for i in range(len(parts))]
    return [layer for layer in chain if layer in available]


def layerfs_path(kernel_args: list[str]) -> str:
    """The ``layerfs-path=`` value among the kernel arguments, or ""."""
    for arg in kernel_args:
        if arg.startswith("layerfs-path="):
            return arg.split("=", 1)[1]
    return ""


def default_chain(available: list[str]) -> list[str]:
    """
    Without ``layerfs-path=``: the longest name containing ``.live``, else
    the longest name, and its prefixes.
    """
    live = [a for a in available if ".live" in a] or available
    if not live:
        return []
    top = max(live, key=lambda a: (a.count("."), a))
    return layer_chain(top, available)


def unsquashfs_list_cmd(image: Path) -> list[str]:
    """unsquashfs listing an image's paths, read-only."""
    return ["unsquashfs", "-l", "-no-progress", str(image)]


def unsquashfs_extract_cmd(image: Path, inner: str, dest: Path) -> list[str]:
    """
    unsquashfs writing one path out of an image. ``-no-xattrs``: the live's
    layers carry trusted.overlay.* attributes, which only root may write; as
    the runner user unsquashfs then exits 2. The probe reads files, never
    their attributes.
    """
    return ["unsquashfs", "-f", "-no-progress", "-no-xattrs", "-d", str(dest), str(image), inner]


def snap_version(snap_yaml: str) -> str:
    """The top-level ``version:`` of a snap's ``meta/snap.yaml``, or ""."""
    for line in snap_yaml.splitlines():
        if line.startswith("version:"):
            return line.partition(":")[2].strip().strip("'\"")
    return ""


def listed_paths(unsquashfs_list: str) -> list[str]:
    """``unsquashfs -l`` output as paths without the ``squashfs-root/`` prefix."""
    out = []
    for line in unsquashfs_list.splitlines():
        if line.startswith("squashfs-root/"):
            out.append(line.removeprefix("squashfs-root/"))
    return out


def package_installed(status: str, package: str) -> str:
    """The version of ``package`` when dpkg's status says it is installed, else ""."""
    for stanza in re.split(r"\n\s*\n", status):
        fields = dict(
            (k.strip(), v.strip())
            for k, _, v in (ln.partition(":") for ln in stanza.splitlines() if ln and ln[0] != " ")
        )
        if fields.get("Package") == package and fields.get("Status") == "install ok installed":
            return fields.get("Version", "?")
    return ""


def datasource_lines(cloud_text: str) -> list[str]:
    """The lines of cloud-init's configuration that choose datasources."""
    keys = ("datasource_list", "datasource:", "policy:", "ds-identify")
    return [ln.rstrip() for ln in cloud_text.splitlines() if any(k in ln for k in keys)]


def git_blob_id(data: bytes) -> str:
    """The id git gives a file's content, to compare with a tag's blob."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data, usedforsecurity=False).hexdigest()
