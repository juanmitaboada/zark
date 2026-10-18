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
Desktop notifications from root (the status timer): one per active
graphical session, sent as that session's user to its own D-Bus session
bus. Sessions are found with ``loginctl``, which lists Wayland and X11
sessions alike. Best effort: nothing here can fail a command.
"""

import shlex

from lib import sh

# The classes of a user's own graphical session (systemd v259.5
# src/login/logind-session.h:57, SESSION_CLASS_CAN_DISPLAY without the
# greeter): an idle login screen is a seat's active session too
# (src/login/logind-session.c:1034-1041), and is not a user to notify.
USER_CLASSES = ("user", "user-early", "user-light", "user-early-light")

# notify-send waits for the session bus to answer; a hung bus must not
# hold up the end of a backup or the timer.
NOTIFY_TIMEOUT_SEC = 10


def desktop_sessions() -> list[tuple[str, str]]:
    """(user name, uid) of every active Wayland or X11 session of a user."""
    r = sh.run("loginctl list-sessions --no-legend")
    out: list[tuple[str, str]] = []
    for line in r.lines if r.ok else []:
        fields = line.split()
        if not fields:
            continue
        session = shlex.quote(fields[0])
        show = sh.run(f"loginctl show-session {session} -p Type -p Class -p Active -p Name -p User")
        props = dict(p.split("=", 1) for p in show.lines if "=" in p) if show.ok else {}
        if (
            props.get("Type") in ("wayland", "x11")
            and props.get("Class") in USER_CLASSES
            and props.get("Active") == "yes"
        ):
            user = (props.get("Name", ""), props.get("User", ""))
            if all(user) and user[1].isdigit() and user not in out:
                out.append(user)
    return out


def notify(title: str, body: str) -> int:
    """Show ``title``/``body`` in every active desktop session; returns how
    many sessions it reached."""
    sent = 0
    for name, uid in desktop_sessions():
        r = sh.run(
            f"runuser -u {shlex.quote(name)} -- env "
            + f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus "
            + "notify-send --app-name=zark --icon=drive-harddisk "
            + f"{shlex.quote(title)} {shlex.quote(body)}",
            timeout=NOTIFY_TIMEOUT_SEC,
        )
        sent += r.ok
    return sent
