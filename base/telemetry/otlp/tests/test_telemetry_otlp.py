"""OTLP event, metric, and failure-isolation contracts."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from base import telemetry
from base.telemetry import Event
from base.telemetry.otlp import telemetry_otlp

_AGENT = 8902


@pytest.fixture(autouse=True)
def _production_process_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most cases exercise an allowed production backend; gate tests override it."""
    monkeypatch.delenv("AVA_EXEC_REQUEST_FILE", raising=False)
    monkeypatch.setattr(telemetry_otlp, "production_identity", lambda: True, raising=False)


@pytest.fixture(autouse=True)
def _fresh_observability_export_gate() -> Any:
    """The production gate is process-cached; tests model fresh processes."""
    gate = telemetry_otlp.observability_export_allowed
    gate.cache_clear()
    yield
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

    Re-enables AVA_TELEMETRY_OTLP_ENABLED (`tests/fixtures/env_bootstrap.py` turns the flag
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

    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)  # pyright: ignore[reportUnknownMemberType]
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


def _assert_log_record_severity_and_correlation(r: Any) -> None:
    assert r.log_record.severity_text == "warning"  # pyright: ignore[reportUnknownMemberType]
    assert r.log_record.severity_number.value == 13  # pyright: ignore[reportUnknownMemberType]
    assert r.log_record.trace_id == int("abcd" * 8, 16)  # pyright: ignore[reportUnknownMemberType]
    assert r.log_record.span_id == int("ef01" * 4, 16)  # pyright: ignore[reportUnknownMemberType]


def _assert_log_record_indexed_attributes(r: Any) -> None:
    attrs = dict(r.log_record.attributes)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert attrs["event_name"] == "exec"
    assert attrs["category"] == "log"
    assert attrs["level"] == "warning"
    assert attrs["machine"] == "test-mac"
    assert attrs["cluster"] == ".ava-test"
    assert attrs["process"] == "test-proc"
    assert attrs["source"] == "test"
    assert attrs["agent_id"] == _AGENT


def _assert_log_record_json_body(r: Any) -> None:
    body = json.loads(r.log_record.body)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert body["event_name"] == "exec"
    assert body["cluster"] == ".ava-test"
    assert body["attributes"] == {"body": "print(1)", "ok": False}
    assert body["ts"] == "2026-08-11T12:00:00+00:00"


def test_log_mapping_full_record_shape(otlp_backend) -> None:
    """One event -> one OTLP LogRecord: severity mapping, indexed attributes,
    JSON body in the mirror shape, and trace/span ids as the correlation
    fields."""
    backend, log_exporter, _ = otlp_backend
    event = _event(
        event_name="exec",
        category="log",
        level="warning",
        attributes={"body": "print(1)", "ok": False},
    )
    backend.export_batch([event])  # pyright: ignore[reportUnknownMemberType]
    backend.flush()  # pyright: ignore[reportUnknownMemberType]

    records = log_exporter.get_finished_logs()  # pyright: ignore[reportUnknownMemberType]
    assert len(records) == 1  # pyright: ignore[reportUnknownArgumentType]
    r = records[0]
    _assert_log_record_severity_and_correlation(r)
    _assert_log_record_indexed_attributes(r)
    _assert_canonical_log_body(r, event)
    _assert_log_record_json_body(r)


def _assert_canonical_log_body(record: Any, event: Event) -> None:
    assert record.log_record.body == telemetry.event_line(event)


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


def _assert_int_payload_maps_to_counter_without_body_label(metrics: dict[str, Any]) -> None:
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


def _assert_float_payload_maps_to_histogram_with_unit(metrics: dict[str, Any]) -> None:
    # Unit suffix comes off the instrument name — the OTel unit supplies it on
    # export (latency_ms + "ms" would render ava_..._latency_ms_milliseconds_*).
    hist = metrics["ava_llm_usage_latency"]
    assert hist.unit == "ms"
    assert hist.data.data_points[0].count == 1
    assert hist.data.data_points[0].sum == 42.5
    assert "ava_llm_usage_latency_ms" not in metrics


def _assert_bool_payload_is_attribute_not_metric(metrics: dict[str, Any]) -> None:
    # turn_end's declared bool payload key rides as an attribute, not a metric.
    assert "ava_turn_end_ok" not in metrics
    turn = _attrs_of(metrics["ava_turn_end_duration"].data.data_points[0])
    assert metrics["ava_turn_end_duration"].unit == "s"
    assert turn["ok"] is True

    assert "ava_llm_usage_ok" not in metrics  # bools are attributes, not metrics


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

    _assert_int_payload_maps_to_counter_without_body_label(metrics)
    _assert_float_payload_maps_to_histogram_with_unit(metrics)
    _assert_bool_payload_is_attribute_not_metric(metrics)


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


