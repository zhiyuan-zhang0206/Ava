import sys
from types import ModuleType


def test_loaded_submodules_are_attributes_of_their_package() -> None:
    assert "meiva.sdk_surface" in sys.modules, "this directory's conftest imports meiva"
    orphans: list[str] = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("meiva.") or not isinstance(module, ModuleType):
            continue
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and hasattr(parent, "__path__") and not hasattr(parent, child):
            orphans.append(name)
    assert not orphans, f"submodules missing from their package object: {sorted(orphans)}"
