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
The VM bench: zark checked end to end in QEMU/KVM, on the system Ubuntu's
installer lays out (docs/BENCH.md).

Every module here but ``run`` and ``__main__`` is pure logic over text,
files and sockets, and is unit-tested (tests/test_bench.py). Those two
start virtual machines and are excluded from coverage by name in
sonar-project.properties.
"""

# The shape of steps.json and the other records a run writes; raised when a
# field changes meaning, so a reader of old artifacts can tell them apart.
RECORD_VERSION = 1
