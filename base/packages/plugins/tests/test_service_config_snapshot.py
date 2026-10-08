"""A plugin service consumes the exact non-secret config captured at launch."""

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV
from base.packages.plugins.config_registration import (
    InvalidConfigData,
    SchemaDriftError,
    read_config_image,
    read_service_config,
    service_config_packet,
)


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool = Field(default=True, strict=True)
    interval: float = Field(default=300, strict=True)


def test_service_birth_snapshot_survives_authority_image_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "config.json"
    image.write_text('{"enabled":true,"interval":42}')
    captured = read_config_image(Config, image)
    monkeypatch.setenv(SERVICE_PLUGIN_CONFIG_ENV, service_config_packet("fleet", captured))
    image.write_text('{"enabled":false,"interval":900}')
    assert read_service_config("fleet", Config) == captured
    assert read_config_image(Config, image).enabled is False


def test_absent_service_carrier_is_distinct_from_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SERVICE_PLUGIN_CONFIG_ENV, raising=False)
    assert read_service_config("fleet", Config) is None


@pytest.mark.parametrize(
    ("packet", "error"),
    [
        ("bad json", InvalidConfigData),
        ('{"plugin":"fleet","config":{},"unknown":1}', InvalidConfigData),
        ('{"plugin":"other","config":{}}', InvalidConfigData),
        ('{"plugin":"fleet","config":{"enabled":true}}', SchemaDriftError),
        ('{"plugin":"fleet","config":{"enabled":"true","interval":300}}', InvalidConfigData),
    ],
)
def test_present_service_packet_never_falls_back(
    packet: str,
    error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SERVICE_PLUGIN_CONFIG_ENV, packet)
    with pytest.raises(error):
        read_service_config("fleet", Config)


def test_canonical_packet_rejects_sensitive_fields_and_nonfinite_values() -> None:
    class SecretConfig(BaseModel):
        token: str = Field(default="secret", json_schema_extra={"sensitive": True})

    with pytest.raises(InvalidConfigData, match="sensitive"):
        service_config_packet("fleet", SecretConfig())
    with pytest.raises(ValueError):
        service_config_packet("fleet", Config(interval=float("nan")))
    assert service_config_packet("fleet", Config()) == service_config_packet("fleet", Config())
