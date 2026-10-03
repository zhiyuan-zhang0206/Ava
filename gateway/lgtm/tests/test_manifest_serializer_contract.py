"""Canonical event bytes shared by JSONL and OTLP."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from base import telemetry
from base.telemetry import Event
from base.telemetry.otlp import telemetry_otlp


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
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)
    monkeypatch.setattr(telemetry_otlp, "observability_export_allowed", lambda: True)
    monkeypatch.setattr(telemetry_otlp, "backend", backend)
    yield backend, exporter
    backend.shutdown()


def test_jsonl_and_otlp_use_one_byte_identity(
    otlp_backend: tuple[Any, Any],
) -> None:
    """The JSONL mirror line and the OTLP log body are the exact same bytes."""
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
    assert record.log_record.body == jsonl
