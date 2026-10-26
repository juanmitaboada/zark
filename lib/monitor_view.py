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
What ``zark monitor`` shows, as lines of styled spans (M8).

Pure: from a :class:`lib.status_reader.Reading` and the live figures to the
frame's lines. The curses and plain front ends in ``commands/monitor.py``
only paint them. Every string that comes from a file or a listing goes
through :func:`clean` first: the files are data, and a control character in
them must not reach the terminal.
"""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lib import status_reader as sr
from lib.live_pools import LiveFigures

Span = tuple[str, str]  # (text, style)
Line = list[Span]

# Styles the front ends know: "" plain, "bold", "dim", "ok", "warn", "crit",
# "wait" (waiting for input), "run" (running).
STATE_STYLE = {
    "waiting": "wait",
    "running": "run",
    "attention": "crit",
    "edge": "warn",
    "overdue": "warn",
    "none": "dim",
    "ok": "ok",
}
STATE_WORD = {
    "waiting": "WAITING FOR INPUT",
    "running": "RUNNING",
    "attention": "ATTENTION",
    "edge": "CHECK",
    "overdue": "OVERDUE",
    "none": "NO STATUS",
    "ok": "OK",
}
SORTS = ("own", "snapshots", "name")
DEFAULT_REGISTRY = "/etc/zark/known_drives.json"
_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
# Characters the repo never prints, from data that may hold them.
_ASCII_FOR = {"\u2013": "-", "\u2014": "-", "\u2026": "..."}


@dataclass(frozen=True)
class ViewOptions:  # pylint: disable=too-many-instance-attributes
    """How to draw one frame."""

    width: int = 80
    version: str = ""
    host: str = ""
    interval: int = 60
    live: bool = True
    show_datasets: bool = True
    sort: str = "own"
    dataset_rows: int | None = None  # None: every row
    unicode_bars: bool = False
    clean_hint: str = ""  # how to run `zark clean` here, from lib.config.zark_command
    help_line: bool = False
    bell: bool = True
    keys: bool = False  # curses: show the key line


# ── formats ──────────────────────────────────────────────────────────────


def clean(value: object) -> str:
    """A file's or a listing's string, safe to print: control and format
    characters become ``?``, the three banned characters their ASCII."""
    text = value if isinstance(value, str) else str(value)
    out: list[str] = []
    for ch in text:
        if ch in _ASCII_FOR:
            out.append(_ASCII_FOR[ch])
        elif unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def fmt_bytes(n: int | None) -> str:
    """Binary units, as ``zfs list`` and ``zpool list`` (R6): ``854.6 GiB``."""
    if n is None:
        return "-"
    size = float(max(n, 0))
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} EiB"  # pragma: no cover


def fmt_duration(seconds: float) -> str:
    """``45s``, ``17m33s``, ``2h05m``, ``3d04h``."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600:02d}h"


def fmt_ago(seconds: float) -> str:
    """``3 s ago``, ``21 min ago``, ``5 h ago``, ``2 days ago``; a time in the
    future (a clock behind the writer's) reads ``just now``."""
    s = int(seconds)
    if s < 1:
        return "just now"
    if s < 120:
        return f"{s} s ago"
    if s < 7200:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def fmt_days(days: int | None) -> str:
    """``1 day``, ``13 days``."""
    if days is None:
        return "-"
    return "1 day" if days == 1 else f"{days} days"


def fmt_local(when: datetime | None, now: datetime) -> str:
    """Local time: ``11:19:44`` today, else ``2026-10-22 23:47``."""
    if when is None:
        return "-"
    local, today = when.astimezone(), now.astimezone()
    if local.date() == today.date():
        return local.strftime("%H:%M:%S")
    return local.strftime("%Y-%m-%d %H:%M")


