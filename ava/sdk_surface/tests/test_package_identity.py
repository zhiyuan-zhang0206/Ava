"""Every loaded `ava` submodule is still an attribute of its parent package.

A second execution of a package (pytest's importlib mode did it for `ava.sdk_surface` when a
conftest below it loaded before `ava` was imported; `ava/conftest.py` prevents it) leaves the
submodules in `sys.modules` but off the new package object, so `ava.sdk_surface.agent_identity`
fails. Only a session that loads this directory's conftest can show it, hence the test lives here.
"""

import sys
from types import ModuleType


def test_loaded_submodules_are_attributes_of_their_package() -> None:
    assert "ava.sdk_surface" in sys.modules, "this directory's conftest imports ava"
    orphans: list[str] = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("ava.") or not isinstance(module, ModuleType):
            continue
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and hasattr(parent, "__path__") and not hasattr(parent, child):
            orphans.append(name)
    assert not orphans, f"submodules missing from their package object: {sorted(orphans)}"
