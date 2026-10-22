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
Self-test reports, the local record, certificates and warnings (M4s-5,
M4s-6, M4s-9, M4s-15).

A run's report is one JSON object: ``certificate`` (what it covers: zark
version, platform series, where, level, each check's result) and
``results`` (the reasons, and for a FAIL what was expected and came).
Nothing in it names the machine: :func:`sanitize` replaces the run's own
names, the hostname, long numbers (GUIDs, ids) and long hex strings.

Coverage: a check (ID and revision) is covered on this platform when the
certificates file shipped with zark, or this machine's own record, has it
as PASS from a complete run with the same series and the same ``where``.
"""

import json
import os
import re
import socket
import tempfile
import time
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib import platform_facts
from lib.config import VERSION, Config, zark_command
from lib.log import Log
from lib.platform_facts import Facts
from lib.selftest import CHECKLIST, Check, Result, all_checks, dumps

FORMAT = 1
RECORD_DIR = "/var/lib/zark/selftest"
REPORTS_KEPT = 10
SETTINGS = "settings.json"
WARNINGS_KEY = "selftest_warnings"
ISSUE_URL = "https://github.com/juanmitaboada/zark/issues/new"
ISSUE_TEMPLATE = "selftest.yml"
# GitHub answers 414 over its URL limit, which it does not document: a link
# longer than this carries no report, and the file is pasted instead.
URL_MAX = 6000

_LONG_NUMBER = re.compile(r"\b\d{8,}\b")
_LONG_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{12,}\b")
_BY_ID = re.compile(r"/dev/disk/by-id/[^\s'\"]+")
_NOT_ALNUM_BEFORE, _NOT_ALNUM_AFTER = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"


def certificates_file() -> Path:
    """The certificates shipped with zark: next to lib/ in a portable copy,
    /usr/share/zark/certificates/selftest.json when installed."""
    return Config.zark_root() / "certificates" / "selftest.json"


# ── reports ──────────────────────────────────────────────────────────────


def sanitize(text: str, masks: dict[str, str]) -> str:
    """``text`` with nothing that names the machine: the run's own names
    (``masks``), by-id paths, the hostname, numbers of 8 digits or more and
    hex strings of 12 or more."""
    # One pass, longest name first, each a whole token: a short serial is
    # not replaced inside a word, nor inside a mask put in before (M4sX.3).
    # Not \b: a serial follows "_" in a by-id name.
    keys = sorted((k for k in masks if k), key=len, reverse=True)
    if keys:
        alt = "|".join(re.escape(k) for k in keys)
        text = re.sub(
            rf"{_NOT_ALNUM_BEFORE}(?:{alt}){_NOT_ALNUM_AFTER}", lambda m: masks[m[0]], text
        )
    text = _BY_ID.sub("<by-id>", text)
    host = socket.gethostname()
    if host and len(host) > 1:
        text = re.sub(rf"{_NOT_ALNUM_BEFORE}{re.escape(host)}{_NOT_ALNUM_AFTER}", "<host>", text)
    text = _LONG_HEX.sub(lambda m: m.group(0) if m.group(0).isdigit() else "<hex>", text)
    return _LONG_NUMBER.sub("<n>", text)


@dataclass
class RunInfo:
    """What a run was: its level, whether it was partial or stopped, the
    stick's USB ids for level 2."""

    level: int
    partial: bool
    bridge: str = ""


def build_report(
    facts: Facts, info: RunInfo, results: list[Result], masks: dict[str, str]
) -> dict[str, Any]:
    """The report of one run (section 7.1 of the M4s plan)."""
    cert: dict[str, Any] = {
        "zark": VERSION,
        "checklist": CHECKLIST,
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "where": facts.where,
        "iso": facts.iso,
        "level": info.level,
        "partial": info.partial,
        "series": dict(facts.series),
        "exact": dict(facts.exact),
        "checks": {r.key: r.result for r in results},
    }
    if info.bridge:
        cert["bridge"] = info.bridge
    clean: list[dict[str, Any]] = []
    for r in results:
        row = r.to_json()
        for k in ("reason", "expected", "came"):
            if k in row:
                row[k] = sanitize(str(row[k]), masks)
        clean.append(row)
    return {"format": FORMAT, "certificate": cert, "results": clean}


