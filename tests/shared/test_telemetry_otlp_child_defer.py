"""Deferred OTLP export for exec children (task #3816 M4b).

Pins the deferral state machine (`shared.telemetry_otlp_defer` + the backend
seams): hold without bring-up, the flush guard, saturation and max-age
transitions, metrics replay exactly once, empty-hold completion, and the env
knobs' parity with the declared Settings fields.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from shared import telemetry_otlp
from shared.telemetry import Event

_AGENT = 8902


@pytest.fixture(autouse=True)
def _clean_deferral_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVA_TELEMETRY_OTLP_CHILD_DEFER", raising=False)
    monkeypatch.delenv("AVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S", raising=False)


@pytest.fixture(autouse=True)
def _production_process_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: True, raising=False)


@pytest.fixture(autouse=True)
def _fresh_observability_export_gate() -> Any:
    gate = getattr(telemetry_otlp, "_observability_export_allowed", None)
    if gate is not None:
        gate.cache_clear()
    yield
    if gate is not None:
        gate.cache_clear()


def _event(**overrides: Any) -> Event:
    fields: dict[str, Any] = {
        "ts": datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC),
        "trace_id": "abcd" * 8,
        "span_id": "ef01" * 4,
        "agent_id": _AGENT,
        "machine": "test-mac",
        "cluster": ".ava-test",
        "process": "test-proc",
        "category": "telemetry",
        "event_name": "sdk_call",
        "level": "info",
        "source": "test",
        "target_agent_id": None,
        "attributes": {},
    }
    fields.update(overrides)
    return Event(**fields)


def _make_backend(*, queue_maxsize: int = 8) -> tuple[Any, Any, Any]:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        InMemoryLogRecordExporter,
        SimpleLogRecordProcessor,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    resource_exporter: Any = telemetry_otlp._EventDimensionResourceExporter(log_exporter)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(resource_exporter))
    metric_reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=[metric_reader])
    backend = telemetry_otlp._OtlpBackend(
        providers=(logger_provider, metric_provider), queue_maxsize=queue_maxsize
    )
    return backend, log_exporter, metric_reader


def _metrics(metric_reader: Any) -> dict[str, Any]:
    data = metric_reader.get_metrics_data()
    out: dict[str, Any] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = m
    return out


def _spy_calls(monkeypatch: pytest.MonkeyPatch, backend: Any, name: str) -> list[Any]:
    """Wrap backend.<name> with a call counter (the deferral looks methods up
    per call, so instance-level patches stay live)."""
    calls: list[Any] = []
    original = getattr(backend, name)

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args or None)
        return original(*args, **kwargs)

    monkeypatch.setattr(backend, name, spy)
    return calls


@pytest.fixture
def otlp_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    """An `_OtlpBackend` with in-memory providers, installed as the module
    singleton (so module-level wrappers hit it); not yet armed."""
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)
    backend, log_exporter, metric_reader = _make_backend()
    monkeypatch.setattr(telemetry_otlp, "backend", backend)
    yield backend, log_exporter, metric_reader
    backend.shutdown()


@pytest.fixture
def deferred_backend(otlp_backend: Any) -> Any:
    backend, log_exporter, metric_reader = otlp_backend
    backend.defer_until_exit()
    assert backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    return backend, log_exporter, metric_reader


def test_hold_without_bringup(deferred_backend: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Held batches touch neither the settings chain nor the OTel stack."""
    backend, log_exporter, _ = deferred_backend
    ensure_calls = _spy_calls(monkeypatch, backend, "_ensure")
    enabled_calls = _spy_calls(monkeypatch, backend, "_enabled")

    for i in range(3):
        backend.export_batch([_event(attributes={"n": i})])

    assert backend._logs is None  # pyright: ignore[reportUnknownMemberType]
    assert ensure_calls == []
    assert enabled_calls == []
    assert backend._queue.qsize() == 3  # pyright: ignore[reportUnknownMemberType]
    assert not log_exporter.get_finished_logs()


def test_flush_while_deferred_keeps_hold(deferred_backend: Any) -> None:
    """The flush guard: a deferred flush must not drain (and thus drop) the hold."""
    backend, log_exporter, _ = deferred_backend
    backend.export_batch([_event(), _event()])

    backend.flush()

    assert backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    assert backend._queue.qsize() == 2  # pyright: ignore[reportUnknownMemberType]
    assert not log_exporter.get_finished_logs()


