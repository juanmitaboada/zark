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
Tests of the status files (docs/STATUS-FILES.md, contract v1): the schemas,
the fixtures and zark's writer of both files (M7-15).

``tests/test_unit.py`` imports every Test* class here by name, so its runner
finds them; run that file, not this one.
"""

# pylint: disable=protected-access
# Rationale: these tests drive the writer's private state and helpers (the
# lock's descriptor, a command's notification step), as test_unit.py does
# line by line; basedpyright's reportPrivateUsage is off for the same reason.

import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import commands.status as status_cmd
from lib import certificates, live, lock, status, statusdirs, zfs
from lib.log import Log
from tests import schema_check
from tests.mock_sh import MockShell, patch_sh

REPO = Path(__file__).resolve().parent.parent
SCHEMAS = REPO / "docs" / "schema"
FIXTURES = REPO / "tests" / "fixtures" / "status"
PROGRESS_REL = Path("run/zark/progress.json")
STATUS_REL = Path("var/lib/zark/status.json")


def schema(kind: str) -> dict[str, object]:
    """The schema of ``progress`` or ``status``."""
    loaded: dict[str, object] = json.loads(
        (SCHEMAS / f"zark-{kind}.schema.json").read_text("utf-8"),
    )
    return loaded


def problems(doc: object, kind: str) -> list[str]:
    """How ``doc`` fails the schema of ``kind`` (empty: valid): ``progress``
    or ``status``, or ``space`` for the Space object both define."""
    if kind == "space":
        root = schema("status")
        return schema_check.errors(doc, {"$ref": "#/$defs/space", "$defs": root["$defs"]})
    return schema_check.errors(doc, schema(kind))


def _cases() -> list[Path]:
    return sorted(p for p in FIXTURES.iterdir() if p.is_dir())


class TestStatusSchemas:  # pylint: disable=missing-function-docstring
    """M7-14 14a: the validator knows every keyword the schemas use, and
    means by each what JSON Schema 2020-12 does."""

    def test_both_schemas_use_only_known_keywords(self):
        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, sub in node.items():
                    if key in ("properties", "$defs"):
                        assert isinstance(sub, dict), key
                        for child in sub.values():
                            walk(child)
                        continue
                    assert key in schema_check.KEYWORDS, key
                    walk(sub)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        for kind in ("progress", "status"):
            s = schema(kind)
            assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            walk(s)

    def test_an_unknown_keyword_fails_the_check(self):
        try:
            _ = schema_check.errors(1, {"type": "integer", "multipleOf": 2})
        except schema_check.UnknownKeyword as e:
            assert "multipleOf" in str(e)
        else:
            raise AssertionError("multipleOf was not refused")
        for bad in ({"$ref": "#/definitions/x"}, {"type": "decimal"}):
            try:
                _ = schema_check.errors(1, bad)
            except schema_check.UnknownKeyword:
                continue
            raise AssertionError(bad)

    def test_each_keyword_as_json_schema_means_it(self):
        root: dict[str, object] = {
            "type": "object",
            "required": ["a", "b"],
            "properties": {
                "a": {"type": "integer", "minimum": 1, "maximum": 3},
                "b": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/word"}]},
                "c": {"enum": ["x", None]},
                "d": {"const": 1},
                "e": {"type": "array", "items": {"type": ["string", "boolean"]}},
            },
            "$defs": {"word": {"type": "string", "minLength": 2, "pattern": "^[a-z]+\\d$"}},
        }
        assert not schema_check.errors({"a": 2, "b": None}, root)
        full = {"a": 1, "b": "ab1", "c": None, "d": 1, "e": ["s", True]}
        assert not schema_check.errors(full, root)
        bad = [
            ({"a": 2}, "'b' missing"),
            ({"a": True, "b": None}, "$.a: not of type integer"),  # a boolean is no integer
            ({"a": 4, "b": None}, "above 3"),
            ({"a": 0, "b": None}, "below 1"),
            ({"a": 2, "b": "1"}, "matches none of anyOf"),  # too short
            ({"a": 2, "b": "ab"}, "matches none of anyOf"),  # no digit
            ({"a": 2, "b": "ab١"}, "matches none of anyOf"),  # \d is ASCII only
            ({"a": 2, "b": None, "c": "y"}, "not one of"),
            ({"a": 2, "b": None, "d": True}, "$.d: not 1"),  # const 1 is not true
            ({"a": 2, "b": None, "e": [1]}, "$.e[0]: not of type string/boolean"),
            ([], "$: not of type object"),
        ]
        assert schema_check.errors("ab", {"minLength": 3}) == ["$: shorter than 3"]
        for doc, want in bad:
            got = schema_check.errors(doc, root)
            assert any(want in g for g in got), (doc, got)

    def test_json_equality_tells_booleans_from_numbers(self):
        assert schema_check.json_equal(1, 1.0)
        assert not schema_check.json_equal(True, 1)
        assert not schema_check.json_equal(0, False)
        assert schema_check.json_equal({"a": [1, None]}, {"a": [1, None]})
        assert not schema_check.json_equal({"a": [1]}, {"a": [True]})


class TestStatusFixtures:  # pylint: disable=missing-function-docstring
    """The contract's fixtures (tests/fixtures/status/README.md): every file
    valid but the two the README names, and every case's expectations."""

    _PROGRESS = ("idle", "running", "stalled", "orphan", "unknown")
    _STATUS = ("ok", "stale", "newer", "unreadable", "absent")
    _INDICATOR = ("waiting", "running", "attention", "edge", "overdue", "none", "ok")

    def test_the_eighteen_cases_are_there(self):
        names = [p.name for p in _cases()]
        assert len(names) == 18, names
        readme = (FIXTURES / "README.md").read_text("utf-8")
        for name in names:
            assert f"| `{name}` |" in readme, name

    def test_every_file_is_valid_but_the_two_the_readme_names(self):
        checked = 0
        for case in _cases():
            for rel, kind in ((PROGRESS_REL, "progress"), (STATUS_REL, "status")):
                path = case / rel
                if not path.exists():
                    continue
                text = path.read_text("utf-8")
                if case.name == "progress-unreadable" and kind == "progress":
                    try:
                        _ = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    raise AssertionError("progress-unreadable parses")
                got = problems(json.loads(text), kind)
                if case.name == "newer-schema" and kind == "status":
                    assert got == ["$.schema_version: not 1"], got
                    continue
                assert not got, (case.name, kind, got)
                checked += 1
        assert checked == 23, checked  # 16 status.json and 7 progress.json

    def test_every_case_says_what_a_reader_concludes(self):
        for case in _cases():
            exp = json.loads((case / "expected.json").read_text("utf-8"))
            assert set(exp) == {
                "now",
                "progress_process",
                "progress",
                "status",
                "indicator",
                "what",
            }, case.name
            assert exp["progress"] in self._PROGRESS, case.name
            assert exp["status"] in self._STATUS, case.name
            assert exp["indicator"] in self._INDICATOR, case.name
            has_progress = (case / PROGRESS_REL).exists()
            # null without a file, or with one that names no process (unknown)
            if not has_progress or exp["progress"] == "unknown":
                assert exp["progress_process"] is None, case.name
            else:
                assert exp["progress_process"] is not None, case.name
            assert (exp["progress"] == "idle") == (not has_progress), case.name
            assert exp["progress_process"] in (None, "alive", "dead"), case.name
            assert (exp["status"] == "absent") == (not (case / STATUS_REL).exists()), case.name
            assert not schema_check.errors(exp["now"], {"pattern": "^\\d{4}-\\d{2}-\\d{2}T"})

    def test_the_contract_files_are_ascii(self):
        files = [REPO / "docs" / "STATUS-FILES.md", *SCHEMAS.glob("*.json")]
        files += [p for p in FIXTURES.rglob("*") if p.is_file()]
        for path in files:
            data = path.read_bytes()
            assert all(b < 0x80 for b in data), path


