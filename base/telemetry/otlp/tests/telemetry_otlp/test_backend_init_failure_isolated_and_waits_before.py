"""Telemetry otlp cases: backend init failure isolated and waits before."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from base import telemetry
from base.telemetry import Event
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.otlp.tests.test_telemetry_otlp import _AGENT, _event, _metrics
from base.telemetry.otlp.tests.test_telemetry_otlp import (
    _bind_telemetry as _bind_telemetry,
)
from base.telemetry.otlp.tests.test_telemetry_otlp import (
    _fresh_observability_export_gate as _fresh_observability_export_gate,
)
from base.telemetry.otlp.tests.test_telemetry_otlp import (
    _production_process_by_default as _production_process_by_default,
)
from base.telemetry.otlp.tests.test_telemetry_otlp import (
    otlp_backend as otlp_backend,
)


def test_backend_init_failure_isolated_and_waits_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that cannot come up (bad endpoint / missing dep) never raises
    into the emitter and does not retry again inside the five-minute window."""
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)

    def reachable(_endpoint: str) -> bool:
        return True

    monkeypatch.setattr(telemetry_otlp._OtlpBackend, "_endpoint_reachable", staticmethod(reachable))
    attempts: list[str] = []

    def boom(endpoint: str) -> tuple[Any, Any]:
        attempts.append(endpoint)
        raise RuntimeError("collector unreachable")

    monkeypatch.setattr(telemetry_otlp, "_build_providers", boom)
    backend = telemetry_otlp._OtlpBackend()
    assert backend.export_batch([_event()]) is None  # does not raise
    assert backend._logs is None
    assert backend.export_batch([_event()]) is None  # no per-batch retry
    assert attempts == ["http://127.0.0.1:4318"]
    assert backend._init_failed_at is not None


