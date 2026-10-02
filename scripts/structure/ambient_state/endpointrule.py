"""The ambient-endpoint rule: only a package's named roots build the endpoint table.

A daemon's port and pidfile come from `base.daemon.endpoints.ServiceEndpoints`, which reads the
settings and `AVA_HOME` once, in `ServiceEndpoints.from_settings()`. In a package listed in
`ENDPOINT_PACKAGES` that call may sit only in the modules named for the package (its composition
roots, or the entry point of a command or probe that is its own root); anywhere else it is a site,
frozen like the other ambient-state sites as `path::ambient-endpoint:ServiceEndpoints.from_settings`.
"""

from __future__ import annotations

import ast

from scripts.structure.ambient_state import allowlist as allow
from scripts.structure.ambient_state.scan import Hit

AMBIENT_ENDPOINT = "ambient-endpoint"
FIX = (
    "take the daemon's `ServiceEndpoint` (or the `ServiceEndpoints` table) from the composition "
    "root named in ENDPOINT_PACKAGES; only the root calls `ServiceEndpoints.from_settings()`"
)
_TABLE_MODULE = "base.daemon.endpoints"
_SITE = "ServiceEndpoints.from_settings"


def package_of(rel: str) -> str | None:
    return next((pkg for pkg in allow.ENDPOINT_PACKAGES if rel.startswith(f"{pkg}/")), None)


def _table_names(tree: ast.Module) -> set[str]:
    """The local names a module binds to `ServiceEndpoints`."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == _TABLE_MODULE
        for alias in node.names
        if alias.name == "ServiceEndpoints"
    }


def _builds_the_table(call: ast.Call, tables: set[str]) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "from_settings"
        and ast.unparse(func.value) in tables
    )


def hits(tree: ast.Module, rel: str) -> list[Hit]:
    """Every `ServiceEndpoints.from_settings()` in a governed module that is not one of its roots."""
    package = package_of(rel)
    if package is None or rel in allow.ENDPOINT_PACKAGES[package]:
        return []
    tables = _table_names(tree)
    return [
        Hit(AMBIENT_ENDPOINT, _SITE, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _builds_the_table(node, tables)
    ]
