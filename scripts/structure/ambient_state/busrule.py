"""The ambient-bus rule: only a package's named roots build the event bus.

Redis and the live-events channel come from `base.events.live.bus.EventBus`, which reads the
settings once, in `EventBus.from_settings()`. In a package listed in `BUS_PACKAGES` that call
may sit only in the modules named for the package (its composition roots, or the entry point of
a command that is its own root); anywhere else it is a site, frozen like the other ambient-state
sites as `path::ambient-bus:EventBus.from_settings`. The gateway builds its bus in the lifespan
(`app.state.bus`) and its routers read it from there, so those packages name no root.
"""

from __future__ import annotations

import ast

from scripts.structure.ambient_state import allowlist as allow
from scripts.structure.ambient_state.scan import Hit

AMBIENT_BUS = "ambient-bus"
FIX = (
    "take the `EventBus` from the composition root named in BUS_PACKAGES (a daemon root, or "
    "`request.app.state.bus` in the gateway); only the root calls `EventBus.from_settings()`"
)
_BUS_MODULES = frozenset({"base.events.live.bus", "base.events.live"})
_SITE = "EventBus.from_settings"


def package_of(rel: str) -> str | None:
    return next((pkg for pkg in allow.BUS_PACKAGES if rel.startswith(f"{pkg}/")), None)


def _bus_names(tree: ast.Module) -> set[str]:
    """The local names a module binds to `EventBus`."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in _BUS_MODULES
        for alias in node.names
        if alias.name == "EventBus"
    }


def _builds_the_bus(call: ast.Call, buses: set[str]) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "from_settings"
        and ast.unparse(func.value) in buses
    )


def builds(tree: ast.Module) -> list[Hit]:
    """Every `EventBus.from_settings()` in one module, whatever package it belongs to."""
    buses = _bus_names(tree)
    return [
        Hit(AMBIENT_BUS, _SITE, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _builds_the_bus(node, buses)
    ]


def hits(tree: ast.Module, rel: str) -> list[Hit]:
    """Every `EventBus.from_settings()` in a governed module that is not one of its roots."""
    package = package_of(rel)
    if package is None or rel in allow.BUS_PACKAGES[package]:
        return []
    return builds(tree)