def test_backend_retry_recovers_and_emits_real_status_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed init reports into the surviving mirror, then a later retry
    initializes the backend and reports recovery."""
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)

    def reachable(_endpoint: str) -> bool:
        return True

    monkeypatch.setattr(telemetry_otlp._OtlpBackend, "_endpoint_reachable", staticmethod(reachable))
    now = [100.0]
    monkeypatch.setattr(telemetry_otlp.time, "monotonic", lambda: now[0])
    emitted: list[tuple[str, dict[str, Any]]] = []

    def capture_emit(
        _category: str, event_name: str, *, attributes: dict[str, Any], **_kwargs: Any
    ) -> None:
        emitted.append((event_name, attributes))

    monkeypatch.setattr(telemetry, "emit", capture_emit)

    class _MetricProvider:
        def get_meter(self, _name: str) -> object:
            return object()

    logs = object()
    metrics = _MetricProvider()
    attempts = 0

    def build(endpoint: str) -> tuple[Any, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("collector booting")
        return logs, metrics

    monkeypatch.setattr(telemetry_otlp, "_build_providers", build)
    backend = telemetry_otlp._OtlpBackend()
    try:
        assert backend._ensure() is False
        assert emitted == [
            (
                "otlp_backend_disabled",
                {
                    "reason": "init failed: RuntimeError('collector booting')",
                    "endpoint": "http://127.0.0.1:4318",
                },
            )
        ]

        assert backend._ensure() is False
        assert attempts == 1

        now[0] += telemetry_otlp.COLLECTOR_RETRY_INTERVAL_S
        assert backend._ensure() is True
        assert backend._logs is logs
        assert emitted[-1] == (
            "otlp_backend_recovered",
            {
                "endpoint": "http://127.0.0.1:4318",
                "disabled_s": telemetry_otlp.COLLECTOR_RETRY_INTERVAL_S,
            },
        )
        assert backend._init_failed_at is None
    finally:
        backend.shutdown()


def test_backend_unreachable_probe_emits_specific_disabled_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)

    def unreachable(_endpoint: str) -> bool:
        return False

    monkeypatch.setattr(
        telemetry_otlp._OtlpBackend, "_endpoint_reachable", staticmethod(unreachable)
    )
    emitted: list[tuple[str, dict[str, Any]]] = []

    def capture_emit(
        _category: str, event_name: str, *, attributes: dict[str, Any], **_kwargs: Any
    ) -> None:
        emitted.append((event_name, attributes))

    monkeypatch.setattr(telemetry, "emit", capture_emit)

    backend = telemetry_otlp._OtlpBackend()
    assert backend._ensure() is False
    assert emitted == [
        (
            "otlp_backend_disabled",
            {
                "reason": "endpoint not answering",
                "endpoint": "http://127.0.0.1:4318",
            },
        )
    ]


def test_queue_full_sheds_counted_not_blocking(monkeypatch) -> None:
    """The bounded queue sheds instead of blocking the caller when the worker
    cannot keep up — the isolation contract for a hung OTLP endpoint."""
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)  # pyright: ignore[reportUnknownMemberType]
    backend = telemetry_otlp._OtlpBackend(providers=(None, None), queue_maxsize=1)
    monkeypatch.setattr(telemetry_otlp._OtlpBackend, "_ensure", lambda _self: True)  # pyright: ignore[reportUnknownMemberType]
    backend._queue.put_nowait(_event())  # fill the queue
    backend.export_batch([_event()])
    assert backend._dropped == 1
    assert backend._queue.qsize() == 1  # still full, caller not blocked


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
    assert metrics["ava_llm_usage_latency"].data.data_points[0].sum == 1.5


def test_pipeline_mirror_survives_otlp_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken OTLP backend never blocks the drain — the JSONL mirror still
    holds the batch (the PG copy is gone, task #1197)."""

    def boom(endpoint: str) -> tuple[Any, Any]:
        raise RuntimeError("collector unreachable")

    monkeypatch.setattr("base.telemetry.emitter.logs_dir", lambda: tmp_path)
    monkeypatch.setattr(telemetry_otlp, "_build_providers", boom)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr(telemetry_otlp, "backend", telemetry_otlp._OtlpBackend())  # pyright: ignore[reportUnknownMemberType]
    telemetry.emit("log", "log", agent_id=_AGENT, attributes={"msg": "boom"})
    telemetry.sync()

    day = datetime.now(UTC).strftime("%Y%m%d")
    path = tmp_path / f"events-{day}.jsonl"
    assert path.exists()
    assert any(
        '"event_name":"log"' in line and '"msg":"boom"' in line
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_handshake_env_no_longer_gates_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AVA_EXEC_REQUEST_FILE handshake was the old child-export gate; it
    must no longer disable OTLP — the export flag is the only authority."""
    monkeypatch.setenv("AVA_EXEC_REQUEST_FILE", "request.json")
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)
    assert telemetry_otlp._OtlpBackend._enabled() is True


def test_flush_force_flushes_sdk_batch_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """flush() must force-flush the SDK batch processor: a short-lived exec
    child exits before the 5s batch window fires on its own, so without the
    force_flush the app-level queue drain is not enough to reach the wire."""
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        BatchLogRecordProcessor,
        InMemoryLogRecordExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    metric_provider = MeterProvider(metric_readers=[InMemoryMetricReader()])
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)
    backend = telemetry_otlp._OtlpBackend(providers=(logger_provider, metric_provider))
    monkeypatch.setattr(telemetry_otlp, "backend", backend)
    try:
        backend.export_batch([_event(attributes={"fn": "files.read"})])
        # Without force_flush the in-memory batch exporter would stay empty
        # until its 5s schedule; the flush must surface the record now.
        telemetry_otlp.flush()
        assert len(log_exporter.get_finished_logs()) == 1
    finally:
        backend.shutdown()


def test_warmup_builds_backend_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """warmup() constructs the OTel providers so a short-lived child never
    first builds them during interpreter shutdown."""
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
    backend = telemetry_otlp._OtlpBackend(
        providers=(
            logger_provider,
            MeterProvider(metric_readers=[InMemoryMetricReader()]),
        )
    )
    monkeypatch.setattr(telemetry_otlp, "backend", backend)
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)
    try:
        telemetry_otlp.warmup()
        assert backend._logs is not None
    finally:
        backend.shutdown()


def test_queue_loss_metric_survives_full_log_lane(
    otlp_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import queue

    backend, _logs, reader = otlp_backend
    assert backend._ensure()

    class FullQueue:
        def put_nowait(self, event: Event) -> None:
            raise queue.Full

    mirrors: list[Event] = []
    monkeypatch.setattr(backend, "_queue", FullQueue())
    monkeypatch.setattr(telemetry, "_append_jsonl", mirrors.extend)
    backend.export_batch([_event(), _event()])
    assert mirrors[0].event_name == "event_log_drop"
    assert mirrors[0].attributes["n"] == 2
    metrics = _metrics(reader)
    assert metrics["ava_event_log_drop_n"].data.data_points[0].value == 2
    assert metrics["ava_event_log_drop_last_dropped_at"].data.data_points[0].value > 0


def test_real_otel_sdk_queue_overflow_is_observed_without_log_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    from opentelemetry._logs import LogRecord
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        BatchLogRecordProcessor,
        LogRecordExporter,
        LogRecordExportResult,
    )

    from base.telemetry import loss

    entered, release = threading.Event(), threading.Event()

    class BlockedExporter(LogRecordExporter):
        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

        def export(self, batch: Any) -> Any:
            entered.set()
            assert release.wait(5)
            return LogRecordExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    sdk_logger = logging.getLogger("opentelemetry.sdk._shared_internal")
    old_filters = list(sdk_logger.filters)
    loss.install_exporter_drop_observer()
    reports: list[Event] = []
    monkeypatch.setattr(telemetry, "_append_jsonl", reports.extend)
    provider = LoggerProvider()
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            BlockedExporter(),
            max_queue_size=1,
            max_export_batch_size=1,
            schedule_delay_millis=60000,
        )
    )
    emitter = provider.get_logger("test")
    try:
        emitter.emit(LogRecord(body="first"))
        assert entered.wait(2)
        for _ in range(3):
            emitter.emit(LogRecord(body="more"))
        assert len(reports) == 2
        assert all(
            row.attributes["queue"] == "otel-sdk" and row.level == "error" for row in reports
        )
    finally:
        release.set()
        provider.shutdown()
        sdk_logger.filters[:] = old_filters


def test_delayed_loss_summary_cannot_rewind_newer_loss_metric(otlp_backend: Any) -> None:
    backend, _logs, reader = otlp_backend
    assert backend._ensure()
    for timestamp in (2000.0, 1000.0):
        backend._record_metrics(
            _event(
                category="telemetry",
                event_name="event_log_drop",
                attributes={"n": 1, "queue": "emitter", "last_dropped_at": timestamp},
            )
        )
    metrics = _metrics(reader)
    assert metrics["ava_event_log_drop_last_dropped_at"].data.data_points[0].value == 2000.0
    assert metrics["ava_event_log_drop_n"].data.data_points[0].value == 2
