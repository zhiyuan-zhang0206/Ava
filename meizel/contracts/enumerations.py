"""Backstop: nothing outside `meizel/` re-lists the packages of a role.

A Python tuple, list or set, or a TOML array, whose strings name every top-level directory
of one role (of three or more directories) is a copy of the declaration in the root
manifest: a package added to the role would silently miss it. Use
`meizel.roles.packages(role)`, or a generated region (`generated.py`), the only exception.
Partial lists (a deliberate subset of a role) are not copies and are not reported.

Tests, documentation and `meizel/` itself are outside the rule: a fixture lists packages on
purpose, and meizel is where the declaration is read. `[tool.importlinter]` is skipped until
the import-linter gate is retired.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from meizel import roles as role_api
from meizel.contracts.generated import regions
from meizel.graph.universe import load_universe

__all__ = ["violations"]

_MIN_ROLE_SIZE = 3
_FIRST = re.compile(r"[/.*\\]")
_SKIPPED_TABLES = ("tool.importlinter",)
_TESTS_DIR = re.compile(r"(^|/)tests?/")


def _outside_rule(path: str) -> bool:
    return (
        path.startswith(("meizel/", "docs/"))
        or _TESTS_DIR.search(path) is not None
        or path.rsplit("/", 1)[-1] == "conftest.py"
    )


def _listed(strings: Iterable[str]) -> set[str]:
    return {_FIRST.split(text, 1)[0] for text in strings if text}


def _covered(strings: Iterable[str], declared: dict[str, tuple[str, ...]]) -> list[str]:
    names = _listed(strings)
    return [
        role
        for role, members in declared.items()
        if len(members) >= _MIN_ROLE_SIZE and set(members) <= names
    ]


def _message(role: str) -> str:
    return (
        f"lists every top-level package of role {role!r}; read it with "
        f"`meizel.roles.packages({role!r})` or generate it (see meizel/contracts/generated.py)"
    )


def _python(
    path: str, text: str, declared: dict[str, tuple[str, ...]], spans: list[tuple[int, int]]
) -> list[str]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple | ast.List | ast.Set):
            continue
        strings = [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if len(strings) < _MIN_ROLE_SIZE:
            continue
        end = node.end_lineno or node.lineno
        if any(node.lineno <= begin and finish <= end for begin, finish in spans):
            continue
        found.extend(
            f"{path}:{node.lineno}: {_message(role)}" for role in _covered(strings, declared)
        )
    return found


def _arrays(value: object, trail: str) -> Iterable[tuple[str, list[str]]]:
    if isinstance(value, dict):
        for key, child in cast("dict[str, object]", value).items():
            here = f"{trail}.{key}" if trail else key
            if here not in _SKIPPED_TABLES:
                yield from _arrays(child, here)
    elif isinstance(value, list):
        items = cast("list[object]", value)
        strings = [x for x in items if isinstance(x, str)]
        if strings:
            yield trail, strings
        for child in items:
            yield from _arrays(child, trail)


def _toml(
    path: str, text: str, declared: dict[str, tuple[str, ...]], spans: list[tuple[int, int]]
) -> list[str]:
    lines = text.splitlines()
    for begin, end in spans:
        for index in range(begin, end - 1):
            lines[index] = ""
    try:
        document = tomllib.loads("\n".join(lines))
    except tomllib.TOMLDecodeError:
        return []
    found: list[str] = []
    for trail, strings in _arrays(document, ""):
        key = trail.rsplit(".", 1)[-1]
        pattern = re.compile(rf"""^\s*["']?{re.escape(key)}["']?\s*=""")
        line = next((n for n, x in enumerate(lines, 1) if pattern.match(x)), 1)
        found.extend(f"{path}:{line}: {_message(role)}" for role in _covered(strings, declared))
    return found


def violations(root: Path) -> list[str]:
    """`path:line: message` for every copy of a role's package list."""
    declared = dict(role_api.roles(root))
    spans: dict[str, list[tuple[int, int]]] = {}
    for region in regions(root):
        spans.setdefault(region.path, []).append((region.begin, region.end))
    found: list[str] = []
    for path in load_universe(root).python_files + tuple(
        p for p in sorted(load_universe(root).files) if p.endswith(".toml")
    ):
        if _outside_rule(path) or path == "meizel.toml":
            continue
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scan = _python if path.endswith(".py") else _toml
        found.extend(scan(path, text, declared, spans.get(path, [])))
    return sorted(found)
