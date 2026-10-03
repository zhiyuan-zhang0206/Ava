"""Directory-level fixtures that follow the tests, not a `conftest.py`.

A conftest's fixtures reach only the tests below it, so a test moved into a
package's `tests/` directory silently loses them. This table is a migration
device that lets moved tests keep the fixture closure they had; the end state is
each package's tests declaring the environment they need, or a local conftest
providing it, so entries only come out (the entry count is the number of
environment dependencies not yet made explicit).

Each fixture module in `tests/path_scoped/` is registered for the paths its scope
names, the way pytest registers a conftest for its directory: the module's
fixtures bind to the collector node of the directory or test file, so the autouse
names, their order, their visibility and their override chain are exactly a
conftest's, and a session-scoped autouse fixture is instantiated only for the tests
under the path.

The scope lives next to the tests it governs: a `path_scopes.toml` in a directory maps
a fixture module to `"."` (the directory itself) or to test files in it. `PATH_SCOPES`
is every such file read together, so a moved test directory carries its declaration
along, and a test file moved into a directory edits only that directory's file.

Moving a test: name its file in the destination directory's `path_scopes.toml` under
the same fixture module, and drop it from the old directory's. A path that does not
exist, or a directory that no longer holds a test file, stops the run
(`tests/ci/test_path_scopes.py`). No count of test files is recorded: two moves that
each adjusted one would collide on every merge.

Depends on `FixtureManager.parsefactories(holder=, node=)`, the semi-internal
interface pytest's own conftest handling uses. `tests/ci/test_path_scopes.py` locks
its signature and behavior; a pytest upgrade (which needs manual approval) must
re-check it.
"""

from __future__ import annotations

import importlib
import os
import tomllib
from pathlib import Path
from typing import NamedTuple, cast

import pytest

SCOPE_FILE = "path_scopes.toml"
_SKIPPED_DIRS = frozenset({"node_modules", "__pycache__"})


class Scope(NamedTuple):
    paths: tuple[str, ...]  # directories or test files whose tests the module governs


def discover_scopes(root: Path) -> dict[str, Scope]:
    """Every `path_scopes.toml` under `root`, merged: fixture module -> its scope.

    Modules come out alphabetically, so the autouse names register in one alphabetical
    batch, as in a conftest. Hidden directories (`.git`, `.venv`, `.worktrees`) and
    `node_modules` are not entered.
    """
    found: dict[str, list[str]] = {}
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
            found.setdefault(module, []).extend(
                directory if name == "." else f"{directory}/{name}" for name in listed
            )
    return {module: Scope(tuple(sorted(paths))) for module, paths in sorted(found.items())}


PATH_SCOPES: dict[str, Scope] = discover_scopes(Path(__file__).resolve().parents[2])


def modules_by_path(scopes: dict[str, Scope]) -> dict[str, list[str]]:
    by_path: dict[str, list[str]] = {}
    for module, scope in scopes.items():
        for path in scope.paths:
            by_path.setdefault(path, []).append(module)
    return by_path


def scope_problems(scopes: dict[str, Scope], root: Path) -> list[str]:
    """What is wrong with the table: a missing path, or a path that holds no test file."""
    problems: list[str] = []
    for module, scope in scopes.items():
        missing = [path for path in scope.paths if not (root / path).exists()]
        if missing:
            problems.append(f"{module}: paths do not exist: {missing}")
            continue
        empty = [
            path
            for path in scope.paths
            if (root / path).is_dir() and not any((root / path).rglob("test_*.py"))
        ]
        if empty:
            problems.append(f"{module}: directories hold no test file, drop their entries: {empty}")
    return problems


_MODULES_BY_PATH = modules_by_path(PATH_SCOPES)


def pytest_configure(config: pytest.Config) -> None:
    missing = sorted(path for path in _MODULES_BY_PATH if not (config.rootpath / path).exists())
    if missing:
        raise pytest.UsageError(
            f"a {SCOPE_FILE} names paths that do not exist: {missing}. "
            f"A moved test file must be renamed in its directory's {SCOPE_FILE}, or its "
            "fixtures stop applying."
        )


def pytest_collectstart(collector: pytest.Collector) -> None:
    for module in _MODULES_BY_PATH.get(collector.nodeid, ()):
        collector.session._fixturemanager.parsefactories(
            holder=importlib.import_module(module), node=collector
        )
