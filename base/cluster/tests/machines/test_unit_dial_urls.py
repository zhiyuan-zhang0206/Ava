"""Registry advertisement preserves role priority with explicit root inputs."""

import pytest

from base.cluster import machines
from base.config import settings


def test_unit_dial_url_pure_station_is_otlp_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure station advertises its bearer-authenticated OTLP ingress — the
    one station address remote consumers dial — derived from reachable_host
    and AVA_TELEMETRY_OTLP_PORT (single source, task #1945). No gateway URL is
    required (a station host does not have one)."""
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
    """gateway + station (no runner) advertises the reachable-host form of the
    gateway URL — the station capability must not change the gateway unit's
    advertised address, but the host is always reachable_host(), never the
    bare gateway URL (WP4: a loopback advertisement makes the page proxy
    refuse the host's page servers)."""
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
    """agent-runner + station keeps the ops-URL dial — the station capability
    must not change the runner unit's advertised address."""
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
    """gateway + agent-runner on a true single box: `reachable_host()` resolves
    to localhost, so the advertised ops URL is loopback (single box needs no
    reachable address)."""
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
    """gateway + agent-runner on a machine with a reachable identity: the unit
    advertises `reachable_host()`, not unconditional localhost — otherwise the
    page proxy's SSRF guard (which only dials registered machine addresses)
    rejects every page registration from agents on the gateway box
    (2026-08-12 serve outage)."""
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
    """agent-runner only: the remote gateway must reach it, so the URL carries
    `reachable_host()`, not loopback."""
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
    """gateway-only advertises reachable_host + the gateway URL's port, NOT the
    bare gateway URL — a gateway_url naming loopback on a host with a
    reachable identity would advertise a self-dialing address that breaks
    page serves (WP4, 2026-08-30 serve 400)."""
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
    """gateway-only with no resolvable gateway URL still advertises the
    reachable host, on the gateway bind-port setting — the advertisement no
    longer depends on gateway_url being configured (the 400-scenario fix)."""
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
    """`ava start` and the ops daemon's boot registration compute this address from
    the SAME function, so a unit cannot be advertised at two addresses depending on
    which one wrote last. Asserted on the identical capability set both pass.
    """
    from base.cluster.machine.boot import machine_from_boot
    from tests.fixtures.configuration import snapshot_process_config

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
