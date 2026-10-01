"""Directory-level fixtures that follow the tests, not a `conftest.py`.

A conftest's fixtures reach only the tests below it, so a test moved into a
package's `tests/` directory silently loses them. This table is a migration
device that lets moved tests keep the fixture closure they had; the end state is
each package's tests declaring the environment they need, or a local conftest
providing it, so entries only come out (the entry count is the number of
environment dependencies not yet made explicit).

Each fixture module in `tests/path_scoped/` is registered for the paths listed in
`PATH_SCOPES`, the way pytest registers a conftest for its directory: the module's
fixtures bind to the collector node of the directory or test file, so the autouse
names, their order, their visibility and their override chain are exactly a
conftest's, and a session-scoped autouse fixture is instantiated only for the tests
under the path.

Moving a test: edit `paths` in its `PATH_SCOPES` entry (directories or single test
files, relative to the repo root, forward slashes); list the new path next to the old
one while both exist. A path may be as narrow as one file, so tests that came from
different directories can sit together. `test_files` does not change on a move: it
counts the test files the paths hold, and `tests/ci/test_path_scopes.py` fails when
fewer are found, which is what a test moved out without its new path listed looks
like. Nothing else changes: the fixture modules do not know where their tests live.
A path that does not exist stops the run.

Depends on `FixtureManager.parsefactories(holder=, node=)`, the semi-internal
interface pytest's own conftest handling uses. `tests/ci/test_path_scopes.py` locks
its signature and behavior; a pytest upgrade (which needs manual approval) must
re-check it.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import NamedTuple

import pytest


class Scope(NamedTuple):
    paths: tuple[str, ...]  # directories or test files whose tests the module governs
    test_files: int  # how many test files those paths hold; unchanged by a move


# Fixture module -> its scope. One module per former conftest, so the autouse names
# register in one alphabetical batch, as in a conftest.
PATH_SCOPES: dict[str, Scope] = {
    "tests.path_scoped.agent_tests": Scope(("tests/agent",), 128),
    "tests.path_scoped.ava_tests": Scope(("tests/ava",), 65),
    "tests.path_scoped.cli_tests": Scope(("tests/cli",), 119),
    "tests.path_scoped.db_authority_tests": Scope(("tests/lifecycle/db_authority",), 9),
    "tests.path_scoped.gateway_tests": Scope(("tests/gateway",), 116),
    "tests.path_scoped.integration_tests": Scope(("tests/integration",), 17),
    "tests.path_scoped.services_tests": Scope(("tests/services",), 161),
    "tests.path_scoped.structure_tests": Scope(("tests/scripts/structure",), 16),
}


def modules_by_path(scopes: dict[str, Scope]) -> dict[str, list[str]]:
    by_path: dict[str, list[str]] = {}
    for module, scope in scopes.items():
        for path in scope.paths:
            by_path.setdefault(path, []).append(module)
    return by_path


def scope_problems(scopes: dict[str, Scope], root: Path) -> list[str]:
    """What is wrong with the table: a missing path, or fewer test files than recorded."""
    problems: list[str] = []
    for module, scope in scopes.items():
        missing = [path for path in scope.paths if not (root / path).exists()]
        if missing:
            problems.append(f"{module}: paths do not exist: {missing}")
            continue
        found = {
            file.resolve()
            for path in scope.paths
            for file in (
                [root / path] if (root / path).is_file() else (root / path).rglob("test_*.py")
            )
        }
        if len(found) < scope.test_files:
            problems.append(
                f"{module}: its paths hold {len(found)} test files, {scope.test_files} are "
                "recorded; a test that moved without its new path listed here has lost "
                "these fixtures"
            )
    return problems


_MODULES_BY_PATH = modules_by_path(PATH_SCOPES)


def pytest_configure(config: pytest.Config) -> None:
    missing = sorted(path for path in _MODULES_BY_PATH if not (config.rootpath / path).exists())
    if missing:
        raise pytest.UsageError(
            f"tests/fixtures/path_scopes.py names paths that do not exist: {missing}. "
            "A moved test directory or file must be renamed in PATH_SCOPES, or its "
            "fixtures stop applying."
        )


def pytest_collectstart(collector: pytest.Collector) -> None:
    for module in _MODULES_BY_PATH.get(collector.nodeid, ()):
        collector.session._fixturemanager.parsefactories(
            holder=importlib.import_module(module), node=collector
        )
