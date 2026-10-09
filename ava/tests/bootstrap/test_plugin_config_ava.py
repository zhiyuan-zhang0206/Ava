"""`ava.settings` exposes registered plugin configs as attributes."""

import pytest
from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.config_registration import (
    bind_plugin_config,
)


class _FixtureConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    flag: bool = Field(default=True)
    marker: str = Field(default=".git", json_schema_extra={"per_agent": True})


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, BaseModel]:
    """The SDK reads its current installation's own config bindings."""
    import ava
    from ava.sdk_surface.install import Installation
    from base.packages.plugins.extensions import EMPTY

    configs: dict[str, BaseModel] = {}
    installation = Installation(
        registry=EMPTY,
        expansions=(),
        wrap_layers={},
        skill_providers=(),
        metered=(),
        disabled=frozenset(),
        faces=False,
        undo=(),
        configs=configs,
    )
    monkeypatch.setattr(ava, "__plugin_installation__", installation, raising=False)
    return configs


def test_ava_settings_plugins_attribute_access(isolated_registry: dict[str, BaseModel], unit_home):
    """`ava.sdk_surface.settings.plugins.<n>` returns instance; unregistered plugin name raise + lists known plugins."""
    bind_plugin_config("test_plugin", _FixtureConfig, isolated_registry)

    import ava.sdk_surface.settings as _ava_settings

    cfg = _ava_settings.plugins.test_plugin
    assert isinstance(cfg, _FixtureConfig)
    assert cfg.marker == ".git"

    with pytest.raises(AttributeError, match="Known plugins"):
        _ = _ava_settings.plugins.nonexistent_plugin
