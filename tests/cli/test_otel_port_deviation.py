"""Remote station routing stays independent of the consuming collector's ports."""

from __future__ import annotations

import pytest

import shared.db
from cli.commands import _otel_collector as oc
from services.heartbeat import station_probe
from shared.config import settings
from tests.cli.test_converge_otel_collector import _render_real_template


@pytest.mark.parametrize("station_port", [4318, 4325])
def test_remote_station_advertisement_drives_render_and_probe(
    monkeypatch: pytest.MonkeyPatch, station_port: int
) -> None:
    monkeypatch.setattr(settings.observability, "telemetry_otlp_port", 4319)
    monkeypatch.setattr(settings.observability, "otel_collector_metrics_port", 8889)
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_units "
            "(machine_name, home, serve_gateway, serve_agent_runner, serve_observability_station, url) "
            "VALUES ('station-deviation', '/station-deviation', false, false, true, %s)",
            (f"http://10.0.0.46:{station_port}",),
        )
        conn.commit()
    try:
        cfg = _render_real_template(
            monkeypatch, frozenset({"gateway"}), observability_url="http://10.0.0.46"
        )
        endpoint = f"http://10.0.0.46:{station_port}"
        for name in ("tempo", "loki", "prometheus"):
            assert cfg["exporters"][f"otlphttp/{name}"]["endpoint"] == endpoint
        assert cfg["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == "127.0.0.1:4319"
        assert oc._lgtm_fanout_bases() == (endpoint, endpoint)
        target = station_probe.resolve_target()
        assert target is not None and target.advertised and target.url == endpoint
    finally:
        with shared.db.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM machine_units WHERE machine_name = 'station-deviation'")
            conn.commit()


def test_unregistered_station_uses_remote_port_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.observability, "telemetry_otlp_port", 4319)
    monkeypatch.setattr(settings.observability, "observability_otlp_port", 4325)
    monkeypatch.setattr(settings.observability, "observability_url", "http://192.0.2.45")
    assert oc.station_otel_ingress_endpoint() == "http://192.0.2.45:4325"
    target = station_probe.resolve_target()
    assert target is not None and not target.advertised
    assert target.url == oc.station_otel_ingress_endpoint()


def test_runner_does_not_discover_station(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_discovery(base: str) -> None:
        pytest.fail("pure runners must use their published gateway relay")

    monkeypatch.setattr("shared.station_endpoint.resolve_station_target", fail_discovery)
    cfg = _render_real_template(
        monkeypatch, frozenset({"agent-runner"}), observability_url="http://10.0.0.46"
    )
    assert cfg["exporters"]["otlphttp/tempo"]["endpoint"] == "http://10.0.0.10:4318"
