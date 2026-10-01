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

Moving a test: edit the path in `PATH_SCOPES` (a directory or a single test file,
relative to the repo root, forward slashes); list the new path next to the old one
while both exist. A path may be as narrow as one file, so tests that came from
different directories can sit together. Nothing else changes: the fixture modules do
not know where their tests live. A path that does not exist stops the run, and
`tests/ci/test_path_scopes.py` fails when a test that should be governed is not
under a listed path.

Depends on `FixtureManager.parsefactories(holder=, node=)`, the semi-internal
interface pytest's own conftest handling uses. `tests/ci/test_path_scopes.py` locks
its signature and behavior; a pytest upgrade (which needs manual approval) must
re-check it.
"""

from __future__ import annotations

import importlib

import pytest

# Fixture module -> the paths whose tests it governs. One module per former
# conftest, so the autouse names register in one alphabetical batch, as in a
# conftest.
PATH_SCOPES: dict[str, tuple[str, ...]] = {
    "tests.path_scoped.ava_tests": ("tests/ava",),
    "tests.path_scoped.agent_tests": ("tests/agent",),
    "tests.path_scoped.services_tests": ("tests/services",),
    "tests.path_scoped.structure_tests": ("tests/scripts/structure",),
}

_MODULES_BY_PATH: dict[str, list[str]] = {}
for _module, _paths in PATH_SCOPES.items():
    for _path in _paths:
        _MODULES_BY_PATH.setdefault(_path, []).append(_module)


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
