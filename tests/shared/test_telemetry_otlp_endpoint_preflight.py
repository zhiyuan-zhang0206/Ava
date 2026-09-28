"""Endpoint preflight (2026-08-12 prod incident) + provider construction contract.

Split out of test_telemetry_otlp.py to stay under the structure-lint's
per-file line budget.

The preflight must treat ANY HTTP answer as "collector up": the OTLP receiver
answers /v1/logs with 415 unless the body carries an OTLP content type, and
urlopen raises HTTPError on 4xx/5xx. The original probe sent no Content-Type
(urllib defaults to application/x-www-form-urlencoded), misread the 415 as
"no collector", and silently disabled OTLP export for every process that
restarted after the #1214 rollout — no events in Loki, no ava_* metrics.
"""

from __future__ import annotations

import http.server
import socket
import threading
from typing import Any

import pytest

from shared.telemetry.otlp import telemetry_otlp, telemetry_otlp_metrics
from tests.shared.test_telemetry_otlp import (
    _fresh_observability_export_gate,  # noqa: F401 — shared autouse fixture  # pyright: ignore[reportUnusedImport] — pytest fixture import
    _production_process_by_default,  # noqa: F401 — shared autouse fixture  # pyright: ignore[reportUnusedImport] — pytest fixture import
)


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    """HTTP server answering POST /v1/logs with a configurable status."""

    status = 200

    def do_POST(self) -> None:
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def probe_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_endpoint_reachable_accepts_any_http_status(
    probe_server: http.server.HTTPServer,
):
    port = probe_server.server_address[1]
    endpoint = f"http://127.0.0.1:{port}"
    reachable = telemetry_otlp._OtlpBackend._endpoint_reachable

    _ProbeHandler.status = 200
    assert reachable(endpoint) is True

    # A 415 (content-type rejection) still proves a listener is up — the
    # 2026-08-12 incident class: the probe misread it as "no collector".
    _ProbeHandler.status = 415
    assert reachable(endpoint) is True

    _ProbeHandler.status = 405
    assert reachable(endpoint) is True


def test_endpoint_reachable_connection_refused_is_false():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert telemetry_otlp._OtlpBackend._endpoint_reachable(f"http://127.0.0.1:{port}") is False


def test_endpoint_reachable_non_http_scheme_skips_probe():
    assert telemetry_otlp._OtlpBackend._endpoint_reachable("file:///tmp/x") is True


def test_production_otlp_provider_construction_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log and metric SDK workers receive the same explicit HTTP deadline, and
    both providers leave the process-exit shutdown to the emitter (task #4320)."""
    calls: dict[str, Any] = {}

    class _LoggerProvider:
        def __init__(self, **kwargs: object) -> None:
            calls["logger_provider"] = kwargs

        def add_log_record_processor(self, processor: object) -> None:
            calls["installed_log_processor"] = processor

    class _BatchLogRecordProcessor:
        def __init__(self, exporter: object, **kwargs: object) -> None:
            calls["log_processor"] = (exporter, kwargs)

    class _PeriodicExportingMetricReader:
        def __init__(self, exporter: object, **kwargs: object) -> None:
            calls["metric_reader"] = (exporter, kwargs)

    class _MeterProvider:
        def __init__(self, **kwargs: object) -> None:
            calls["meter_provider"] = kwargs

    def _log_exporter(**kwargs: object) -> object:
        calls["log_exporter"] = kwargs
        return object()

    def _metric_exporter(**kwargs: object) -> object:
        calls["metric_exporter"] = kwargs
        return object()

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter", _log_exporter
    )
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter",
        _metric_exporter,
    )
    monkeypatch.setattr("opentelemetry.sdk._logs.LoggerProvider", _LoggerProvider)
    monkeypatch.setattr(
        "opentelemetry.sdk._logs.export.BatchLogRecordProcessor", _BatchLogRecordProcessor
    )
    monkeypatch.setattr("opentelemetry.sdk.metrics.MeterProvider", _MeterProvider)
    monkeypatch.setattr(
        "opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader",
        _PeriodicExportingMetricReader,
    )
    monkeypatch.setattr(telemetry_otlp_metrics, "_metrics_resource", object)
    monkeypatch.setattr(telemetry_otlp_metrics, "_metric_views", list)

    telemetry_otlp_metrics._build_providers("http://127.0.0.1:4318")
    timeout_s = telemetry_otlp_metrics._OTLP_HTTP_TIMEOUT_S

    assert calls["log_exporter"] == {
        "endpoint": "http://127.0.0.1:4318/v1/logs",
        "timeout": timeout_s,
    }
    assert calls["metric_exporter"] == {
        "endpoint": "http://127.0.0.1:4318/v1/metrics",
        "timeout": timeout_s,
    }
    assert calls["log_processor"][1]["export_timeout_millis"] == timeout_s * 1000
    assert calls["metric_reader"][1]["export_timeout_millis"] == timeout_s * 1000
    assert 0 < timeout_s <= 5.0
    # Both providers must NOT register their own atexit shutdown: it would fire
    # ahead of `shared.telemetry._drain_on_exit` (LIFO) and strand every tail
    # record on a shut-down processor (task #4314 triage / #4320).
    assert calls["logger_provider"] == {"shutdown_on_exit": False}
    assert calls["meter_provider"]["shutdown_on_exit"] is False
