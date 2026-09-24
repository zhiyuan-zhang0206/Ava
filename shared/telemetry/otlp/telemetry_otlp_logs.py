"""Log-side mapping machinery for the OTLP export backend.

`telemetry_otlp` was pushed to the 800-line ceiling by the child-deferral work
(task #3816 M4b), so the event -> OTLP LogRecord mapping moved here, mirroring
the earlier metric-side split (`telemetry_otlp_metrics`). One `Event` becomes
one OTLP LogRecord:

- body = the full event as JSON (the id-free mirror shape; the mirror row
  itself also carries the surrogate `id`, which the body deliberately does not);
- attributes = the indexed dimensions (event_name / category / level /
  machine / cluster / process / source / agent ids);
- trace_id / span_id fill the LogRecord fields so Loki rows correlate with
  Tempo spans (captured at enqueue time, so the deferral delay does not affect
  them); the timestamp is the event's own `ts` — a late export never moves it.

Runs on the OTLP worker thread (or `flush()`); the OTel imports stay inside
the function so flag-off processes never pay for the SDK.
"""

from __future__ import annotations

import time
from typing import Any

from shared.telemetry import Event, event_line

# Log-record severity mapping — OTel SeverityNumber values (plain ints; the
# enum is constructed at the record site, see _emit_log_record).
_SEVERITY_NUMBERS: dict[str, int] = {
    "debug": 5,
    "info": 9,
    "warning": 13,
    "error": 17,
    "critical": 21,
}


def _emit_log_record(logs: Any, event: Event) -> None:
    """Map one Event to an OTLP LogRecord and emit it through `logs`.

    `logs` is the backend's LoggerProvider (an OTel type; any-typed here
    because the SDK imports stay lazy)."""
    from opentelemetry._logs import LogRecord
    from opentelemetry._logs.severity import SeverityNumber
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        TraceFlags,
        set_span_in_context,
    )

    attributes: dict[str, Any] = {
        "event_name": event.event_name,
        "category": event.category,
        "level": event.level,
        "machine": event.machine,
        "cluster": event.cluster,
        "process": event.process,
        "source": event.source,
    }
    if event.agent_id is not None:
        attributes["agent_id"] = event.agent_id
    if event.target_agent_id is not None:
        attributes["target_agent_id"] = event.target_agent_id
    body = event_line(event)
    # Trace correlation via `context` (the non-deprecated LogRecord
    # constructor): a NonRecordingSpan carries the captured trace/span ids
    # into the OTLP LogRecord fields. No ids -> no context -> trace_id 0
    # (OTLP's "no trace").
    context: Any = None
    if event.trace_id and event.span_id:
        span_context = SpanContext(
            trace_id=int(event.trace_id, 16),
            span_id=int(event.span_id, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        context = set_span_in_context(NonRecordingSpan(span_context))
    record = LogRecord(
        timestamp=int(event.ts.timestamp() * 1_000_000_000),
        observed_timestamp=time.time_ns(),
        context=context,
        severity_text=event.level,
        # The event.name semantic field is not in the stub overloads yet;
        # event_name rides as an attribute (Loki label) either way.
        severity_number=SeverityNumber(_SEVERITY_NUMBERS[event.level]),
        body=body,
        attributes=attributes,
    )
    logs.get_logger("ava.telemetry").emit(record)
