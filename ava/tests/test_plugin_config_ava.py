"""`ava.settings` exposes registered plugin configs as attributes."""

import pytest
from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.config_registration import (
    _PLUGIN_CONFIG_CLASSES,
    _PLUGIN_CONFIGS,
    bind_from_disk,
    clear_plugin_configs,
    register_plugin_config,
)
from base.packages.plugins.context import PluginContext


class _FixtureConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    flag: bool = Field(default=True)
    marker: str = Field(default=".git", json_schema_extra={"per_agent": True})


@pytest.fixture
def isolated_registry():
    """Per-test clean registry — avoids cross-test pollution.

    This fixture teardown re-registers to restore initial state
    (note: registration order doesn't matter; zero cross-test impact).
    """
    # Snapshot before
    snap_classes = dict(_PLUGIN_CONFIG_CLASSES)
    snap_configs = dict(_PLUGIN_CONFIGS)
    clear_plugin_configs()
    yield
    clear_plugin_configs()
    _PLUGIN_CONFIG_CLASSES.update(snap_classes)
    _PLUGIN_CONFIGS.update(snap_configs)


def test_ava_settings_plugins_attribute_access(isolated_registry, unit_home):
    """`ava._settings.plugins.<n>` returns instance; unregistered plugin name raise + lists known plugins."""
    with PluginContext("test_plugin"):
        register_plugin_config(_FixtureConfig)
    bind_from_disk()

    import ava._settings as _ava_settings

    cfg = _ava_settings.plugins.test_plugin
    assert isinstance(cfg, _FixtureConfig)
    assert cfg.marker == ".git"

    with pytest.raises(AttributeError, match="Known plugins"):
        _ = _ava_settings.plugins.nonexistent_plugin
