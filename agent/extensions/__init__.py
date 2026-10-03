"""Plugin/extension loader — imports each plugin's faces and installs what they declare.

A plugin declares; the framework registers. Every plugin loads in up to three faces here, each exporting a
pure `contribute()` that returns the `PluginContributions` fields it owns (`registry.py` merges them):

- ``plugin.py`` — the SDK **surface**: `sdk_namespaces`, `sdk_members`, `sdk_wraps`, `skill_sources`,
  `flags`. Its imports must stay off the agent runtime (no `agent.state`, `agent.hooks`,
  `agent.graph.*`, or LangChain chain), because every process that runs agent code loads it.
- ``default_config.py`` — optional sibling: the plugin's **config class** (`config`), declared on its own so
  the gateway and `ava plugins update` can read it without importing `plugin.py`
  (`base/packages/plugins/config_face.py`). Loaded with the surface.
- ``agent_runtime.py`` — optional sibling file: the plugin's **agent runtime** (hooks, state, system
  prompt sections, context notes). Imported on the full path only (the agent process:
  `load_extensions()` at host boot, and `load_agent_faces()` for a child upgrading to the full load). An
  exec / watcher / schedule child never imports it — its boot stays off the graph and LM stacks.

Entry points:

- ``load_extensions(surface=False)`` — full load, the agent-side contract: uninstall the previous load's
  SDK surface, import plugin.py + agent_runtime.py for every enabled plugin, build the gated registry of
  what they declare, install its SDK surface into `ava`, return the admitted registry (plus the enable
  config). The host builds the graph, the checkpoint serde and its turns from that one registry.
- ``load_extensions(surface=True)`` — surfaces only (child contexts); no uninstall.
- ``load_agent_faces()`` — runtime faces only, for a process that already loaded the surfaces (a child upgrading to the full
  load because its request carries a state snapshot).

`registry.py` builds the `ExtensionRegistry` from the loaded faces; `catalog.py` reads back what the
loaded plugins declare (`ava plugins inspect`) and runs this loader in the calling process. This module
stays the loader itself so `importlib.import_module("agent.extensions")` (the `ava` layer's runtime-string
reach) keeps resolving to it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from base import paths
from base.packages.plugins import enable_config as plugins_cfg
from base.packages.plugins import load_report
from base.packages.plugins.config_face import CONFIG_FACE
from base.packages.plugins.extensions import ExtensionRegistry

SURFACE_MODULE = "plugin"
FACE_MODULE = "agent_runtime"


def _pkg_of(plugin_dir: Path) -> str:
    """The dotted package a plugin's modules load under (builtin vs external)."""
    repo_dir = str(paths.repo_plugins_dir())
    return "ava_builtins.plugins" if repo_dir in str(plugin_dir.resolve()) else "plugins"


def _discovered_and_config(
    report: load_report.Reporter | None = None,
) -> tuple[dict[str, Path], plugins_cfg.PluginsConfig]:
    """Discover plugins + read the enable config, fail-soft on dangling entries.

    A config entry whose plugin directory is gone (interrupted upgrade, manual
    rm) must not block the load: each dangling name is reported through the one
    canonical reporter (once per process, via `plugins_cfg.report_dangling`)
    and treated as disabled (the same contract the loader always had —
    2026-08-28 ava_ledger incident).
    """
    discovered = plugins_cfg.discover_plugins()
    known = set(discovered)
    try:
        config = plugins_cfg.load(known)
    except plugins_cfg.DanglingPlugin as exc:
        plugins_cfg.report_dangling(exc, report)
        config = plugins_cfg.load(known, allow_dangling=True)
    return discovered, config


def _enabled_plugin_dirs(
    report: load_report.Reporter | None = None,
) -> list[tuple[str, Path]]:
    """(name, plugin_dir) for every enabled discovered plugin, name-sorted."""
    discovered, config = _discovered_and_config(report)
    return [
        (name, discovered[name])
        for name in sorted(config.plugins)
        if config.plugins[name].enabled and name in discovered
    ]


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
    from ava.sdk_surface.plugin_loader import safe_load_plugin_module

    safe_load_plugin_module(face_py, name=name, pkg=pkg, module=module, report=report)


