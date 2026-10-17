from collections.abc import Generator
from contextlib import contextmanager

import pytest

from base.packages.plugins.extensions import ExtensionRegistry


def fleet_registry() -> ExtensionRegistry:
    from meiva_builtins.plugins.meiva_fleet import agent_runtime

    return ExtensionRegistry((("meiva_fleet", agent_runtime.contribute()),))


@contextmanager
def installed_fleet_surface() -> Generator[ExtensionRegistry]:
    from meiva.sdk_surface import install
    from meiva_builtins.plugins.meiva_fleet import agent_runtime, default_config, plugin

    contributions = (
        plugin.contribute().merged(default_config.contribute()).merged(agent_runtime.contribute())
    )
    with pytest.MonkeyPatch.context() as environment:
        _deliver_cluster_config(environment, default_config.FleetConfig())
        admitted = install.install(ExtensionRegistry((("meiva_fleet", contributions),)))
        try:
            yield admitted
        finally:
            install.uninstall()


def set_fleet_configuration(*, reduce_context_switch: bool) -> None:
    from base.packages.plugins.config_registration import disk_image_path
    from meiva.sdk_surface import install
    from meiva_builtins.plugins.meiva_fleet import agent_runtime, default_config, plugin

    path = disk_image_path("meiva_fleet")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        default_config.FleetConfig(reduce_context_switch=reduce_context_switch).model_dump_json()
    )
    install.uninstall()
    contributions = (
        plugin.contribute().merged(default_config.contribute()).merged(agent_runtime.contribute())
    )
    with pytest.MonkeyPatch.context() as environment:
        _deliver_cluster_config(
            environment, default_config.FleetConfig(reduce_context_switch=reduce_context_switch)
        )
        install.install(ExtensionRegistry((("meiva_fleet", contributions),)))


def _deliver_cluster_config(environment: pytest.MonkeyPatch, config: object) -> None:
    import json

    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV
    from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig

    assert isinstance(config, FleetConfig)
    values = config.model_dump(mode="json")
    values.pop("task_maintenance_enabled")
    environment.setenv(PLUGIN_CLUSTER_CONFIG_ENV, json.dumps({"meiva_fleet": values}))