def test_finalize_completes_hold_and_replays_metrics_once(deferred_backend: Any) -> None:
    backend, log_exporter, metric_reader = deferred_backend
    backend.export_batch([_event(attributes={"tokens": 5})])
    backend.export_batch([_event(attributes={"tokens": 7})])

    backend.finalize()

    assert not backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    logs = log_exporter.get_finished_logs()
    assert len(logs) == 2
    metrics = _metrics(metric_reader)
    name = next(n for n in metrics if n.endswith("tokens"))
    assert sum(dp.value for dp in metrics[name].data.data_points) == 12
    # Flushed exactly-once after completion: a second finalize is a no-op.
    backend.finalize()
    assert len(log_exporter.get_finished_logs()) == 2


def test_saturation_goes_live_once(deferred_backend: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, log_exporter, _ = deferred_backend
    backend._queue.maxsize = 4  # shrink the bound for a cheap saturation
    ensure_calls = _spy_calls(monkeypatch, backend, "_ensure")

    backend.export_batch([_event() for _ in range(4)])
    assert backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    backend.export_batch([_event()])  # saturates -> bring-up -> live

    assert not backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    assert len(ensure_calls) == 1
    backend.flush()
    assert len(log_exporter.get_finished_logs()) >= 4


def test_max_age_clock_goes_live(deferred_backend: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, log_exporter, _ = deferred_backend
    monkeypatch.setenv("AVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S", "0.15")
    ensure_calls = _spy_calls(monkeypatch, backend, "_ensure")
    backend.export_batch([_event()])

    deadline = time.monotonic() + 5.0
    while backend._deferral.is_active() and time.monotonic() < deadline:  # pyright: ignore[reportUnknownMemberType]
        time.sleep(0.02)

    assert not backend._deferral.is_active(), "max-age clock must complete the deferral"  # pyright: ignore[reportUnknownMemberType]
    assert len(ensure_calls) == 1
    backend.flush()
    assert len(log_exporter.get_finished_logs()) == 1


def test_empty_hold_finalize_skips_bringup(
    deferred_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred child whose records never arrived completes for free."""
    backend, _, _ = deferred_backend
    ensure_calls = _spy_calls(monkeypatch, backend, "_ensure")

    backend.finalize()

    assert not backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    assert ensure_calls == []
    assert backend._thread is None  # pyright: ignore[reportUnknownMemberType]


def test_module_wrappers_arm_and_complete(otlp_backend: Any) -> None:
    backend, log_exporter, _ = otlp_backend

    telemetry_otlp.defer_until_exit()
    assert telemetry_otlp.deferred_state() is True
    telemetry_otlp.export_batch([_event()])
    assert backend._queue.qsize() == 1  # pyright: ignore[reportUnknownMemberType]
    assert not log_exporter.get_finished_logs()

    telemetry_otlp.finalize()
    assert telemetry_otlp.deferred_state() is False
    assert len(log_exporter.get_finished_logs()) == 1


def test_flag_off_disables_defer(otlp_backend: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_TELEMETRY_OTLP_CHILD_DEFER=0 restores the eager first-record bring-up."""
    backend, log_exporter, _ = otlp_backend
    monkeypatch.setenv("AVA_TELEMETRY_OTLP_CHILD_DEFER", "0")

    backend.defer_until_exit()

    assert not backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    backend.export_batch([_event()])
    backend.flush()
    assert len(log_exporter.get_finished_logs()) == 1


def test_env_defaults_locked_to_settings_fields() -> None:
    """The env-direct fallback mirrors the declared Settings defaults (one home
    per fact — `shared/telemetry_otlp_defer` cannot import the config module)."""
    from shared.config.observability import ObservabilitySettings
    from shared.telemetry_otlp_defer import CHILD_DEFER_MAX_AGE_DEFAULT_S

    fields = ObservabilitySettings.model_fields
    assert fields["telemetry_otlp_child_defer"].default is True
    assert fields["telemetry_otlp_child_defer"].alias == "AVA_TELEMETRY_OTLP_CHILD_DEFER"
    assert fields["telemetry_otlp_child_defer_max_age_s"].default == CHILD_DEFER_MAX_AGE_DEFAULT_S
    assert (
        fields["telemetry_otlp_child_defer_max_age_s"].alias
        == "AVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S"
    )
