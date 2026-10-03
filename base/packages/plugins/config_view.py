"""An agent's plugin-config overrides — the agent-scoped read path for plugin `per_agent` fields.

`_PLUGIN_CONFIGS` (`base/packages/plugins/config_registration.py`) is a process-global
`plugin -> frozen instance` map that boot mutates in place from the agent's `config_overlay`. One
agent per process makes that exact; in the hosted runner one process serves many agents, so the
last booted overlay would be every agent's plugin config. The host therefore routes each agent's
overlay to its plugin owners (`resolve_agent_plugin_pins`), carries it on the agent's
`AgentSlices`, and `PluginConfigView` layers it over the process-global instance:

    config_overlay (agents_meta)  >  the bound disk image (_PLUGIN_CONFIGS)

There is no plugin-scope birth_config: only framework fields are `frozen`, so
`agents_meta.birth_config` never carries a plugin key (`base/agents/birth_config.py`).

Instances are built lazily and memoized per view: a plugin config is a frozen pydantic model, and
rebuilding one per read would put model validation in the agent's hot path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


class PluginConfigView:
    """One agent's plugin overrides plus the instances built from them.

    The cache is per view (the host resolves one view per agent turn, on its `AgentSlices`).
    Concurrent first reads may build the same instance twice; both are equal frozen models and the last write wins,
    which is why this needs no lock.
    """

    __slots__ = ("_cache", "_overrides")

    def __init__(self, overrides: Mapping[str, Mapping[str, Any]]) -> None:
        self._overrides = {p: dict(fields) for p, fields in overrides.items()}
        self._cache: dict[str, BaseModel] = {}

    def flat(self) -> dict[str, Any]:
        """The overrides as the flat execution-child overlay."""
        return {key: value for fields in self._overrides.values() for key, value in fields.items()}

    def config_for(self, plugin: str) -> BaseModel:
        """This agent's instance for `plugin` — the process-global one when the
        agent overrides nothing in it."""
        from base.packages.plugins.config_registration import _PLUGIN_CONFIGS

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


def resolve_agent_plugin_pins(
    config_overlay: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Route an agent's flat `config_overlay` to its plugin owners.

    The overlay is flat (`{"marker": ".git"}`) and each key belongs to exactly
    one owner — `resolve_overlay_targets` rejects a key that collides across
    framework and plugin, or across two plugins, at write time. This is the
    read-side counterpart and is deliberately tolerant of the same drift
    `base/config/agent_pins.py:resolve_agent_config_pins` tolerates: a key
    whose owning plugin has since been removed (or whose field was deleted)
    is dropped rather than raised, because the stored map was validated against
    a schema that no longer exists and a read must not be stricter than the
    boot that would consume it.
    """
    if not config_overlay:
        return {}
    from base.config import field_names
    from base.packages.plugins.config_registration import _PLUGIN_CONFIG_CLASSES

    framework = field_names()
    pins: dict[str, dict[str, Any]] = {}
    for key, value in config_overlay.items():
        if key in framework:
            continue  # framework scope — base/config/agent_pins.py owns it
        owners = [p for p, cls in _PLUGIN_CONFIG_CLASSES.items() if key in cls.model_fields]
        if len(owners) != 1:
            continue  # unknown, or an ambiguity validation would never have stored
        pins.setdefault(owners[0], {})[key] = value
    return pins
