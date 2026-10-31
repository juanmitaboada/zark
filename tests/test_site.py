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
Tests of the certificates matrix generator (pages/matrix.py): the shipped
certificates build, nothing but allowlisted fields reaches the page, the
page needs nothing from outside, and its palette keeps its contrast.

``tests/test_unit.py`` imports every Test* class here by name, so its runner
finds them; run that file, not this one.
"""

import copy
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pages"))

import matrix as gen  # type: ignore[import-not-found]  # noqa: E402  # pylint: disable=wrong-import-position,import-error

from lib import certificates  # noqa: E402  # pylint: disable=wrong-import-position


def _entry(**over: Any) -> dict[str, Any]:
    base = json.loads((ROOT / "certificates" / "selftest.json").read_text(encoding="utf-8"))[0]
    out = copy.deepcopy(base)
    out.update(over)
    return out


def _tree(td: str, community: dict[str, Any] | None = None) -> Path:
    """A copy of what the generator reads, with ``community`` files added."""
    root = Path(td)
    (root / "certificates").mkdir()
    (root / "lib").mkdir()
    (root / "pages" / "community").mkdir(parents=True)
    _ = (root / "certificates" / "selftest.json").write_text(
        (ROOT / "certificates" / "selftest.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _ = (root / "lib" / "config.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")
    for name, data in (community or {}).items():
        text = data if isinstance(data, str) else json.dumps(data)
        _ = (root / "pages" / "community" / name).write_text(text, encoding="utf-8")
    return root


class TestSiteCertificates:  # pylint: disable=missing-function-docstring
    """The certificates file and the generator agree on the format."""

    def test_the_shipped_certificates_are_all_valid(self):
        certs = gen.load_all(ROOT)
        raw = json.loads((ROOT / "certificates" / "selftest.json").read_text(encoding="utf-8"))
        assert len(certs) == len(raw) > 0
        assert all(c.source is None for c in certs)

    def test_the_format_is_the_one_zark_writes(self):
        assert gen.FORMAT == certificates.FORMAT
        for raw in json.loads(
            (ROOT / "certificates" / "selftest.json").read_text(encoding="utf-8")
        ):
            # What zark's own entries carry is all the generator knows how to read.
            assert set(raw) <= {
                "zark", "checklist", "date", "where", "iso", "level", "partial", "series",
                "exact", "checks", "bridge", "stopped",
            }, sorted(raw)  # fmt: skip

    def test_a_report_is_turned_into_an_entry_the_generator_reads(self):
        report = {"format": certificates.FORMAT, "certificate": _entry(), "results": []}
        entry = certificates.certificate_entry(report)
        assert gen.validate_entry(entry, 7).source == 7

    def test_versions_sort_with_a_release_after_its_rcs(self):
        order = ["2.0.0-rc9", "2.0.0-rc10", "2.0.0", "2.1.0-rc1", "2.1.1"]
        assert sorted(order[::-1], key=gen.version_key) == order

    def test_a_partial_stopped_or_malformed_entry_is_refused(self):
        bad = [
            _entry(partial=True),
            _entry(stopped=True),
            _entry(where="cloud"),
            _entry(level=3),
            _entry(level=True),
            _entry(zark="2.1.1 <script>"),
            _entry(date="today"),
            _entry(checks={}),
            _entry(checks={"KEY-01.1": "MAYBE"}),
            _entry(checks={"not an id": "PASS"}),
            _entry(series={"ubuntu": "26.04"}),
        ]
        for raw in bad:
            try:
                gen.validate_entry(raw, None)
            except ValueError:
                continue
            raise AssertionError(f"accepted: {raw}")

    def test_a_check_that_failed_is_counted_and_shown(self):
        raw = _entry()
        key = next(iter(raw["checks"]))
        raw["checks"][key] = "FAIL"
        cert = gen.validate_entry(raw, None)
        assert cert.count("FAIL") == 1
        assert "1 FAIL" in gen.render_page([cert], "abc1234", "9.9.9")


class TestSitePrivacy:  # pylint: disable=missing-function-docstring
    """Nothing that could name a machine reaches the page or the dataset."""

    def test_free_text_fields_that_do_not_match_are_dropped(self):
        raw = _entry(
            iso='Ubuntu 26.04 "Juan\'s remix" - Release amd64 (20260423.1)',
            bridge="0951:16ae",
            hostname="carmen",
            exact={
                "kernel": "7.0.0-38-generic-carmen.example.org",
                "ubuntu": "26.04.1 LTS (Resolute Raccoon)",
                "zfs": "zfs-2.4.1-1ubuntu5.1",
                "python": "3.14.4",
            },
        )
        cert = gen.validate_entry(raw, 12)
        assert cert.iso is None
        assert "kernel" not in cert.exact
        assert cert.exact["ubuntu"] == "26.04.1 LTS (Resolute Raccoon)"
        page = gen.render_page([cert], "abc1234", "9.9.9") + gen.render_json([cert], "abc1234")
        for secret in ("0951", "16ae", "carmen", "Juan", "bridge", "hostname"):
            assert secret not in page, secret

    def test_an_official_live_image_line_is_shown(self):
        raw = _entry(
            where="live", iso='Ubuntu 24.04.5 LTS "Noble Numbat" - Release amd64 (20260914)'
        )
        cert = gen.validate_entry(raw, None)
        assert cert.iso is not None and "Noble Numbat" in gen.render_page([cert], "x", "9.9.9")

    def test_the_shipped_page_has_no_bridge_and_no_machine_names(self):
        certs = gen.load_all(ROOT)
        page = gen.render_page(certs, "abc1234", "9.9.9") + gen.render_json(certs, "abc1234")
        raw = json.loads((ROOT / "certificates" / "selftest.json").read_text(encoding="utf-8"))
        for entry in raw:
            if entry.get("bridge"):
                assert entry["bridge"] not in page
        for word in ("bridge", "eli", "carmen", "backup2", "/dev/", "by-id"):
            assert not re.search(rf"\b{re.escape(word)}\b", page), word

    def test_every_value_is_escaped(self):
        cert = gen.validate_entry(_entry(), 5)
        page = gen.render_page([cert], "<script>", "<b>x</b>")
        assert "&lt;script&gt;" in page
        assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
        assert "<script" not in page.lower()

    def test_community_files_are_named_by_issue_and_fully_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tree(td, {"41.json": [_entry()]})
            assert [c.source for c in gen.load_all(root)][-1] == 41
        for name, data in (
            ("notes.json", [_entry()]),
            ("41.json", [_entry(partial=True)]),
            ("42.json", "not json"),
            ("43.json", []),
            ("44.json", {"zark": "2.1.1"}),
        ):
            with tempfile.TemporaryDirectory() as td:
                root = _tree(td, {name: data})
                try:
                    _ = gen.load_all(root)
                except ValueError:
                    continue
                raise AssertionError(f"accepted {name}")

    def test_a_community_row_links_its_issue_and_the_maintainers_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tree(td, {"41.json": [_entry(date="2026-11-02", zark="2.1.2")]})
            certs = gen.load_all(root)
            page = gen.render_page(certs, "abc1234", "9.9.9")
        assert "Community report #41" in page and "/issues/41" in page
        assert "Maintainer's test machine" in page


class TestSitePage:  # pylint: disable=missing-function-docstring
    """The page is static, self-contained, reproducible and readable."""

    @staticmethod
    def _build() -> tuple[Path, str]:
        out = Path(tempfile.mkdtemp())
        gen.build(out, ROOT, commit="abc1234")
        return out, (out / "index.html").read_text(encoding="utf-8")

    def test_the_page_is_ascii_and_needs_nothing_from_outside(self):
        _, page = self._build()
        assert page.isascii()
        assert "<script" not in page.lower()
        assert "@import" not in page and "url(http" not in page
        for tag in re.findall(r"<(?:link|img|iframe|source|video|audio)\b[^>]*>", page):
            urls = re.findall(r'(?:src|href)="([^"]*)"', tag)
            assert all(u.startswith("data:") for u in urls), tag
        # The only absolute links are anchors to the project and its issues, plus the
        # absolute address of the preview image in the metadata.
        for url in re.findall(r'<a [^>]*href="(https?://[^"]*)"', page):
            assert url.startswith(gen.REPO_URL), url

    def test_two_builds_of_the_same_inputs_are_the_same_bytes(self):
        a, _ = self._build()
        b, _ = self._build()
        for name in ("index.html", "certificates.json", "social-preview.png"):
            assert (a / name).read_bytes() == (b / name).read_bytes(), name

    def test_the_dataset_holds_only_the_allowed_keys(self):
        out, _ = self._build()
        data = json.loads((out / "certificates.json").read_text(encoding="utf-8"))
        assert data["format"] == 1
        for item in data["certificates"]:
            assert set(item) <= {
                "source", "zark", "date", "where", "level", "series", "exact", "checks", "iso"
            }, sorted(item)  # fmt: skip

    def test_every_platform_and_certificate_is_on_the_page(self):
        certs = gen.load_all(ROOT)
        _, page = self._build()
        assert page.count("<article") == len({c.platform for c in certs})
        assert page.count('<tr class="detail">') == len(certs)
        assert f"/issues/{gen.REPORT_ISSUE}" in page
        assert "not affiliated with Canonical" in page

    def test_the_palette_keeps_its_contrast_in_both_schemes(self):
        for scheme, colours in gen.PALETTE.items():
            for fg, bg in gen.TEXT_PAIRS:
                ratio = gen.contrast(colours[fg], colours[bg])
                assert ratio >= 4.5, (scheme, fg, bg, round(ratio, 2))

    def test_the_palette_is_the_brand_one(self):
        brand = {"#E8592A", "#1B2236", "#F2F4F7", "#9AA4B2", "#4A5568", "#0E1422"}
        used = {v for colours in gen.PALETTE.values() for v in colours.values()}
        assert brand <= used
        assert gen.PALETTE["light"]["accent"] == gen.PALETTE["dark"]["accent"] == "#E8592A"

    def test_the_site_is_not_shipped_in_the_package(self):
        install = (ROOT / "debian" / "install").read_text(encoding="utf-8").split()
        assert "pages" not in install
