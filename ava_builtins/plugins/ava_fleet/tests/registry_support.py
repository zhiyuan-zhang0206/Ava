"""Test support: the registry the loader would build with only the fleet plugin enabled."""

from collections.abc import Generator
from contextlib import contextmanager

import pytest

from base.packages.plugins.extensions import ExtensionRegistry


def fleet_registry() -> ExtensionRegistry:
    from ava_builtins.plugins.ava_fleet import agent_runtime

    return ExtensionRegistry((("ava_fleet", agent_runtime.contribute()),))


@contextmanager
def installed_fleet_surface() -> Generator[ExtensionRegistry]:
    """The fleet plugin's SDK surface installed into `ava` for the duration of the block."""
    from ava.sdk_surface import install
    from ava_builtins.plugins.ava_fleet import agent_runtime, default_config, plugin

    contributions = (
        plugin.contribute().merged(default_config.contribute()).merged(agent_runtime.contribute())
    )
    with pytest.MonkeyPatch.context() as environment:
        _deliver_cluster_config(environment, default_config.FleetConfig())
        admitted = install.install(ExtensionRegistry((("ava_fleet", contributions),)))
        try:
            yield admitted
        finally:
            install.uninstall()


def set_fleet_configuration(*, reduce_context_switch: bool) -> None:
    """Reinstall the actual Fleet faces against a changed authority image."""
    from ava.sdk_surface import install
    from ava_builtins.plugins.ava_fleet import agent_runtime, default_config, plugin
    from base.packages.plugins.config_registration import disk_image_path

    path = disk_image_path("ava_fleet")
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
        install.install(ExtensionRegistry((("ava_fleet", contributions),)))


def _deliver_cluster_config(environment: pytest.MonkeyPatch, config: object) -> None:
    import json

    from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV

    assert isinstance(config, FleetConfig)
    values = config.model_dump(mode="json")
    values.pop("task_maintenance_enabled")
    environment.setenv(PLUGIN_CLUSTER_CONFIG_ENV, json.dumps({"ava_fleet": values}))
