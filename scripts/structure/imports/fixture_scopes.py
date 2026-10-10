"""Pure declarative fixture scope discovery shared by pytest and CI dependency queries."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import NamedTuple, cast

SCOPE_FILE = "path_scopes.toml"
_SKIPPED_DIRS = frozenset({"node_modules", "__pycache__"})


class Scope(NamedTuple):
    paths: tuple[str, ...]  # directories or test files whose tests the module governs


class Declaration(NamedTuple):
    source: str  # repository-relative TOML input, retained for reverse impact
    module: str
    paths: tuple[str, ...]


def declarations(root: Path) -> tuple[Declaration, ...]:
    """Read each declaration once, retaining its source instead of only the merged scope."""
    found: list[Declaration] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIPPED_DIRS]
        if SCOPE_FILE not in files:
            continue
        directory = Path(current).relative_to(root).as_posix()
        declared = tomllib.loads((Path(current) / SCOPE_FILE).read_text(encoding="utf-8"))
        for module, names in declared.items():
            listed = cast("list[str]", names)
            if not isinstance(names, list) or not all(isinstance(n, str) for n in listed):
                raise ValueError(f"{directory}/{SCOPE_FILE}: {module} must be a list of names")
            paths = tuple(directory if name == "." else f"{directory}/{name}" for name in listed)
            source = (Path(directory) / SCOPE_FILE).as_posix()
            found.append(Declaration(source, module, paths))
    return tuple(sorted(found))


def discover_scopes(root: Path) -> dict[str, Scope]:
    """Every `path_scopes.toml` under `root`, merged: fixture module -> its scope.

    Modules come out alphabetically, so the autouse names register in one alphabetical
    batch, as in a conftest. Hidden directories (`.git`, `.venv`, `.worktrees`) and
    `node_modules` are not entered.
    """
    found: dict[str, list[str]] = {}
    for declaration in declarations(root):
        found.setdefault(declaration.module, []).extend(declaration.paths)
    return {module: Scope(tuple(sorted(paths))) for module, paths in sorted(found.items())}


def modules_by_path(scopes: dict[str, Scope]) -> dict[str, list[str]]:
    by_path: dict[str, list[str]] = {}
    for module, scope in scopes.items():
        for path in scope.paths:
            by_path.setdefault(path, []).append(module)
    return by_path
