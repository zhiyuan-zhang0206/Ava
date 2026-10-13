"""Plugin-config routing for one agent: pins by owner, and the cluster policy overlay.

The per-agent view itself (``PluginConfigView``) lives beside ``AgentSlices``
(``base/host/env/agent_slices.py``), which carries it.

Plugin fields have no birth_config: an explicit overlay pins them; otherwise
another process start reads the current authority image. See
``docs/plugin-config.ava.okf.md`` for the shared lifecycle contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


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

    from base.config import schema_extra
    from base.host.env.bootstrap import (
        cluster_plugin_config_values,
        config_source_is_local,
        should_fetch_from_gateway,
    )
    from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError

    cluster_fields = {
        name
        for name, info in type(config).model_fields.items()
        if schema_extra(info).get("scope") in {"cluster-pinned", "cluster-default"}
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
