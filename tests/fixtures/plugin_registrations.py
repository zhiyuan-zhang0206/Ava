"""Per-test isolation for plugin registrations on the process-global singletons.

A full plugin load (`agent/_extensions.load_extensions`, reached through
`_build._load_extensions()`, the plugin catalog, or a lazy `ava.*` miss) fills
process-global registries at once: prompt sections, `ava.<namespace>`
surfaces and members, hooks, state fields. Nothing used to put them back, so
one plugin-loading test left the whole set in its xdist worker. A later test
that cleaned up with `ava.clear_registered_namespaces()` then dropped the
namespaces but not the sections, and the next `build_system_prompt()` called
the ava_code section, which needs `ava.cwd`: `module 'ava' has no attribute
'cwd'`. The failure hit only the tests that happened to share that worker
(CI backend shard 14/16 on PR #3513).

The guard below returns the registries to empty after any test that started
with none and ended with some. It uses the framework's own reset,
`agent.state.clear_plugin_registrations`, which drops every kind of
registration together, so sections and their namespaces can never go out of
step. Metering is uninstalled first, because it sits outermost over plugin
wraps. A test that starts with registrations already present (for example from
a module-level plugin import) is left alone: there is no empty state to return
to. Modules are read from `sys.modules` rather than imported, so a test
process that never loaded the agent layer does not load it here.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator

import pytest


def plugin_registrations_present() -> bool:
    """Whether any plugin prompt section, SDK namespace/member or state field is registered."""
    prompt = sys.modules.get("agent.graph._system_prompt")
    surface = sys.modules.get("ava.sdk_surface.plugins")
    state = sys.modules.get("agent.state")
    return bool(
        (
            prompt is not None
            and len(prompt._SYSTEM_PROMPT_SECTIONS) > prompt._FRAMEWORK_SECTION_COUNT
        )
        or (surface is not None and (surface._REGISTERED_NAMESPACES or surface._REGISTERED_MEMBERS))
        or (state is not None and state._EXTRA_FIELDS)
    )


def drop_plugin_registrations() -> None:
    """Uninstall metering, then reset every plugin registration together."""
    metering = sys.modules.get("ava.sdk_metering")
    if metering is not None:
        metering.uninstall()
    importlib.import_module("agent.state").clear_plugin_registrations()


@pytest.fixture(autouse=True)
def _restore_plugin_registrations() -> Iterator[None]:
    started_empty = not plugin_registrations_present()
    yield
    if started_empty and plugin_registrations_present():
        drop_plugin_registrations()