def meter(fraction: float | None, width: int, unicode_bars: bool) -> str:
    """A bar ``width`` cells wide; empty when ``fraction`` is None."""
    full, empty = ("\u2588", "\u2591") if unicode_bars else ("#", ".")
    if fraction is None:
        return "[" + " " * width + "]"
    filled = min(width, max(0, int(fraction * width)))
    return "[" + full * filled + empty * (width - filled) + "]"


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dicts(value: object) -> list[Mapping[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _ts(value: object) -> datetime | None:
    return sr.parse_timestamp(value)


# ── sections ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Inputs:
    """Everything one frame shows."""

    reading: sr.Reading
    live: LiveFigures | None = None
    # The last ok status.json of this session, shown while the current one is
    # newer or unreadable (contract, Versioning).
    last_good_status: Mapping[str, Any] | None = None
    by_id: Any = sr.BY_ID


def _status_doc(inp: Inputs) -> tuple[Mapping[str, Any] | None, bool]:
    """The status.json to show and whether it is an older, last good one."""
    r = inp.reading
    if r.status_class in ("ok", "stale") and r.status.doc is not None:
        return r.status.doc, False
    if inp.last_good_status is not None:
        return inp.last_good_status, True
    return None, False


def _thresholds(doc: Mapping[str, Any] | None) -> Mapping[str, Any]:
    value = (doc or {}).get("thresholds")
    return value if isinstance(value, dict) else {}


def _ago(now: datetime, value: object) -> str:
    when = _ts(value)
    return fmt_ago((now - when).total_seconds()) if when else "-"


def _file_ages(r: sr.Reading) -> Line:
    line: Line = []
    doc = r.status.doc
    if r.status.state == "absent":
        line.append(("status.json absent", "dim"))
    elif doc is not None:
        by = clean(doc.get("generated_by", "?"))
        line.append((f"status.json {_ago(r.now, doc.get('generated_at'))} ({by})", ""))
    else:
        line.append((f"status.json {r.status.state}", "warn"))
    if r.progress.doc is not None:
        line.append((f"   progress.json {_ago(r.now, r.progress.doc.get('updated_at'))}", ""))
    return line


def _header(inp: Inputs, opt: ViewOptions) -> list[Line]:
    r = inp.reading
    local = r.now.astimezone()
    live = f"live {opt.interval}s" if opt.live else "live off"
    lines: list[Line] = [
        [
            (f"zark monitor {opt.version}", "bold"),
            (f"   {clean(opt.host)}   {local.strftime('%Y-%m-%d %H:%M:%S %Z')}", ""),
            (f"   files 1s  {live}", "dim"),
        ],
        _file_ages(r),
    ]
    doc, pdoc = r.status.doc, r.progress.doc
    notes: list[str] = []
    writer = (pdoc or doc or {}).get("zark_version")
    if isinstance(writer, str) and opt.version and writer != opt.version:
        notes.append(f"written by zark {clean(writer)}")
    registry = (doc or {}).get("registry")
    if isinstance(registry, str) and registry != DEFAULT_REGISTRY:
        notes.append(f"registry {clean(registry)}")
    if notes:
        lines.append([("   ".join(notes), "dim")])
    return lines


def _progress_title(pdoc: Mapping[str, Any]) -> str:
    title = clean(pdoc.get("command", "zark"))
    drive = pdoc.get("drive")
    if isinstance(drive, dict) and isinstance(drive.get("name"), str):
        joiner = "to" if pdoc.get("command") == "backup" else "on"
        title += f" {joiner} {clean(drive['name'])}"
    step = pdoc.get("step")
    if isinstance(step, dict) and _int(step.get("index")) and _int(step.get("total")):
        title += f"   step {step['index']}/{step['total']}  {clean(step.get('label', ''))}"
    return title


def _byte_line(transfer: Mapping[str, Any], opt: ViewOptions, style: str) -> list[Line]:
    fraction = sr.byte_fraction(transfer)
    done, total = _int(transfer.get("bytes_done")), _int(transfer.get("bytes_total"))
    if fraction is not None:
        floor = ">= " if transfer.get("bytes_total_partial") is True else ""
        sent = f"  {fmt_bytes(done)} of {floor}{fmt_bytes(total)}  {int(fraction * 100)} %"
        return [[("  " + meter(fraction, 40, opt.unicode_bars), style), (sent, style)]]
    if done is not None:
        return [[(f"  {fmt_bytes(done)} sent, total not estimated", style)]]
    return []


def _rate_line(transfer: Mapping[str, Any], style: str) -> Line:
    line: Line = []
    if _int(transfer.get("bytes_done")) is None:
        line.append(("  bytes not measured   ", "dim"))
    else:
        rate, eta = _int(transfer.get("rate_bytes_per_s")), sr.eta_seconds(transfer)
        line.append((f"  {fmt_bytes(rate)}/s" if rate is not None else "  rate -", style))
        line.append((f"   ETA {fmt_duration(eta)}   " if eta is not None else "   ETA -   ", style))
    done = _int(transfer.get("datasets_done")) or 0
    planned = _int(transfer.get("datasets_total")) or 0
    failed = _int(transfer.get("datasets_failed")) or 0
    line.append((f"transfers {done} of {planned}", style))
    line.append((f" ({failed} failed)", "crit" if failed else style))
    return line


def _transfer_lines(transfer: Mapping[str, Any], opt: ViewOptions, style: str) -> list[Line]:
    lines = _byte_line(transfer, opt, style)
    lines.append(_rate_line(transfer, style))
    dataset = transfer.get("dataset")
    if isinstance(dataset, str):
        state = transfer.get("dataset_state")
        what = f" ({clean(state)})" if isinstance(state, str) else ""
        lines.append([(f"  now {clean(dataset)}{what}", style)])
    return lines


def _step_meter(pdoc: Mapping[str, Any], opt: ViewOptions, style: str) -> list[Line]:
    step = pdoc.get("step")
    if not isinstance(step, dict):
        return []
    index, total = _int(step.get("index")), _int(step.get("total"))
    if not index or not total:
        return []
    return [[("  " + meter(index / total, 40, opt.unicode_bars), style)]]


def _active_lines(inp: Inputs, opt: ViewOptions) -> list[Line]:
    r = inp.reading
    pdoc = r.progress.doc or {}
    style = "dim" if r.progress_class == "stalled" else ""
    state = r.indicator if r.indicator in ("waiting", "running") else "running"
    lines: list[Line] = [
        [(STATE_WORD[state], "bold " + STATE_STYLE[state]), (f"  {_progress_title(pdoc)}", "")]
    ]
    if r.indicator == "waiting":
        asks = pdoc.get("waiting_for")
        lines.append(
            [("  asks: ", "wait"), (clean(asks) if isinstance(asks, str) else "-", "bold")]
        )
    started = _ts(pdoc.get("started_at"))
    info = f"  elapsed {fmt_duration((r.now - started).total_seconds())}" if started else "  "
    if isinstance(pdoc.get("point"), str):
        info += f"   point {clean(pdoc['point'])}"
    lines.append([(info, "")])
    updated = _ts(pdoc.get("updated_at"))
    if r.progress_class == "stalled" and updated is not None:
        silent = fmt_duration((r.now - updated).total_seconds())
        lines.append([(f"  no heartbeat for {silent}: the figures below are not current", "warn")])
    transfer = pdoc.get("transfer")
    if isinstance(transfer, dict):
        return lines + _transfer_lines(transfer, opt, style)
    return lines + _step_meter(pdoc, opt, style)


def _drive_attention(doc: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for drive in _dicts(doc.get("drives")):
        run = drive.get("last_run")
        if isinstance(run, dict) and run.get("result") in sr.ATTENTION_RESULTS:
            out.append(f"{clean(drive.get('name', '?'))}: last backup run {clean(run['result'])}")
    for notice in _dicts(doc.get("notices")):
        if notice.get("level") == "critical":
            out.append(clean(notice.get("text", "")))
    return out


def _progress_why(r: sr.Reading, opt: ViewOptions) -> list[tuple[str, str]]:
    pdoc = r.progress.doc
    if r.progress_class == "unknown":
        return [(f"progress.json not readable: {clean(r.progress.error)}", "warn")]
    if r.progress_class != "orphan" or pdoc is None:
        return []
    drive = pdoc.get("drive")
    on = f" to {clean(drive['name'])}" if isinstance(drive, dict) and "name" in drive else ""
    who = f"zark {clean(pdoc.get('command', '?'))}{on} (pid {clean(pdoc.get('pid', '?'))})"
    heard = fmt_local(_ts(pdoc.get("updated_at")), r.now)
    return [
        (f"{who} is gone; last heard {heard}", "warn"),
        (
            f"{opt.clean_hint} releases what it left and records the run as interrupted"
            if opt.clean_hint
            else "the next zark command that changes state records the run as interrupted",
            "",
        ),
    ]


def _status_why(r: sr.Reading, doc: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    if r.status_class == "newer":
        return [
            (f"status.json is from a newer zark ({clean(r.status.error)}): update zark", "warn")
        ]
    if r.status_class == "unreadable":
        return [(f"status.json not readable: {clean(r.status.error)}", "warn")]
    if r.status_class == "stale" and doc is not None:
        ago = _ago(r.now, doc.get("generated_at"))
        return [(f"status.json written {ago}: is zark's alerts timer installed?", "warn")]
    if r.status_class == "absent":
        return [("no status.json: zark writes it when a command that changes state ends", "dim")]
    return []


def _idle_lines(inp: Inputs, opt: ViewOptions) -> list[Line]:
    r = inp.reading
    state = r.indicator
    doc, older = _status_doc(inp)
    why = _progress_why(r, opt) + _status_why(r, doc)
    if older:
        why.append(("showing the last status.json this monitor could read", "dim"))
    if state == "attention" and doc is not None:
        why += [(t, "crit") for t in _drive_attention(doc)]
    if state == "overdue" and doc is not None:
        why += [
            (clean(n.get("text", "")), "warn")
            for n in _dicts(doc.get("notices"))
            if n.get("code") == "backup_age"
        ]
    return [[(STATE_WORD[state], "bold " + STATE_STYLE[state])]] + [
        [(f"  {text}", style)] for text, style in why
    ]


def _level_style(space: Mapping[str, Any], thresholds: Mapping[str, Any]) -> tuple[str, str]:
    cap = _int(space.get("capacity_pct"))
    level = sr.capacity_level(cap, thresholds) if cap is not None else "ok"
    return level, {"critical": "crit", "warning": "warn"}.get(level, "")


def _cap_fraction(space: Mapping[str, Any]) -> float | None:
    cap = _int(space.get("capacity_pct"))
    return cap / 100 if cap is not None else None


def _space_row(
    name: str,
    space: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    health: str,
    opt: ViewOptions,
) -> Line:
    level, style = _level_style(space, thresholds)
    cap = _int(space.get("capacity_pct"))
    row: Line = [
        (f"  {clean(name):<5} ", "bold"),
        (meter(_cap_fraction(space), 12, opt.unicode_bars), style),
        (f"{fmt_bytes(_int(space.get('used_bytes'))):>11}", ""),
        (f"{fmt_bytes(_int(space.get('available_bytes'))):>11}", ""),
        (f"{fmt_bytes(sr.reserved_bytes(space)):>11}", ""),
        (f"{cap if cap is not None else '-':>5} %", style),
        (f"  {clean(health):<8}", "" if health in ("ONLINE", "-") else "crit"),
    ]
    if level != "ok":
        row.append((level, style))
    return row


def _pools_when(inp: Inputs, doc: Mapping[str, Any] | None) -> str:
    if inp.live is not None and inp.live.spaces:
        return f"live {fmt_local(inp.live.measured_at, inp.reading.now)}"
    gen = _ts((doc or {}).get("generated_at"))
    return f"status.json {fmt_local(gen, inp.reading.now)}" if gen else "no figures"


def _pool_row(inp: Inputs, opt: ViewOptions, name: str, file_space: object) -> Line:
    live = inp.live
    spaces = live.spaces if live else {}
    health = clean(live.pools[name]["health"]) if live and name in live.pools else "-"
    space = spaces.get(name) or file_space
    if not isinstance(space, dict):
        return [(f"  {clean(name):<5} not read", "dim")]
    doc, _older = _status_doc(inp)
    row = _space_row(name, space, _thresholds(doc), health, opt)
    if spaces and name not in spaces:
        row.append((f"  ({fmt_local(_ts(space.get('measured_at')), inp.reading.now)})", "dim"))
    return row


def _pool_lines(inp: Inputs, opt: ViewOptions) -> list[Line]:
    live = inp.live
    doc, _older = _status_doc(inp)
    files = {
        str(p.get("name")): p for p in _dicts((doc or {}).get("source_pools")) if p.get("name")
    }
    head = f"{'':<22}{'used':>11}{'free':>11}{'reserved':>11}    cap  health"
    lines: list[Line] = [
        [("POOLS", "bold"), (f"   ({_pools_when(inp, doc)})", "dim")],
        [(head, "dim")],
    ]
    if live and live.error:
        lines.append([(f"  live figures unavailable: {clean(live.error)}", "warn")])
    names: list[str] = list(dict.fromkeys([*(live.spaces if live else {}), *files]))
    if not names:
        lines.append([("  no source pool imported", "dim")])
    lines += [_pool_row(inp, opt, n, files.get(n, {}).get("space")) for n in names]
    return lines


def _drive_row(drive: Mapping[str, Any], width: int, where: str, now: datetime) -> Line:
    last = _ts(drive.get("last_backup_at"))
    age = sr.age_days(drive.get("last_backup_at"), now)
    text = f"last backup {fmt_local(last, now)} ({fmt_days(age)})" if last else "no backup yet"
    row: Line = [
        (f"  {clean(drive.get('name', '?')):<{width}}  ", "bold"),
        (f"{where:<19}", "dim" if where == "not connected" else "ok"),
        (text, ""),
    ]
    run = drive.get("last_run")
    if isinstance(run, dict) and isinstance(run.get("result"), str):
        result = str(run["result"])
        row.append(("   " + clean(result), "crit" if result in sr.ATTENTION_RESULTS else "dim"))
    return row


def _run_lines(drive: Mapping[str, Any], pad: str, now: datetime) -> list[Line]:
    run = drive.get("last_run")
    if not isinstance(run, dict) or run.get("result") not in sr.ATTENTION_RESULTS:
        return []
    missing = _int(run.get("datasets_missing")) or 0
    extra = f", {missing} dataset(s) missing" if missing else ""
    lines: list[Line] = [
        [(f"{pad}ended {fmt_local(_ts(run.get('ended_at')), now)}{extra}", "crit")]
    ]
    if isinstance(run.get("message"), str) and run["message"]:
        lines.append([(f"{pad}{clean(run['message'])}", "crit")])
    return lines


def _fill_line(  # pylint: disable=too-many-arguments
    space: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    pad: str,
    health: str,
    opt: ViewOptions,
    now: datetime,
) -> Line:
    _level, style = _level_style(space, thresholds)
    cap = _int(space.get("capacity_pct"))
    line: Line = [
        (pad + meter(_cap_fraction(space), 20, opt.unicode_bars), style),
        (f" {cap if cap is not None else '-'} %", style),
        (f"  {fmt_bytes(_int(space.get('used_bytes')))} used", ""),
        (f"  {fmt_bytes(_int(space.get('available_bytes')))} avail", ""),
        (f"  ({fmt_local(_ts(space.get('measured_at')), now)})", "dim"),
    ]
    if health:
        line.append((f"  {health}", "" if health == "ONLINE" else "crit"))
    return line


def _where(drive: Mapping[str, Any], in_use: bool, by_id: Any) -> str:
    if in_use:
        return "connected, in use"
    return "connected" if sr.drive_connected(drive.get("drive_id"), by_id) else "not connected"


def _drive_fill(
    inp: Inputs, opt: ViewOptions, drive: Mapping[str, Any], space: Mapping[str, Any], in_use: bool
) -> Line:
    doc, _older = _status_doc(inp)
    width = max(len(clean(d.get("name", "?"))) for d in _dicts((doc or {}).get("drives")))
    health = _drive_health(inp, drive) if in_use else ""
    fill = _fill_line(space, _thresholds(doc), " " * (width + 4), health, opt, inp.reading.now)
    margin = sr.drive_margin_bytes({**drive, "space": space})
    if margin is not None:
        fill.append((f"  margin {fmt_bytes(margin)}", "dim"))
    return fill


def _drive_lines(inp: Inputs, opt: ViewOptions) -> list[Line]:
    r = inp.reading
    doc, _older = _status_doc(inp)
    drives = _dicts((doc or {}).get("drives"))
    if not drives:
        return [[("DRIVES", "bold")], [("  no drive known", "dim")]]
    pdoc = r.progress.doc if r.progress_class in ("running", "stalled") else None
    using = (pdoc or {}).get("drive")
    using = using if isinstance(using, dict) else {}
    width = max(len(clean(d.get("name", "?"))) for d in drives)
    pad = " " * (width + 4)
    lines: list[Line] = [[("DRIVES", "bold")]]
    for drive in drives:
        in_use = bool(using) and using.get("guid") == drive.get("guid")
        lines.append(_drive_row(drive, width, _where(drive, in_use, inp.by_id), r.now))
        lines += _run_lines(drive, pad, r.now)
        space = using.get("space") if in_use else drive.get("space")
        if isinstance(space, dict):
            lines.append(_drive_fill(inp, opt, drive, space, in_use))
        if not in_use:
            # The contents are dated by the file's space; never paired with another.
            lines += _contents_lines(drive, pad, r.now)
    return lines


def _month_of(point: str, points: Sequence[Mapping[str, Any]]) -> str:
    for p in points:
        if p.get("name") == point and _ts(p.get("created_at")):
            return (_ts(p.get("created_at")) or datetime.min).strftime("%Y-%m")
    return point[5:12] if point.startswith("zark_") else "?"


def _points_line(c: sr.DriveContents, pad: str) -> list[Line]:
    if c.points is None or c.months is None:
        return []
    months = sorted(c.months, reverse=True)
    shown = ", ".join(f"{m} {c.months[m]}" for m in months[:4])
    older = months[4:]
    rest = f"; {sum(c.months[m] for m in older)} in {len(older)} older months" if older else ""
    return [[(f"{pad}points {c.points}: {shown}{rest}", "")]]


def _next_line(
    drive: Mapping[str, Any], c: sr.DriveContents, pad: str, now: datetime
) -> list[Line]:
    nxt = drive.get("retention_next")
    if c.next_points is None or not isinstance(nxt, dict):
        return []
    if not c.next_points:
        return [[(f"{pad}next under the policy: nothing", "dim")]]
    points = _dicts(drive.get("points"))
    first = _month_of(str(nxt["points"][0]), points)
    text = f"{pad}next under the policy: {c.next_points} point(s) from {first}"
    if c.next_reclaim_bytes is not None:
        text += f", frees {fmt_bytes(c.next_reclaim_bytes)}"
    if c.available_after_policy_bytes is not None:
        text += f", then {fmt_bytes(c.available_after_policy_bytes)} free"
    floor = " (at least)" if sr.retention_next_is_floor(drive, now) else ""
    return [[(text + floor, "")]]


def _others_line(drive: Mapping[str, Any], pad: str) -> list[Line]:
    others = drive.get("non_point_snapshots")
    if not isinstance(others, dict):
        return []
    parts = [
        f"{_int(v.get('count')) or 0} {k} ({fmt_bytes(_int(v.get('used_bytes')))})"
        for k, v in others.items()
        if isinstance(v, dict)
    ]
    return [[(f"{pad}other snapshots: {', '.join(parts)}", "dim")]] if parts else []


def _largest_line(drive: Mapping[str, Any], pad: str) -> list[Line]:
    rows = [d for d in _dicts(drive.get("datasets")) if d.get("name") != drive.get("name")]
    if not rows:
        return []
    pool = str(drive.get("name"))
    top = sorted(rows, key=lambda d: -sr.own_share(d))[:3]
    names = ", ".join(
        f"{_relative(pool, clean(d.get('name', '?')))} {fmt_bytes(sr.own_share(d))}" for d in top
    )
    return [[(f"{pad}largest: {names}", "")]]


def _contents_lines(drive: Mapping[str, Any], pad: str, now: datetime) -> list[Line]:
    """The drive contents (optional fields, "Drive contents"; M8F-5)."""
    c = sr.drive_contents(drive)
    if c is None:
        return []
    return (
        _points_line(c, pad)
        + _next_line(drive, c, pad, now)
        + _others_line(drive, pad)
        + _largest_line(drive, pad)
    )


def _drive_health(inp: Inputs, drive: Mapping[str, Any]) -> str:
    """Live health of a drive zark has imported, matched by GUID (M8-5)."""
    if inp.live is None:
        return ""
    for pool in inp.live.pools.values():
        if pool.get("guid") == drive.get("guid"):
            return clean(pool.get("health", ""))
    return ""


def _group(pool: str, name: str) -> str:
    if name.startswith(f"{pool}/USERDATA/home_"):
        return "home"
    if name.startswith(f"{pool}/ROOT"):
        return "system"
    return "other"


DatasetSource = tuple[str, list[Mapping[str, Any]], str]


def _dataset_sources(inp: Inputs) -> list[DatasetSource]:
    """(pool, datasets, when) for each source pool: live when listed, else
    the file's, with its date."""
    live = inp.live
    if live and live.datasets:
        return [(pool, list(rows), "live") for pool, rows in live.datasets.items()]
    doc, _older = _status_doc(inp)
    out: list[DatasetSource] = []
    for entry in _dicts((doc or {}).get("source_pools")):
        if isinstance(entry.get("datasets"), list):
            space = entry.get("space")
            measured = space.get("measured_at") if isinstance(space, dict) else None
            when = fmt_local(_ts(measured), inp.reading.now)
            out.append((str(entry.get("name")), _dicts(entry["datasets"]), when))
    return out


def _group_lines(pool: str, rows: Sequence[Mapping[str, Any]]) -> list[Line]:
    totals: dict[str, list[int]] = {}
    for d in rows:
        if d.get("name") == pool:
            continue
        g = totals.setdefault(_group(pool, str(d.get("name"))), [0, 0])
        g[0] += sr.own_share(d)
        g[1] += _int(d.get("usedbysnapshots_bytes")) or 0
    lines: list[Line] = [
        [(f"  {g:<7}{fmt_bytes(totals[g][0]):>11}   snapshots {fmt_bytes(totals[g][1])}", "")]
        for g in ("home", "system", "other")
        if g in totals
    ]
    return lines + _metadata_line(pool, rows)


def _metadata_line(pool: str, rows: Sequence[Mapping[str, Any]]) -> list[Line]:
    """The root's used beyond the datasets' shares, as pool metadata (M7F-10)."""
    rest = sr.pool_metadata_bytes(pool, rows)
    return (
        [[(f"  {'metadata':<9}{fmt_bytes(rest):>9}   not in any dataset", "dim")]] if rest else []
    )


def _dataset_row(pool: str, d: Mapping[str, Any]) -> Line:
    own = sr.own_share(d)
    snaps = _int(d.get("usedbysnapshots_bytes")) or 0
    cells = (
        own,
        _int(d.get("usedbydataset_bytes")),
        snaps,
        _int(d.get("usedbychildren_bytes")),
        _int(d.get("used_bytes")),
    )
    pct = f"{snaps * 100 // own}" if own else "-"
    text = "  " + " ".join(f"{fmt_bytes(c):>10}" for c in cells) + f" {pct:>5}  "
    return [(text, ""), (_relative(pool, clean(d.get("name", "?"))), "bold")]


def _dataset_lines(inp: Inputs, opt: ViewOptions) -> list[Line]:
    lines: list[Line] = []
    for pool, rows, when in _dataset_sources(inp):
        if not rows:
            continue
        lines.append([(f"DATASETS {clean(pool)}", "bold"), (f"   ({when})", "dim")])
        lines += _group_lines(pool, rows) if pool == "rpool" else _metadata_line(pool, rows)
        head = "  " + " ".join(f"{h:>10}" for h in ("own", "data", "snapshots", "children", "used"))
        lines.append([(f"{head} {'snap%':>5}  name", "dim")])
        ordered = _sorted_datasets(rows, opt.sort)
        shown = ordered if opt.dataset_rows is None else ordered[: max(0, opt.dataset_rows)]
        lines += [_dataset_row(pool, d) for d in shown]
        if len(shown) < len(ordered):
            lines.append([(f"  ... {len(ordered) - len(shown)} more", "dim")])
    return lines


def _relative(pool: str, name: str) -> str:
    """A dataset's name under its pool's heading: the pool itself as is."""
    return name[len(pool) + 1 :] if name.startswith(f"{pool}/") else name


def _sorted_datasets(rows: Sequence[Mapping[str, Any]], sort: str) -> list[Mapping[str, Any]]:
    if sort == "name":
        return sorted(rows, key=lambda d: str(d.get("name")))
    if sort == "snapshots":
        return sorted(
            rows, key=lambda d: (-(_int(d.get("usedbysnapshots_bytes")) or 0), str(d.get("name")))
        )
    return sorted(rows, key=lambda d: (-sr.own_share(d), str(d.get("name"))))


def _notice_lines(inp: Inputs) -> list[Line]:
    doc, _older = _status_doc(inp)
    lines: list[Line] = [[("NOTICES", "bold")]]
    notices = _dicts((doc or {}).get("notices"))
    if not notices:
        lines.append([("  none", "dim")])
    for notice in notices:
        level = notice.get("level")
        style = {"critical": "crit", "warning": "warn"}.get(str(level), "")
        lines.append([(f"  {clean(level):<8} ", style), (clean(notice.get("text", "")), "")])
    return lines


def key_help(opt: ViewOptions) -> Line:
    """The key line (``h`` toggles it)."""
    bell = "on" if opt.bell else "off"
    return [
        (
            f"q quit  d datasets  s sort ({opt.sort})  -/+ interval  r refresh  "
            f"b bell ({bell})  h help",
            "dim",
        )
    ]


def frame(inp: Inputs, opt: ViewOptions) -> list[Line]:
    """Every line of one frame, top to bottom."""
    r = inp.reading
    lines = _header(inp, opt)
    lines.append([])
    if r.progress_class in ("running", "stalled"):
        lines += _active_lines(inp, opt)
    else:
        lines += _idle_lines(inp, opt)
    lines.append([])
    lines += _pool_lines(inp, opt)
    lines.append([])
    lines += _drive_lines(inp, opt)
    if opt.show_datasets:
        datasets = _dataset_lines(inp, opt)
        if datasets:
            lines.append([])
            lines += datasets
    lines.append([])
    lines += _notice_lines(inp)
    if opt.help_line:
        lines += [[], key_help(opt)]
    return lines


def line_text(line: Line) -> str:
    """A line's text, without styles."""
    return "".join(text for text, _style in line)


def truncate(line: Line, width: int) -> Line:
    """``line`` cut to ``width`` characters."""
    out: Line = []
    room = width
    for text, style in line:
        if room <= 0:
            break
        out.append((text[:room], style))
        room -= len(text[:room])
    return out
