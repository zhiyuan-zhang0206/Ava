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
from pathlib import Path

import pytest

from scripts.structure.imports.fixture_scopes import SCOPE_FILE as SCOPE_FILE
from scripts.structure.imports.fixture_scopes import Scope as Scope
from scripts.structure.imports.fixture_scopes import discover_scopes as discover_scopes
from scripts.structure.imports.fixture_scopes import modules_by_path as modules_by_path

PATH_SCOPES: dict[str, Scope] = discover_scopes(Path(__file__).resolve().parents[2])


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
