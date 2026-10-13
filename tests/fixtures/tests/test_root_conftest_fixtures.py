"""The root conftest re-exports only plain fixtures of the plugins it names.

The conftest module registers before every plugin, so an autouse fixture imported
into it would set up ahead of `leak_guard` and be torn down after the guard compares,
and a name that is not a fixture of its plugin would be dead weight in the export.
"""

import ast
from pathlib import Path
from types import ModuleType

import pytest

from tests.fixtures import guards, provisioning

ROOT_CONFTEST = Path(__file__).resolve().parents[3] / "conftest.py"
PLUGINS: dict[str, ModuleType] = {
    "tests.fixtures.provisioning": provisioning,
    "tests.fixtures.guards": guards,
}


def _module_level(tree: ast.Module) -> tuple[list[tuple[str, str]], list[str]]:
    """The (plugin, name) pairs imported from the plugins and the names in `__all__`."""
    imported: list[tuple[str, str]] = []
    exported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in PLUGINS:
            imported.extend((node.module, alias.name) for alias in node.names)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            exported.extend(ast.literal_eval(node.value))
    return imported, exported


def test_root_conftest_exports_exactly_what_it_imports_from_its_plugins() -> None:
    imported, exported = _module_level(ast.parse(ROOT_CONFTEST.read_text()))
    assert imported
    assert sorted(name for _, name in imported) == sorted(exported)


@pytest.mark.parametrize(("plugin", "name"), _module_level(ast.parse(ROOT_CONFTEST.read_text()))[0])
def test_root_conftest_import_is_a_non_autouse_fixture(plugin: str, name: str) -> None:
    definition = getattr(PLUGINS[plugin], name)
    assert not definition._fixture_function_marker.autouse
