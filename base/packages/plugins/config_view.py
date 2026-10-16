from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


# ava:ref base/packages/plugins/docs/plugin-config/config-view.ava.okf.md#plugin-pin-routing
def resolve_agent_plugin_pins(
    config_overlay: Mapping[str, Any] | None,
    configs: Mapping[str, BaseModel],
) -> dict[str, dict[str, Any]]:
    if not config_overlay:
        return {}
    from base.config import field_names

    framework = field_names()
    pins: dict[str, dict[str, Any]] = {}
    for key, value in config_overlay.items():
        if key in framework:
            continue
        owners = [p for p, config in configs.items() if key in type(config).model_fields]
        if len(owners) != 1:
            continue
        pins.setdefault(owners[0], {})[key] = value
    return pins


# ava:ref base/packages/plugins/docs/plugin-config/config-view.ava.okf.md#cluster-projection-gate
def with_cluster_policy[C: BaseModel](plugin: str, config: C) -> C:
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
        if cluster_fields and not config_source_is_local() and should_fetch_from_gateway():
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
