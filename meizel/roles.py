"""The top-level packages of the repository and the role of each, declared once.

The root `meizel.toml` holds a `[roles]` table that maps a role to the top-level
directories playing it. This module reads that table; every other place that needs
to know which top-level packages exist (a scan scope, a test host list, a packaging
list) calls `packages()` instead of keeping a copy, or is generated from the table by
`python -m meizel roles` and kept fresh by `--check`.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import cast

from meizel.graph.model import MeizelError
from meizel.graph.universe import MANIFEST

__all__ = ["ROLE_NAMES", "all_packages", "check_table", "packages", "roles"]

# The closed set of roles; an unknown role is an error, never a new category.
ROLE_NAMES = ("application", "templates", "examples", "tooling", "tests", "frontend")
_ROOT = Path(__file__).resolve().parents[1]


def check_table(table: object) -> tuple[dict[str, tuple[str, ...]], list[tuple[str, str]]]:
    """The roles of a parsed `[roles]` table and the `(key, message)` problems found in it."""
    if not isinstance(table, dict):
        return {}, [("roles", "[roles] must be a table")]
    found: dict[str, tuple[str, ...]] = {}
    problems: list[tuple[str, str]] = []
    owner: dict[str, str] = {}
    for role, value in cast("dict[str, object]", table).items():
        if role not in ROLE_NAMES:
            problems.append((role, f"unknown role {role!r}; the roles are {', '.join(ROLE_NAMES)}"))
            continue
        items = cast("list[object]", value) if isinstance(value, list) else None
        if items is None or not all(isinstance(name, str) for name in items):
            problems.append((role, f"role {role!r} must be a list of strings"))
            continue
        names = cast("list[str]", items)
        for name in names:
            if name in owner:
                problems.append((role, f"{name!r} is in role {owner[name]!r} and in {role!r}"))
            owner.setdefault(name, role)
        found[role] = tuple(dict.fromkeys(names))
    return found, problems


@cache
def _read(root: Path) -> dict[str, tuple[str, ...]]:
    path = root / MANIFEST
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise MeizelError(f"{path}: cannot read the roles: {error}") from error
    if "roles" not in document:
        raise MeizelError(f"{path}: has no [roles] table")
    found, problems = check_table(document["roles"])
    if problems:
        raise MeizelError(f"{path}: invalid [roles]: " + "; ".join(m for _, m in problems))
    return found


def roles(root: Path | None = None) -> Mapping[str, tuple[str, ...]]:
    """Role -> its top-level directories, in the order the root manifest lists them."""
    return _read((root or _ROOT).resolve())


def packages(*role_names: str, root: Path | None = None) -> tuple[str, ...]:
    """The top-level directories of the given roles, in the order given, each once."""
    declared = roles(root)
    unknown = [name for name in role_names if name not in ROLE_NAMES]
    if unknown:
        raise MeizelError(f"unknown role(s) {unknown}; the roles are {', '.join(ROLE_NAMES)}")
    return tuple(dict.fromkeys(p for name in role_names for p in declared.get(name, ())))


def all_packages(root: Path | None = None) -> tuple[str, ...]:
    """Every declared top-level directory, by role order."""
    return packages(*ROLE_NAMES, root=root)
