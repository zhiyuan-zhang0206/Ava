import ast
from pathlib import Path
from types import ModuleType

import pytest

from tests.fixtures import env_bootstrap, guards, provisioning

ROOT_CONFTEST = Path(__file__).resolve().parents[3] / "conftest.py"
PLUGINS: dict[str, ModuleType] = {
    "tests.fixtures.env_bootstrap": env_bootstrap,
    "tests.fixtures.provisioning": provisioning,
    "tests.fixtures.guards": guards,
}


def _module_level(tree: ast.Module) -> tuple[list[tuple[str, str]], list[str]]:
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
