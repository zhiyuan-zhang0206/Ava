"""Per-turn plugin-config view — the agent-scoped read path for plugin `per_agent` fields.

Prerequisite 2 of Phase 1 in `future/infra/agent-runner-as-server.md`, and the
plugin-scope half of what `shared/config/turn_view.py` did for framework
`Settings`. `_PLUGIN_CONFIGS` (`shared/plugin_config_registry.py`) is a
process-global `plugin -> frozen instance` map that boot mutates in place from
the agent's `config_overlay`. One agent per process makes that exact; in the
hosted runner one process serves many agents, so the last booted overlay would
be every agent's plugin config.

The replacement is the same shape as the framework view: a contextvar carrying
`plugin -> {field: value}` overrides, bound before the turn task is created, and
a read that layers those overrides over the process-global instance.

    config_overlay (agents_meta)  >  the bound disk image (_PLUGIN_CONFIGS)

There is no plugin-scope birth_config: only framework fields are `frozen`, so
`agents_meta.birth_config` never carries a plugin key (`shared/birth_config.py`).

Mode equivalence:

- **Process mode (today)**: nothing binds the contextvar, so every read returns
  `_PLUGIN_CONFIGS[plugin]` — the instance boot's `apply_config_overlay`
  already rebuilt with the overlay merged in. Byte-for-byte unchanged.
- **Hosted mode (Phase 1)**: the host binds the agent's overrides before
  creating its turn task; `asyncio.create_task` and LangGraph's node tasks copy
  the context, so one bind covers the whole turn (the empirical propagation
  result `turn_view` documents).

Instances are built lazily and memoized per bound view: a plugin config is a
frozen pydantic model, and rebuilding one per `ava._settings.plugins.<name>`
read would put model validation in the agent's hot path.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator, Mapping
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel


class _PluginConfigView:
    """One agent's plugin overrides plus the instances built from them.

    The cache is per view (so per turn-task binding, since the host builds one
    view per agent), and it is shared by every context copied from the binding
    within the agent process. Concurrent first reads may build the
    same instance twice; both are equal frozen models and the last write wins,
    which is why this needs no lock.
    """

    __slots__ = ("_cache", "_overrides")

    def __init__(self, overrides: Mapping[str, Mapping[str, Any]]) -> None:
        self._overrides = {p: dict(fields) for p, fields in overrides.items()}
        self._cache: dict[str, BaseModel] = {}

    def config_for(self, plugin: str) -> BaseModel:
        """This agent's instance for `plugin` — the process-global one when the
        agent overrides nothing in it."""
        from shared.plugin_config_registry import _PLUGIN_CONFIGS

        base = _PLUGIN_CONFIGS[plugin]  # KeyError = not registered / not bound, as before
        updates = self._overrides.get(plugin)
        if not updates:
            return base
        cached = self._cache.get(plugin)
        if cached is not None and type(cached) is type(base):
            return cached
        built = type(base)(**{**base.model_dump(), **updates})
        self._cache[plugin] = built
        return built


_AGENT_PLUGIN_CONFIG: ContextVar[_PluginConfigView | None] = ContextVar(
    "ava_agent_plugin_config", default=None
)


def resolve_agent_plugin_pins(
    config_overlay: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Route an agent's flat `config_overlay` to its plugin owners.

    The overlay is flat (`{"marker": ".git"}`) and each key belongs to exactly
    one owner — `resolve_overlay_targets` rejects a key that collides across
    framework and plugin, or across two plugins, at write time. This is the
    read-side counterpart and is deliberately tolerant of the same drift
    `shared/config/turn_view.py:resolve_agent_config_pins` tolerates: a key
    whose owning plugin has since been removed (or whose field was deleted)
    is dropped rather than raised, because the stored map was validated against
    a schema that no longer exists and a read must not be stricter than the
    boot that would consume it.
    """
    if not config_overlay:
        return {}
    from shared.config import field_names
    from shared.plugin_config_registry import _PLUGIN_CONFIG_CLASSES

    framework = field_names()
    pins: dict[str, dict[str, Any]] = {}
    for key, value in config_overlay.items():
        if key in framework:
            continue  # framework scope — shared/config/turn_view.py owns it
        owners = [p for p, cls in _PLUGIN_CONFIG_CLASSES.items() if key in cls.model_fields]
        if len(owners) != 1:
            continue  # unknown, or an ambiguity validation would never have stored
        pins.setdefault(owners[0], {})[key] = value
    return pins


@contextlib.contextmanager
def bind_agent_plugin_config(
    pins: Mapping[str, Mapping[str, Any]],
) -> Generator[None]:
    """Bind `pins` as the current context's plugin-config view.

    The hosted dispatcher wraps turn-task creation in this, alongside
    `shared.config.bind_agent_config` and `shared.turn_identity.bind_turn_identity`.
    """
    token = _AGENT_PLUGIN_CONFIG.set(_PluginConfigView(pins))
    try:
        yield
    finally:
        _AGENT_PLUGIN_CONFIG.reset(token)


def current_plugin_config_view() -> _PluginConfigView | None:
    """The view bound in the current context, or None outside an agent turn."""
    return _AGENT_PLUGIN_CONFIG.get()


def current_agent_plugin_pins() -> dict[str, Any]:
    """Copy the bound plugin overrides as the flat execution-child overlay."""
    view = _AGENT_PLUGIN_CONFIG.get()
    if view is None:
        return {}
    return {key: value for fields in view._overrides.values() for key, value in fields.items()}


def turn_plugin_config(plugin: str) -> BaseModel:
    """The plugin config instance this turn must read.

    The single read seam: `shared.plugin_config_registry.get_plugin_config` and
    `ava._settings.plugins.<name>` both come through here, so no agent-facing
    path reaches `_PLUGIN_CONFIGS` directly.
    """
    view = _AGENT_PLUGIN_CONFIG.get()
    if view is None:
        from shared.plugin_config_registry import _PLUGIN_CONFIGS

        return _PLUGIN_CONFIGS[plugin]
    return view.config_for(plugin)


def turn_plugin_configs() -> dict[str, BaseModel]:
    """Every bound plugin's instance, agent-scoped — the map behind
    `ava._settings.plugins` enumeration and `all_plugin_configs()`."""
    from shared.plugin_config_registry import _PLUGIN_CONFIGS

    view = _AGENT_PLUGIN_CONFIG.get()
    if view is None:
        return dict(_PLUGIN_CONFIGS)
    return {plugin: view.config_for(plugin) for plugin in _PLUGIN_CONFIGS}