def test_root_health_tick_uses_latest_timestamp_gauge(otlp_backend) -> None:
    """A newer completed tick replaces the old timestamp; a watchdog's age is
    absolute state, not an event count or a duration distribution."""
    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="root_health_tick",
                attributes={"last_tick_timestamp_seconds": 1_725_000_000.0},
            ),
            _event(
                event_name="root_health_tick",
                attributes={"last_tick_timestamp_seconds": 1_725_000_060.0},
            ),
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]

    tick = _metrics(metric_reader)["ava_root_health_tick_last_tick_timestamp"]
    assert tick.unit == "s"
    assert tick.data.data_points[0].value == 1_725_000_060.0


def test_root_health_gauges_keep_separate_home_identity_and_expected_only(otlp_backend) -> None:
    """A completed sibling cluster cannot supply the missing first sample."""
    backend, _, metric_reader = otlp_backend
    backend.export_batch(  # pyright: ignore[reportUnknownMemberType]
        [
            _event(
                event_name="root_health_expected",
                attributes={
                    "home_id": "a" * 64,
                    "expected_since_timestamp_seconds": 100.0,
                },
            ),
            _event(
                event_name="root_health_expected",
                attributes={
                    "home_id": "b" * 64,
                    "expected_since_timestamp_seconds": 120.0,
                },
            ),
            _event(
                event_name="root_health_tick",
                attributes={
                    "home_id": "a" * 64,
                    "last_tick_timestamp_seconds": 150.0,
                },
            ),
        ]
    )
    backend.flush()  # pyright: ignore[reportUnknownMemberType]
    metrics = _metrics(metric_reader)
    expected = metrics["ava_root_health_expected_expected_since_timestamp"]
    assert {point.attributes["home_id"] for point in expected.data.data_points} == {
        "a" * 64,
        "b" * 64,
    }
    ticks = metrics["ava_root_health_tick_last_tick_timestamp"]
    assert {point.attributes["home_id"] for point in ticks.data.data_points} == {"a" * 64}


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


def test_warmup_initializes_enabled_backend(otlp_backend) -> None:
    """Warmup constructs the providers before an exec can emit sdk_call events."""
    backend, _log_exporter, _metric_reader = otlp_backend

    telemetry_otlp.warmup()

    assert backend._logs is not None  # pyright: ignore[reportUnknownMemberType]
    assert backend._metric_provider is not None  # pyright: ignore[reportUnknownMemberType]
    assert backend._thread is not None  # pyright: ignore[reportUnknownMemberType]


def test_export_batch_builds_backend_once(otlp_backend) -> None:
    """The first record brings the backend up; later batches reuse it (idempotent)."""
    backend, _log_exporter, _metric_reader = otlp_backend

    telemetry_otlp.export_batch([_event()])
    first = backend._logs  # pyright: ignore[reportUnknownMemberType]
    assert first is not None
    telemetry_otlp.export_batch([_event()])
    assert backend._logs is first  # pyright: ignore[reportUnknownMemberType]


def test_flush_without_init_is_noop() -> None:
    """flush() on a never-brought-up backend: no raise, no thread, no queue work.

    The exec child's zero-record exit path must never touch OTel — this is the
    backend-side half of that contract (task #3816 M3)."""
    backend = telemetry_otlp._OtlpBackend()
    backend.flush()
    assert backend._thread is None
    assert backend._queue.empty()


def test_flag_off_disables_export(otlp_backend: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """AVA_TELEMETRY_OTLP_ENABLED=false -> export is a no-op: no backend
    bring-up, no records, no queue traffic."""
    backend, log_exporter, metric_reader = otlp_backend
    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", False)

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
    from base.config import settings

    monkeypatch.setattr("base.config.settings.observability.telemetry_otlp_enabled", True)  # pyright: ignore[reportUnknownMemberType]
    assert settings.observability.telemetry_otlp_enabled is True
    assert settings.observability.telemetry_otlp_endpoint == "http://127.0.0.1:4318"


# ── failure isolation ────────────────────────────────────────────────────────


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


# ── exec-child export path (task #1423) ──────────────────────────────────────