def write_report(report: dict[str, Any], record_dir: str = RECORD_DIR) -> Path:
    """Write ``report`` as ``report-<UTC>.json`` (root only), keep the newest
    :data:`REPORTS_KEPT`, and add it to the local record."""
    d = Path(record_dir)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = d / f"report-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    _write_atomic(path, dumps(report))
    for old in sorted(d.glob("report-*.json"))[:-REPORTS_KEPT]:
        old.unlink(missing_ok=True)
    _add_to_record(report, d / "record.json")
    return path


def latest_report(record_dir: str = RECORD_DIR) -> Path | None:
    """The newest report file, or None."""
    files = sorted(Path(record_dir).glob("report-*.json"))
    return files[-1] if files else None


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _add_to_record(report: dict[str, Any], path: Path) -> None:
    """The record keeps the newest entry per where, level and completeness
    (a partial or stopped run never replaces a complete one): the
    certificate plus the IDs that failed and what depends on them. A check
    this run passed leaves the failures of every complete entry of the same
    where and series, at any level (M4sR-4); coverage is not changed."""
    record = read_record(path)
    cert = report["certificate"]
    passed = {r["id"] for r in report["results"] if r["result"] == "PASS"}
    for e in record:
        if (
            not e.get("partial")
            and e.get("where") == cert["where"]
            and e.get("series") == cert["series"]
            and isinstance(e.get("failed"), list)
        ):
            e["failed"] = [f for f in e["failed"] if f.get("id") not in passed]
    failed = [
        {"id": r["id"], "depends": r.get("depends", [])}
        for r in report["results"]
        if r["result"] == "FAIL"
    ]
    entry = {**cert, "failed": failed}
    keep = [
        e
        for e in record
        if not (
            e.get("where") == cert["where"]
            and e.get("level") == cert["level"]
            and bool(e.get("partial")) == bool(cert["partial"])
        )
    ]
    _write_atomic(path, dumps({"format": FORMAT, "entries": [*keep, entry]}))


