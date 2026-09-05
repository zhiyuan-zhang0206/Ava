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
import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from shared import observability, telemetry, telemetry_otlp
from shared.telemetry import Event

_AGENT = 8902


@pytest.fixture(autouse=True)
def _production_process_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most cases exercise an allowed production backend; gate tests override it."""
    monkeypatch.delenv("AVA_EXEC_REQUEST_FILE", raising=False)
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: True, raising=False)


@pytest.fixture(autouse=True)
def _fresh_observability_export_gate() -> Any:
    """The production gate is process-cached; tests model fresh processes."""
    gate = getattr(telemetry_otlp, "_observability_export_allowed", None)
    if gate is not None:
        gate.cache_clear()
    yield
    if gate is not None:
        gate.cache_clear()


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
        cluster=".ava-test",
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
    resource_exporter: Any = telemetry_otlp._EventDimensionResourceExporter(log_exporter)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(resource_exporter))
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
    assert attrs["cluster"] == ".ava-test"
    assert attrs["process"] == "test-proc"
    assert attrs["source"] == "test"
    assert attrs["agent_id"] == _AGENT
    body = json.loads(r.log_record.body)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert body["event_name"] == "exec"
    assert body["cluster"] == ".ava-test"
    assert body["attributes"] == {"body": "print(1)", "ok": False}
    assert body["ts"] == "2026-08-11T12:00:00+00:00"


def test_flush_groups_each_event_name_under_its_matching_resource(otlp_backend) -> None:
    """A mixed emitter flush serializes only resource-homogeneous event groups.

    Loki indexes resource attributes, while the OTel SDK batches several log
    records into one request. Every group in that request must therefore carry
    the same ``event_name`` resource attribute as all of its records.
    """
    from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs

    backend, log_exporter, _ = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [_event(event_name=name) for name in ("llm_usage", "node_exit", "log")]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        records = log_exporter.get_finished_logs()  # pyright: ignore[reportUnknownMemberType]
        if len(records) == 3:  # pyright: ignore[reportUnknownArgumentType]
            break
        time.sleep(0.01)
    records = log_exporter.get_finished_logs()  # pyright: ignore[reportUnknownMemberType]
    request = encode_logs(records)  # pyright: ignore[reportUnknownArgumentType]
    assert len(request.resource_logs) == 3
    for resource_logs in request.resource_logs:
        resource_event_name = next(
            attribute.value.string_value
            for attribute in resource_logs.resource.attributes
            if attribute.key == "event_name"
        )
        resource_cluster = next(
            attribute.value.string_value
            for attribute in resource_logs.resource.attributes
            if attribute.key == "cluster"
        )
        assert resource_cluster == ".ava-test"
        for scope_logs in resource_logs.scope_logs:
            for record in scope_logs.log_records:
                assert (
                    next(
                        attribute.value.string_value
                        for attribute in record.attributes
                        if attribute.key == "event_name"
                    )
                    == resource_event_name
                )


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

    # Unit suffix comes off the instrument name — the OTel unit supplies it on
    # export (latency_ms + "ms" would render ava_..._latency_ms_milliseconds_*).
    hist = metrics["ava_llm_usage_latency"]
    assert hist.unit == "ms"
    assert hist.data.data_points[0].count == 1
    assert hist.data.data_points[0].sum == 42.5
    assert "ava_llm_usage_latency_ms" not in metrics

    # turn_end's declared bool payload key rides as an attribute, not a metric.
    assert "ava_turn_end_ok" not in metrics
    turn = _attrs_of(metrics["ava_turn_end_duration"].data.data_points[0])
    assert metrics["ava_turn_end_duration"].unit == "s"
    assert turn["ok"] is True

    assert "ava_llm_usage_ok" not in metrics  # bools are attributes, not metrics


def test_compaction_completed_maps_size_samples_and_frequency_counter(otlp_backend) -> None:
    """A completed compaction contributes distribution samples and one rate increment."""
    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="compaction_completed",
                attributes={
                    "compact_kind": "auto",
                    "compactions": 1,
                    "history_chars": 5000,
                    "summary_chars": 1500,
                    "summary_history_ratio": 0.3,
                },
            )
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]

    metrics = _metrics(metric_reader)
    count = metrics["ava_compaction_completed_compactions"].data.data_points[0]
    assert count.value == 1
    assert _attrs_of(count)["compact_kind"] == "auto"
    for field, value in (("history_chars", 5000), ("summary_chars", 1500)):
        sample = metrics[f"ava_compaction_completed_{field}"].data.data_points[0]
        assert sample.count == 1
        assert sample.sum == value
    ratio = metrics["ava_compaction_completed_summary_history_ratio"].data.data_points[0]
    assert ratio.count == 1
    assert ratio.sum == 0.3


def test_metrics_resource_carries_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_otlp, "cluster_label", lambda: ".ava-preview")

    resource = telemetry_otlp._metrics_resource()

    assert resource.attributes["cluster"] == ".ava-preview"


def test_resolution_status_uses_latest_value_gauges(otlp_backend) -> None:
    """Absolute unresolved/dismissed counts are gauges, not counters adding
    every pass (task #1935)."""

    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="resolution_status",
                attributes={
                    "unresolved_warnings": 9,
                    "unresolved_errors": 4,
                    "dismissed_warnings": 5,
                    "dismissed_errors": 2,
                    "window": "6h",
                },
            ),
            _event(
                event_name="resolution_status",
                attributes={
                    "unresolved_warnings": 2,
                    "unresolved_errors": 1,
                    "dismissed_warnings": 7,
                    "dismissed_errors": 3,
                    "window": "6h",
                },
            ),
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]

    metrics = _metrics(metric_reader)
    warning = metrics["ava_resolution_status_unresolved_warnings"]
    error = metrics["ava_resolution_status_unresolved_errors"]
    dismissed_warning = metrics["ava_resolution_status_dismissed_warnings"]
    dismissed_error = metrics["ava_resolution_status_dismissed_errors"]
    assert warning.data.data_points[0].value == 2.0
    assert error.data.data_points[0].value == 1.0
    assert dismissed_warning.data.data_points[0].value == 7.0
    assert dismissed_error.data.data_points[0].value == 3.0
    assert _attrs_of(warning.data.data_points[0])["window"] == "6h"


