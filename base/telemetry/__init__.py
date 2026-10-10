"""Unified telemetry: the event emitter and its OTLP export backend, trace
recording, metrics, Loki index labels and read budget, observability identity,
and operational alerts.

The door re-exports the emitter's API (`base/telemetry/emitter.py`), the single
entry point for every event in every process. The other members are imported
directly: `otlp/` (the OTLP export backend and its doc nodes), `tracing` and
`trace_mirror` (span recording and the sidecar mirror's disk guards), `metrics/`
(metric registries, aggregates, Grafana dashboard supply), `loki_index_labels`
(Loki selectors), `observability`,
`lgtm_local` and `station_endpoint` (observability identity and endpoints),
`alerts` and `alerts_copy` (alert ingest and its IM copy), and `audit_events`
(the audit side of the event stream).

This init intentionally pulls nothing beyond the emitter's own chain (events
contract + observability + paths) and the docstring-only otlp subpackage init.
Internal details (module-level constants, counters, helpers the implementation
reads as its own globals) live in `base/telemetry/emitter.py`; code that
patches those internals must patch them on that module — a patch on a package
attribute here is not seen by the emitter's own global reads.
"""

from base.telemetry import otlp as otlp
from base.telemetry.emitter import (
    _NO_EMITTER,
    _TELEMETRY_KINDS,
    Category,
    DrainPhase,
    DrainResult,
    DrainStatus,
    Event,
    _ambient_agent_id,
    _append_jsonl,
    _drain_on_exit,
    _EventPipeline,
    _prune_jsonl_mirror,
    _resolve_machine,
    _state,
    _write_batch,
    capture_trace_ids,
    category_for_kind,
    emit,
    emit_prepared,
    event_id,
    event_line,
    event_line_digest,
    event_payload,
    event_row,
    failure_isolated,
    flush,
    init_telemetry,
    prepare_event,
    process_name,
    report_no_pipeline,
    report_sink_failure,
    stop,
    sync,
)

__all__ = [
    "_NO_EMITTER",
    "_TELEMETRY_KINDS",
    "Category",
    "DrainPhase",
    "DrainResult",
    "DrainStatus",
    "Event",
    "_EventPipeline",
    "_ambient_agent_id",
    "_append_jsonl",
    "_drain_on_exit",
    "_prune_jsonl_mirror",
    "_resolve_machine",
    "_state",
    "_write_batch",
    "capture_trace_ids",
    "category_for_kind",
    "emit",
    "emit_prepared",
    "event_id",
    "event_line",
    "event_line_digest",
    "event_payload",
    "event_row",
    "failure_isolated",
    "flush",
    "init_telemetry",
    "otlp",
    "prepare_event",
    "process_name",
    "report_no_pipeline",
    "report_sink_failure",
    "stop",
    "sync",
]
