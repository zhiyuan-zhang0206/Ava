"""The root conftest re-exports only plain fixtures of `provisioning`.

The conftest module registers before every plugin, so an autouse fixture imported
into it would set up ahead of `leak_guard` and be torn down after the guard compares,
and a name that is not a fixture of `provisioning` would be dead weight in the export.
"""

import ast
from pathlib import Path

import pytest

from tests.fixtures import provisioning

ROOT_CONFTEST = Path(__file__).resolve().parents[3] / "conftest.py"


def _module_level(tree: ast.Module) -> tuple[list[str], list[str]]:
    """The names imported from `provisioning` and the names in `__all__`."""
    imported: list[str] = []
    exported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "tests.fixtures.provisioning":
            imported.extend(alias.name for alias in node.names)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            exported.extend(ast.literal_eval(node.value))
    return imported, exported


def test_root_conftest_exports_exactly_what_it_imports_from_provisioning() -> None:
    imported, exported = _module_level(ast.parse(ROOT_CONFTEST.read_text()))
    assert imported
    assert sorted(imported) == sorted(exported)


@pytest.mark.parametrize("name", _module_level(ast.parse(ROOT_CONFTEST.read_text()))[0])
def test_root_conftest_import_is_a_non_autouse_fixture(name: str) -> None:
    definition = getattr(provisioning, name)
    assert not definition._fixture_function_marker.autouse