def test_watchdog_tick_uses_latest_timestamp_gauge(otlp_backend) -> None:
    """A newer completed tick replaces the old timestamp; a watchdog's age is
    absolute state, not an event count or a duration distribution."""
    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="watchdog_tick",
                attributes={"last_tick_timestamp_seconds": 1_725_000_000.0},
            ),
            _event(
                event_name="watchdog_tick",
                attributes={"last_tick_timestamp_seconds": 1_725_000_060.0},
            ),
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]

    tick = _metrics(metric_reader)["ava_watchdog_tick_last_tick_timestamp"]
    assert tick.unit == "s"
    assert tick.data.data_points[0].value == 1_725_000_060.0


def test_metric_disposition_cost_counter_price_excluded(otlp_backend) -> None:
    """The per-field disposition overrides: cost_usd (a float that is a SUM)
    records as a float Counter; the price_* rate snapshot mints no metric at
    all (it stays in the event body only); unpriced/calls int fields follow
    the default int -> Counter rule."""
    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="llm_usage",
                attributes={
                    "model": "claude-sonnet",
                    "calls": 1,
                    "in_total": 10,
                    "cost_usd": 0.125,
                    "price_miss": 3.0,
                    "price_hit": 0.3,
                    "price_out": 15.0,
                },
            ),
            _event(
                event_name="llm_usage",
                attributes={"model": "claude-sonnet", "calls": 1, "cost_usd": 0.375},
            ),
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]
    metrics = _metrics(metric_reader)

    cost = metrics["ava_llm_usage_cost_usd"]
    assert cost.data.is_monotonic  # a Counter, not a Histogram
    assert abs(cost.data.data_points[0].value - 0.5) < 1e-9
    assert metrics["ava_llm_usage_calls"].data.data_points[0].value == 2
    for excluded in ("price_miss", "price_hit", "price_out"):
        assert f"ava_llm_usage_{excluded}" not in metrics


