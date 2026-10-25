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
# smoke-deb.sh <zark_*_amd64.deb> - install a built .deb on the runner,
# check that zark starts, then purge it. CI runs it on Ubuntu 24.04, where
# the package is built, and on 26.04 (RELP.8).
#
# It catches regressions in debian/control's Depends: if a dependency is
# missing, apt fails here. It also checks that the symlink layout works
# (/usr/bin/zark resolves to /usr/share/zark/zark via realpath) and that
# --help / --version produce output, which exercises the lib/ and
# commands/ imports.
set -euo pipefail

DEB=$(ls "$@" | head -1)
echo "Testing: $DEB"

# Inspect package metadata before installing
dpkg-deb -I "$DEB"

# Verify the Depends list is what we declared. If Depends ever drifts (a
# refactor adds a new tool, a new debian/control field is mis-edited), this
# catches it before users do.
dpkg-deb -f "$DEB" Depends | grep -q 'zfsutils-linux'
dpkg-deb -f "$DEB" Depends | grep -q 'sanoid'
dpkg-deb -f "$DEB" Depends | grep -q 'cryptsetup-bin'
dpkg-deb -f "$DEB" Depends | grep -q 'gdisk'

# Install with apt (resolves Depends from the Ubuntu archive). A relative
# path must start with ./ or apt takes it for a package name.
case "$DEB" in
    /*) ;;
    *) DEB="./$DEB" ;;
esac
sudo apt-get install -y "$DEB"

# Layout: symlink in /usr/bin pointing at /usr/share/zark/zark
which zark
test -L /usr/bin/zark
readlink -f /usr/bin/zark | grep -q '/usr/share/zark/zark'

# Entry point: --version and --help must work without root (we are root in
# the runner, but the entry point's adaptive help logic should produce
# sensible output either way). We capture --help and assert key content is
# present, rather than piping to head -N - head closes its read end after N
# lines, killing zark with SIGPIPE, which trips up `set -e` under pipefail.
zark --version
zark --help > /tmp/zark_help.out
test -s /tmp/zark_help.out
grep -q "Commands:" /tmp/zark_help.out
grep -q "Examples:" /tmp/zark_help.out
grep -q "backup" /tmp/zark_help.out
grep -q "recover" /tmp/zark_help.out

# Clean up so subsequent steps see a pristine system
sudo apt-get purge -y zark
# bash caches resolved command paths in this shell; the earlier `which zark`
# populated the cache. After purge the file is gone but `command -v zark`
# still returns the stale cached path. `hash -r` flushes the cache so the
# assertion below reflects reality.
hash -r
! command -v zark
