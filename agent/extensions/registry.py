"""The `ExtensionRegistry` of what every enabled plugin declares, from the faces a process loaded.

A plugin's faces: `plugin.py` is the SDK surface (`sdk_*`, `skill_sources`, `config`, `flags`), loaded by
every process that runs agent code; `agent_runtime.py` is the agent runtime (hooks, state, system prompt
sections, context notes), loaded only by the agent host. Each exports `contribute()` returning the
`PluginContributions` fields it owns; `declarations(faces)` merges the faces a process has loaded.

Light on purpose: a child's surface-only load builds its registry here and must stay off the LM stack
(`agent/tests/test_lazy_child_imports.py`), so the heavy checks (state validation, LangChain) are imported
only when a plugin actually declares something that needs them.

`build_registry()` is the load-time gate. A plugin enters the registry only when its declaration is sound:
each `contribute()` returns a `PluginContributions`, its state classes validate against the framework's
state schema, and — when the plugin ships an `ava-plugin.json` — what it declares there under the keys of
the loaded faces matches what `contribute()` actually provides, in both directions. Anything else is a
load failure of that plugin: reported, and the plugin contributes nothing (the rest of the registry is
unaffected, like a plugin that fails to import). Installing the SDK surface can refuse a plugin too
(`ava.sdk_surface.install.install` returns the admitted registry).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from agent.extensions import FACE_MODULE, SURFACE_MODULE, _enabled_plugin_dirs, _pkg_of
from base.packages.plugins import load_report
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.plugins.gate import check_manifest

ALL_FACES: tuple[str, ...] = (SURFACE_MODULE, FACE_MODULE)

# Manifest contribution keys whose runtime side is a `PluginContributions` field a face owns (the key is
# also the attribution surface id). Face -> keys. The data faces (`metrics.py`, `inspector.py`) are gated
# by `base.packages.plugins.data_registry`, the provider face by the provider loader.
FACE_KEYS: dict[str, tuple[str, ...]] = {
    SURFACE_MODULE: ("sdkNamespaces", "sdkWraps", "config"),
    FACE_MODULE: ("hooks", "systemPromptSections"),
}


def _declared(plugin: str, face: str, contribute: Callable[[], object]) -> PluginContributions:
    """One face's `contribute()` result, refused unless it is a `PluginContributions`."""
    contributions = contribute()
    if not isinstance(contributions, PluginContributions):
        raise TypeError(
            f"{plugin}.{face}.contribute() returned {type(contributions).__name__}, "
            "not PluginContributions"
        )
    return contributions


def declarations(
    faces: Sequence[str] = ALL_FACES,
) -> list[tuple[str, Path, PluginContributions]]:
    """(plugin, directory, declaration) for every enabled plugin whose loaded faces declare one, ungated.

    Calls `contribute()` on each loaded face of `faces`, in plugin name order, and merges a plugin's faces.
    A face with no `contribute()` declares nothing; one whose `contribute()` raises or returns something
    else is reported and the plugin is skipped.
    """
    found: list[tuple[str, Path, PluginContributions]] = []
    for name, plugin_dir in _enabled_plugin_dirs():
        merged: PluginContributions | None = None
        try:
            for face in faces:
                module = sys.modules.get(f"{_pkg_of(plugin_dir)}.{name}.{face}")
                contribute = getattr(module, "contribute", None)
                if contribute is None:
                    continue
                declared = _declared(name, face, contribute)
                merged = declared if merged is None else merged.merged(declared)
        except Exception as exc:
            load_report.report_plugin_load_failure(name, exc)
            continue
        if merged is not None:
            found.append((name, plugin_dir, merged))
    return found


def build_registry(faces: Sequence[str] = ALL_FACES) -> ExtensionRegistry:
    """What every enabled plugin declares through `faces`, as a new registry.

    Pure: nothing is registered anywhere, so building it again is the whole of a registry reload. Each
    declaration passes the gate (see the module docstring) or its plugin is reported as a load failure
    and left out.
    """
    keys = tuple(key for face in faces for key in FACE_KEYS.get(face, ()))
    admitted: list[tuple[str, PluginContributions]] = []
    for name, plugin_dir, contributions in declarations(faces):
        try:
            if contributions.state:
                from agent.state import plugin_state_schema

                plugin_state_schema(ExtensionRegistry(((name, contributions),)))
            check_manifest(name, plugin_dir, contributions, keys)
        except Exception as exc:
            load_report.report_plugin_load_failure(name, exc)
            continue
        admitted.append((name, contributions))
    return ExtensionRegistry(tuple(admitted))
