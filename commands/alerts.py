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
zark alerts - install or remove the space and backup-age alerts.

  zark alerts install   the MOTD script and the hourly zark-status.timer
                        (zark setup does this)
  zark alerts remove    disable the timer and remove both
"""

from lib import alerts
from lib.config import zark_command
from lib.log import Log

USAGE = f"Usage: {zark_command()} alerts install | remove"


def run(args: list[str]) -> None:
    """Main entry point for 'zark alerts'."""
    log = Log()
    if args == ["install"]:
        alerts.install(log)
    elif args == ["remove"]:
        alerts.remove(log)
    else:
        log.fatal(f"Unknown alerts command: {' '.join(args)}", solutions=[USAGE])
