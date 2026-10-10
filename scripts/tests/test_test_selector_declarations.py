"""Fixture declarations are runtime inputs, including when removed from the head tree."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci import test_selector
from scripts.ci.test_impact import build_impact

_TABLE = "base/path_scopes.toml"
_TEST = "base/consumer/tests/test_bound.py"
_MODULE = "tests.support.fixture"


def _write(root: Path, name: str, text: str = "") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _repo(root: Path) -> Path:
    _write(root, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["base/**/tests"]\n')
    _write(root, _TEST, "def test_bound(): pass\n")
    _write(root, "base/unrelated/tests/test_filler.py", "def test_filler(): pass\n")
    _write(
        root,
        ".test_durations",
        json.dumps({"base/unrelated/tests/test_filler.py::test_filler": 1000}),
    )
    _write(root, "tests/support/fixture.py")
    _write(root, _TABLE, f'"{_MODULE}" = ["consumer/tests"]\n')
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git commands against a test-owned checkout
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_changed_declaration_input_selects_its_bound_test(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    impact = build_impact(root, frozenset(test_selector.collectable_test_paths(root)))
    assert impact.tests_by_input[_TABLE] == {_TEST}
    assert impact.tests_by_input["tests/support/fixture.py"] == {_TEST}
    assert test_selector.package_tests(_TABLE, test_selector.load_checkout(root)) == set()
    result = test_selector.select_tests([_TABLE], repo_root=root)
    assert result.decision == "SELECTED", result.as_json()
    assert result.tests == (_TEST,)


@pytest.mark.parametrize("delete_table", [False, True])
def test_base_declaration_input_preserves_removed_binding_impact(
    tmp_path: Path, delete_table: bool
) -> None:
    root = _repo(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    if delete_table:
        (root / _TABLE).unlink()
    else:
        _write(root, _TABLE, f'"{_MODULE}" = []\n')
    assert (
        _TABLE
        not in build_impact(
            root, frozenset(test_selector.collectable_test_paths(root))
        ).tests_by_input
    )
    result = test_selector.select_tests([_TABLE], repo_root=root, base_ref="HEAD")
    assert result.decision == "SELECTED", result.as_json()
    assert result.tests == (_TEST,)


def test_missing_declared_first_party_module_runs_its_scope_visibly(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "tests/support/__init__.py")
    (root / "tests/support/fixture.py").unlink()
    result = test_selector.select_tests([_TABLE], repo_root=root)
    assert result.decision == "SELECTED", result.as_json()
    assert result.tests == (_TEST,)
    assert result.diagnostics == (
        f"head:{_TABLE}:0: Declared first-party fixture module is missing: {_MODULE}",
    )


def test_missing_module_in_an_unbound_declaration_does_not_force_full(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/unbound/path_scopes.toml", '"tests.missing" = ["absent"]\n')
    result = test_selector.select_tests([_TABLE], repo_root=root)
    assert result.decision == "SELECTED", result.as_json()
    assert not result.diagnostics


def test_path_scope_readers_are_modeled_by_their_scoped_edges(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(
        root,
        "tests/fixtures/path_scopes.py",
        "import importlib\nfrom pathlib import Path\n"
        "def load(module, current):\n"
        "    importlib.import_module(module)\n"
        "    (Path(current) / 'path_scopes.toml').read_text()\n",
    )
    _write(root, "conftest.py", 'pytest_plugins = ["tests.fixtures.path_scopes"]\n')
    impact = build_impact(root, frozenset(test_selector.collectable_test_paths(root)))
    # The fixture-module import is modeled by the scoped edges; the table read in
    # this file is not the declared reader's, so it stays visible.
    assert [item.reason for item in impact.unknown] == [
        "Resource read has no proven repository or external path anchor"
    ]
    assert impact.tests_by_input[_TABLE] == {_TEST}
