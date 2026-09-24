"""Canonical event bytes shared by JSONL, OTLP, and Loki."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from shared import telemetry
from shared.telemetry import Event
from shared.telemetry.otlp import telemetry_otlp


@pytest.fixture
def otlp_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    """An in-memory OTLP log exporter for the cross-store byte contract."""
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    exporter = InMemoryLogRecordExporter()
    logs = LoggerProvider()
    resource_exporter: Any = telemetry_otlp._EventDimensionResourceExporter(exporter)
    logs.add_log_record_processor(SimpleLogRecordProcessor(resource_exporter))
    backend = telemetry_otlp._OtlpBackend(
        providers=(logs, MeterProvider(metric_readers=[InMemoryMetricReader()]))
    )
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)
    monkeypatch.setattr(telemetry_otlp, "_observability_export_allowed", lambda: True)
    monkeypatch.setattr(telemetry_otlp, "backend", backend)
    yield backend, exporter
    backend.shutdown()


def test_jsonl_otlp_and_loki_use_one_byte_identity_with_drift_rejected(
    otlp_backend: tuple[Any, Any],
) -> None:
    """The producer census and Loki reader must share the exact event bytes."""
    from gateway._loki_event_rows import _parse_line

    backend, log_exporter = otlp_backend
    event = Event(
        ts=datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC),
        trace_id="abcd" * 8,
        span_id="ef01" * 4,
        agent_id=8902,
        machine="test-mac",
        cluster=".ava-test",
        process="test-proc",
        category="audit",
        event_name="send_message",
        level="info",
        source="test",
        target_agent_id=None,
        attributes={"content": "canonical manifest bytes", "inbound_id": 42},
    )
    backend.export_batch([event])
    backend.flush()
    record = log_exporter.get_finished_logs()[0]
    jsonl = telemetry.event_line(event)
    timestamp_ns = int(event.ts.timestamp() * 1_000_000_000)
    loki = _parse_line(jsonl, timestamp_ns)
    assert loki is not None
    assert record.log_record.body == jsonl
    assert telemetry.event_row(event)["id"] == loki["id"]
    assert telemetry.event_line_digest(event) == loki["line_sha256"]

    drifted = _parse_line(jsonl + " ", timestamp_ns)
    assert drifted is not None
    with pytest.raises(AssertionError):
        assert telemetry.event_row(event)["id"] == drifted["id"]
