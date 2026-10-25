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
A JSON Schema validator for exactly the keywords of ``docs/schema/`` (M7-14 14a).

The suite is standard library only, so it does not use ``jsonschema``.
This validator knows the keywords the two status schemas use, with their
Draft 2020-12 meaning, and raises :class:`UnknownKeyword` on any other: a
schema that grows a keyword fails the suite until this file learns it.

  * annotations, never checked: ``$schema``, ``$id``, ``title``,
    ``description``, ``$defs`` (the definitions themselves are checked when
    a ``$ref`` reaches them);
  * ``type`` (a name or a list), ``const``, ``enum``, ``required``,
    ``properties``, ``items``, ``anyOf``, ``$ref`` (``#/$defs/<name>`` only),
    ``pattern``, ``minLength``, ``minimum``, ``maximum``.

JSON equality, not Python's: ``true`` is not ``1``. ``pattern`` is searched,
not matched, as in JSON Schema, with ASCII classes (``\\d`` is ``[0-9]``, as
in ECMA-262).
"""

import re
from collections.abc import Callable, Mapping

_ANNOTATIONS = frozenset({"$schema", "$id", "title", "description", "$defs"})
_ASSERTIONS = frozenset(
    {
        "type",
        "const",
        "enum",
        "required",
        "properties",
        "items",
        "anyOf",
        "$ref",
        "pattern",
        "minLength",
        "minimum",
        "maximum",
    },
)
KEYWORDS = _ANNOTATIONS | _ASSERTIONS


class UnknownKeyword(Exception):
    """The schema uses a keyword this validator does not implement."""


_TYPES: dict[str, Callable[[object], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def _is_type(value: object, name: str) -> bool:
    check = _TYPES.get(name)
    if check is None:
        raise UnknownKeyword(f"type {name!r}")
    return check(value)


def json_equal(a: object, b: object) -> bool:
    """Equality as JSON has it: a boolean equals only a boolean."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
    if _is_number(a) and _is_number(b):
        return a == b
    return type(a) is type(b) and a == b


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def errors(instance: object, schema: Mapping[str, object]) -> list[str]:
    """Every way ``instance`` fails ``schema`` (a root schema with its
    ``$defs``), as ``<json path>: <what>``; empty when it is valid."""
    out: list[str] = []
    _check(instance, schema, schema, "$", out)
    return out


def _ref(root: Mapping[str, object], ref: object) -> Mapping[str, object]:
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        raise UnknownKeyword(f"$ref {ref!r}")
    defs = root.get("$defs")
    target = defs.get(ref[len("#/$defs/") :]) if isinstance(defs, dict) else None
    if not isinstance(target, dict):
        raise UnknownKeyword(f"$ref {ref!r} names no definition")
    return target


def _check(  # noqa: C901 # pylint: disable=too-many-branches
    value: object,
    schema: Mapping[str, object],
    root: Mapping[str, object],
    path: str,
    out: list[str],
) -> None:
    unknown = sorted(set(schema) - KEYWORDS)
    if unknown:
        raise UnknownKeyword(f"{path}: {', '.join(unknown)}")
    if "$ref" in schema:
        _check(value, _ref(root, schema["$ref"]), root, path, out)
    if "type" in schema:
        names = schema["type"]
        wanted = [names] if isinstance(names, str) else names
        if not isinstance(wanted, list) or not all(isinstance(n, str) for n in wanted):
            raise UnknownKeyword(f"{path}: type {names!r}")
        if not any(_is_type(value, str(n)) for n in wanted):
            out.append(f"{path}: not of type {'/'.join(str(n) for n in wanted)}")
            return
    if "const" in schema and not json_equal(value, schema["const"]):
        out.append(f"{path}: not {schema['const']!r}")
    if "enum" in schema:
        options = schema["enum"]
        if not isinstance(options, list):
            raise UnknownKeyword(f"{path}: enum {options!r}")
        if not any(json_equal(value, o) for o in options):
            out.append(f"{path}: {value!r} not one of {options!r}")
    if "anyOf" in schema:
        branches = schema["anyOf"]
        if not isinstance(branches, list):
            raise UnknownKeyword(f"{path}: anyOf {branches!r}")
        if not any(_passes(value, b, root, path) for b in branches):
            out.append(f"{path}: matches none of anyOf")
    if isinstance(value, str):
        _check_string(value, schema, path, out)
    if _is_number(value):
        _check_number(value, schema, path, out)
    if isinstance(value, dict):
        _check_object(value, schema, root, path, out)
    if isinstance(value, list) and "items" in schema:
        items = schema["items"]
        if not isinstance(items, dict):
            raise UnknownKeyword(f"{path}: items {items!r}")
        for i, item in enumerate(value):
            _check(item, items, root, f"{path}[{i}]", out)


def _passes(value: object, branch: object, root: Mapping[str, object], path: str) -> bool:
    if not isinstance(branch, dict):
        raise UnknownKeyword(f"{path}: anyOf branch {branch!r}")
    sub: list[str] = []
    _check(value, branch, root, path, sub)
    return not sub


def _check_string(value: str, schema: Mapping[str, object], path: str, out: list[str]) -> None:
    if "minLength" in schema:
        n = schema["minLength"]
        if not isinstance(n, int):
            raise UnknownKeyword(f"{path}: minLength {n!r}")
        if len(value) < n:
            out.append(f"{path}: shorter than {n}")
    if "pattern" in schema:
        pat = schema["pattern"]
        if not isinstance(pat, str):
            raise UnknownKeyword(f"{path}: pattern {pat!r}")
        if not re.search(pat, value, re.ASCII):
            out.append(f"{path}: {value!r} does not match {pat}")


def _check_number(value: object, schema: Mapping[str, object], path: str, out: list[str]) -> None:
    assert isinstance(value, int | float)
    lo, hi = schema.get("minimum"), schema.get("maximum")
    if lo is not None:
        if not _is_number(lo):
            raise UnknownKeyword(f"{path}: minimum {lo!r}")
        assert isinstance(lo, int | float)
        if value < lo:
            out.append(f"{path}: {value} below {lo}")
    if hi is not None:
        if not _is_number(hi):
            raise UnknownKeyword(f"{path}: maximum {hi!r}")
        assert isinstance(hi, int | float)
        if value > hi:
            out.append(f"{path}: {value} above {hi}")


def _check_object(
    value: dict[object, object],
    schema: Mapping[str, object],
    root: Mapping[str, object],
    path: str,
    out: list[str],
) -> None:
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise UnknownKeyword(f"{path}: required {required!r}")
    out.extend(f"{path}: {name!r} missing" for name in required if name not in value)
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        raise UnknownKeyword(f"{path}: properties {props!r}")
    for name, sub in props.items():
        if name in value:
            if not isinstance(sub, dict):
                raise UnknownKeyword(f"{path}.{name}: {sub!r}")
            _check(value[name], sub, root, f"{path}.{name}", out)
