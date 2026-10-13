"""Unified telemetry: the event emitter and its OTLP export backend, trace
recording, metrics, Loki index labels and read budget, observability identity,
and operational alerts.

The door re-exports the emitter's API (`base/telemetry/emitter.py`), the single
entry point for every event in every process. The other members are imported
directly: `otlp/` (the OTLP export backend and its doc nodes), `tracing` and
`trace_mirror` (span recording and the sidecar mirror's disk guards), `metrics/`
(metric registries, aggregates), `loki_index_labels`
(Loki selectors), `observability`,
`lgtm_local` and `station_endpoint` (observability identity and endpoints),
`alerts` and `alerts_copy` (alert ingest and its IM copy), `audit_events`
(the audit side of the event stream), and `process_logging` (the loguru adapter into
the event pipeline and the gateway/CLI logging entry points).

This init intentionally pulls nothing beyond the emitter's own chain (events
contract + observability) and the docstring-only otlp subpackage init. Mirror
paths are resolved inside their write/prune operations after entry admission.
Internal details (module-level constants, counters, helpers the implementation
reads as its own globals) live in their defining modules (`base/telemetry/emitter.py`,
`base/telemetry/delivery/`, `base/telemetry/pipeline_build.py`); code that patches those
internals must patch them on that module — a patch on a package attribute here is not seen by
the defining module's own global reads. Modules inside this package import their definers, never
this door.
"""

from base.telemetry.delivery.mirror import append_jsonl, event_id, event_row, prune_jsonl_mirror
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import Category, DrainPhase, DrainResult, DrainStatus, Event
from base.telemetry.delivery.reporting import (
    NO_EMITTER,
    failure_isolated,
    report_no_pipeline,
    report_sink_failure,
)
from base.telemetry.delivery.serialization import event_line, event_line_digest, event_payload
from base.telemetry.delivery.state import (
    ambient_agent_id,
    process_name,
    process_state,
    resolve_machine,
)
from base.telemetry.emitter import (
    _TELEMETRY_KINDS,
    capture_trace_ids,
    category_for_kind,
    emit,
    emit_prepared,
    flush,
    init_telemetry,
    prepare_event,
    stop,
    sync,
)
from base.telemetry.pipeline_build import _drain_on_exit, _write_batch, build_pipeline

__all__ = [
    "NO_EMITTER",
    "_TELEMETRY_KINDS",
    "Category",
    "DrainPhase",
    "DrainResult",
    "DrainStatus",
    "Event",
    "EventPipeline",
    "_drain_on_exit",
    "_write_batch",
    "ambient_agent_id",
    "append_jsonl",
    "build_pipeline",
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
    "prepare_event",
    "process_name",
    "process_state",
    "prune_jsonl_mirror",
    "report_no_pipeline",
    "report_sink_failure",
    "resolve_machine",
    "stop",
    "sync",
]