@dataclass(frozen=True)
class LoadedExtensions:
    """What a load produced: the enable config and the registry of the plugins admitted."""

    config: plugins_cfg.PluginsConfig
    registry: ExtensionRegistry


def load_extensions(
    *, surface: bool = False, report: load_report.Reporter | None = None
) -> LoadedExtensions:
    """Read plugins_config.json, import the enabled plugins' faces, install what they declare.

    Full form (`surface=False`, the default): uninstall the previous round's SDK surface, import every
    enabled plugin's `plugin.py` followed by its `agent_runtime.py` face, build the gated registry of
    what the faces declare and install its SDK surface into `ava` (the one place `ava` is written:
    `ava.sdk_surface.install`). Called at host boot and by the agent-side tooling. The returned registry
    holds the plugins admitted — one the install refused (a namespace conflict, a bad wrap target, a
    config that does not bind) is reported and absent.

    Surface form (`surface=True`): import only the `plugin.py` surfaces and install the surface-only
    registry — the load an agent-launched child runs. No uninstall: a child loads once per process and
    never runs the agent runtime, and the graph/LM stacks stay off its boot path.

    The import loop is fail-soft (2026-08-28 ava_ledger incident): a broken plugin — a missing sibling
    module, a syntax error, a top-level exception — is skipped with a loud report, never a blocked
    `import ava` / host boot for the whole cluster. The remaining enabled plugins keep loading; the
    half-executed module was dropped from `sys.modules` so a later reload retries from a clean slate.

    ``report`` receives each contained failure instead of the canonical reporter (log + telemetry);
    `ava plugins verify` passes a collector, so the failures come back as values.
    """
    from agent.extensions.registry import ALL_FACES, SURFACE_FACES, build_registry
    from ava.sdk_surface import install as sdk_install
    from ava.sdk_surface.plugin_loader import safe_load_plugin_module

    if not surface:
        sdk_install.uninstall()

    discovered, config = _discovered_and_config(report)

    for name in sorted(config.plugins):
        if not config.plugins[name].enabled:
            continue
        # Invariant: load() already validated DanglingPlugin, name must be in
        # discovered; an enabled plugin silently vanishing with 0 log is the
        # hardest bug to chase. Assert rather than silent continue.
        assert name in discovered, f"load() invariant broken: {name} not in discovered"  # noqa: S101
        plugin_dir = discovered[name]
        pkg = _pkg_of(plugin_dir)
        if (
            safe_load_plugin_module(plugin_dir / "plugin.py", name=name, pkg=pkg, report=report)
            is None
        ):
            continue
        _load_face(name, plugin_dir, pkg=pkg, module=CONFIG_FACE, report=report)
        if not surface:
            _load_face(name, plugin_dir, pkg=pkg, report=report)

    registry = build_registry(SURFACE_FACES if surface else ALL_FACES, report)
    return LoadedExtensions(config, sdk_install.install(registry, report))


def load_agent_faces() -> None:
    """Import the runtime face of every enabled plugin whose surface is loaded.

    Faces only: no reset (it would tear down the already-loaded surface),
    no plugin.py re-execution. A face already in `sys.modules` is skipped — its
    registrations are process-global and already active. Faces of plugins whose
    surface has not loaded are skipped too: the surface owns the module
    identity the face imports against.
    """
    for name, plugin_dir in _enabled_plugin_dirs():
        pkg = _pkg_of(plugin_dir)
        if f"{pkg}.{name}.{FACE_MODULE}" in sys.modules:
            continue
        if f"{pkg}.{name}.{SURFACE_MODULE}" not in sys.modules:
            continue
        _load_face(name, plugin_dir, pkg=pkg)
