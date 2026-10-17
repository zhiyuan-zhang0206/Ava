from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel

from base.packages.plugins import load_report
from base.packages.plugins.config_face import CONFIG_FACE
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.plugins.gate import check_manifest
from base.packages.plugins.loading import StateGate
from base.packages.plugins.loading.discovery import (
    FACE_MODULE,
    SURFACE_MODULE,
    enabled_plugin_dirs,
    pkg_of,
)
from base.packages.plugins.manifest import load_manifest
from base.telemetry import report_sink_failure

SURFACE_FACES: tuple[str, ...] = (SURFACE_MODULE, CONFIG_FACE)
_PLUGIN_PACKAGES = frozenset({"meiva_builtins.plugins", "plugins"})
ALL_FACES: tuple[str, ...] = (*SURFACE_FACES, FACE_MODULE)

FACE_KEYS: dict[str, tuple[str, ...]] = {
    SURFACE_MODULE: ("sdkNamespaces", "sdkWraps"),
    CONFIG_FACE: ("config",),
    FACE_MODULE: ("hooks", "systemPromptSections", "compactReminderSections"),
}


def _declared(plugin: str, face: str, contribute: Callable[[], object]) -> PluginContributions:
    contributions = contribute()
    if not isinstance(contributions, PluginContributions):
        raise TypeError(
            f"{plugin}.{face}.contribute() returned {type(contributions).__name__}, "
            "not PluginContributions"
        )
    return contributions


# meiva:ref base/packages/plugins/docs/runtime-contributions.meiva.okf.md#runtime-face-admission
def declarations(
    faces: Sequence[str] = ALL_FACES, report: load_report.Reporter | None = None
) -> list[tuple[str, Path, PluginContributions]]:
    found: list[tuple[str, Path, PluginContributions]] = []
    for name, plugin_dir in enabled_plugin_dirs(report):
        merged: PluginContributions | None = None
        try:
            load_manifest(plugin_dir)
            for face in faces:
                module = sys.modules.get(f"{pkg_of(plugin_dir)}.{name}.{face}")
                contribute = getattr(module, "contribute", None)
                if contribute is None:
                    continue
                declared = _declared(name, face, contribute)
                merged = declared if merged is None else merged.merged(declared)
        except Exception as exc:
            load_report.reporter(report)(name, exc)
            continue
        if merged is not None:
            found.append((name, plugin_dir, merged))
    return found


def loaded_state_classes() -> frozenset[type[BaseModel]]:
    classes: set[type[BaseModel]] = set()
    for dotted, module in tuple(sys.modules.items()):
        parts = dotted.split(".")
        if parts[-1] != FACE_MODULE or ".".join(parts[:-2]) not in _PLUGIN_PACKAGES:
            continue
        contribute = getattr(module, "contribute", None)
        if contribute is None:
            continue
        try:
            classes.update(_declared(parts[-2], FACE_MODULE, contribute).state)
        except Exception as exc:
            report_sink_failure(
                f"plugin {parts[-2]} agent_runtime face (its state classes are missing "
                "from the serializer allowlist)",
                exc,
            )
    return frozenset(classes)


# meiva:ref base/packages/plugins/docs/runtime-contributions.meiva.okf.md#runtime-face-admission
def build_registry(
    faces: Sequence[str] = ALL_FACES,
    report: load_report.Reporter | None = None,
    *,
    validate_state: StateGate | None = None,
) -> ExtensionRegistry:
    keys = tuple(key for face in faces for key in FACE_KEYS.get(face, ()))
    admitted: list[tuple[str, PluginContributions]] = []
    for name, plugin_dir, contributions in declarations(faces, report):
        if contributions.state and validate_state is None:
            raise RuntimeError(
                f"plugin {name!r} declares state classes but this load supplied no state gate "
                "(`validate_state`); only a caller that runs agent code can load an agent-runtime face"
            )
        try:
            if contributions.state and validate_state is not None:
                validate_state(ExtensionRegistry(((name, contributions),)))
            check_manifest(name, plugin_dir, contributions, keys)
        except Exception as exc:
            load_report.reporter(report)(name, exc)
            continue
        admitted.append((name, contributions))
    return ExtensionRegistry(tuple(admitted))
