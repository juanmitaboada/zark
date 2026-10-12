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
Read sanoid's retention configuration to compute how long a backup drive
anchored only on shared snapshots stays incremental after its last backup
(hallazgo 21).

It applies to drives with no anchor in origin: a drive written by zark 1.x
before its first zark 2 backup, or one whose anchors were lost. Beyond the
horizon some dataset has no snapshot left in common with the drive, and
its next backup needs a full resend of it. Bookmark-anchored drives have
no horizon.

Why parse sanoid.conf at runtime instead of a hardcoded constant: the
operator can edit ``/etc/sanoid/sanoid.conf`` freely and the retention
that actually applies depends on those values.

Computation, from sanoid's own rule (sanoid v2.3.0, ``sanoid:338-370``): a
snapshot of type t is pruned only when it is older than ``n_t * period_t``
and more than ``n_t`` of that type exist, with periods of 1 hour, 1 day,
7 days, 31 days and 365.25 days (``:342-347``). The newest snapshot of a
type is up to one period old when the drive receives it, so it is
guaranteed to stay ``(n_t - 1) * period_t`` more (``n_t * period_t`` is
only the most it may stay); a template guarantees the longest of those
over its types: monthly=3 guarantees 62 days.

The datasets of one drive may use different templates, and the drive
needs every one of them, so the horizon is the **smallest** over the
templates used by ``[rpool*]`` or ``[bpool*]`` sections. Templates that
no section uses are ignored.
"""

import configparser
from pathlib import Path

from lib.log import Log

# Standard location of sanoid's config. The package ships
# ``/etc/sanoid/sanoid.defaults.conf`` separately; we don't consult that
# one because zark's setup writes ``sanoid.conf`` with explicit values
# for every template, so the defaults file never contributes to the
# effective retention here.
SANOID_CONF_PATH = Path("/etc/sanoid/sanoid.conf")


# sanoid's periods, in days (sanoid:342-347).
_PERIOD_DAYS = {"hourly": 1 / 24, "daily": 1.0, "weekly": 7.0, "monthly": 31.0, "yearly": 365.25}


def _guaranteed_days_of_template(settings: dict[str, str]) -> float:
    """Days a snapshot this template took is guaranteed to stay after a drive
    received it: the longest ``(n - 1) * period`` over its types; 0 when
    the template keeps nothing."""
    days = [
        (n - 1) * period
        for kind, period in _PERIOD_DAYS.items()
        if (n := _safe_int(settings.get(kind, "0"))) >= 1
    ]
    return max(days, default=0.0)


def _safe_int(s: str) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return 0


def _is_managed_section(name: str) -> bool:
    """Whether a section in sanoid.conf describes one of our pools."""
    head = name.split("/", 1)[0]
    return head in ("rpool", "bpool")


def guaranteed_overlap_days(
    log: Log,
    *,
    conf_path: Path = SANOID_CONF_PATH,
) -> int | None:
    """Whole days a drive anchored only on shared snapshots is guaranteed
    to stay incremental after its last backup: the smallest horizon over
    the templates used by ``rpool*`` or ``bpool*`` sections.

    Returns ``None`` and emits a visible WARN if the config file does not
    exist; ``None`` silently when it has no managed sections.
    """
    if not conf_path.exists():
        log.warn(
            f"sanoid.conf missing at {conf_path} — staleness detection disabled. "
            "Run 'sudo ./zark setup' to generate it.",
        )
        return None

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(conf_path, encoding="utf-8")
    except (configparser.Error, OSError) as e:
        log.warn(f"Could not parse {conf_path}: {e} — staleness detection disabled")
        return None

    templates: dict[str, dict[str, str]] = {
        section: dict(parser.items(section))
        for section in parser.sections()
        if section.startswith("template_")
    }
    used: set[str] = set()
    for section in parser.sections():
        if not _is_managed_section(section):
            continue
        tpl = dict(parser.items(section)).get("use_template", "").strip()
        if tpl:
            # ``use_template = minimal`` refers to ``[template_minimal]``.
            used.add(tpl if tpl.startswith("template_") else f"template_{tpl}")
    horizons = [_guaranteed_days_of_template(templates[t]) for t in used if t in templates]
    return int(min(horizons)) if horizons else None