class TestStatusDirs:  # pylint: disable=missing-function-docstring
    """M7-10 10a: /run/zark and /var/lib/zark are 0755 whatever the umask,
    and an earlier zark's narrower one is widened; nothing else is touched."""

    def _mode(self, p: Path) -> int:
        return stat.S_IMODE(os.lstat(p).st_mode)

    def test_created_0755_under_any_umask(self):
        for mask in (0o077, 0o027, 0o022, 0o000):
            with tempfile.TemporaryDirectory() as td:
                old = os.umask(mask)
                try:
                    got = statusdirs.ensure(Path(td, "var", "lib", "zark"))
                finally:
                    _ = os.umask(old)
                assert got == Path(td, "var", "lib", "zark")
                assert self._mode(got) == 0o755, (oct(mask), oct(self._mode(got)))

    def test_a_narrower_one_is_widened_a_wider_or_special_one_left(self):
        with tempfile.TemporaryDirectory() as td:
            for mode, want in (
                (0o700, 0o755),
                (0o750, 0o755),
                (0o755, 0o755),
                (0o775, 0o775),
                (0o1777, 0o1777),
                (0o2755, 0o2755),
            ):
                d = Path(td, f"d{mode:o}")
                d.mkdir()
                os.chmod(d, mode)
                _ = statusdirs.ensure(d)
                assert self._mode(d) == want, (oct(mode), oct(self._mode(d)))

    def test_another_owner_a_link_or_a_file_is_not_changed(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td, "d")
            d.mkdir()
            os.chmod(d, 0o700)
            with patch("lib.statusdirs.os.geteuid", return_value=os.geteuid() + 1):
                _ = statusdirs.ensure(d)
            assert self._mode(d) == 0o700
            link = Path(td, "link")
            link.symlink_to(d)
            assert statusdirs.ensure(link) == link
            assert self._mode(d) == 0o700
            f = Path(td, "f")
            _ = f.write_text("x", encoding="utf-8")
            for bad in (f, Path(td, "dangling")):
                if bad.name == "dangling":
                    bad.symlink_to(Path(td, "nowhere"))
                try:
                    _ = statusdirs.ensure(bad)
                except NotADirectoryError:
                    continue
                raise AssertionError(bad)

    def test_every_creator_goes_through_it(self):
        # The lock, the MOTD, the self-test's record, the notification log
        # and the live session's marker (docs/STATUS-FILES.md, Directories).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = os.umask(0o077)
            try:
                lock_path = str(root / "lock" / "zark.lock")
                with (
                    patch.object(lock, "LOCK_PATH", lock_path),
                    patch.dict(lock._STATE, clear=True),
                ):
                    lock.acquire("test")
                    os.close(lock._STATE["fd"])
                status.write_motd("x\n", root / "motd" / "status.motd")
                with (
                    patch.object(certificates, "_write_atomic"),
                    patch.object(certificates, "_add_to_record"),
                ):
                    _ = certificates.write_report({}, str(root / "state" / "selftest"))
                with (
                    patch.object(status, "NOTIFIED_FILE", root / "notified" / "notified"),
                    patch.object(status_cmd.notify, "notify", return_value=True),
                ):
                    status_cmd._notify(status.Status())
                marker = root / "live" / "installer-probe-stopped"
                with (
                    patch.object(live, "PROBE_MARKER", marker),
                    patch.object(live.sh, "is_live_usb", return_value=True),
                    patch.object(live.sh, "run", return_value=MagicMock(ok=True)),
                ):
                    assert live.stop_installer_probe("mount", [], Log(os.devnull))
            finally:
                _ = os.umask(old)
            for d in ("lock", "motd", "state", "notified"):
                assert self._mode(root / d) == 0o755, d
            assert self._mode(root / "state" / "selftest") == 0o700
            assert self._mode(root / "live") == 0o755 and marker.exists()


