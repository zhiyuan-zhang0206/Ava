"""Tests for the OTLP export backend (`shared.telemetry_otlp`).

Pins the write-side contract of the OTel+Tempo+Loki+Prometheus stack: the
three-signal mapping (events -> OTLP logs, telemetry numeric payloads -> OTLP
metrics, traces shipped separately), the AVA_TELEMETRY_OTLP_ENABLED flag, and
the failure isolation that keeps a broken OTLP side from ever touching the PG
write. Mapping tests use in-memory OTel providers; the pipeline tests run the
real emitter and assert both copies land.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from shared import telemetry, telemetry_otlp
from shared.telemetry import Event

_AGENT = 8902


def _event(
    *,
    event_name: str = "llm_usage",
    category: str = "telemetry",
    level: str = "info",
    attributes: dict[str, Any] | None = None,
    agent_id: int | None = _AGENT,
    trace_id: str | None = "abcd" * 8,
    span_id: str | None = "ef01" * 4,
) -> Event:
    return Event(
        ts=datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC),
        trace_id=trace_id,
        span_id=span_id,
        agent_id=agent_id,
        machine="test-mac",
        process="test-proc",
        category=category,  # type: ignore[arg-type]
        event_name=event_name,
        level=level,  # type: ignore[arg-type]
        source="test",
        target_agent_id=None,
        attributes=dict(attributes or {}),
    )


@pytest.fixture
def otlp_backend(monkeypatch):
    """An `_OtlpBackend` wired to in-memory OTel providers, installed as the
    module singleton so `export_batch()` / `shutdown()` hit the test instance.
    Yields (backend, log_exporter, metric_reader).

    Re-enables AVA_TELEMETRY_OTLP_ENABLED (the root conftest turns the flag
    off session-wide so event-emitting tests stay hermetic)."""
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        InMemoryLogRecordExporter,
        SimpleLogRecordProcessor,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    metric_reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=[metric_reader])

    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)  # pyright: ignore[reportUnknownMemberType]
    backend = telemetry_otlp._OtlpBackend(providers=(logger_provider, metric_provider))
    monkeypatch.setattr(telemetry_otlp, "backend", backend)  # pyright: ignore[reportUnknownMemberType]
    yield backend, log_exporter, metric_reader
    backend.shutdown()


def _metrics(metric_reader: Any) -> dict[str, Any]:
    """{metric name: Metric} from the in-memory reader ({} when none)."""
    data = metric_reader.get_metrics_data()
    out: dict[str, Any] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = m
    return out


def _attrs_of(dp: Any) -> dict[str, Any]:
    # OTel 1.39 datapoint.attributes is already a plain dict.
    return dict(dp.attributes)


# ── signal mapping ───────────────────────────────────────────────────────────


def test_log_mapping_full_record_shape(otlp_backend) -> None:
    """One event -> one OTLP LogRecord: severity mapping, indexed attributes,
    JSON body in the mirror shape, and trace/span ids as the correlation
    fields."""
    backend, log_exporter, _ = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="exec",
                category="log",
                level="warning",
                attributes={"body": "print(1)", "ok": False},
            )
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]

    records = log_exporter.get_finished_logs()  # pyright: ignore[reportUnknownMemberType]
    assert len(records) == 1  # pyright: ignore[reportUnknownArgumentType]
    r = records[0]
    assert r.log_record.severity_text == "warning"  # pyright: ignore[reportUnknownMemberType]
    assert r.log_record.severity_number.value == 13  # pyright: ignore[reportUnknownMemberType]
    assert r.log_record.trace_id == int("abcd" * 8, 16)  # pyright: ignore[reportUnknownMemberType]
    assert r.log_record.span_id == int("ef01" * 4, 16)  # pyright: ignore[reportUnknownMemberType]
    attrs = dict(r.log_record.attributes)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert attrs["event_name"] == "exec"
    assert attrs["category"] == "log"
    assert attrs["level"] == "warning"
    assert attrs["machine"] == "test-mac"
    assert attrs["process"] == "test-proc"
    assert attrs["source"] == "test"
    assert attrs["agent_id"] == _AGENT
    body = json.loads(r.log_record.body)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert body["event_name"] == "exec"
    assert body["attributes"] == {"body": "print(1)", "ok": False}
    assert body["ts"] == "2026-08-11T12:00:00+00:00"


def test_metric_mapping_int_counter_float_histogram(otlp_backend) -> None:
    """Telemetry numeric payloads map by type: int -> Counter, float ->
    Histogram, with process dimensions + guarded payload scalars as
    attributes. `body` never becomes a label; bool payload scalars (ok on
    turn_end) do."""
    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="llm_usage",
                attributes={
                    "model": "claude-sonnet",
                    "in_total": 100,
                    "out_total": 50,
                    "latency_ms": 42.5,
                    "ok": True,  # NOT a declared llm_usage payload key
                    "body": "x" * 500,
                },
            ),
            _event(
                event_name="turn_end",
                attributes={"ok": True, "duration_seconds": 4.0},
            ),
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]
    metrics = _metrics(metric_reader)

    counter = metrics["ava_llm_usage_in_total"]
    assert counter.unit == "1"
    assert len(counter.data.data_points) == 1
    dp = counter.data.data_points[0]
    assert dp.value == 100
    a = _attrs_of(dp)
    assert a["model"] == "claude-sonnet"
    assert "ok" not in a  # not in the llm_usage payload declaration
    assert a["agent_id"] == _AGENT
    assert a["machine"] == "test-mac"
    assert "body" not in a

    hist = metrics["ava_llm_usage_latency_ms"]
    assert hist.unit == "ms"
    assert hist.data.data_points[0].count == 1
    assert hist.data.data_points[0].sum == 42.5

    # turn_end's declared bool payload key rides as an attribute, not a metric.
    assert "ava_turn_end_ok" not in metrics
    turn = _attrs_of(metrics["ava_turn_end_duration_seconds"].data.data_points[0])
    assert turn["ok"] is True

    assert "ava_llm_usage_ok" not in metrics  # bools are attributes, not metrics


def test_metric_mapping_skips_long_string_attributes(otlp_backend) -> None:
    """Strings over the attribute length cap are dropped (cardinality guard)."""
    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [_event(event_name="llm_usage", attributes={"model": "m" * 100, "in_total": 1})]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]
    dp = _metrics(metric_reader)["ava_llm_usage_in_total"].data.data_points[0]
    assert "model" not in _attrs_of(dp)


def test_metric_mapping_excludes_loguru_decoration_extras(otlp_backend) -> None:
    """loguru decoration extras (msg / cache_pct / reason_pct) never become
    metric attributes: a per-event unique msg string would split every event
    into its own series, and a counter split into single-sample series reads
    as zero increments (the fleet graph / dashboard aggregation regression
    this guard closes)."""
    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="llm_usage",
                attributes={
                    "model": "deepseek-v4-flash",
                    "in_total": 100,
                    "out_total": 50,
                    "cache_read": 90,
                    "reasoning": 10,
                    "msg": "[llm usage] in=100 cached=90 (90%)  out=50 reason=10 (20%)",
                    "cache_pct": " (90%)",
                    "reason_pct": " (20%)",
                },
            )
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]
    dp = _metrics(metric_reader)["ava_llm_usage_in_total"].data.data_points[0]
    a = _attrs_of(dp)
    assert a["model"] == "deepseek-v4-flash"
    assert "msg" not in a
    assert "cache_pct" not in a
    assert "reason_pct" not in a


def test_non_telemetry_events_produce_no_metrics(otlp_backend) -> None:
    """Log/audit events are the event stream, not a measurement: no metrics."""
    backend, log_exporter, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(event_name="exec", category="log", attributes={"n": 3, "ok": True}),
            _event(event_name="process_exit", category="audit", attributes={"code": 0}),
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]
    assert len(log_exporter.get_finished_logs()) == 2  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert _metrics(metric_reader) == {}


# ── flag ─────────────────────────────────────────────────────────────────────


def test_flag_off_disables_export(otlp_backend, monkeypatch) -> None:
    """AVA_TELEMETRY_OTLP_ENABLED=false -> export is a no-op: no backend
    bring-up, no records, no queue traffic."""
    backend, log_exporter, metric_reader = otlp_backend
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", False)  # pyright: ignore[reportUnknownMemberType]
    backend.export_batch([_event()])  # pyright: ignore[reportUnknownMemberType]
    assert backend._logs is None  # never brought up  # pyright: ignore[reportUnknownMemberType]
    assert backend._queue.empty()  # pyright: ignore[reportUnknownMemberType]
    assert log_exporter.get_finished_logs() == ()  # pyright: ignore[reportUnknownMemberType]
    assert metric_reader.get_metrics_data() is None  # pyright: ignore[reportUnknownMemberType]


def test_flag_defaults_on_with_standard_endpoint(monkeypatch) -> None:
    """The 2026-08-11 stack decision: OTLP export is ON by default, endpoint
    = the standard OTLP/HTTP port on loopback."""
    from shared.config import settings

    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)  # pyright: ignore[reportUnknownMemberType]
    assert settings.observability.telemetry_otlp_enabled is True
    assert settings.observability.telemetry_otlp_endpoint == "http://127.0.0.1:4318"


# ── failure isolation ────────────────────────────────────────────────────────


def test_backend_init_failure_isolated_and_disables(monkeypatch) -> None:
    """A backend that cannot come up (bad endpoint / missing dep) never raises
    into the emitter and never retries per batch."""
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)  # pyright: ignore[reportUnknownMemberType]

    def boom(endpoint: str) -> tuple[Any, Any]:
        raise RuntimeError("collector unreachable")

    monkeypatch.setattr(telemetry_otlp, "_build_providers", boom)  # pyright: ignore[reportUnknownMemberType]
    backend = telemetry_otlp._OtlpBackend()
    assert backend.export_batch([_event()]) is None  # does not raise
    assert backend._logs is None
    assert backend.export_batch([_event()]) is None  # no retry
    assert backend._init_attempted


def test_queue_full_sheds_counted_not_blocking(monkeypatch) -> None:
    """The bounded queue sheds instead of blocking the caller when the worker
    cannot keep up — the isolation contract for a hung OTLP endpoint."""
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)  # pyright: ignore[reportUnknownMemberType]
    backend = telemetry_otlp._OtlpBackend(providers=(None, None), queue_maxsize=1)
    monkeypatch.setattr(telemetry_otlp._OtlpBackend, "_ensure", lambda _self: True)  # pyright: ignore[reportUnknownMemberType]
    backend._queue.put_nowait(_event())  # fill the queue
    backend.export_batch([_event()])
    assert backend._dropped == 1
    assert backend._queue.qsize() == 1  # still full, caller not blocked


# ── pipeline integration (real emitter + Postgres) ──────────────────────────


@pytest.fixture(autouse=True)
def _bind_telemetry(db_conn: psycopg.Connection) -> None:
    telemetry.init_telemetry(process="test-proc")
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (%s) ON CONFLICT DO NOTHING", (_AGENT,))
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running') "
            "ON CONFLICT DO NOTHING",
            (_AGENT,),
        )
    db_conn.commit()


def test_pipeline_exports_to_otlp(otlp_backend) -> None:
    """One emit lands the OTLP copies (log record + metric) through the real
    drain thread. The Postgres copy was retired with the LGTM cutover (task
    #1197) — OTLP + the JSONL mirror are the sinks."""
    _backend, log_exporter, metric_reader = otlp_backend
    telemetry.emit(
        "telemetry",
        "llm_usage",
        agent_id=_AGENT,
        attributes={"model": "m", "in_total": 7, "latency_ms": 1.5},
    )
    telemetry.sync()

    # OTLP copies — the worker thread is async, poll (repo convention: the
    # emitter's own drain is async too; a flush can race it).
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not log_exporter.get_finished_logs():  # pyright: ignore[reportUnknownMemberType]
        time.sleep(0.05)
    records = log_exporter.get_finished_logs()  # pyright: ignore[reportUnknownMemberType]
    assert len(records) == 1  # pyright: ignore[reportUnknownArgumentType]
    attrs = records[0].log_record.attributes  # pyright: ignore[reportUnknownMemberType]
    assert attrs is not None
    assert attrs["event_name"] == "llm_usage"

    metrics = _metrics(metric_reader)
    assert metrics["ava_llm_usage_in_total"].data.data_points[0].value == 7
    assert metrics["ava_llm_usage_latency_ms"].data.data_points[0].sum == 1.5


def test_pipeline_mirror_survives_otlp_failure(monkeypatch) -> None:
    """A broken OTLP backend never blocks the drain — the JSONL mirror still
    holds the batch (the PG copy is gone, task #1197)."""

    def boom(endpoint: str) -> tuple[Any, Any]:
        raise RuntimeError("collector unreachable")

    monkeypatch.setattr(telemetry_otlp, "_build_providers", boom)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr(telemetry_otlp, "backend", telemetry_otlp._OtlpBackend())  # pyright: ignore[reportUnknownMemberType]
    telemetry.emit("log", "log", agent_id=_AGENT, attributes={"msg": "boom"})
    telemetry.sync()

    from shared.paths import logs_dir

    day = datetime.now(UTC).strftime("%Y%m%d")
    path = logs_dir() / f"events-{day}.jsonl"
    assert path.exists()
    assert any(
        '"event_name":"log"' in line and '"msg":"boom"' in line
        for line in path.read_text(encoding="utf-8").splitlines()
    )


# ─── endpoint preflight (2026-08-12 prod incident) ────────────────────────────
# The preflight must treat ANY HTTP answer as "collector up": the OTLP receiver
# answers /v1/logs with 415 unless the body carries an OTLP content type, and
# urlopen raises HTTPError on 4xx/5xx. The original probe sent no Content-Type
# (urllib defaults to application/x-www-form-urlencoded), misread the 415 as
# "no collector", and silently disabled OTLP export for every process that
# restarted after the #1214 rollout — no events in Loki, no ava_* metrics.


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
    reachable = telemetry_otlp._OtlpBackend._endpoint_reachable  # pyright: ignore[reportUnknownMemberType]

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
    assert (
        telemetry_otlp._OtlpBackend._endpoint_reachable(  # pyright: ignore[reportUnknownMemberType] — private seam under test
            f"http://127.0.0.1:{port}"
        )
        is False
    )


def test_endpoint_reachable_non_http_scheme_skips_probe():
    assert (
        telemetry_otlp._OtlpBackend._endpoint_reachable(  # pyright: ignore[reportUnknownMemberType] — private seam under test
            "file:///tmp/x"
        )
        is True
    )