def test_metric_views_shape_latency_histograms() -> None:
    """The production Views: LLM-scale explicit buckets (defaults clip at 10s)
    and no agent_id key on the latency histograms — percentiles are read per
    model/fleet, and dropping the key removes the per-agent histogram fan-out.
    Built with a real MeterProvider + the production views (the test seam
    providers skip views, so this pins the view definitions themselves)."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], views=telemetry_otlp._metric_views())
    meter = provider.get_meter("ava.telemetry")
    hist = meter.create_histogram("ava_llm_usage_latency", unit="ms")
    hist.record(45000.0, {"model": "m", "agent_id": 7, "machine": "x", "process": "p"})

    dp = _metrics(reader)["ava_llm_usage_latency"].data.data_points[0]
    assert list(dp.explicit_bounds) == list(telemetry_otlp._LLM_LATENCY_BUCKETS_MS)
    assert "agent_id" not in _attrs_of(dp)
    assert _attrs_of(dp)["model"] == "m"


def test_metric_views_shape_gateway_event_loop_lag_histogram() -> None:
    """Loop stalls beyond the OTel 10s default retain useful upper buckets."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], views=telemetry_otlp._metric_views())
    meter = provider.get_meter("ava.telemetry")
    hist = meter.create_histogram("ava_gateway_event_loop_lag", unit="ms")
    hist.record(45000.0, {"machine": "x", "process": "gateway"})

    dp = _metrics(reader)["ava_gateway_event_loop_lag"].data.data_points[0]
    assert list(dp.explicit_bounds) == list(telemetry_otlp._EVENT_LOOP_LAG_BUCKETS_MS)


def test_gateway_runtime_and_sse_absolute_metrics_use_gauges(otlp_backend) -> None:
    """Absolute resources and connection depth replace rather than accrue."""
    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="sse",
                attributes={"mode": "filtered", "active_connections": 2, "opened": 1},
            ),
            _event(
                event_name="sse",
                attributes={"mode": "filtered", "active_connections": 1, "closed": 1},
            ),
            _event(
                event_name="gateway_process",
                attributes={
                    "cpu_percent": 12.5,
                    "rss_bytes": 268_435_456,
                    "fd_count": 23,
                },
            ),
            _event(
                event_name="gateway_event_loop",
                attributes={"lag_ms": 250.0, "slow_ticks": 1},
            ),
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]

    metrics = _metrics(metric_reader)
    active = metrics["ava_sse_active_connections"].data.data_points[0]
    assert active.value == 1.0
    assert _attrs_of(active)["mode"] == "filtered"
    assert metrics["ava_sse_opened"].data.data_points[0].value == 1
    assert metrics["ava_sse_closed"].data.data_points[0].value == 1
    assert metrics["ava_gateway_process_cpu_percent"].data.data_points[0].value == 12.5
    assert metrics["ava_gateway_process_rss_bytes"].data.data_points[0].value == 268_435_456
    assert metrics["ava_gateway_process_fd_count"].data.data_points[0].value == 23
    lag = metrics["ava_gateway_event_loop_lag"].data.data_points[0]
    assert lag.count == 1
    assert lag.sum == 250.0
    assert metrics["ava_gateway_event_loop_slow_ticks"].data.data_points[0].value == 1


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
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if len(log_exporter.get_finished_logs()) == 2:  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            break
        time.sleep(0.01)
    assert len(log_exporter.get_finished_logs()) == 2  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert _metrics(metric_reader) == {}


# ── flag ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("machine_registered", "cluster", "expected"),
    [
        (True, ".ava", True),
        (True, "home", False),
        (True, ".unknown", False),
        (True, "ava_test_home_123", False),
        (False, ".ava", False),
    ],
)
def test_production_identity_requires_registered_machine_and_production_cluster(
    monkeypatch: pytest.MonkeyPatch,
    machine_registered: bool,
    cluster: str,
    expected: bool,
) -> None:
    from shared import machine

    if machine_registered:
        monkeypatch.setattr(machine, "machine_name", lambda: "registered-runner")
    else:

        def missing_machine_name() -> str:
            raise machine.MachineNameMissing("machine name unavailable")

        monkeypatch.setattr(machine, "machine_name", missing_machine_name)
    monkeypatch.setattr(observability, "cluster_label", lambda: cluster)

    assert observability.production_identity() is expected


@pytest.mark.parametrize("configured", [False, True])
def test_enabled_follows_setting_outside_exec_child(
    monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    monkeypatch.delenv("AVA_EXEC_REQUEST_FILE", raising=False)
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", configured)

    assert telemetry_otlp._OtlpBackend._enabled() is configured


@pytest.mark.parametrize(
    ("marker", "endpoint_override", "expected"),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
    ],
)
def test_gateway_export_gate_requires_lgtm_marker_or_explicit_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    marker: bool,
    endpoint_override: bool,
    expected: bool,
) -> None:
    home = tmp_path / ".ava"
    home.mkdir()
    if marker:
        (home / "lgtm-host").touch()
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.machine.machine_name", lambda: "macmini")
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)
    if endpoint_override:
        monkeypatch.setitem(
            os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318"
        )
    else:
        monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    telemetry_otlp._observability_export_allowed.cache_clear()

    assert telemetry_otlp._OtlpBackend._enabled() is expected

    # The isolation verdict is frozen once per process, even if the marker
    # changes later; a restart is the apply boundary.
    if not marker and not endpoint_override:
        (home / "lgtm-host").touch()
        assert telemetry_otlp._OtlpBackend._enabled() is False


