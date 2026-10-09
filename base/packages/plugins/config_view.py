"""One agent's plugin overrides over an explicitly supplied boot config image.

The host receives the installer's resolved image and creates one view per
agent turn, carrying it on ``AgentSlices``. An exec child applies its overlay
at boot; an external attachment owns its agent's view. Overrides affect only
that view. Frozen plugin instances are memoized per view, never globally.

Plugin fields have no birth_config: an explicit overlay pins them; otherwise
another process start reads the current authority image. See
``docs/plugin-config.ava.okf.md`` for the shared lifecycle contract.
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

    __slots__ = ("_cache", "_configs", "_overrides")

    def __init__(
        self, configs: Mapping[str, BaseModel], overrides: Mapping[str, Mapping[str, Any]]
    ) -> None:
        self._configs = configs
        self._overrides = {p: dict(fields) for p, fields in overrides.items()}
        self._cache: dict[str, BaseModel] = {}

    def flat(self) -> dict[str, Any]:
        """The overrides as the flat execution-child overlay."""
        return {key: value for fields in self._overrides.values() for key, value in fields.items()}

    def config_for(self, plugin: str) -> BaseModel:
        """This agent's instance, or its supplied base when no override is set."""
        base = self._configs[plugin]
        updates = self._overrides.get(plugin)
        if not updates:
            return base
        cached = self._cache.get(plugin)
        if cached is not None and type(cached) is type(base):
            return cached
        built = type(base)(**{**base.model_dump(), **updates})
        self._cache[plugin] = built
        return built

    def configs(self) -> dict[str, BaseModel]:
        """The bound plugin configs with this agent's overrides applied."""
        return {plugin: self.config_for(plugin) for plugin in self._configs}


def resolve_agent_plugin_pins(
    config_overlay: Mapping[str, Any] | None,
    configs: Mapping[str, BaseModel],
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

    framework = field_names()
    pins: dict[str, dict[str, Any]] = {}
    for key, value in config_overlay.items():
        if key in framework:
            continue  # framework scope — base/config/agent_pins.py owns it
        owners = [p for p, config in configs.items() if key in type(config).model_fields]
        if len(owners) != 1:
            continue  # unknown, or an ambiguity validation would never have stored
        pins.setdefault(owners[0], {})[key] = value
    return pins


def with_cluster_policy[C: BaseModel](plugin: str, config: C) -> C:
    """Combine local host fields with the complete declared gateway projection."""
    from pydantic import ValidationError

    from base.host.env.bootstrap import (
        cluster_plugin_config_values,
        config_source_is_local,
        should_fetch_from_gateway,
    )
    from base.packages.plugins.config_registration import (
        InvalidConfigData,
        SchemaDriftError,
        _schema_extra,
    )

    cluster_fields = {
        name
        for name, info in type(config).model_fields.items()
        if _schema_extra(info).get("scope") in {"cluster-pinned", "cluster-default"}
    }
    projection = cluster_plugin_config_values(plugin)
    if projection is None:
        if cluster_fields and should_fetch_from_gateway():
            raise InvalidConfigData(f"bootstrap lacks cluster config for plugin {plugin!r}")
        return config
    if set(projection) != cluster_fields:
        raise SchemaDriftError(
            f"plugin {plugin!r} bootstrap cluster fields differ from declaration"
        )
    try:
        composed = type(config).model_validate({**config.model_dump(), **projection})
    except ValidationError as exc:
        raise InvalidConfigData(f"invalid cluster config for plugin {plugin!r}: {exc}") from exc
    return config if config_source_is_local() else composed
