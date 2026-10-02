"""The shape the endpoint, bus and clock rules share: `Handle.from_settings()` only in the roots.

A kernel handle (`ServiceEndpoints`, `EventBus`, `Clock`) is built from the live settings by its
`from_settings()` constructor. In a package listed in the rule's registry that call may sit only
in the modules named for the package (its composition roots, or the entry point of a command or
probe that is its own root); anywhere else it is a site, frozen like the other ambient-state sites
as `path::<rule>:<Class>.from_settings`.
"""

from __future__ import annotations

import ast
from collections.abc import Collection

from scripts.structure.ambient_state import allowlist as allow
from scripts.structure.ambient_state.scan import Hit


class FromSettingsRule:
    """One `Handle.from_settings()`-only-in-roots rule over one `allowlist` registry."""

    def __init__(
        self, *, rule: str, class_name: str, modules: Collection[str], registry: str
    ) -> None:
        self.rule = rule
        self._class_name = class_name
        self._modules = frozenset(modules)
        self._registry = registry
        self.site = f"{class_name}.from_settings"

    def _roots(self) -> dict[str, frozenset[str]]:
        return getattr(allow, self._registry)

    def package_of(self, rel: str) -> str | None:
        return next((pkg for pkg in self._roots() if rel.startswith(f"{pkg}/")), None)

    def _local_names(self, tree: ast.Module) -> set[str]:
        """The local names a module binds to the handle class."""
        return {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in self._modules
            for alias in node.names
            if alias.name == self._class_name
        }

    def builds(self, tree: ast.Module) -> list[Hit]:
        """Every `Handle.from_settings()` in one module, whatever package it belongs to."""
        names = self._local_names(tree)
        return [
            Hit(self.rule, self.site, node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "from_settings"
            and ast.unparse(node.func.value) in names
        ]

    def hits(self, tree: ast.Module, rel: str) -> list[Hit]:
        """Every `Handle.from_settings()` in a governed module that is not one of its roots."""
        package = self.package_of(rel)
        if package is None or rel in self._roots()[package]:
            return []
        return self.builds(tree)
