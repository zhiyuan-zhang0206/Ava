"""Plugin face import: load each enabled plugin's faces and build the registry of what they declare.

A plugin declares; the framework registers. Every plugin loads in up to three faces here, each exporting a
pure `contribute()` that returns the `PluginContributions` fields it owns (`registry.py` merges them):

- ``plugin.py`` — the SDK **surface**: `sdk_namespaces`, `sdk_members`, `sdk_wraps`, `skill_sources`,
  `flags`. Its imports must stay off the agent runtime (no `agent.state`, `agent.hooks`,
  `agent.graph.*`, or LangChain chain), because every process that runs agent code loads it.
- ``default_config.py`` — optional sibling: the plugin's **config class** (`config`), declared on its own so
  the gateway and `meiva plugins update` can read it without importing `plugin.py`
  (`base/packages/plugins/config_face.py`). Loaded with the surface.
- ``agent_runtime.py`` — optional sibling file: the plugin's **agent runtime** (hooks, state, system
  prompt sections, context notes). Imported on the full path only (the agent process, and
  `load_agent_faces()` for a child upgrading to the full load). An exec / watcher / schedule child
  never imports it — its boot stays off the graph and LM stacks.

Entry points:

- ``import_faces(surface=False)`` — import plugin.py + agent_runtime.py for every enabled plugin and
  build the gated registry of what they declare.
- ``import_faces(surface=True)`` — surfaces only (child contexts).
- ``load_agent_faces()`` — runtime faces only, for a process that already loaded the surfaces (a child
  upgrading to the full load because its request carries a state snapshot).

Installing the registry's SDK surface into `meiva` is `meiva.sdk_surface.extensions.load_extensions`; this
module imports nothing from `meiva` or `agent`. A plugin whose agent-runtime face declares state is
validated against the agent kernel's state schema, which the caller that runs agent code supplies as
`validate_state` (`agent.state.plugin_state_schema`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from base.packages.plugins import enable_config as plugins_cfg
from base.packages.plugins import load_report
from base.packages.plugins.config_face import CONFIG_FACE
from base.packages.plugins.extensions import ExtensionRegistry
from base.packages.plugins.loading import StateGate
from base.packages.plugins.loading.discovery import (
    FACE_MODULE,
    SURFACE_MODULE,
    discovered_and_config,
    enabled_plugin_dirs,
    pkg_of,
)
from base.packages.plugins.loading.plugin_loader import safe_load_plugin_module
from base.packages.plugins.loading.registry import ALL_FACES, SURFACE_FACES, build_registry


def _load_face(
    name: str,
    plugin_dir: Path,
    *,
    pkg: str,
    module: str = FACE_MODULE,
    report: load_report.Reporter | None = None,
) -> None:
    """Import one of a plugin's optional faces (`agent_runtime` by default), when it ships one.

    Same by-path loader and fail-soft containment as the surface: a face whose
    module body raises is reported and skipped without taking down the plugin
    (the surface stays loaded).
    """
    face_py = plugin_dir / f"{module}.py"
    if not face_py.exists():
        return
    safe_load_plugin_module(face_py, name=name, pkg=pkg, module=module, report=report)


@dataclass(frozen=True)
class LoadedExtensions:
    """What a load produced: the enable config and the registry of the plugins admitted."""

    config: plugins_cfg.PluginsConfig
    registry: ExtensionRegistry


def import_faces(
    *,
    surface: bool = False,
    report: load_report.Reporter | None = None,
    validate_state: StateGate | None = None,
) -> LoadedExtensions:
    """Read plugins_config.json, import the enabled plugins' faces, build the registry they declare.

    Full form (`surface=False`, the default): import every enabled plugin's `plugin.py` followed by its
    `agent_runtime.py` face. Surface form (`surface=True`): import only the `plugin.py` surfaces and
    the config class — the load an agent-launched child runs.

    The import loop is fail-soft (2026-08-28 meiva_ledger incident): a broken plugin — a missing sibling
    module, a syntax error, a top-level exception — is skipped with a loud report, never a blocked
    `import meiva` / host boot for the whole cluster. The remaining enabled plugins keep loading; the
    half-executed module was dropped from `sys.modules` so a later reload retries from a clean slate.

    ``report`` receives each contained failure instead of the canonical reporter (log + telemetry);
    `meiva plugins verify` passes a collector, so the failures come back as values.
    """
    discovered, config = discovered_and_config(report)

    for name in sorted(config.plugins):
        if not config.plugins[name].enabled:
            continue
        # Invariant: load() already validated DanglingPlugin, name must be in
        # discovered; an enabled plugin silently vanishing with 0 log is the
        # hardest bug to chase. Assert rather than silent continue.
        assert name in discovered, f"load() invariant broken: {name} not in discovered"  # noqa: S101
        plugin_dir = discovered[name]
        pkg = pkg_of(plugin_dir)
        if (
            safe_load_plugin_module(plugin_dir / "plugin.py", name=name, pkg=pkg, report=report)
            is None
        ):
            continue
        _load_face(name, plugin_dir, pkg=pkg, module=CONFIG_FACE, report=report)
        if not surface:
            _load_face(name, plugin_dir, pkg=pkg, report=report)

    registry = build_registry(
        SURFACE_FACES if surface else ALL_FACES, report, validate_state=validate_state
    )
    return LoadedExtensions(config, registry)


def load_agent_faces() -> None:
    """Import the runtime face of every enabled plugin whose surface is loaded.

    Faces only: no reset (it would tear down the already-loaded surface),
    no plugin.py re-execution. A face already in `sys.modules` is skipped — its
    registrations are process-global and already active. Faces of plugins whose
    surface has not loaded are skipped too: the surface owns the module
    identity the face imports against.
    """
    for name, plugin_dir in enabled_plugin_dirs():
        pkg = pkg_of(plugin_dir)
        if f"{pkg}.{name}.{FACE_MODULE}" in sys.modules:
            continue
        if f"{pkg}.{name}.{SURFACE_MODULE}" not in sys.modules:
            continue
        _load_face(name, plugin_dir, pkg=pkg)