class TestTmpfiles:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """M7-11: the package creates /run/zark at boot, 0755 root root."""

    def test_the_tmpfiles_line(self):
        text = (REPO / "debian" / "zark.tmpfiles").read_text("ascii")
        assert text == "d /run/zark 0755 root root -\n", text
        assert str(statusdirs.RUN_DIR) == "/run/zark" and statusdirs.MODE == 0o755


class TestSpaceReads:  # pylint: disable=missing-function-docstring
    """R1 and R2: a pool's zfs and zpool figures in one pass, and its
    datasets' usage; nothing partial when a read fails."""

    _CARMEN_RPOOL = (
        "917611225088\t71470788608\n",
        "1005022347264\t902414041088\t102608306176\t89\n",
    )

    def _mock(self, zfs_out: str, zpool_out: str, rc: int = 0) -> MockShell:
        mock = MockShell()
        mock.on("zfs list -Hp -o used,avail rpool").returns(rc, zfs_out)
        mock.on("zpool list -Hp -o size,alloc,free,capacity rpool").returns(rc, zpool_out)
        return mock

    def test_the_space_object_of_a_pool(self):
        with (
            patch_sh(self._mock(*self._CARMEN_RPOOL)),
            patch.object(zfs, "now_utc_iso", lambda: "2026-10-24T08:58:12Z"),
        ):
            sp = zfs.read_space("rpool")
        assert sp is not None
        want = json.loads((FIXTURES / "idle" / STATUS_REL).read_text("utf-8"))
        assert sp.to_json() == want["source_pools"][0]["space"]
        assert not problems(sp.to_json(), "space")

    def test_a_failed_or_odd_read_gives_no_space(self):
        zf, zp = self._CARMEN_RPOOL
        cases: list[tuple[str, str, int]] = [
            (zf, zp, 1),
            ("", zp, 0),
            (zf, "1\t2\t3\n", 0),
            ("12G\t3G\n", zp, 0),
            (zf, "1\t2\t3\t-\n", 0),
        ]
        for zfs_out, zpool_out, rc in cases:
            with patch_sh(self._mock(zfs_out, zpool_out, rc)):
                assert zfs.read_space("rpool") is None, (zfs_out, zpool_out, rc)
        with patch_sh(self._mock(zf, "1\t2\t3\t104\n")):
            sp = zfs.read_space("rpool")
        assert sp is not None and sp.capacity_pct == 100

    def test_the_datasets_of_a_pool(self):
        mock = MockShell()
        mock.on(f"zfs list -Hp -t filesystem,volume -r -o {zfs.DATASET_USAGE} rpool").returns(
            0,
            "rpool\tfilesystem\t300\t10\t0\t290\t0\n"
            + "rpool/keystore\tvolume\t200\t20\t0\t0\t180\n"
            + "rpool/USERDATA\tfilesystem\t90\t5\t15\t70\t0\n"
            + "rpool/USERDATA/home_x\tfilesystem\t70\t50\t20\t0\t0\n",
        )
        with patch_sh(mock):
            got = zfs.read_datasets("rpool") or []
        assert [d.name for d in got] == [
            "rpool",
            "rpool/USERDATA",
            "rpool/USERDATA/home_x",
            "rpool/keystore",
        ]
        own = sum(d.usedbydataset + d.usedbysnapshots + d.usedbyrefreservation for d in got)
        assert own == got[0].used == 300
        assert got[3].to_json() == {
            "name": "rpool/keystore",
            "type": "volume",
            "used_bytes": 200,
            "usedbydataset_bytes": 20,
            "usedbysnapshots_bytes": 0,
            "usedbychildren_bytes": 0,
            "usedbyrefreservation_bytes": 180,
        }

    def test_a_listing_that_fails_gives_no_partial_list(self):
        listing = f"zfs list -Hp -t filesystem,volume -r -o {zfs.DATASET_USAGE} rpool"
        for rc, out in (
            (1, ""),
            (0, ""),
            (0, "rpool\tfilesystem\t1\t1\t0\t0\t0\nrpool/x\tsnapshot\t1\t1\t0\t0\t0\n"),
            (0, "rpool\tfilesystem\t1\t-\t0\t0\t0\n"),
        ):
            mock = MockShell()
            mock.on(listing).returns(rc, out)
            with patch_sh(mock):
                assert zfs.read_datasets("rpool") is None, (rc, out)

    def test_the_fixtures_shares_add_up_to_the_roots_used(self):
        doc = json.loads((FIXTURES / "idle" / STATUS_REL).read_text("utf-8"))
        for pool in doc["source_pools"]:
            rows = pool["datasets"]
            own = sum(
                r["usedbydataset_bytes"]
                + r["usedbysnapshots_bytes"]
                + r["usedbyrefreservation_bytes"]
                for r in rows
            )
            assert own == rows[0]["used_bytes"] == pool["space"]["used_bytes"], pool["name"]