def test_registered_production_identity_with_lgtm_marker_enables_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".ava"
    home.mkdir()
    (home / "lgtm-host").touch()
    monkeypatch.setattr("shared.machine.machine_name", lambda: "macmini")
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.setattr(telemetry_otlp, "production_identity", observability.production_identity)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)

    assert telemetry_otlp._OtlpBackend._enabled() is True


def test_explicit_endpoint_override_allows_non_production_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: False)
    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)

    assert telemetry_otlp._OtlpBackend._enabled() is True


def test_pure_runner_export_relay_is_not_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".ava"
    home.mkdir()
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr("shared.machine.machine_name", lambda: "macmini")
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)
    telemetry_otlp._observability_export_allowed.cache_clear()

    assert telemetry_otlp._OtlpBackend._enabled() is True


@pytest.mark.parametrize("exception_name", ["MachineRoleMissing", "MachineRoleInvalid"])
def test_unconfigured_machine_role_does_not_disable_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exception_name: str
) -> None:
    from shared import machine

    exception_type = getattr(machine, exception_name)

    def missing_role() -> frozenset[str]:
        raise exception_type("role unavailable")

    monkeypatch.setattr(machine, "machine_role", missing_role)
    monkeypatch.setattr(machine, "machine_name", lambda: "macmini")
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path / ".ava")
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)
    telemetry_otlp._observability_export_allowed.cache_clear()

    assert telemetry_otlp._OtlpBackend._enabled() is True


def test_warmup_initializes_enabled_backend(otlp_backend) -> None:
    """Warmup constructs the providers before an exec can emit sdk_call events."""
    backend, _log_exporter, _metric_reader = otlp_backend

    telemetry_otlp.warmup()

    assert backend._logs is not None  # pyright: ignore[reportUnknownMemberType]
    assert backend._metric_provider is not None  # pyright: ignore[reportUnknownMemberType]
    assert backend._thread is not None  # pyright: ignore[reportUnknownMemberType]


def test_flag_off_disables_export(otlp_backend: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_TELEMETRY_OTLP_ENABLED=false -> export is a no-op: no backend
    bring-up, no records, no queue traffic."""
    backend, log_exporter, metric_reader = otlp_backend
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", False)

    def fail_emit(*_args: object, **_kwargs: object) -> None:
        pytest.fail("flag-off must not emit backend status events")

    monkeypatch.setattr(telemetry, "emit", fail_emit)
    backend.export_batch([_event()])
    assert backend._logs is None  # never brought up
    assert backend._queue.empty()
    assert log_exporter.get_finished_logs() == ()
    assert metric_reader.get_metrics_data() is None


def test_flag_defaults_on_with_standard_endpoint(monkeypatch) -> None:
    """The 2026-08-11 stack decision: OTLP export is ON by default, endpoint
    = the standard OTLP/HTTP port on loopback."""
    from shared.config import settings

    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)  # pyright: ignore[reportUnknownMemberType]
    assert settings.observability.telemetry_otlp_enabled is True
    assert settings.observability.telemetry_otlp_endpoint == "http://127.0.0.1:4318"


# ── failure isolation ────────────────────────────────────────────────────────


def test_backend_init_failure_isolated_and_waits_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that cannot come up (bad endpoint / missing dep) never raises
    into the emitter and does not retry again inside the five-minute window."""
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)

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
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)

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
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)

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
    assert metrics["ava_llm_usage_latency"].data.data_points[0].sum == 1.5


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


# ── exec-child export path (task #1423) ──────────────────────────────────────


def test_handshake_env_no_longer_gates_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AVA_EXEC_REQUEST_FILE handshake was the old child-export gate; it
    must no longer disable OTLP — the export flag is the only authority."""
    monkeypatch.setenv("AVA_EXEC_REQUEST_FILE", "request.json")
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)
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
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)
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
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)
    try:
        telemetry_otlp.warmup()
        assert backend._logs is not None
    finally:
        backend.shutdown()
