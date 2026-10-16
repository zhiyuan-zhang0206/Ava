"""Real OTLP worker support for callers that prove their close-time flush."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from threading import Event

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from base.telemetry.delivery.receipts import Event as TelemetryEvent
from base.telemetry.otlp import telemetry_otlp, telemetry_otlp_exporting

# A bounded handshake allows close telemetry to drain on a loaded runner.
HANDSHAKE_BOUND_S = 30.0


@pytest.fixture
def paused_otlp_record(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Event, InMemoryLogRecordExporter]]:
    """Pause an already-dequeued real record until the caller releases its flush."""
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        providers=(logger_provider, MeterProvider(metric_readers=[InMemoryMetricReader()])),
    )
    monkeypatch.setattr(backend, "_enabled", lambda: True)

    def build_backend(**_inputs: object) -> telemetry_otlp.OtlpBackend:
        return backend

    monkeypatch.setattr(telemetry_otlp_exporting, "OtlpBackend", build_backend)

    paused = Event()
    release = Event()
    original_emit = backend._emit_log

    def pause_after_dequeue(event: TelemetryEvent) -> None:
        paused.set()
        assert release.wait(HANDSHAKE_BOUND_S), "close did not release the paused OTLP worker"
        original_emit(event)

    monkeypatch.setattr(backend, "_emit_log", pause_after_dequeue)
    backend.export_batch(
        [
            TelemetryEvent(
                ts=datetime.now(UTC),
                trace_id=None,
                span_id=None,
                agent_id=405,
                machine="test",
                cluster="test",
                process="external-test",
                category="telemetry",
                event_name="sdk_call",
                level="info",
                source="agent:405",
                target_agent_id=None,
                attributes={"fn": "files.read", "duration": 0.01},
            )
        ]
    )
    try:
        assert paused.wait(HANDSHAKE_BOUND_S), "OTLP worker did not dequeue the tail record"
        yield release, exporter
    finally:
        release.set()
        backend.shutdown()