def read_record(path: Path | None = None) -> list[dict[str, Any]]:
    """This machine's record entries; empty when absent or unreadable."""
    p = path or Path(RECORD_DIR, "record.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return [e for e in entries if isinstance(e, dict)]


def read_certificates(path: Path | None = None) -> list[dict[str, Any]]:
    """The certificates shipped with zark; empty when absent or unreadable."""
    p = path or certificates_file()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def certificate_entry(report: dict[str, Any]) -> dict[str, Any]:
    """The certificates-file entry of a report, or ValueError saying why it
    cannot be one (partial, wrong format, a check failed or missing)."""
    if not isinstance(report, dict) or report.get("format") != FORMAT:
        raise ValueError("not a zark self-test report (format 1)")
    cert = report.get("certificate")
    if not isinstance(cert, dict) or not isinstance(cert.get("checks"), dict):
        raise ValueError("the report has no certificate")
    if cert.get("partial"):
        raise ValueError("a partial or stopped run certifies nothing")
    for key in ("zark", "where", "level", "series"):
        if key not in cert:
            raise ValueError(f"the certificate has no {key}")
    return {k: v for k, v in cert.items() if k != "failed"}


# ── coverage ─────────────────────────────────────────────────────────────


def _matches(entry: dict[str, Any], facts: Facts) -> bool:
    return (
        not entry.get("partial")
        and entry.get("where") == facts.where
        and entry.get("series") == facts.series
    )


def covered(facts: Facts, entries: Iterable[dict[str, Any]]) -> set[str]:
    """Check keys (``ID.rev``) some matching entry has as PASS."""
    out: set[str] = set()
    for e in entries:
        if _matches(e, facts):
            checks = e.get("checks", {})
            if isinstance(checks, dict):
                out |= {k for k, v in checks.items() if v == "PASS"}
    return out


def uncovered(facts: Facts, checks: list[Check], entries: Iterable[dict[str, Any]]) -> list[Check]:
    """The level-1 checks no entry covers on this platform."""
    have = covered(facts, entries)
    return [c for c in checks if c.level == 1 and c.key not in have]


def newest_failure(facts: Facts, record: list[dict[str, Any]]) -> dict[str, Any] | None:
    """This machine's newest record entry for this platform with a FAIL."""
    hits = [e for e in record if _matches(e, facts) and e.get("failed")]
    return max(hits, key=lambda e: str(e.get("date", ""))) if hits else None


# ── subscription and warnings ────────────────────────────────────────────


def settings_path() -> Path:
    """settings.json, next to known_drives.json."""
    return Config.default_config_dir() / SETTINGS


def subscribed(log: Log | None = None, path: Path | None = None) -> bool:
    """Whether the warnings are on; absent, unreadable or malformed is off
    (one line to zark.log)."""
    p = path or settings_path()
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if log:
            log.file(f"[WARN]    {p} ignored: {e}")
        return False
    value = data.get(WARNINGS_KEY) if isinstance(data, dict) else None
    if not isinstance(value, bool):
        if log and value is not None:
            log.file(f"[WARN]    {p}: {WARNINGS_KEY} is not true or false; warnings off")
        return False
    return value


def set_subscribed(on: bool, path: Path | None = None) -> Path:
    """Turn the warnings on or off, keeping any other setting."""
    p = path or settings_path()
    data: dict[str, Any] = {}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    data[WARNINGS_KEY] = on
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(p, json.dumps(data, indent=2, sort_keys=True) + "\n")
    return p


def warning_lines(
    facts: Facts,
    checks: list[Check],
    record: list[dict[str, Any]],
    shipped: list[dict[str, Any]],
) -> list[str]:
    """The self-test lines for this platform (M4s-15), without colour."""
    lines: list[str] = []
    missing = uncovered(facts, checks, [*shipped, *record])
    total = len([c for c in checks if c.level == 1])
    if missing:
        lines.append(
            f"self-test: no certificate covers this platform ({len(missing)} of {total} "
            + f"checks): run {zark_command()} selftest"
        )
    fail = newest_failure(facts, record)
    if fail:
        first = fail["failed"][0]
        depends = "; ".join(first.get("depends", [])) or "see zark selftest status"
        more = f" and {len(fail['failed']) - 1} more" if len(fail["failed"]) > 1 else ""
        lines.append(
            f"self-test: {first['id']}{more} failed on this machine on {fail.get('date', '?')}: "
            + depends
        )
    if facts.zfs_mismatch:
        lines.append(
            f"self-test: the loaded ZFS module ({facts.exact['zfs_kmod']}) is not the one "
            + f"installed for this kernel ({facts.zfs_kmod_installed}): reboot after updating ZFS"
        )
    return lines


def warnings(log: Log | None = None) -> list[str]:
    """The self-test lines when the warnings are subscribed, else none.
    Never raises: a failure to read is one line to zark.log."""
    if not subscribed(log):
        return []
    try:
        facts = platform_facts.read()
        return warning_lines(facts, all_checks(), read_record(), read_certificates())
    except (OSError, ValueError, KeyError, TypeError) as e:
        if log:
            log.file(f"[WARN]    self-test warnings not computed: {e}")
        return []


def show_warnings(log: Log) -> None:
    """The subscribed self-test lines, at the start of setup, backup and
    recover: information, never a question."""
    for line in warnings(log):
        log.warn(line)


# ── the GitHub link ──────────────────────────────────────────────────────


def issue_title(report: dict[str, Any]) -> str:
    """``selftest: zark <v> on Ubuntu <x> (<where>), <n> FAIL``."""
    cert = report["certificate"]
    fails = sum(1 for v in cert["checks"].values() if v == "FAIL")
    return (
        f"selftest: zark {cert['zark']} on Ubuntu {cert['series'].get('ubuntu', '?')} "
        + f"({cert['where']}), {fails} FAIL"
    )


def issue_link(report: dict[str, Any]) -> tuple[str, bool]:
    """The link that opens the issue form with the report filled in, and
    whether the report fits in it (when not, the link carries the title
    only and the file is pasted by hand). zark sends nothing itself."""
    body = json.dumps(report, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    base = {"template": ISSUE_TEMPLATE, "title": issue_title(report)}
    full = f"{ISSUE_URL}?" + urllib.parse.urlencode(
        {**base, "report": body}, quote_via=urllib.parse.quote
    )
    if len(full) <= URL_MAX:
        return full, True
    return f"{ISSUE_URL}?" + urllib.parse.urlencode(base, quote_via=urllib.parse.quote), False
