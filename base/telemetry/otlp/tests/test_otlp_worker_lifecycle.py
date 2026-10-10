"""Real owned Thread proofs for bounded OTLP init, flush and teardown."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from base.telemetry import DrainStatus, Event
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.otlp import telemetry_otlp_worker as worker_module
from base.telemetry.otlp.tests.test_telemetry_otlp import _event


class _Provider:
    def __init__(self) -> None:
        self.closed = threading.Event()
        self.flushed = threading.Event()

    def get_meter(self, _name: str) -> object:
        return object()

    def force_flush(self, *, timeout_millis: int) -> None:
        self.flushed.set()

    def shutdown(self) -> None:
        self.closed.set()


def _backend(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Provider, _Provider]:
    logs, metrics = _Provider(), _Provider()
    backend = telemetry_otlp._OtlpBackend(providers=(logs, metrics))
    monkeypatch.setattr(backend, "_enabled", lambda: True)

    def ignore(_event: Event) -> None:
        pass

    monkeypatch.setattr(backend, "_emit_log", ignore)
    monkeypatch.setattr(backend, "_record_metrics", ignore)
    return backend, logs, metrics


def test_blocked_sdk_init_stays_owned_after_finite_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, logs, metrics = _backend(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = backend._initialize_worker

    def initialize(worker: Any) -> bool:
        entered.set()
        assert release.wait(5)
        return original(worker)

    monkeypatch.setattr(backend, "_initialize_worker", initialize)
    worker = backend._get_worker()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert time.monotonic() - started < 1
        assert worker._thread.is_alive()
        assert backend._worker is worker
        backend.export_batch([_event()])
        assert backend._get_worker() is None
        assert not logs.closed.is_set()
    finally:
        release.set()
        assert worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
    assert logs.closed.is_set() and metrics.closed.is_set()


def test_sdk_flush_does_not_block_observing_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, logs, _ = _backend(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def flush(*, timeout_millis: int) -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(logs, "force_flush", flush)
    backend.export_batch([_event()])
    try:
        started = time.monotonic()
        assert backend.flush(timeout=0.01).status is DrainStatus.UNFINISHED
        assert entered.is_set()
        assert time.monotonic() - started < 1
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert not logs.closed.is_set()
    finally:
        release.set()
        assert backend._worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
    assert logs.closed.is_set()


def test_full_queue_stop_uses_same_worker_and_preserves_admitted_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, logs, _ = _backend(monkeypatch)
    backend._queue.maxsize = 1
    entered, release = threading.Event(), threading.Event()
    delivered: list[Any] = []

    def emit(event: Any) -> None:
        entered.set()
        assert release.wait(5)
        delivered.append(event)

    monkeypatch.setattr(backend, "_emit_log", emit)
    first, second, after_stop = _event(), _event(), _event()
    backend.export_batch([first])
    try:
        assert entered.wait(5)
        backend.export_batch([second])
        assert backend._queue.full()
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        backend.export_batch([after_stop])
        assert not logs.closed.is_set()
    finally:
        release.set()
        assert backend._worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
    assert delivered == [first, second]


def test_unknown_error_after_stop_is_immediate_and_same_owner_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _, _ = _backend(monkeypatch)
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    error = BaseException("unknown late initialization defect")
    seen: list[BaseException] = []

    def initialize(_worker: Any) -> bool:
        entered.set()
        assert release.wait(5)
        raise error

    def report(_message: str, **details: Any) -> None:
        if "exc" in details:
            seen.append(details["exc"])
            observed.set()

    monkeypatch.setattr(backend, "_initialize_worker", initialize)
    monkeypatch.setattr(worker_module, "report_no_pipeline", report)
    worker = backend._get_worker()
    try:
        assert entered.wait(5)
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert not observed.is_set()
        release.set()
        assert observed.wait(5)
        assert worker.finished.wait(5)
        assert seen == [error]
        with pytest.raises(BaseException) as raised:
            backend.shutdown()
        assert raised.value is error
        assert backend._worker is worker
    finally:
        release.set()
        worker._thread.join(timeout=5)
        assert not worker._thread.is_alive()


def test_failed_sdk_build_keeps_partial_resources_until_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = telemetry_otlp._OtlpBackend()
    logs, reader = _Provider(), _Provider()
    cleaning, release = threading.Event(), threading.Event()

    def close() -> None:
        cleaning.set()
        assert release.wait(5)
        logs.closed.set()

    def build(_endpoint: str, **receipts: Any) -> tuple[Any, Any]:
        receipts["keep_logs"](logs)
        receipts["keep_reader"](reader)
        raise RuntimeError("named SDK construction failure")

    monkeypatch.setattr(logs, "shutdown", close)

    def reachable(_endpoint: str) -> bool:
        return True

    monkeypatch.setattr(backend, "_endpoint_reachable", reachable)
    monkeypatch.setattr(telemetry_otlp, "_build_providers", build)
    try:
        assert backend._ensure() is False
        assert cleaning.wait(5)
        worker = backend._worker
        assert worker is not None
        assert worker.logs is logs and worker.metric_reader is reader
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert not reader.closed.is_set()
    finally:
        release.set()
        worker = backend._worker
        assert worker is not None
        assert worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
    assert logs.closed.is_set() and reader.closed.is_set()


def test_shutdown_replays_deferred_metrics_before_logs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _, _ = _backend(monkeypatch)
    calls: list[tuple[str, Any]] = []

    def record(event: Event) -> None:
        calls.append(("metric", event))

    def emit(event: Event) -> None:
        calls.append(("log", event))

    monkeypatch.setattr(backend, "_record_metrics", record)
    monkeypatch.setattr(backend, "_emit_log", emit)
    first, second = _event(), _event()
    backend.defer_until_exit()
    backend.export_batch([first, second])
    assert backend.shutdown().status is DrainStatus.COMPLETED
    assert calls == [("metric", first), ("metric", second), ("log", first), ("log", second)]
    assert backend.shutdown().status is DrainStatus.COMPLETED
    assert len(calls) == 4


def test_disabled_nonempty_deferred_shutdown_does_not_start_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, logs, metrics = _backend(monkeypatch)
    reads: list[bool] = []
    delivered: list[Event] = []

    def enabled_at_bringup() -> bool:
        reads.append(False)
        return False

    monkeypatch.setattr(backend, "_enabled", enabled_at_bringup)
    monkeypatch.setattr(backend, "_emit_log", delivered.append)
    event = _event()
    backend.defer_until_exit()
    backend.export_batch([event])
    assert reads == []  # holding remains cold; the flag is read only at bring-up
    result = backend.shutdown()
    assert reads == [False]
    assert backend._closed.is_set()
    assert backend._deferral.stop(timeout=5)
    assert result.status is DrainStatus.UNFINISHED
    assert backend._worker is None
    assert backend._queue.qsize() == 1
    assert delivered == []
    assert not logs.flushed.is_set() and not logs.closed.is_set()
    assert not metrics.closed.is_set()


def test_enabled_nonempty_deferred_shutdown_reads_flag_only_at_bringup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, logs, metrics = _backend(monkeypatch)
    reads: list[bool] = []
    delivered: list[Event] = []

    def enabled_at_bringup() -> bool:
        reads.append(True)
        return True

    monkeypatch.setattr(backend, "_enabled", enabled_at_bringup)
    monkeypatch.setattr(backend, "_emit_log", delivered.append)
    event = _event()
    backend.defer_until_exit()
    backend.export_batch([event])
    assert reads == []
    assert backend.shutdown().status is DrainStatus.COMPLETED
    assert reads == [True]
    assert backend._worker is not None
    assert not backend._worker._thread.is_alive()
    assert delivered == [event]
    assert logs.closed.is_set() and metrics.closed.is_set()


def test_primary_error_survives_blocked_secondary_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, logs, metrics = _backend(monkeypatch)
    primary = BaseException("original writer defect")
    secondary = BaseException("secondary SDK teardown defect")
    cleaning, release = threading.Event(), threading.Event()
    seen: list[BaseException] = []

    def initialize(worker: Any) -> bool:
        worker.logs, worker.metrics = logs, metrics
        raise primary

    def close() -> None:
        cleaning.set()
        assert release.wait(5)
        raise secondary

    def report(_message: str, **details: Any) -> None:
        seen.append(details["exc"])

    monkeypatch.setattr(backend, "_initialize_worker", initialize)
    monkeypatch.setattr(logs, "shutdown", close)
    monkeypatch.setattr(worker_module, "report_no_pipeline", report)
    worker = backend._get_worker()
    try:
        assert cleaning.wait(5)
        assert seen[0] is primary
        with pytest.raises(BaseException) as raised:
            backend.shutdown(timeout=0.01)
        assert raised.value is primary
        assert worker._thread.is_alive()
        release.set()
        assert worker.finished.wait(5)
        assert secondary in seen
        assert metrics.closed.is_set()
        with pytest.raises(BaseException) as raised_again:
            backend.shutdown()
        assert raised_again.value is primary
    finally:
        release.set()
        worker._thread.join(timeout=5)
        assert not worker._thread.is_alive()


def test_known_sdk_shutdown_failure_is_isolated_but_not_claimed_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, logs, metrics = _backend(monkeypatch)
    names: list[str] = []

    def close() -> None:
        raise RuntimeError("named optional exporter cleanup failure")

    def report(sink: str, _error: Exception) -> None:
        names.append(sink)

    monkeypatch.setattr(logs, "shutdown", close)
    monkeypatch.setattr("base.telemetry.emitter.report_sink_failure", report)
    assert backend._ensure()
    assert backend.shutdown().status is DrainStatus.UNFINISHED
    assert backend._worker.finished.is_set()
    assert not backend._worker._thread.is_alive()
    assert "otlp logs shutdown" in names
    assert metrics.closed.is_set()
    assert backend.shutdown().status is DrainStatus.UNFINISHED


def test_deferral_completion_in_flight_cannot_close_providers_under_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, logs, _ = _backend(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls: list[tuple[str, Event]] = []
    original = backend._initialize_worker

    def initialize(worker: Any) -> bool:
        entered.set()
        assert release.wait(5)
        return original(worker)

    def record(event: Event) -> None:
        calls.append(("metric", event))

    def emit(event: Event) -> None:
        calls.append(("log", event))

    monkeypatch.setattr(backend, "_initialize_worker", initialize)
    monkeypatch.setattr(backend, "_record_metrics", record)
    monkeypatch.setattr(backend, "_emit_log", emit)
    backend.defer_until_exit()
    event = _event()
    backend.export_batch([event])
    caller = threading.Thread(target=backend.finalize)
    caller.start()
    try:
        assert entered.wait(5)
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        caller.join(timeout=5)
        assert not caller.is_alive()
        assert not logs.closed.is_set()
        release.set()
        assert backend._worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
        assert calls == [("metric", event), ("log", event)]
    finally:
        release.set()
        caller.join(timeout=5)
        backend._worker._thread.join(timeout=5)
        assert not caller.is_alive() and not backend._worker._thread.is_alive()


def test_admitted_metric_call_retains_provider_until_it_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, logs, metrics = _backend(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def record(_event: Event) -> None:
        entered.set()
        assert release.wait(5)
        assert not metrics.closed.is_set()

    monkeypatch.setattr(backend, "_record_metrics", record)
    caller = threading.Thread(target=backend.export_batch, args=([_event()],))
    caller.start()
    try:
        assert entered.wait(5)
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert not logs.closed.is_set() and not metrics.closed.is_set()
        release.set()
        caller.join(timeout=5)
        assert backend._worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
        assert logs.closed.is_set() and metrics.closed.is_set()
    finally:
        release.set()
        caller.join(timeout=5)
        backend._worker._thread.join(timeout=5)
        assert not caller.is_alive() and not backend._worker._thread.is_alive()


def test_real_sdk_partial_build_threads_are_owned_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.telemetry.otlp import telemetry_otlp_metrics

    backend = telemetry_otlp._OtlpBackend()

    def reachable(_endpoint: str) -> bool:
        return True

    def resource() -> None:
        raise RuntimeError("failure after real log processor and metric reader creation")

    monkeypatch.setattr(backend, "_endpoint_reachable", reachable)
    monkeypatch.setattr(telemetry_otlp_metrics, "_metrics_resource", resource)
    assert backend._ensure() is False
    worker = backend._worker
    assert worker is not None
    assert worker.finished.wait(5)
    assert worker.logs is not None and worker.metric_reader is not None
    assert worker.metrics is None
    processor = worker.logs._multi_log_record_processor._log_record_processors[0]
    assert not processor._batch_processor._worker_thread.is_alive()
    assert not worker.metric_reader._daemon_thread.is_alive()
    assert backend.shutdown().status is DrainStatus.COMPLETED


def test_failed_teardown_owner_is_not_overwritten_at_retry_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = telemetry_otlp._OtlpBackend()
    logs = _Provider()
    attempts: list[str] = []

    def reachable(_endpoint: str) -> bool:
        return True

    def build(endpoint: str, **receipts: Any) -> tuple[Any, Any]:
        attempts.append(endpoint)
        receipts["keep_logs"](logs)
        raise RuntimeError("named SDK init failure")

    def close() -> None:
        raise RuntimeError("SDK cannot close the half-built resource")

    monkeypatch.setattr(backend, "_endpoint_reachable", reachable)
    monkeypatch.setattr(telemetry_otlp, "_build_providers", build)
    monkeypatch.setattr(logs, "shutdown", close)
    assert backend._ensure() is False
    worker = backend._worker
    assert worker is not None
    assert worker.finished.wait(5)
    assert backend._init_failed_at is not None
    backend._init_failed_at -= telemetry_otlp.COLLECTOR_RETRY_INTERVAL_S
    assert backend._ensure() is False
    assert backend._worker is worker
    assert len(attempts) == 1
    assert backend.shutdown().status is DrainStatus.UNFINISHED


def test_unknown_failed_attempt_still_closes_deferred_admission_and_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _, _ = _backend(monkeypatch)
    error = BaseException("failed attempt before deferred stop")

    def initialize(_worker: Any) -> bool:
        raise error

    backend.defer_until_exit()
    backend.export_batch([_event()])
    monkeypatch.setattr(backend, "_initialize_worker", initialize)
    worker = backend._get_worker()
    assert worker.finished.wait(5)
    try:
        with pytest.raises(BaseException) as raised:
            backend.shutdown(timeout=0.01)
        assert raised.value is error
        assert backend._closed.is_set()
        assert backend._deferral._clock.finished.is_set()
    finally:
        assert backend._deferral.stop(timeout=5)
        worker._thread.join(timeout=5)
        assert not worker._thread.is_alive()
