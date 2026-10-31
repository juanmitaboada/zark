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
The certificates matrix: a static page generated from zark's self-test
certificates.

Sources, and what each row says about where it comes from:

* ``certificates/selftest.json``: the maintainer's test machine (the file
  zark's own coverage warning trusts).
* ``pages/community/<N>.json``: the output of ``zark selftest certificate``
  for a report filed on issue N. Provenance is the file name; no field was
  added to the certificate.

Every entry is untrusted: each field is checked against an allowlist and
whatever does not pass is dropped (``iso`` and the exact versions) or makes
the build fail (a malformed entry, a partial run, a bad file name). Nothing
is copied into the page without HTML escaping, and no field that could name
a machine is published (``bridge`` included).

Standard library only. Usage::

    python3 pages/matrix.py check
    python3 pages/matrix.py build --out _site
"""

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/juanmitaboada/zark"
SITE_URL = "https://juanmitaboada.github.io/zark/"
REPORT_ISSUE = 2
FORMAT = 1

MAX_FILE_BYTES = 1_000_000
MAX_ENTRIES_PER_FILE = 200
MAX_CHECKS = 500

# ── allowlists ───────────────────────────────────────────────────────────

_VERSION_TAIL = r"[0-9A-Za-z.+~:-]+"
RE_ZARK = re.compile(r"\d+\.\d+\.\d+(?:[-~][0-9A-Za-z.]+)?")
RE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
RE_CHECK = re.compile(r"[A-Z]{2,4}-\d{2}\.\d+")
RE_COMMUNITY = re.compile(r"(\d{1,9})\.json")

SERIES_RE: dict[str, re.Pattern[str]] = {
    "ubuntu": re.compile(r"\d\d\.\d\d"),
    "kernel": re.compile(r"\d+\.\d+"),
    "zfs": re.compile(r"\d+\.\d+(?:\.\d+)?"),
    "zfs_kmod": re.compile(r"\d+\.\d+(?:\.\d+)?"),
    "util_linux": re.compile(r"\d+\.\d+"),
    "systemd": re.compile(r"\d+"),
}
EXACT_RE: dict[str, re.Pattern[str]] = {
    "ubuntu": re.compile(r"\d\d\.\d\d(?:\.\d+)?(?: LTS)? \([A-Za-z ]{1,40}\)"),
    "kernel": re.compile(r"\d+\.\d+\.\d+-\d+-[a-z0-9]{1,20}"),
    "zfs": re.compile(rf"zfs-\d+\.\d+(?:\.\d+)?-{_VERSION_TAIL}"),
    "zfs_kmod": re.compile(rf"zfs-kmod-\d+\.\d+(?:\.\d+)?-{_VERSION_TAIL}"),
    "util_linux": re.compile(r"\d+\.\d+(?:\.\d+)?"),
    "systemd": re.compile(rf"\d+ \(\d+(?:\.\d+)*-{_VERSION_TAIL}\)"),
    "python": re.compile(r"\d+\.\d+\.\d+"),
    "sgdisk": re.compile(r"\d+\.\d+\.\d+"),
}
# Ubuntu's own live image line (/cdrom/.disk/info); a remastered image can
# say anything, so only this shape is shown.
RE_ISO = re.compile(
    r'Ubuntu \d\d\.\d\d(?:\.\d+)?(?: LTS)? "[A-Za-z ]{1,40}" - '
    + r"(?:Release|Daily|Beta|Alpha) amd64 \(\d{8}(?:\.\d+)?\)"
)
WHERES = ("installed", "live")
LEVELS = (1, 2)
RESULTS = ("PASS", "FAIL", "SKIP")
SERIES_KEYS = tuple(SERIES_RE)

# ── the palette (brand doc) and the contrast the page promises ───────────

PALETTE: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#FFFFFF",
        "fg": "#1B2236",
        "muted": "#4A5568",
        "card": "#F3F5F8",
        "border": "#CBD2DC",
        "ok": "#1B6E37",
        "bad": "#B3261E",
        "accent": "#E8592A",
    },
    "dark": {
        "bg": "#0E1422",
        "fg": "#F2F4F7",
        "muted": "#9AA4B2",
        "card": "#151D30",
        "border": "#2A3550",
        "ok": "#5FCB7A",
        "bad": "#FF8A80",
        "accent": "#E8592A",
    },
}
# (foreground, background) pairs that carry text, each at least 4.5:1.
TEXT_PAIRS = (
    ("fg", "bg"),
    ("muted", "bg"),
    ("fg", "card"),
    ("muted", "card"),
    ("ok", "card"),
    ("bad", "card"),
    ("ok", "bg"),
    ("bad", "bg"),
)


def _lin(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio of two ``#RRGGBB`` colours."""

    def lum(h: str) -> float:
        r, g, b = (int(h[i : i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


# ── certificates ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Cert:  # pylint: disable=too-many-instance-attributes
    """One validated certificate, with only fields that may be published."""

    zark: str
    date: str
    where: str
    level: int
    series: dict[str, str]
    exact: dict[str, str]
    iso: str | None
    checks: dict[str, str]
    source: int | None  # None: the maintainer's file; N: community report on issue N

    @property
    def platform(self) -> tuple[str, ...]:
        """The key zark's coverage matches on (with ``where``)."""
        return tuple(self.series[k] for k in SERIES_KEYS)

    def count(self, result: str) -> int:
        """How many checks gave ``result``."""
        return sum(1 for v in self.checks.values() if v == result)


def version_key(zark: str) -> tuple[int, int, int, int, int]:
    """Sort key of a zark version: a release is newer than its rcs."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(?:[-~]rc(\d+))?", zark)
    if not m:
        return (0, 0, 0, 0, 0)
    major, minor, patch, rc = m.groups()
    return (int(major), int(minor), int(patch), 0 if rc else 1, int(rc or 0))


def _text_field(value: Any, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} is not in the expected form")
    return value


def validate_entry(raw: Any, source: int | None) -> Cert:  # pylint: disable=too-many-locals
    """The publishable form of one certificate entry, or ValueError saying why not."""
    if not isinstance(raw, dict):
        raise ValueError("an entry is not an object")
    if raw.get("partial") is not False:
        raise ValueError("a partial or stopped run certifies nothing")
    if raw.get("stopped"):
        raise ValueError("a stopped run certifies nothing")
    zark = _text_field(raw.get("zark"), RE_ZARK, "zark")
    date = _text_field(raw.get("date"), RE_DATE, "date")
    where = raw.get("where")
    if where not in WHERES:
        raise ValueError("where is neither installed nor live")
    level = raw.get("level")
    if level not in LEVELS or isinstance(level, bool):
        raise ValueError("level is neither 1 nor 2")
    series_raw = raw.get("series")
    if not isinstance(series_raw, dict):
        raise ValueError("series is missing")
    series = {k: _text_field(series_raw.get(k), SERIES_RE[k], f"series.{k}") for k in SERIES_KEYS}
    exact: dict[str, str] = {}
    exact_raw = raw.get("exact")
    if isinstance(exact_raw, dict):
        for key, pattern in EXACT_RE.items():
            value = exact_raw.get(key)
            if isinstance(value, str) and pattern.fullmatch(value):
                exact[key] = value
    iso = raw.get("iso")
    iso_ok = iso if isinstance(iso, str) and RE_ISO.fullmatch(iso) else None
    checks_raw = raw.get("checks")
    if not isinstance(checks_raw, dict) or not checks_raw or len(checks_raw) > MAX_CHECKS:
        raise ValueError("checks is missing or too long")
    checks: dict[str, str] = {}
    for key, value in checks_raw.items():
        if not isinstance(key, str) or not RE_CHECK.fullmatch(key) or value not in RESULTS:
            raise ValueError("a check is not <ID>.<revision> with PASS, FAIL or SKIP")
        checks[key] = value
    return Cert(
        zark, date, where, level, series, exact, iso_ok, dict(sorted(checks.items())), source
    )


def _load_list(path: Path, source: int | None) -> list[Cert]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{path.name}: file too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{path.name}: {e}") from e
    if not isinstance(data, list) or not data or len(data) > MAX_ENTRIES_PER_FILE:
        raise ValueError(f"{path.name}: expected a list of 1 to {MAX_ENTRIES_PER_FILE} entries")
    out: list[Cert] = []
    for i, raw in enumerate(data):
        try:
            out.append(validate_entry(raw, source))
        except ValueError as e:
            raise ValueError(f"{path.name}, entry {i}: {e}") from e
    return out


def load_all(root: Path = ROOT) -> list[Cert]:
    """Every certificate, maintainer's first, then each community file."""
    certs = _load_list(root / "certificates" / "selftest.json", None)
    community = root / "pages" / "community"
    if community.is_dir():
        for path in sorted(community.iterdir()):
            if path.name == "README.md":
                continue
            m = RE_COMMUNITY.fullmatch(path.name)
            if not m or not path.is_file():
                raise ValueError(
                    f"pages/community/{path.name}: a report file is named <issue>.json"
                )
            certs.extend(_load_list(path, int(m.group(1))))
    return certs


# ── building the page ────────────────────────────────────────────────────


def esc(text: object) -> str:
    """HTML-escaped text."""
    return html.escape(str(text), quote=True)


def _source_html(cert: Cert) -> str:
    if cert.source is None:
        return "Maintainer's test machine"
    url = f"{REPO_URL}/issues/{cert.source}"
    return f'<a href="{esc(url)}">Community report #{cert.source}</a>'


def _result_text(cert: Cert) -> str:
    total = len(cert.checks)
    if cert.count("PASS") == total:
        return f"{total} of {total} PASS"
    parts = [f"{cert.count(r)} {r}" for r in RESULTS if cert.count(r)]
    return f"{', '.join(parts)} of {total}"


def _result_class(cert: Cert) -> str:
    return "ok" if cert.count("PASS") == len(cert.checks) else "bad"


def _newest(certs: list[Cert]) -> Cert:
    return max(certs, key=lambda c: (c.date, version_key(c.zark)))


def _vkey(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text))


def _card(platform: tuple[str, ...], certs: list[Cert]) -> str:
    series = dict(zip(SERIES_KEYS, platform, strict=True))
    kernels = sorted({c.exact["kernel"] for c in certs if "kernel" in c.exact})
    zfs_line = f"OpenZFS tools {series['zfs']}, module {series['zfs_kmod']}"
    rows: list[str] = []
    for where in WHERES:
        items: list[str] = []
        for level in LEVELS:
            pick = [c for c in certs if c.where == where and c.level == level]
            if not pick:
                continue
            c = _newest(pick)
            items.append(
                f'<li><span class="lv">Level {level}</span> zark {esc(c.zark)}, '
                + f"<time>{esc(c.date)}</time>: "
                + f'<strong class="{_result_class(c)}">{esc(_result_text(c))}</strong> '
                + f'<span class="src">{_source_html(c)}</span></li>'
            )
        body = "".join(items) if items else '<li class="none">No certificate yet.</li>'
        rows.append(f'<div class="where"><h4>{where.capitalize()}</h4><ul>{body}</ul></div>')
    built = f"kernel builds: {', '.join(kernels)}" if kernels else f"kernel {series['kernel']}"
    return (
        '<article class="card">'
        + f"<h3>Kernel {esc(series['kernel'])}, {esc(zfs_line)}</h3>"
        + f'<p class="meta">util-linux {esc(series["util_linux"])}, '
        + f"systemd {esc(series['systemd'])}, {esc(built)}</p>"
        + "".join(rows)
        + "</article>"
    )


def _check_lists(cert: Cert) -> str:
    parts: list[str] = []
    for result in RESULTS:
        ids = [k for k, v in cert.checks.items() if v == result]
        if ids:
            names = esc(", ".join(ids))
            parts.append(f'<p><strong>{result} ({len(ids)})</strong></p><p class="ids">{names}</p>')
    return "".join(parts)


def _history_row(cert: Cert) -> str:
    live = f" ({esc(cert.iso)})" if cert.iso else ""
    kernel = cert.exact.get("kernel", cert.series["kernel"])
    cells = (
        ("Date", f"<time>{esc(cert.date)}</time>"),
        ("zark", esc(cert.zark)),
        ("Ubuntu", esc(cert.series["ubuntu"])),
        ("Where", esc(cert.where) + live),
        ("Level", str(cert.level)),
        ("Result", f'<strong class="{_result_class(cert)}">{esc(_result_text(cert))}</strong>'),
        ("Kernel", esc(kernel)),
        ("OpenZFS", esc(f"{cert.series['zfs']} / {cert.series['zfs_kmod']}")),
        ("Source", _source_html(cert)),
    )
    tds = "".join(f'<td data-label="{label}">{value}</td>' for label, value in cells)
    return (
        f"<tr>{tds}</tr>"
        + '<tr class="detail"><td colspan="9"><details>'
        + f"<summary>The {len(cert.checks)} checks</summary>"
        + f"{_check_lists(cert)}</details></td></tr>"
    )


def repo_version(root: Path = ROOT) -> str:
    """zark's version in the repository this page is built from."""
    text = (root / "lib" / "config.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "unknown"


def repo_commit(root: Path = ROOT) -> str:
    """The commit being built: GITHUB_SHA, else git, else ``unknown``."""
    import os  # pylint: disable=import-outside-toplevel

    sha = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        try:
            sha = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            sha = ""
    return sha[:7] if re.fullmatch(r"[0-9a-f]{7,40}", sha) else "unknown"


def _css() -> str:
    def block(name: str) -> str:
        return "".join(f"--{k}:{v};" for k, v in PALETTE[name].items())

    return (
        f":root{{color-scheme:light dark;{block('light')}}}"
        + f"@media (prefers-color-scheme:dark){{:root{{{block('dark')}}}}}"
        + _CSS_BODY
        # After the base rules, so the dark logo wins when the scheme is dark.
        + "@media (prefers-color-scheme:dark){.logo-light{display:none}.logo-dark{display:block}}"
    )


_CSS_BODY = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);line-height:1.55;
font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
main,header,footer{max-width:62rem;margin:0 auto;padding:1rem}
a{color:var(--fg);text-decoration:underline;text-decoration-color:var(--accent);
text-decoration-thickness:2px;text-underline-offset:3px}
a:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
code,.ids,time{font-family:"DejaVu Sans Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:.9em}
h1{font-size:1.9rem;letter-spacing:-.03em;margin:.2rem 0}
h2{font-size:1.35rem;margin:2rem 0 .5rem;border-bottom:2px solid var(--accent);padding-bottom:.2rem}
h3{font-size:1.05rem;margin:0 0 .2rem}
h4{margin:.8rem 0 .2rem;font-size:.95rem}
.brand{display:flex;align-items:center;gap:1rem}
.logo{width:64px;height:64px}
.logo-dark{display:none}
.lead{color:var(--muted);margin:.2rem 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(18rem,1fr));gap:1rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:1rem}
.meta{color:var(--muted);font-size:.85rem;margin:.1rem 0}
.where ul{list-style:none;margin:0;padding:0}
.where li{padding:.2rem 0;border-top:1px solid var(--border)}
.lv{font-weight:600}
.src,.none{color:var(--muted);font-size:.85rem}
.ok{color:var(--ok)}.bad{color:var(--bad)}
dl.legend{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem}
dl.legend dt{font-weight:600}dl.legend dd{margin:0}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid var(--border);vertical-align:top}
tr.detail td{border-bottom:2px solid var(--border)}
summary{cursor:pointer;color:var(--muted)}
.ids{overflow-wrap:anywhere}
.cmd{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:.6rem 1rem;
overflow-x:auto}
footer{color:var(--muted);font-size:.85rem;border-top:1px solid var(--border);margin-top:2rem}
@media (max-width:640px){
h1{font-size:1.5rem}
thead{display:none}
table,tbody,tr,td{display:block;width:100%}
tr:not(.detail){padding-top:.6rem;border-top:2px solid var(--border)}
td{border:0;padding:.1rem 0}
td[data-label]::before{content:attr(data-label) ": ";color:var(--muted)}
tr.detail td{border:0}
dl.legend{grid-template-columns:1fr}
}
"""


def _logo(name: str, cls: str) -> str:
    svg = (ROOT / "docs" / "images" / "brand" / "svg" / name).read_text(encoding="utf-8")
    svg = re.sub(r'\s(?:width|height)="512"', "", svg)
    svg = svg.replace("<svg ", f'<svg class="logo {cls}" role="img" aria-label="zark" ', 1)
    return re.sub(r"<title>.*?</title>\s*", "", svg, flags=re.S)


def _favicon() -> str:
    svg = (ROOT / "docs" / "images" / "brand" / "favicon" / "favicon.svg").read_text(
        encoding="utf-8"
    )
    return "data:image/svg+xml," + urllib.parse.quote(svg)


def render_page(certs: list[Cert], commit: str, version: str) -> str:  # pylint: disable=too-many-locals
    """The whole page for ``certs``."""
    groups: dict[tuple[str, ...], list[Cert]] = defaultdict(list)
    for c in certs:
        groups[c.platform].append(c)
    by_ubuntu: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for platform in groups:
        by_ubuntu[platform[0]].append(platform)
    sections: list[str] = []
    for ubuntu in sorted(by_ubuntu, key=_vkey, reverse=True):
        platforms = sorted(
            by_ubuntu[ubuntu], key=lambda p: tuple(_vkey(x) for x in p[1:]), reverse=True
        )
        cards = "".join(_card(p, groups[p]) for p in platforms)
        sections.append(f'<h2>Ubuntu {esc(ubuntu)} LTS</h2><div class="cards">{cards}</div>')
    history = sorted(certs, key=lambda c: (c.date, version_key(c.zark)), reverse=True)
    rows = "".join(_history_row(c) for c in history)
    report_url = f"{REPO_URL}/issues/{REPORT_ISSUE}"
    title = "Where zark is certified"
    desc = "Ubuntu series, kernels and OpenZFS versions on which zark's own self-test has passed."
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(title)} - zark</title>
<meta name="description" content="{esc(desc)}">
<meta property="og:title" content="{esc(title)} - zark">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{esc(SITE_URL)}">
<meta property="og:image" content="{esc(SITE_URL)}social-preview.png">
<link rel="icon" href="{esc(_favicon())}">
<style>{_css()}</style>
</head>
<body>
<header>
<div class="brand">{_logo("zark-icon.svg", "logo-light")}{_logo("zark-icon-dark.svg", "logo-dark")}
<div><h1>{esc(title)}</h1>
<p class="lead">zark is bare-metal backup and recovery for ZFS on Ubuntu 24.04 and 26.04 LTS.
This is where its own checks have passed.</p></div></div>
</header>
<main>
<p>Each row is a certificate: one complete run of <code>zark selftest</code>, recorded by
zark itself. The self-test checks the ZFS, kernel, util-linux, systemd and Python behaviours
zark relies on. A certificate says those behaviours held on that platform. It is not a
guarantee that your backup will work, and it says nothing about hardware.</p>
<dl class="legend">
<dt>Level 1</dt><dd>Checks on file-backed pools in RAM. No disk is touched.</dd>
<dt>Level 2</dt><dd>The same, plus checks on a blank scratch USB stick that the test erases.</dd>
<dt>Installed</dt><dd>Run on the installed system.</dd>
<dt>Live</dt><dd>Run from an Ubuntu live session, the way a recovery runs.</dd>
</dl>
{"".join(sections)}
<h2>Add your platform</h2>
<p>Every report widens this page. Run the self-test and share the report:</p>
<pre class="cmd"><code>sudo zark selftest
sudo zark selftest report</code></pre>
<p>The second command prints a link that opens the
<a href="{esc(report_url)}">self-test report issue</a> form with the report filled in. zark
sends nothing itself. The report names no pool, disk, serial, GUID or host, but read it before
you submit. Reports the maintainer has checked appear below as community reports, linked to
their issue.</p>
<h2>All certificates</h2>
<table>
<thead><tr><th>Date</th><th>zark</th><th>Ubuntu</th><th>Where</th><th>Level</th><th>Result</th>
<th>Kernel</th><th>OpenZFS</th><th>Source</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<h2>How this page is made</h2>
<p>It is generated from
<a href="{esc(REPO_URL)}/blob/main/certificates/selftest.json">
<code>certificates/selftest.json</code></a>
(the maintainer's test machine) and from
<a href="{esc(REPO_URL)}/tree/main/pages/community"><code>pages/community/</code></a>
(community reports), by
<a href="{esc(REPO_URL)}/blob/main/pages/matrix.py"><code>pages/matrix.py</code></a>,
which publishes only versions, dates and results. The same data is in
<a href="certificates.json"><code>certificates.json</code></a>.</p>
</main>
<footer>
<p>Built from commit <code>{esc(commit)}</code> of zark {esc(version)}. Certificates keep the
zark version they were made with.</p>
<p>zark is an independent project, not affiliated with Canonical.
<a href="{esc(REPO_URL)}">Source on GitHub</a>.</p>
</footer>
</body>
</html>
"""


def render_json(certs: list[Cert], commit: str) -> str:
    """The sanitized dataset, for other tools."""
    items: list[dict[str, Any]] = []
    for c in certs:
        item: dict[str, Any] = {
            "source": "maintainer" if c.source is None else f"issue-{c.source}",
            "zark": c.zark,
            "date": c.date,
            "where": c.where,
            "level": c.level,
            "series": c.series,
            "exact": c.exact,
            "checks": c.checks,
        }
        if c.iso:
            item["iso"] = c.iso
        items.append(item)
    return (
        json.dumps(
            {"format": FORMAT, "commit": commit, "certificates": items}, indent=2, sort_keys=True
        )
        + "\n"
    )


def build(out: Path, root: Path = ROOT, commit: str | None = None) -> None:
    """Write ``index.html``, ``certificates.json`` and the preview image into ``out``."""
    certs = load_all(root)
    sha = commit if commit is not None else repo_commit(root)
    out.mkdir(parents=True, exist_ok=True)
    _ = (out / "index.html").write_text(
        render_page(certs, sha, repo_version(root)), encoding="utf-8"
    )
    _ = (out / "certificates.json").write_text(render_json(certs, sha), encoding="utf-8")
    _ = shutil.copyfile(
        root / "docs" / "images" / "brand" / "zark-social-preview.png", out / "social-preview.png"
    )


def main(argv: list[str] | None = None) -> int:
    """Command line: ``check`` or ``build --out DIR``."""
    parser = argparse.ArgumentParser(description="zark certificates matrix")
    sub = parser.add_subparsers(dest="cmd", required=True)
    _ = sub.add_parser("check", help="validate every certificate file")
    b = sub.add_parser("build", help="write the site")
    _ = b.add_argument("--out", type=Path, default=Path("_site"))
    args = parser.parse_args(argv)
    try:
        if args.cmd == "check":
            n = len(load_all())
            print(f"{n} certificates, all valid")
        else:
            build(args.out)
            print(f"site written to {args.out}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
