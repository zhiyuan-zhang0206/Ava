"""`services.healthchecks.otel_collector` unit tests — OTLP probe + respawn.

The sidecar's health is "does the OTLP receiver answer" — ANY HTTP status from
/v1/traces proves the collector's listener is up (same probe the agent
exporters use at init). These tests verify the probe logic and the respawn
invocation without running a collector.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from services.healthchecks import otel_collector as hc
from shared.daemon_health import DaemonProbe


def test_is_alive_rejecting_valid_otlp_is_not_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A listener that rejects the collector's valid OTLP body is not a healthy
    ingestion pipeline; a bare socket answer is insufficient."""

    def _raise(_req, **_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise urllib.error.HTTPError(
            "http://127.0.0.1:4318/v1/traces",
            415,
            "Unsupported Media Type",
            {},  # pyright: ignore[reportArgumentType]
            None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert hc._is_alive() is False


def test_is_alive_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx proves the receiver parsed and accepted a valid OTLP request."""

    seen: dict[str, object] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def _open(req: urllib.request.Request, **_kw: object) -> _Resp:
        seen["url"] = req.full_url
        seen["body"] = req.data
        seen["content_type"] = req.headers["Content-type"]
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _open)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._is_alive() is True
    assert seen == {
        "url": "http://127.0.0.1:4318/v1/traces",
        "body": b'{"resourceSpans":[]}',
        "content_type": "application/json",
    }


def test_is_alive_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection-level failure — the sidecar is down."""

    def _raise(_req, **_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._is_alive() is False


def test_restart_invokes_verified_respawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_restart_daemon` takes over stale holders and verifies the roster command."""

    took_over: list[int] = []

    def fake_respawn(session: str, cmd: str, _repo, **kwargs) -> DaemonProbe:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        assert session == "otel-collector"
        assert "otelcol-contrib" in cmd and "config.yaml" in cmd
        assert kwargs["verify"] is hc.probe_collector
        return DaemonProbe.up("collector pid 222")

    monkeypatch.setattr(hc, "take_over_stale_collector", lambda: took_over.append(1))
    monkeypatch.setattr(hc, "respawn_and_verify", fake_respawn)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._restart_daemon().alive is True
    assert took_over == [1]


def test_main_restarts_a_stale_collector_even_when_its_otlp_port_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale collector's 2xx cannot suppress the restarter's recovery path."""
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "_is_alive", lambda: True)
    monkeypatch.setattr(
        hc,
        "probe_collector",
        lambda: DaemonProbe.down("collector pid 1109 has no live ava-otel-collector record"),
        raising=False,
    )
    restarts: list[int] = []
    monkeypatch.setattr(
        hc,
        "_restart_daemon",
        lambda: (restarts.append(1), DaemonProbe.up("collector pid 222"))[1],
    )

    hc.main()

    assert restarts == [1]


def test_queue_pressure_uses_configured_self_metrics_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog probes the same per-unit endpoint that converge renders."""
    seen: list[str] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def _open(url: str, **_kw: object) -> _Resp:
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(hc.settings.observability, "otel_collector_metrics_port", 8889)
    monkeypatch.setattr(urllib.request, "urlopen", _open)

    assert hc._queue_pressure() == hc.CollectorPressure(saturated=(), enqueue_failures={})
    assert seen == ["http://localhost:8889/metrics"]


def test_stale_collector_reclaim_uses_configured_self_metrics_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Takeover inspects this unit's configured listener, not another unit's default."""
    seen: list[tuple[int, ...]] = []

    def _reclaim(_service: str, *, ports: tuple[int, ...], binary: object) -> None:
        assert binary == hc.otel_collector_binary()
        seen.append(ports)

    monkeypatch.setattr(hc.settings.observability, "otel_collector_metrics_port", 8889)
    monkeypatch.setattr(hc, "reclaim_stale_supervised_listener", _reclaim)

    hc.take_over_stale_collector()

    assert seen == [(4318, 8889)]


def test_queue_pressure_reports_full_queue_and_drop_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collector internal metrics make the exact queue and rejected-item count
    visible; the healthcheck does not infer pressure from process liveness."""
    payload = b"""\
otelcol_exporter_queue_capacity{data_type="metrics",exporter="otlphttp/prometheus"} 1000
otelcol_exporter_queue_size{data_type="metrics",exporter="otlphttp/prometheus"} 1000
otelcol_exporter_enqueue_failed_metric_points{exporter="otlphttp/prometheus"} 78336
otelcol_exporter_queue_capacity{data_type="logs",exporter="otlphttp/loki"} 5000
otelcol_exporter_queue_size{data_type="logs",exporter="otlphttp/loki"} 12
"""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return payload

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_kw: _Resp())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    pressure = hc._queue_pressure()
    assert pressure is not None
    assert pressure.saturated == ("otlphttp/prometheus",)
    assert pressure.enqueue_failures == {"otlphttp/prometheus": 78336}


def test_main_warns_on_queue_pressure_without_restart_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote outage cannot be repaired by respawning the healthy local
    collector every minute; pressure is loud but service liveness stays up."""
    pressure = hc.CollectorPressure(
        saturated=("otlphttp/prometheus",),
        enqueue_failures={"otlphttp/prometheus": 42},
    )

    def _ignore_process_init(_name: str) -> None:
        return None

    monkeypatch.setattr(hc, "init_gateway_process", _ignore_process_init)
    monkeypatch.setattr(hc, "_is_alive", lambda: True)
    monkeypatch.setattr(hc, "probe_collector", lambda: DaemonProbe.up("collector pid 111"))
    monkeypatch.setattr(hc, "_queue_pressure", lambda: pressure)
    monkeypatch.setattr(
        hc,
        "_restart_daemon",
        lambda: pytest.fail("healthy local collector must not restart for remote pressure"),
    )

    records: list[str] = []

    class _Log:
        def bind(self, **_kw: object) -> _Log:
            return self

        def warning(self, message: str, *args: object) -> None:
            records.append(message.format(*args))

    monkeypatch.setattr(hc, "logger", _Log())

    hc.main()

    assert len(records) == 1
    assert "queue saturated" in records[0]
    assert "otlphttp/prometheus" in records[0]
    assert "enqueue failures=42" in records[0]
