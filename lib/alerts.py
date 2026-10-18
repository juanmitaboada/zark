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
The status alerts (M3 decision 13a): a MOTD script that prints the file
`zark status --refresh` writes, and a systemd timer that refreshes it every
hour (and two minutes after boot) and sends the CAP notifications.

Installed by `zark setup` (or `zark alerts install`), removed by
`zark alerts remove` and by the Debian package's prerm on remove. Both are
idempotent. The service runs the zark that installed it, by absolute path,
and does nothing if that path is gone (a zark run from a stick).
"""

from pathlib import Path

from lib import sh, status
from lib.log import Log

MOTD_SCRIPT_PATH = "etc/update-motd.d/97-zark-status"
SERVICE_PATH = "etc/systemd/system/zark-status.service"
TIMER_PATH = "etc/systemd/system/zark-status.timer"
TIMER = "zark-status.timer"

MOTD_SCRIPT = f"""\
#!/bin/sh
# 97-zark-status - zark's space and backup-age lines, written hourly by
# zark-status.timer (`zark status --refresh`). Nothing is computed at login.
#
# Installed by zark; `sudo zark alerts remove` removes it.
[ -r {status.MOTD_FILE} ] && cat {status.MOTD_FILE}
exit 0
"""

TIMER_UNIT = """\
[Unit]
Description=zark: refresh the space and backup-age status hourly

[Timer]
OnBootSec=2min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
"""


def unsafe_char(path: str) -> str:
    """The first character of ``path`` systemd refuses in an executable path
    (a quote, a backslash or a control character: ``string_is_safe``,
    src/basic/string-util.c:1078-1093, and config_parse_exec,
    src/core/load-fragment.c:989-994, v258), or ""."""
    return next((c for c in path if c in "\"'\\" or ord(c) < 0x20 or ord(c) == 0x7F), "")


def service_unit(zark: str) -> str:
    """The service, running ``zark`` (an absolute path, spaces allowed: a
    stick mounted under /run/media/<user>/<label>; see :func:`unsafe_char`
    for what systemd refuses)."""
    # systemd expands % specifiers in both settings; ExecStart also splits
    # on spaces and unquotes C-style (src/core/load-fragment.c, v258:
    # config_parse_exec, config_parse_unit_condition_path).
    path = zark.replace("%", "%%")
    quoted = f'"{path}"'
    return f"""\
[Unit]
Description=zark: space and backup-age status (MOTD, desktop notifications)
ConditionPathExists={path}

[Service]
Type=oneshot
ExecStart={quoted} status --refresh --notify
"""


def zark_path() -> str:
    """Absolute path of the running zark (the tree this module belongs to)."""
    return str(Path(__file__).resolve().parent.parent / "zark")


def _write(path: Path, text: str, mode: int) -> bool:
    """Write ``text`` unless it is already there; True when it changed."""
    try:
        if path.read_text(encoding="utf-8") == text and path.stat().st_mode & 0o777 == mode:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return True


def install(log: Log, root: str = "/", zark: str = "") -> None:
    """Install the MOTD script and the timer, and enable the timer. A zark
    path systemd would refuse in ExecStart installs nothing (M3F-9 9a)."""
    zark = zark or zark_path()
    if bad := unsafe_char(zark):
        log.warn(
            f"zark's path contains {bad!r}, which systemd refuses in ExecStart: {zark}; "
            + "the space and backup-age alerts were not installed"
        )
        return
    base = Path(root)
    changed = _write(base / MOTD_SCRIPT_PATH, MOTD_SCRIPT, 0o755)
    changed = _write(base / SERVICE_PATH, service_unit(zark), 0o644) or changed
    changed = _write(base / TIMER_PATH, TIMER_UNIT, 0o644) or changed
    if changed:
        _ = sh.run("systemctl daemon-reload", log=log)
    if sh.run(f"systemctl enable --now {TIMER}", log=log).ok:
        log.ok("Space and backup-age alerts installed (MOTD, hourly timer, notifications) ✓")
    else:
        log.warn(f"Could not enable {TIMER}; the MOTD stays empty until it runs")


def remove(log: Log, root: str = "/") -> None:
    """Disable the timer and remove what :func:`install` wrote, and what the
    timer wrote under /run/zark (the directory stays: other markers live
    there, and it is tmpfs; M3F-7 7a)."""
    base = Path(root)
    _ = sh.run(f"systemctl disable --now {TIMER}", log=log)
    removed = 0
    for rel in (MOTD_SCRIPT_PATH, SERVICE_PATH, TIMER_PATH):
        p = base / rel
        if p.exists():
            p.unlink()
            removed += 1
    for written in (status.MOTD_FILE, status.NOTIFIED_FILE):
        written.unlink(missing_ok=True)
    _ = sh.run("systemctl daemon-reload", log=log)
    log.ok(f"Space and backup-age alerts removed ({removed} file(s))")
