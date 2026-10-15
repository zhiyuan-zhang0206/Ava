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
