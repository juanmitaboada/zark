#!/bin/bash
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
#
# Prepares a GitHub-hosted runner for the VM bench (docs/BENCH.md): frees
# disk, installs what the bench runs, opens /dev/kvm to the runner user,
# and prints where the work directory is. Shell only here; everything else
# is bench/ in Python.
#
# Usage: bench/prepare-runner.sh
# Writes the chosen work directory to $GITHUB_ENV as BENCH_WORK when that
# variable is set (on a runner), else prints it.

set -euo pipefail

echo "== disk before"
df -h / /mnt 2>/dev/null || df -h /

# Toolchains the image carries and the bench never uses; removed only when
# present, so a new image layout is not an error.
for dir in /usr/local/lib/android /usr/share/dotnet /opt/ghc /opt/hostedtoolcache/CodeQL; do
    if [ -d "$dir" ]; then
        sudo rm -rf "$dir"
    fi
done

echo "== packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    qemu-system-x86 qemu-utils ovmf xorriso squashfs-tools cloud-image-utils gpgv \
    grub-efi-amd64-bin mtools dosfstools openssh-client openssl
dpkg-query -W -f '${Package} ${Version}\n' \
    qemu-system-x86 ovmf xorriso squashfs-tools cloud-image-utils gpgv ubuntu-keyring \
    grub-efi-amd64-bin
test -r /usr/share/keyrings/ubuntu-archive-keyring.gpg || { echo "STOP: no ubuntu-keyring"; exit 1; }
if [ "${BENCH_MODE:-}" = full ]; then
    # zark's own make deb, as ci.yml's package job builds it.
    sudo apt-get install -y --no-install-recommends \
        build-essential devscripts debhelper dh-python fakeroot lintian pandoc
    dpkg-query -W -f '${Package} ${Version}\n' debhelper devscripts dh-python pandoc
fi

echo "== kvm"
# GitHub's rule for hardware acceleration on its Linux runners (changelog
# 2024-04-02): /dev/kvm for every user, so QEMU runs as the runner user.
echo 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"' \
    | sudo tee /etc/udev/rules.d/99-kvm4all.rules >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=kvm
sudo udevadm settle
test -r /dev/kvm -a -w /dev/kvm || { echo "STOP: /dev/kvm is not usable"; exit 1; }
ls -l /dev/kvm

echo "== work directory"
# The mount with the most free space among /mnt and the runner's temp
# directory: the ISO, its remaster and the target disk need ~25 GB, mode full
# about 35 GB with the USB drive's and the new disk's images.
best="" best_free=0
for candidate in /mnt "${RUNNER_TEMP:-/tmp}"; do
    [ -d "$candidate" ] || continue
    free=$(df --output=avail -k "$candidate" | tail -n 1 | tr -d ' ')
    if [ "$free" -gt "$best_free" ]; then
        best=$candidate best_free=$free
    fi
done
[ -n "$best" ] || { echo "STOP: no work directory"; exit 1; }
work="$best/zark-bench"
sudo mkdir -p "$work"
sudo chown "$(id -u):$(id -g)" "$work"
echo "work directory: $work ($((best_free / 1024 / 1024)) GiB free)"
if [ -n "${GITHUB_ENV:-}" ]; then
    echo "BENCH_WORK=$work" >> "$GITHUB_ENV"
fi
df -h / "$best"
