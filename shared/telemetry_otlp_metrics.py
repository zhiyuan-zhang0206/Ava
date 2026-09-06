"""Metric-side provider and naming machinery for OTLP telemetry export."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from shared.observability import cluster_label

# Metrics export cadence (PeriodicExportingMetricReader). 15 s keeps Grafana
# near-live without a per-batch network round-trip; the reader thread owns the
# timing, the emitter never waits on it.
_METRICS_INTERVAL_S = 15
_OTLP_HTTP_TIMEOUT_S = 2.0

# Histogram bucket boundaries for LLM-scale latencies (ms). The OTel defaults
# top out at 10000 — every call slower than 10s fell into +Inf and clipped
# p95/p50 at exactly 10s on the ops panels.
_LLM_LATENCY_BUCKETS_MS = (250, 500, 1000, 2000, 4000, 8000, 15000, 30000, 60000, 120000, 300000)

# A recovered event loop reports the whole stall on its next tick. Preserve
# minute-scale freezes instead of folding every delay above 10s into +Inf.
_EVENT_LOOP_LAG_BUCKETS_MS = (
    1,
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    5000,
    10000,
    30000,
    60000,
    120000,
    300000,
)


class _EventDimensionResourceExporter:
    """Give each event its own OTLP resource dimensions before serialization.

    A ``LoggerProvider`` has one static Resource, but Loki indexes resource
    attributes and the OTLP encoder groups a batch by that Resource. Rewriting
    a shared resource downstream therefore makes the last record's dimensions
    describe every record in the batch. This wrapper keeps one bounded SDK
    exporter worker while handing the encoder a resource that matches each
    individual record; the encoder then emits resource-homogeneous groups.
    """

    def __init__(self, exporter: Any) -> None:
        self._exporter = exporter

    def export(self, batch: Sequence[Any]) -> Any:
        from opentelemetry.sdk._logs import ReadableLogRecord
        from opentelemetry.sdk.resources import Resource

        resource_tagged: list[Any] = []
        for record in batch:
            dimensions = dict(record.resource.attributes)
            attributes = record.log_record.attributes
            dimensions["event_name"] = attributes["event_name"]
            if "agent_id" in attributes:
                dimensions["agent_id"] = attributes["agent_id"]
            if "cluster" in attributes:
                dimensions["cluster"] = attributes["cluster"]
            resource_tagged.append(
                ReadableLogRecord(
                    log_record=record.log_record,
                    resource=Resource(dimensions, schema_url=record.resource.schema_url),
                    instrumentation_scope=record.instrumentation_scope,
                    limits=record.limits,
                )
            )
        return self._exporter.export(resource_tagged)

    def shutdown(self) -> None:
        self._exporter.shutdown()


def _unit_for(field: str) -> str:
    """OTel unit for a payload field, from its name. Durations say what they
    are; everything else is a dimensionless count ("1")."""
    if field.endswith("_ms"):
        return "ms"
    if field.endswith("_seconds") or field in ("duration", "latency"):
        return "s"
    return "1"


def _strip_unit_suffix(field: str) -> str:
    """Instrument name for a payload field: the unit suffix comes off, because
    the OTel unit supplies it on export — `latency_ms` + unit "ms" would
    otherwise render as `ava_..._latency_ms_milliseconds_*` in Prometheus
    (the unit stated twice)."""
    for suffix in ("_ms", "_seconds"):
        if field.endswith(suffix):
            return field[: -len(suffix)]
    return field


def _metric_views() -> list[Any]:
    """Shape long LLM latencies and recovered gateway event-loop stalls.

    LLM percentiles are read per model/fleet, never per agent, so dropping
    `agent_id` removes the per-(agent, model) histogram fan-out. Event-loop
    lag retains minute-scale stalls that the OTel 10s default clips. Views
    match the INSTRUMENT name (unit suffix already stripped).
    """
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    llm_aggregation = ExplicitBucketHistogramAggregation(list(_LLM_LATENCY_BUCKETS_MS))
    views = [
        View(
            instrument_name=name,
            aggregation=llm_aggregation,
            attribute_keys={"machine", "process", "model"},
        )
        for name in ("ava_llm_usage_latency", "ava_llm_usage_decode")
    ]
    views.append(
        View(
            instrument_name="ava_gateway_event_loop_lag",
            aggregation=ExplicitBucketHistogramAggregation(list(_EVENT_LOOP_LAG_BUCKETS_MS)),
            attribute_keys={"machine", "process"},
        )
    )
    return views


def _metrics_resource() -> Any:
    """The metrics-side OTel Resource: `service.name=ava-<process>` (the
    bounded process dimension) + a per-process-instance `service.instance.id`.
    Without it every series lands as job="unknown_service" and two same-named
    processes exporting the same counter collide into ONE series with
    interleaved cumulative values (increase() reads garbage). Metrics only:
    the logs Resource is deliberately untouched — Loki default-promotes
    resource attributes to index labels, so a per-process-unique instance id
    there would mint a new stream per process start. Per-agent event indexing
    (Task #1327) keeps this resource as-is: the collector promotes the
    per-record agent_id/event_name resource attributes (set in
    _EventDimensionResourceExporter) to Loki index labels, and the read side
    era-slices its selector around the cutover (shared/loki_index_labels.py)
    — no service_name rewrite needed."""
    import uuid
    from importlib.metadata import version

    from opentelemetry.sdk.resources import Resource

    from shared.telemetry import process_name

    return Resource.create(
        {
            "service.namespace": "ava",
            "service.name": f"ava-{process_name()}",
            "service.instance.id": str(uuid.uuid4()),
            "service.version": version("ava"),
            "cluster": cluster_label(),
        }
    )


def _build_providers(endpoint: str) -> tuple[Any, Any]:
    """Build the production (LoggerProvider, MeterProvider) pair exporting
    OTLP/HTTP to ``endpoint`` (signal paths /v1/logs, /v1/metrics are appended
    by the exporters). Imports the OTel SDK lazily — the no-tracing path and
    flag-off processes never pay for it."""
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    logs = LoggerProvider()
    log_exporter: Any = _EventDimensionResourceExporter(
        OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", timeout=_OTLP_HTTP_TIMEOUT_S)
    )
    logs.add_log_record_processor(
        BatchLogRecordProcessor(log_exporter, export_timeout_millis=_OTLP_HTTP_TIMEOUT_S * 1000)
    )
    metrics = MeterProvider(
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", timeout=_OTLP_HTTP_TIMEOUT_S),
                export_interval_millis=_METRICS_INTERVAL_S * 1000,
                export_timeout_millis=_OTLP_HTTP_TIMEOUT_S * 1000,
            )
        ],
        resource=_metrics_resource(),
        views=_metric_views(),
    )
    return logs, metrics
