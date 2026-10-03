"""The `ExtensionRegistry` of what every enabled plugin declares for the agent runtime.

Separate from the loader (`agent/extensions/__init__.py`): the declaration types import LangChain,
and the loader is imported by a child's surface-only load, which must stay off the LM stack
(`agent/tests/test_lazy_child_imports.py`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from agent.extensions import FACE_MODULE, _enabled_plugin_dirs, _pkg_of
from base.packages.plugins import load_report
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions


def _declared(name: str, contribute: Callable[[], object]) -> PluginContributions:
    """One face's `contribute()` result, refused unless it is a `PluginContributions`."""
    contributions = contribute()
    if not isinstance(contributions, PluginContributions):
        raise TypeError(
            f"{name}.{FACE_MODULE}.contribute() returned {type(contributions).__name__}, "
            "not PluginContributions"
        )
    return contributions


def build_registry() -> ExtensionRegistry:
    """What every enabled plugin declares for the agent runtime, as a new registry.

    Calls `contribute()` on each loaded `agent_runtime` face, in plugin name order. Pure: nothing
    is registered anywhere, so calling it again after a reload is the whole of a registry reload.
    A face with no `contribute()` contributes nothing; one whose `contribute()` raises or returns
    something else is reported and skipped, like a plugin that fails to import.
    """
    declared: list[tuple[str, PluginContributions]] = []
    for name, plugin_dir in _enabled_plugin_dirs():
        face = sys.modules.get(f"{_pkg_of(plugin_dir)}.{name}.{FACE_MODULE}")
        contribute = getattr(face, "contribute", None)
        if contribute is None:
            continue
        try:
            contributions = _declared(name, contribute)
        except Exception as exc:
            load_report.report_plugin_load_failure(name, exc)
            continue
        declared.append((name, contributions))
    return ExtensionRegistry(tuple(declared))
