"""Remote station routing stays independent of the consuming collector's ports."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from base import db
from base.config import settings
from cli.commands.observability import otel_collector as oc
from cli.commands.observability.tests.test_converge_otel_collector import _render_real_template
from services.wake.heartbeat import station_probe
from tests.path_scoped.cli_tests import operator_database as operator_database


@pytest.mark.parametrize("station_port", [4318, 4325])
def test_remote_station_advertisement_drives_render_and_probe(
    monkeypatch: pytest.MonkeyPatch, station_port: int, operator_database: Callable[[], Any]
) -> None:
    monkeypatch.setattr(settings.observability, "telemetry_otlp_port", 4319)
    monkeypatch.setattr(settings.observability, "otel_collector_metrics_port", 8889)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_units "
            "(machine_name, home, serve_gateway, serve_agent_runner, serve_observability_station, url) "
            "VALUES ('station-deviation', '/station-deviation', false, false, true, %s)",
            (f"http://10.0.0.46:{station_port}",),
        )
        conn.commit()
    try:
        cfg = _render_real_template(
            monkeypatch,
            frozenset({"gateway"}),
            observability_url="http://10.0.0.46",
            operator_database=operator_database,
        )
        endpoint = f"http://10.0.0.46:{station_port}"
        for name in ("tempo", "loki", "prometheus"):
            assert cfg["exporters"][f"otlphttp/{name}"]["endpoint"] == endpoint
        assert cfg["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == "127.0.0.1:4319"
        assert oc._lgtm_fanout_bases(database_factory=operator_database) == (endpoint, endpoint)
        target = station_probe.resolve_target(database=operator_database)
        assert target is not None and target.advertised and target.url == endpoint
    finally:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM machine_units WHERE machine_name = 'station-deviation'")
            conn.commit()


def test_unregistered_station_uses_remote_port_projection(
    monkeypatch: pytest.MonkeyPatch, operator_database: Callable[[], Any]
) -> None:
    monkeypatch.setattr(settings.observability, "telemetry_otlp_port", 4319)
    monkeypatch.setattr(settings.observability, "observability_otlp_port", 4325)
    monkeypatch.setattr(settings.observability, "observability_url", "http://192.0.2.45")
    assert (
        oc.station_otel_ingress_endpoint(database_factory=operator_database)
        == "http://192.0.2.45:4325"
    )
    target = station_probe.resolve_target(database=operator_database)
    assert target is not None and not target.advertised
    assert target.url == oc.station_otel_ingress_endpoint(database_factory=operator_database)


def test_runner_does_not_discover_station(
    monkeypatch: pytest.MonkeyPatch, operator_database: Callable[[], Any]
) -> None:
    def fail_discovery(_db: object, base: str) -> None:
        pytest.fail("pure runners must use their published gateway relay")

    monkeypatch.setattr("base.telemetry.station_endpoint.resolve_station_target", fail_discovery)
    cfg = _render_real_template(
        monkeypatch,
        frozenset({"agent-runner"}),
        observability_url="http://10.0.0.46",
        operator_database=operator_database,
    )
    assert cfg["exporters"]["otlphttp/tempo"]["endpoint"] == "http://10.0.0.10:4318"
