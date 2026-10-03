"""The `ExtensionRegistry` of what every enabled plugin declares for the agent runtime.

Separate from the loader (`agent/extensions/__init__.py`): the declaration types import LangChain,
and the loader is imported by a child's surface-only load, which must stay off the LM stack
(`agent/tests/test_lazy_child_imports.py`).

`build_registry()` is the load-time gate. A plugin enters the registry only when its declaration
is sound: `contribute()` returns a `PluginContributions`, its state classes validate against the
framework's state schema, and — when the plugin ships an `ava-plugin.json` — what it declares
there under the keys the registry owns matches what `contribute()` actually provides, in both
directions. Anything else is a load failure of that plugin: reported, and the plugin contributes
nothing (the rest of the registry is unaffected, like a plugin that fails to import).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from agent.extensions import FACE_MODULE, _enabled_plugin_dirs, _pkg_of
from agent.state import plugin_state_schema
from base.packages.plugins import load_report
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.plugins.gate import check_manifest

# Manifest contribution keys whose runtime side is a `PluginContributions` field this registry owns
# (the key is also the attribution surface id). The remaining keys (sdkNamespaces, sdkWraps, config)
# are still registered at import and compared read-only by the catalog; metrics and inspectWidgets are
# the data registry's (`base.packages.plugins.data_registry`).
GATED_KEYS: tuple[str, ...] = ("hooks", "systemPromptSections")


def _declared(name: str, contribute: Callable[[], object]) -> PluginContributions:
    """One face's `contribute()` result, refused unless it is a `PluginContributions`."""
    contributions = contribute()
    if not isinstance(contributions, PluginContributions):
        raise TypeError(
            f"{name}.{FACE_MODULE}.contribute() returned {type(contributions).__name__}, "
            "not PluginContributions"
        )
    return contributions


def declarations() -> list[tuple[str, Path, PluginContributions]]:
    """(plugin, directory, declaration) for every enabled plugin whose face declares one, ungated.

    Calls `contribute()` on each loaded `agent_runtime` face, in plugin name order. A face with no
    `contribute()` declares nothing; one whose `contribute()` raises or returns something else is
    reported and skipped.
    """
    found: list[tuple[str, Path, PluginContributions]] = []
    for name, plugin_dir in _enabled_plugin_dirs():
        face = sys.modules.get(f"{_pkg_of(plugin_dir)}.{name}.{FACE_MODULE}")
        contribute = getattr(face, "contribute", None)
        if contribute is None:
            continue
        try:
            found.append((name, plugin_dir, _declared(name, contribute)))
        except Exception as exc:
            load_report.report_plugin_load_failure(name, exc)
    return found


def build_registry() -> ExtensionRegistry:
    """What every enabled plugin declares for the agent runtime, as a new registry.

    Pure: nothing is registered anywhere, so building it again is the whole of a registry reload.
    Each declaration passes the gate (see the module docstring) or its plugin is reported as a load
    failure and left out.
    """
    admitted: list[tuple[str, PluginContributions]] = []
    for name, plugin_dir, contributions in declarations():
        try:
            plugin_state_schema(ExtensionRegistry(((name, contributions),)))
            check_manifest(name, plugin_dir, contributions, GATED_KEYS)
        except Exception as exc:
            load_report.report_plugin_load_failure(name, exc)
            continue
        admitted.append((name, contributions))
    return ExtensionRegistry(tuple(admitted))
