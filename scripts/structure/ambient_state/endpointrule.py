"""The ambient-endpoint rule: a package that holds endpoints looks no daemon's port or pidfile up ambiently.

`base.daemon.health.health_port(name)` and `base.paths.pid_path(name)` read the settings and
`AVA_HOME` at each call. A package listed in `ENDPOINT_PACKAGES` takes its daemon's row (or the
table) of `base.daemon.endpoints.ServiceEndpoints` from its composition root instead; either call
in one of its non-test modules is a site, frozen like the other ambient-state sites as
`path::ambient-endpoint:<name>`, and `ServiceEndpoints.from_settings()` outside the roots named
for the package is one too. When the last package is listed the shim functions are deleted.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping

from scripts.structure.ambient_state import allowlist as allow
from scripts.structure.ambient_state.scan import Hit

AMBIENT_ENDPOINT = "ambient-endpoint"
FIX = (
    "take the daemon's `ServiceEndpoint` (or the `ServiceEndpoints` table) from the composition "
    "root named in ENDPOINT_PACKAGES; only the root calls `ServiceEndpoints.from_settings()`"
)
# module -> the ambient lookups it exports
_LOOKUPS: Mapping[str, frozenset[str]] = {
    "base.daemon.health": frozenset({"health_port"}),
    "base.paths": frozenset({"pid_path"}),
}
_TABLE_MODULE = "base.daemon.endpoints"


def package_of(rel: str) -> str | None:
    return next((pkg for pkg in allow.ENDPOINT_PACKAGES if rel.startswith(f"{pkg}/")), None)


class _Names:
    """What a module binds from the lookup modules and the endpoints module."""

    def __init__(self, tree: ast.Module) -> None:
        self.modules: dict[str, str] = {m: m for m in _LOOKUPS}  # local dotted name -> module
        self.functions: dict[str, str] = {}  # local name -> exported lookup
        self.tables: set[str] = set()  # local names of ServiceEndpoints
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _LOOKUPS and alias.asname:
                        self.modules[alias.asname] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                self._from_import(node.module, node.names)

    def _from_import(self, module: str, imported: list[ast.alias]) -> None:
        for alias in imported:
            local = alias.asname or alias.name
            full = f"{module}.{alias.name}"
            if full in _LOOKUPS:
                self.modules[local] = full
            elif alias.name in _LOOKUPS.get(module, frozenset()):
                self.functions[local] = alias.name
            elif module == _TABLE_MODULE and alias.name == "ServiceEndpoints":
                self.tables.add(local)


def _call_hit(call: ast.Call, names: _Names, *, is_root: bool) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return names.functions.get(func.id)
    if not isinstance(func, ast.Attribute):
        return None
    owner = ast.unparse(func.value)
    module = names.modules.get(owner)
    if module is not None and func.attr in _LOOKUPS[module]:
        return func.attr
    if owner in names.tables and func.attr == "from_settings" and not is_root:
        return "ServiceEndpoints.from_settings"
    return None


def hits(tree: ast.Module, rel: str) -> list[Hit]:
    """Every ambient endpoint lookup in a governed module."""
    package = package_of(rel)
    if package is None:
        return []
    names = _Names(tree)
    is_root = rel in allow.ENDPOINT_PACKAGES[package]
    return [
        Hit(AMBIENT_ENDPOINT, name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (name := _call_hit(node, names, is_root=is_root)) is not None
    ]
