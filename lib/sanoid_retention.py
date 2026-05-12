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
Read sanoid's retention configuration to compute the worst-case overlap
window between a backup drive and the source pool.

Used by ``commands/backup.py`` and ``commands/repair_divergent.py`` to
report drives that are getting close to their retention horizon —
beyond that horizon the snapshot chain shared between source and target
is gone and syncoid would abort with "cowardly refusing".

Why parse sanoid.conf at runtime instead of a hardcoded constant: the
operator can edit ``/etc/sanoid/sanoid.conf`` freely and the retention
that actually applies depends on those values. Reading the file is the
only authoritative answer; a hardcoded "90 days" would lie the moment
someone tunes sanoid.

Computation: the longest snapshot retention is the longest single
bucket among ``daily``, ``weekly``, ``monthly`` (in their own day
units), because the buckets *layer* — sanoid keeps the oldest of each
kind, and the longest one defines the actual horizon. Adding them
double-counts: the monthly snapshot, taken once per month, replaces
older daily/weekly snapshots once they age past their bucket. So
``max(daily, weekly*7, monthly*30)`` is the right metric, not the sum.

For multi-template setups, the worst case is the *largest* horizon
across all templates that are actually used by sections under
``[rpool*]`` or ``[bpool*]``: divergence on any dataset blocks the
whole backup, so the most-tolerant template defines safety. Templates
defined but never used (orphan template_* sections) are ignored.
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


def _retention_days_of_template(settings: dict[str, str]) -> int:
    """Largest bucket horizon, in days, for one template's settings.

    A missing field is treated as zero (sanoid's own default for
    explicitly-omitted retentions). The non-day buckets (``hourly``,
    ``frequently``) are intentionally not consulted: they never define
    the retention horizon because their bucket is always shorter than
    a single ``daily``.
    """
    daily = _safe_int(settings.get("daily", "0"))
    weekly = _safe_int(settings.get("weekly", "0"))
    monthly = _safe_int(settings.get("monthly", "0"))
    return max(daily, weekly * 7, monthly * 30)


def _safe_int(s: str) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return 0


def _is_managed_section(name: str) -> bool:
    """Whether a section in sanoid.conf describes one of our pools."""
    head = name.split("/", 1)[0]
    return head in ("rpool", "bpool")


def worst_case_retention_days(
    log: Log,
    *,
    conf_path: Path = SANOID_CONF_PATH,
) -> int | None:
    """Return the longest retention horizon across templates that are
    actually used by ``rpool*`` or ``bpool*`` sections.

    Returns ``None`` and emits a visible WARN if the config file does
    not exist — the caller should treat staleness reporting as
    unavailable in that case (no FATAL: backup itself does not depend
    on this file).

    Returns ``None`` silently if the file exists but contains no
    managed sections — a freshly written but un-templated sanoid.conf
    is a transient state during setup and not worth annoying the user
    over.
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

    # Map template_name → settings dict.
    templates: dict[str, dict[str, str]] = {
        section: dict(parser.items(section))
        for section in parser.sections()
        if section.startswith("template_")
    }
    if not templates:
        return None

    # Collect templates referenced by managed sections via use_template.
    # Sections without use_template (e.g. autosnap=no zvol stubs) don't
    # contribute to retention because they don't take snapshots.
    used: set[str] = set()
    for section in parser.sections():
        if not _is_managed_section(section):
            continue
        items = dict(parser.items(section))
        tpl = items.get("use_template", "").strip()
        if not tpl:
            continue
        # Sanoid's config format prefixes templates with ``template_``
        # only in the section header; ``use_template = minimal`` refers
        # to ``[template_minimal]``. Normalize accordingly.
        full = tpl if tpl.startswith("template_") else f"template_{tpl}"
        used.add(full)

    if not used:
        return None

    horizons = [_retention_days_of_template(templates[t]) for t in used if t in templates]
    horizons = [h for h in horizons if h > 0]
    if not horizons:
        return None
    return max(horizons)
