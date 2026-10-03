"""`ava.settings` exposes registered plugin configs as attributes."""

import pytest
from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.config_registration import (
    _PLUGIN_CONFIG_CLASSES,
    _PLUGIN_CONFIGS,
    bind_plugin_config,
)


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
    _PLUGIN_CONFIG_CLASSES.clear()
    _PLUGIN_CONFIGS.clear()
    yield
    _PLUGIN_CONFIG_CLASSES.clear()
    _PLUGIN_CONFIGS.clear()
    _PLUGIN_CONFIG_CLASSES.update(snap_classes)
    _PLUGIN_CONFIGS.update(snap_configs)


def test_ava_settings_plugins_attribute_access(isolated_registry, unit_home):
    """`ava._settings.plugins.<n>` returns instance; unregistered plugin name raise + lists known plugins."""
    bind_plugin_config("test_plugin", _FixtureConfig)

    import ava._settings as _ava_settings

    cfg = _ava_settings.plugins.test_plugin
    assert isinstance(cfg, _FixtureConfig)
    assert cfg.marker == ".git"

    with pytest.raises(AttributeError, match="Known plugins"):
        _ = _ava_settings.plugins.nonexistent_plugin
