import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from base import config, paths
from base.cluster.machine import MachineIdentity
from base.config.domains.observability.settings import ObservabilitySettings
from base.config.service_read import ConfigAuthority
from base.host.db_authority import unit
from base.host.db_authority.api import telemetry_token
from base.host.env import dotenv_file
from cli.commands.observability import otel_collector as collector
from cli.database.tests.command_inputs import command_machine as command_machine
from gateway.tests.agents.gateway_auth import without_credentials
from tests.fixtures.gateway_config import gateway_test_client
from tests.path_scoped.cli_tests import operator_database as operator_database


@pytest.mark.parametrize(("gateway_port", "local_port"), [(4318, 4319), (54318, 4318)])
def test_bootstrap_routes_to_gateway_with_distinct_local_listener(
    gateway_data_plane: None,
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gateway_port: int,
    local_port: int,
    operator_database: Callable[[], Any],
    command_machine: MachineIdentity,
) -> None:
    (tmp_path / ".env").write_text(
        "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
        f"MEIVA_DB_URL={config.settings.data_plane.db_url}\n"
        f"MEIVA_REDIS_URL={config.settings.data_plane.redis_url}\n"
        "MEIVA_RUNNER_DB_PASSWORD=relay-test-password\n"
        f"MEIVA_TELEMETRY_OTLP_PORT={gateway_port}\n"
        f"MEIVA_TELEMETRY_OTLP_ENDPOINT=http://127.0.0.1:{gateway_port}\n"
        "MEIVA_GATEWAY_OTLP_ENDPOINT=http://stale.invalid:1\n"
    )
    monkeypatch.setattr(dotenv_file, "unit_home", lambda: tmp_path)
    monkeypatch.setattr(paths, "meiva_home", lambda: tmp_path)
    monkeypatch.setattr(config.settings.general, "machine_host", "10.0.0.10")
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "relay-test-token")
    owner = config.ConfigBoot()
    with patch.dict(os.environ, {"MEIVA_HOME": str(tmp_path)}):
        owner.ensure_eager()
    owner.set_field("machine_host", "10.0.0.10")
    owner.set_field("db_url", config.settings.data_plane.db_url)
    owner.set_field("telemetry_otlp_port", gateway_port)
    owner.set_field("cluster_secret", "relay-test-token")
    with gateway_test_client(boot=owner) as client:
        assert without_credentials(client).get("/api/bootstrap").status_code == 401
        response = client.get(
            "/api/bootstrap", headers={"Authorization": "Bearer relay-test-token"}
        )
    assert response.status_code == 200
    payload = response.json()
    assert "MEIVA_TELEMETRY_OTLP_ENDPOINT" not in payload
    assert "MEIVA_TELEMETRY_OTLP_PORT" not in payload
    remote = f"http://10.0.0.10:{gateway_port}"
    assert payload["MEIVA_GATEWAY_OTLP_ENDPOINT"] == remote
    local = f"http://127.0.0.1:{local_port}"
    settings = ObservabilitySettings.model_validate(
        {
            **payload,
            "MEIVA_TELEMETRY_OTLP_PORT": local_port,
        }
    )
    monkeypatch.setattr(config.settings, "observability", settings)
    command_machine.set(name="relay-test-runner")
    telemetry = telemetry_token("relay-test-token")
    capability = SimpleNamespace(api=SimpleNamespace(telemetry=telemetry))

    def installed(_home: Path) -> SimpleNamespace:
        return capability

    monkeypatch.setattr(unit, "load_unit_capability", installed)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "")
    rendered = yaml.safe_load(
        collector.generate_config(
            Path(__file__).resolve().parents[3],
            tmp_path,
            roles=frozenset({"agent-runner"}),
            database_factory=operator_database,
            machine=command_machine,
        )
    )
    assert settings.telemetry_otlp_endpoint == local
    assert (
        rendered["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == f"127.0.0.1:{local_port}"
    )
    assert "otlp/remote" not in rendered["receivers"]
    for name in ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"):
        assert rendered["exporters"][name]["endpoint"] == remote
        assert rendered["exporters"][name]["headers"] == {"Authorization": f"Bearer {telemetry}"}
    assert "storage" not in rendered["exporters"]["otlphttp/tempo"]["sending_queue"]


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


def test_a_valid_gateway_projection_carries_no_problem() -> None:
    assert collector.gateway_otlp_endpoint_problem("http://example.test:54318") is None
    assert collector.gateway_otlp_endpoint_problem("http://10.0.0.5:4318/") is None


def test_an_invalid_gateway_projection_names_its_problem() -> None:
    problem = collector.gateway_otlp_endpoint_problem("http://127.0.0.1:4318")
    assert problem is not None
    assert "non-loopback" in problem


def test_local_otlp_config_stays_host_owned(config_authority: ConfigAuthority) -> None:
    metadata = {row.name: row for row in config.get_config_metadata(authority=config_authority)}
    for field in ("telemetry_otlp_endpoint", "telemetry_otlp_port"):
        assert metadata[field].scope == "host"
        assert field not in config.BOOTSTRAP_FIELDS
    assert "gateway_otlp_endpoint" in config.BOOTSTRAP_FIELDS
    assert metadata["gateway_otlp_endpoint"].writable is False


def test_explicit_local_collector_endpoint_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    local = ObservabilitySettings.model_validate({"MEIVA_TELEMETRY_OTLP_PORT": 4319})
    assert local.telemetry_otlp_endpoint == "http://127.0.0.1:4319"
    explicit = ObservabilitySettings.model_validate(
        {
            "MEIVA_TELEMETRY_OTLP_PORT": 4319,
            "MEIVA_TELEMETRY_OTLP_ENDPOINT": "http://collector.test:54444",
        }
    )
    assert explicit.telemetry_otlp_endpoint == "http://collector.test:54444"
