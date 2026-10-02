"""The ambient-bus rule: a package that holds an `EventBus` handle reaches Redis through no ambient entry.

`base.events.live.redis_client.get_async_redis()` / `sync_redis()` / `open_async_redis()` /
`publish_best_effort()` / `publish_best_effort_sync()` build the client from the live settings at
each call: a process-default Redis that nothing passes in. A package listed in `BUS_PACKAGES`
takes an `EventBus` from its composition root instead; any of those entries in one of its
non-test modules is a site, frozen like the other ambient-state sites as `path::ambient-bus:<name>`,
and `EventBus.from_settings()` outside the roots named for the package is one too. When the last
package is listed the shim functions are deleted.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping

from scripts.structure.ambient_state import allowlist as allow
from scripts.structure.ambient_state.scan import Hit

AMBIENT_BUS = "ambient-bus"
FIX = (
    "take an `EventBus` (or a client it opened) from the composition root named in BUS_PACKAGES "
    "instead of reaching Redis from the live settings; only the root calls "
    "`EventBus.from_settings()`"
)
_SHIM_MODULE = "base.events.live.redis_client"
_SHIM_NAMES: Mapping[str, str] = {
    "get_async_redis": "get_async_redis",
    "sync_redis": "sync_redis",
    "open_async_redis": "open_async_redis",
    "publish_best_effort": "publish_best_effort",
    "publish_best_effort_sync": "publish_best_effort_sync",
}
_BUS_MODULES = frozenset({"base.events.live.bus", "base.events.live"})
_FROM_SETTINGS = "EventBus.from_settings"


def package_of(rel: str) -> str | None:
    return next((pkg for pkg in allow.BUS_PACKAGES if rel.startswith(f"{pkg}/")), None)


class _Names:
    """What a module binds from the shim module and the bus module."""

    def __init__(self, tree: ast.Module) -> None:
        self.modules: set[str] = {_SHIM_MODULE}  # dotted names that reach the shim module
        self.functions: dict[str, str] = {}  # local name -> shim function
        self.buses: set[str] = set()  # local names of EventBus
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == _SHIM_MODULE and alias.asname:
                        self.modules.add(alias.asname)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                self._from_import(node.module, node.names)

    def _from_import(self, module: str, imported: list[ast.alias]) -> None:
        for alias in imported:
            local = alias.asname or alias.name
            if f"{module}.{alias.name}" == _SHIM_MODULE:
                self.modules.add(local)
            elif module == _SHIM_MODULE and alias.name in _SHIM_NAMES:
                self.functions[local] = _SHIM_NAMES[alias.name]
            elif module in _BUS_MODULES and alias.name == "EventBus":
                self.buses.add(local)


def _call_hit(call: ast.Call, names: _Names, *, is_root: bool) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return names.functions.get(func.id)
    if not isinstance(func, ast.Attribute):
        return None
    owner = ast.unparse(func.value)
    if owner in names.modules and func.attr in _SHIM_NAMES:
        return _SHIM_NAMES[func.attr]
    if owner in names.buses and func.attr == "from_settings" and not is_root:
        return _FROM_SETTINGS
    return None


def hits(tree: ast.Module, rel: str) -> list[Hit]:
    """Every ambient Redis entry in a governed module."""
    package = package_of(rel)
    if package is None:
        return []
    names = _Names(tree)
    is_root = rel in allow.BUS_PACKAGES[package]
    return [
        Hit(AMBIENT_BUS, name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (name := _call_hit(node, names, is_root=is_root)) is not None
    ]
