"""Unified event emitter — the single entry point for every event in every process.

The emitter implementation moved to `shared/telemetry/emitter.py` (task #4555
R3, from the former single-module layout); this package door re-exports its
API so every existing `shared.telemetry.<name>` caller keeps resolving (the
conflict-package facade, design rule R3). The OTLP export backend lives in
`shared/telemetry/otlp/`; the doc nodes move with it.

This init intentionally pulls nothing beyond the emitter's own chain (events
contract + observability + paths) and the docstring-only otlp subpackage init.
Internal details (module-level constants, counters, helpers the implementation
reads as its own globals) live in `shared/telemetry/emitter.py`; code that
patches those internals must patch them on that module — a patch on a package
attribute here is not seen by the emitter's own global reads.
"""

from shared.telemetry import otlp as otlp
from shared.telemetry.emitter import (
    _AUDIT_BLOCK_S,
    _NO_EMITTER,
    _TELEMETRY_KINDS,
    Category,
    Event,
    _ambient_agent_id,
    _append_jsonl,
    _capture_trace_ids,
    _drain_on_exit,
    _EventPipeline,
    _is_rollup_source,
    _prune_jsonl_mirror,
    _report_no_pipeline,
    _resolve_machine,
    _state,
    _write_batch,
    category_for_kind,
    emit,
    emit_prepared,
    event_id,
    event_line,
    event_line_digest,
    event_payload,
    event_row,
    flush,
    init_telemetry,
    prepare_event,
    process_name,
    stop,
    sync,
)

__all__ = [
    "_AUDIT_BLOCK_S",
    "_NO_EMITTER",
    "_TELEMETRY_KINDS",
    "Category",
    "Event",
    "_EventPipeline",
    "_ambient_agent_id",
    "_append_jsonl",
    "_capture_trace_ids",
    "_drain_on_exit",
    "_is_rollup_source",
    "_prune_jsonl_mirror",
    "_report_no_pipeline",
    "_resolve_machine",
    "_state",
    "_write_batch",
    "category_for_kind",
    "emit",
    "emit_prepared",
    "event_id",
    "event_line",
    "event_line_digest",
    "event_payload",
    "event_row",
    "flush",
    "init_telemetry",
    "otlp",
    "prepare_event",
    "process_name",
    "stop",
    "sync",
]
