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
The directories of zark's state, readable by anyone (M7-10 10a).

``/run/zark`` (the lock, ``progress.json``, the MOTD file, markers) and
``/var/lib/zark`` (``status.json``, the self-test's record) hold what other
programs read without root (docs/STATUS-FILES.md, "Directories"): they are
0755 whatever the umask. ``mkdir`` applies the umask, so a directory zark
creates is given 0755 explicitly afterwards; one an earlier zark left
narrower under a strict umask (0700, which no reader can enter) is widened
to 0755. Nothing else is changed: a directory with bits outside 0755 (a
sticky ``/tmp``), one owned by another user, or a link, is left as it is.

The package's tmpfiles.d entry makes ``/run/zark`` at boot (M7-11); this
module covers a zark run from a stick or a checkout, and ``/var/lib/zark``.
"""

import os
import stat
from pathlib import Path

RUN_DIR = Path("/run/zark")
STATE_DIR = Path("/var/lib/zark")
MODE = 0o755


def ensure(path: Path | str) -> Path:
    """Make ``path`` an existing directory readers can enter, and return it.

    Raises OSError when it cannot be created, or exists and is not a
    directory (a link to one is accepted as it is).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(p, MODE)
    except FileExistsError:
        pass
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        if not p.is_dir():
            raise NotADirectoryError(f"{p} is a link to no directory")
        return p
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(f"{p} exists and is not a directory")
    mode = stat.S_IMODE(st.st_mode)
    # Only widen a mode narrower than 0755, and only zark's own (root's) directory.
    if mode != MODE and mode & ~MODE == 0 and st.st_uid == os.geteuid():
        os.chmod(p, MODE)
    return p
