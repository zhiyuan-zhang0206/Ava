"""Gateway bootstrap publishes relay routing independently of local listeners."""

import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from cli.commands import _otel_collector as collector
from gateway.app import app
from shared import config, runtime_config
from shared.config.observability import ObservabilitySettings


@pytest.mark.parametrize(("gateway_port", "local_port"), [(4318, 4319), (54318, 4318)])
def test_bootstrap_routes_to_gateway_with_distinct_local_listener(
    db_conn, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gateway_port: int, local_port: int
) -> None:
    """The current split and mirrored Windows/WSL migration both keep local ports."""
    (tmp_path / ".env").write_text(
        f"AVA_DB_URL={config.settings.data_plane.db_url}\n"
        "AVA_RUNNER_DB_PASSWORD=relay-test-password\n"
        f"AVA_TELEMETRY_OTLP_PORT={gateway_port}\n"
        f"AVA_TELEMETRY_OTLP_ENDPOINT=http://127.0.0.1:{gateway_port}\n"
        "AVA_GATEWAY_OTLP_ENDPOINT=http://stale.invalid:1\n"
    )
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    monkeypatch.setattr(config, "_self_machine_host", lambda: "10.0.0.10")
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "relay-test-token")
    with TestClient(app) as client:
        assert client.get("/api/bootstrap").status_code == 401
        response = client.get(
            "/api/bootstrap", headers={"Authorization": "Bearer relay-test-token"}
        )
    assert response.status_code == 200
    payload = response.json()
    assert "AVA_TELEMETRY_OTLP_ENDPOINT" not in payload
    assert "AVA_TELEMETRY_OTLP_PORT" not in payload
    remote = f"http://10.0.0.10:{gateway_port}"
    assert payload["AVA_GATEWAY_OTLP_ENDPOINT"] == remote
    local = f"http://127.0.0.1:{local_port}"
    settings = ObservabilitySettings.model_validate(
        {
            **payload,
            "AVA_TELEMETRY_OTLP_PORT": local_port,
        }
    )
    monkeypatch.setattr(config.settings, "observability", settings)
    monkeypatch.setattr("shared.machine.machine_name", lambda: "relay-test-runner")
    rendered = yaml.safe_load(
        collector.generate_config(
            Path(__file__).resolve().parents[2], tmp_path, roles=frozenset({"agent-runner"})
        )
    )
    assert settings.telemetry_otlp_endpoint == local
    assert (
        rendered["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == f"127.0.0.1:{local_port}"
    )
    assert "otlp/remote" not in rendered["receivers"]
    for name in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        assert rendered["exporters"][name]["endpoint"] == remote
        assert rendered["exporters"][name]["headers"] == {
            "Authorization": "Bearer relay-test-token"
        }
    assert rendered["exporters"]["otlphttp/tempo"]["sending_queue"]["storage"] == "file_storage"


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://localhost:54318",
        "http://0.0.0.0:54318",
        "http://[::]:54318",
        "https://example.test:54318",
        "http://example.test",
        "http://user:pass@example.test:54318",
        "http://example.test:54318/other",
        "http://example.test:54318?x=y",
        "http://example.test:54318#fragment",
    ],
)
def test_missing_or_invalid_gateway_projection_fails_closed(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setattr(config.settings.observability, "gateway_otlp_endpoint", endpoint)
    with pytest.raises(RuntimeError, match="gateway bootstrap"):
        collector.gateway_otel_ingress_endpoint()


def test_local_otlp_config_stays_host_owned() -> None:
    metadata = {row.name: row for row in config.get_config_metadata()}
    for field in ("telemetry_otlp_endpoint", "telemetry_otlp_port"):
        assert metadata[field].scope == "host"
        assert field not in config.BOOTSTRAP_FIELDS
    assert "gateway_otlp_endpoint" in config.BOOTSTRAP_FIELDS
    assert metadata["gateway_otlp_endpoint"].writable is False


def test_explicit_local_collector_endpoint_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise a fresh Settings construction, not the module-load singleton.
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    local = ObservabilitySettings.model_validate({"AVA_TELEMETRY_OTLP_PORT": 4319})
    assert local.telemetry_otlp_endpoint == "http://127.0.0.1:4319"
    explicit = ObservabilitySettings.model_validate(
        {
            "AVA_TELEMETRY_OTLP_PORT": 4319,
            "AVA_TELEMETRY_OTLP_ENDPOINT": "http://collector.test:54444",
        }
    )
    assert explicit.telemetry_otlp_endpoint == "http://collector.test:54444"
