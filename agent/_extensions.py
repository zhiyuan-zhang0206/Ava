"""Plugin/extension loader — the surface and agent-runtime faces.

The loader lives in the agent layer and is reached from `ava` via importlib
(a runtime string, not a static import), so the ava-layer module keeps no
static dependency on agent. Moved here from `agent.graph._build` (task #3633):
a process that only needs the plugin surface must not import the graph kernel
to reach it.

Every plugin loads in up to two faces:

- ``plugin.py`` — the SDK **surface**: namespaces, wraps, and the other
  registrations an agent-launched child needs to run agent-authored code.
  Its imports must stay off the agent runtime (no `agent.state`,
  `agent.hooks`, `agent.graph.*`, or LangChain chain).
- ``agent_runtime.py`` — optional sibling file: the plugin's **agent-runtime
  registrations** (state fields, graph hooks, system-prompt sections).
  Imported on the full path only (the agent process: `load_agent_faces()`
  after the host boot's `scan_and_load`, and `load_extensions()` per graph
  build). An exec / watcher / schedule child never imports it — its boot
  stays off the graph and LM stacks (task #3633).

Entry points:

- ``load_extensions(surface=False)`` — full load, the agent-side contract
  (reset, then import plugin.py + agent_runtime.py for every enabled plugin).
- ``load_extensions(surface=True)`` — surfaces only (child contexts).
- ``load_agent_faces()`` — runtime faces only, for a process that already
  loaded the surfaces (host boot after `scan_and_load`; a child upgrading to
  the full load because its request carries a state snapshot).
"""

from __future__ import annotations

import sys
from pathlib import Path

from shared import paths
from shared import plugins_config as plugins_cfg

FACE_MODULE = "agent_runtime"


def _pkg_of(plugin_dir: Path) -> str:
    """The dotted package a plugin's modules load under (builtin vs external)."""
    repo_dir = str(paths.repo_plugins_dir())
    return "ava_builtins.plugins" if repo_dir in str(plugin_dir.resolve()) else "plugins"


def _discovered_and_config() -> tuple[dict[str, Path], plugins_cfg.PluginsConfig]:
    """Discover plugins + read the enable config, fail-soft on dangling entries.

    A config entry whose plugin directory is gone (interrupted upgrade, manual
    rm) must not block the load: each dangling name is reported through the one
    canonical reporter (once per process, via `plugins_cfg._report_dangling`)
    and treated as disabled (the same contract the loader always had —
    2026-08-28 ava_ledger incident).
    """
    discovered = plugins_cfg._discover_plugins()
    known = set(discovered)
    try:
        config = plugins_cfg.load(known)
    except plugins_cfg.DanglingPlugin as exc:
        plugins_cfg._report_dangling(exc)
        config = plugins_cfg.load(known, allow_dangling=True)
    return discovered, config


def _enabled_plugin_dirs() -> list[tuple[str, Path]]:
    """(name, plugin_dir) for every enabled discovered plugin, name-sorted."""
    discovered, config = _discovered_and_config()
    return [
        (name, discovered[name])
        for name in sorted(config.plugins)
        if config.plugins[name].enabled and name in discovered
    ]


def _load_face(name: str, plugin_dir: Path, *, pkg: str) -> None:
    """Import one plugin's `agent_runtime` face, when it ships one.

    Same by-path loader and fail-soft containment as the surface: a face whose
    module body raises is reported and skipped without taking down the plugin
    (the surface stays loaded).
    """
    face_py = plugin_dir / f"{FACE_MODULE}.py"
    if not face_py.exists():
        return
    from ava.sdk_surface.plugin_loader import safe_load_plugin_module

    safe_load_plugin_module(face_py, name=name, pkg=pkg, module=FACE_MODULE)


def load_extensions(*, surface: bool = False) -> plugins_cfg.PluginsConfig:
    """Read plugins_config.json, trigger import side-effects for enabled plugins.

    Full form (`surface=False`, the default): reset the previous round, then
    import every enabled plugin's `plugin.py` (hook registration + Layer A wrap
    + system prompt contribution) followed by its `agent_runtime.py` face
    (state fields, hooks, prompt sections). Called per graph build and by the
    agent-side tooling.

    Surface form (`surface=True`): import only the `plugin.py` surfaces — the
    load an agent-launched child runs. No reset: a child loads once per process
    and never runs the agent runtime, and importing `agent.state` for the reset
    would put the graph/LM stack back on the child's boot path (task #3633).

    The import loop is fail-soft (2026-08-28 ava_ledger incident): a broken
    plugin — a missing sibling module, a syntax error, a top-level exception —
    is skipped with a loud report, never a blocked `import ava` / graph build
    for the whole cluster. The remaining enabled plugins keep loading; the
    half-executed module was dropped from `sys.modules` so a later reload
    retries from a clean slate.
    """
    if not surface:
        from agent.state import clear_plugin_registrations

        clear_plugin_registrations()

    discovered, config = _discovered_and_config()

    from ava.sdk_surface.plugin_loader import safe_load_plugin_module
    from shared.plugin_context import PluginContext

    for name in sorted(config.plugins):
        if not config.plugins[name].enabled:
            continue
        # Invariant: load() already validated DanglingPlugin, name must be in
        # discovered; an enabled plugin silently vanishing with 0 log is the
        # hardest bug to chase. Assert rather than silent continue.
        assert name in discovered, f"load() invariant broken: {name} not in discovered"  # noqa: S101
        plugin_dir = discovered[name]
        pkg = _pkg_of(plugin_dir)
        with PluginContext(name):
            if safe_load_plugin_module(plugin_dir / "plugin.py", name=name, pkg=pkg) is None:
                continue
            if not surface:
                _load_face(name, plugin_dir, pkg=pkg)

    # Bind plugin config from disk — only batch bind after all plugin imports
    # complete, so that when hook callbacks actually fire,
    # `ava._settings.plugins.<n>` is ready. Missing disk image auto-writes
    # default; schema drift raises (guides `ava plugins update`).
    from shared.plugin_config_registry import bind_from_disk

    bind_from_disk()

    # Install the SDK-usage recorder over the final ava.* surface. Runs last so
    # it wraps plugin-registered namespaces / members and sits outermost of any
    # plugin `ava.extend.wrap` layer (one count per agent call). Idempotent; a
    # plugin reload re-runs it after clear_wraps restores plugin-touched
    # targets.
    from ava import sdk_metering

    sdk_metering.install()

    return config


def load_agent_faces() -> None:
    """Import the runtime face of every enabled plugin whose surface is loaded.

    Faces only: no reset (it would tear down the already-loaded surface),
    no plugin.py re-execution. A face already in `sys.modules` is skipped — its
    registrations are process-global and already active. Faces of plugins whose
    surface has not loaded are skipped too: the surface owns the module
    identity the face imports against.
    """
    from shared.plugin_context import PluginContext

    for name, plugin_dir in _enabled_plugin_dirs():
        pkg = _pkg_of(plugin_dir)
        if f"{pkg}.{name}.{FACE_MODULE}" in sys.modules:
            continue
        if f"{pkg}.{name}.plugin" not in sys.modules:
            continue
        with PluginContext(name):
            _load_face(name, plugin_dir, pkg=pkg)
