import pytest

from base.cluster import machines
from base.config import settings


def test_unit_dial_url_pure_station_is_otlp_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    assert machines.unit_dial_url(
        frozenset({"observability-station"}),
        host="10.0.0.9",
        ops_port=8600,
        gateway_port=settings.gateway.gateway_port,
        telemetry_otlp_port=settings.observability.telemetry_otlp_port,
        gateway=settings.gateway.gateway_url,
    ) == ("http://10.0.0.9:4318")


def test_unit_dial_url_gateway_station_is_reachable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://gw:8000")
    assert machines.unit_dial_url(
        frozenset({"gateway", "observability-station"}),
        host="10.0.0.5",
        ops_port=8600,
        gateway_port=settings.gateway.gateway_port,
        telemetry_otlp_port=settings.observability.telemetry_otlp_port,
        gateway=settings.gateway.gateway_url,
    ) == ("http://10.0.0.5:8000")


def test_unit_dial_url_runner_station_is_ops_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://gw:8000")
    url = machines.unit_dial_url(
        frozenset({"agent-runner", "observability-station"}),
        host="10.0.0.5",
        ops_port=8600,
        gateway_port=settings.gateway.gateway_port,
        telemetry_otlp_port=settings.observability.telemetry_otlp_port,
        gateway=settings.gateway.gateway_url,
    )
    assert url is not None and url.startswith("http://10.0.0.5:")


def test_unit_dial_url_single_box_is_loopback_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        machines.unit_dial_url(
            frozenset({"gateway", "agent-runner"}),
            host="localhost",
            ops_port=8600,
            gateway_port=settings.gateway.gateway_port,
            telemetry_otlp_port=settings.observability.telemetry_otlp_port,
            gateway=settings.gateway.gateway_url,
        )
        == "http://localhost:8600"
    )


def test_unit_dial_url_gateway_runner_uses_reachable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        machines.unit_dial_url(
            frozenset({"gateway", "agent-runner"}),
            host="10.0.0.2",
            ops_port=8600,
            gateway_port=settings.gateway.gateway_port,
            telemetry_otlp_port=settings.observability.telemetry_otlp_port,
            gateway=settings.gateway.gateway_url,
        )
        == "http://10.0.0.2:8600"
    )


def test_unit_dial_url_split_runner_is_reachable_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        machines.unit_dial_url(
            frozenset({"agent-runner"}),
            host="10.0.0.2",
            ops_port=8600,
            gateway_port=settings.gateway.gateway_port,
            telemetry_otlp_port=settings.observability.telemetry_otlp_port,
            gateway=settings.gateway.gateway_url,
        )
        == "http://10.0.0.2:8600"
    )


def test_unit_dial_url_gateway_only_is_reachable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://ava.example:8000")
    assert (
        machines.unit_dial_url(
            frozenset({"gateway"}),
            host="10.0.0.2",
            ops_port=8600,
            gateway_port=settings.gateway.gateway_port,
            telemetry_otlp_port=settings.observability.telemetry_otlp_port,
            gateway=settings.gateway.gateway_url,
        )
        == "http://10.0.0.2:8000"
    )


def test_unit_dial_url_gateway_only_defaults_port_when_url_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr(settings.gateway, "gateway_port", 8000)
    assert (
        machines.unit_dial_url(
            frozenset({"gateway"}),
            host="10.0.0.2",
            ops_port=8600,
            gateway_port=settings.gateway.gateway_port,
            telemetry_otlp_port=settings.observability.telemetry_otlp_port,
            gateway=settings.gateway.gateway_url,
        )
        == "http://10.0.0.2:8000"
    )


def test_unit_dial_url_agrees_across_both_writers(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.cluster.machine.boot import machine_from_boot
    from base.config.tests.configuration import snapshot_process_config

    config = snapshot_process_config()
    config.set_field("machine_name", "split-runner")
    config.set_field("machine_serve_gateway", False)
    config.set_field("machine_serve_agent_runner", True)
    config.set_field("machine_serve_observability_station", False)
    config.set_field("machine_host", "10.0.0.7")
    machine = machine_from_boot(config)
    from_start = machines.unit_dial_url(
        frozenset({"agent-runner"}),
        host=machine.host(),
        ops_port=8600,
        gateway_port=8000,
        telemetry_otlp_port=4318,
        gateway=None,
    )
    from_daemon = machines.unit_dial_url(
        machine.role(),
        host=machine.host(),
        ops_port=8600,
        gateway_port=8000,
        telemetry_otlp_port=4318,
        gateway=None,
    )
    assert from_start == from_daemon == "http://10.0.0.7:8600"
