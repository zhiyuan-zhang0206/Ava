"""Per-test isolation for plugin registrations on the process-global singletons.

A full plugin load (`agent.extensions.load_extensions`, reached through
`agent.extensions.load_extensions()`, the plugin catalog, or a lazy `ava.*` miss) fills
process-global SDK-surface registries at once: `ava.<namespace>`
surfaces and members. Nothing used to put them back, so
one plugin-loading test left the whole set in its xdist worker, and a later test that cleaned
up only part of it saw a half-registered surface (CI backend shard 14/16 on PR #3513).

The guard below returns the registries to empty after any test that started
with none and ended with some. It uses the framework's own reset,
`agent.state.clear_plugin_registrations`, which drops every kind of
SDK-surface registration together, so namespaces and their wraps can never go
out of step. Metering is uninstalled first, because it sits outermost over plugin
wraps. A test that starts with registrations already present (for example from
a module-level plugin import) is left alone: there is no empty state to return
to. The registries asked about are the SDK surface's, the one place a plugin load without an
agent (the schedule runner's in-process script) can register into; the reset itself loads the
agent layer, but only after a test left registrations behind. Hooks, state, prompt sections
and notes are declared values (`PluginContributions`), not process-global registrations.

The framework's own reset leaves one mark behind: `register_namespace` stamps `_qualname`
onto the namespace's module object, and `clear_registered_namespaces` removes the
`ava.<name>` entry but not that stamp. The module outlives the test, so the stamp would stay in
its `__dict__` for every later test in the worker (the leak guard names it as a module attribute
that was added). `drop_plugin_registrations` takes the stamps off the modules it is about to
unregister.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from types import ModuleType

import pytest


def _surface_registrations() -> bool:
    """Whether the SDK surface holds a registered plugin namespace or namespace member."""
    surface = sys.modules.get("ava.sdk_surface.plugins")
    return surface is not None and bool(
        surface._REGISTERED_NAMESPACES or surface._REGISTERED_MEMBERS
    )


def plugin_registrations_present() -> bool:
    """Whether any plugin SDK namespace/member is registered.

    Hooks, state classes, prompt sections and context notes are not process-global: a plugin
    declares them through `contribute()` and the registry built from them is a value the caller
    holds. Only the SDK surface is process-global.
    """
    return _surface_registrations()


def _unstamp_registered_namespaces() -> None:
    """Remove the `_qualname` stamp `register_namespace` put on each registered namespace module.

    Read through `vars`, not `getattr`: `ava.__getattr__` lazily loads plugins for a missing name.
    """
    surface = sys.modules.get("ava.sdk_surface.plugins")
    if surface is None:
        return
    for name in surface._REGISTERED_NAMESPACES:
        module = vars(sys.modules["ava"]).get(name)
        if isinstance(module, ModuleType):
            module.__dict__.pop("_qualname", None)


def drop_plugin_registrations() -> None:
    """Uninstall metering, then reset every plugin registration together."""
    if "ava" in sys.modules:
        from ava.sdk_surface import metering

        metering.uninstall()
        _unstamp_registered_namespaces()
    importlib.import_module("agent.state").clear_plugin_registrations()


@pytest.fixture(autouse=True)
def _restore_plugin_registrations() -> Iterator[None]:
    started_empty = not plugin_registrations_present()
    yield
    if started_empty and plugin_registrations_present():
        drop_plugin_registrations()
