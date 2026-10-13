"""Unified event emitter — the single entry point for every event in every process.

One event stream serves audit, telemetry, and log records under one schema and
one correlation key (`trace_id`). This module is its only writer.

Pipeline (Layer 1): a bounded queue and drain thread batch every event into
local JSONL mirrors, the `telemetry_events` table (telemetry and log events), best-effort
compact metrics, and OTLP logs/metrics. The record of an event is a Postgres table: audit
events in `audit_events` (written before the event is emitted), telemetry and log events in
`telemetry_events` (written here, from the drain thread, with the mirror as the fallback).
Loki and Prometheus hold the observation copy for Grafana and short windows.

Backpressure sheds records under overload, audit events included: their record
is `audit_events`, written before the event is emitted, so shedding the
projection loses nothing. Trace ids are captured at enqueue, and machine and
cluster dimensions are always populated.

Emit is best-effort and never raises: a broken sink must not crash the caller
(JSONL mirror + loguru file sinks are the durable backfill for everything
that reaches the drain thread; the mirror is replayed into `telemetry_events`).
Startup init (`init_telemetry`) is the one place that fails loud — a process
whose event pipeline cannot come up should not start silently blind.

Import discipline: this module imports `base.log`, `base.db`, and `base.paths` lazily
(inside functions) — `base/db/connections.py` imports `base/log/__init__.py` at module scope
for `logger`, so a top-level import of log or DB from here is a circular-import
failure for any process that reaches `base.db` first. Mirror path resolution stays
inside its write/prune operations so entry inputs can be typed before Settings admission.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from base.events.contract import EVENTS
from base.events.contract import category_for_kind as registry_category
from base.telemetry.delivery.mirror import event_id
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import (
    Category,
    DrainPhase,
    DrainResult,
    DrainStatus,
    Event,
    Level,
)
from base.telemetry.delivery.reporting import (
    failure_isolated,
    report_sink_failure,
)
from base.telemetry.delivery.serialization import event_line, event_line_digest, event_payload
from base.telemetry.delivery.state import ambient_agent_id, process_state, resolve_machine
from base.telemetry.observability import cluster_label

__all__ = [
    "Category",
    "DrainPhase",
    "DrainResult",
    "DrainStatus",
    "Event",
    "EventPipeline",
    "category_for_kind",
    "emit",
    "emit_prepared",
    "event_id",
    "event_line",
    "event_line_digest",
    "event_payload",
    "flush",
    "init_telemetry",
    "prepare_event",
    "stop",
]


# Telemetry event names — derived from the event contract registry
# (base/events/contract.py EVENTS, R2-C): the registry is the single source
# of truth; this set is its category projection (kept as a module-level name
# because tests/lint import it). Adding an event = one registry entry; this
# view updates automatically.
TELEMETRY_KINDS = frozenset(name for name, spec in EVENTS.items() if spec.category == "telemetry")


def category_for_kind(event_name: str) -> Category:
    """Map an event name to its declared category (registry EVENTS, R2-C);
    a name with no declaration falls back to the bare-log category."""
    return registry_category(event_name)


def capture_trace_ids() -> tuple[str | None, str | None]:
    """Read the current OTel span's trace_id/span_id, if any.

    Called at enqueue time — the drain thread runs outside the span context, so
    capturing there would lose every trace id. Imported lazily: processes
    without OTel tracing should not pay the import for an always-None path.
    """
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except ImportError:
        return None, None  # OTel is not installed in this process: no trace to capture
    except Exception as exc:  # trace capture must never break an emit — but is never silent
        report_sink_failure("trace-id capture", exc)
    return None, None


def init_telemetry(
    *,
    process: str = "unknown",
    agent_id: int | None = None,
    pipeline: EventPipeline,
    machine_reader: Callable[[], str] | None = None,
) -> None:
    """Bind process identity and bring up the event pipeline. Idempotent.

    Called from the loguru `init_*` entry points (the single boot seam every
    process shares): `init_gateway_process(name)` → process=name; the exec
    child's `add_event_sink` → process="agent-exec" plus its agent id. The
    entry supplies its owned writer; later calls reuse that same resource and
    refresh the identity binding. Startup never depends on the DB (the `telemetry_events` sink connects
    lazily on the drain thread)."""
    bound = process_state["pipeline"]
    if bound is not None and bound is not pipeline:
        if not bound.stopped:
            raise RuntimeError("this process already binds another event pipeline")
        # A completed owner may leave its process binding behind. Collect its
        # original failure before admitting the next concrete entry resource.
        bound.stop(timeout=0)
        process_state["pipeline"] = pipeline
    process_state["process"] = process
    process_state["agent_id"] = agent_id
    if machine_reader is not None or process_state["machine"] is None:
        process_state["machine"] = resolve_machine(machine_reader)
    if process_state["cluster"] is None:
        process_state["cluster"] = cluster_label()
    if process_state["pipeline"] is None:
        process_state["pipeline"] = pipeline


def _as_utc(ts: datetime | None) -> datetime:
    """Normalize one event timestamp onto the stream's single clock: UTC.

    `emit()`'s default is `datetime.now(UTC)`; an explicit `ts` — the loguru
    adapter passes loguru's local-zone record time, replay/migration passes
    stored rows — is converted to UTC here so the Event and every
    serialization of it (the JSONL mirror `ts` field, the OTLP body) carries
    one offset. A naive `ts` is UTC by contract (the one-time-source rule);
    treating it as local would mix clocks. The 2026-08-25 mirror audit: loguru
    rows wrote +08:00 into the mirror while direct emits wrote +00:00, which
    made any local-wall-clock filter of the mirror misread gateway telemetry
    as missing since the UTC-day rollover (task #1638)."""
    if ts is None:
        return datetime.now(UTC)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def emit(
    category: Category,
    event_name: str,
    *,
    level: Level = "info",
    agent_id: int | None = None,
    source: str = "system",
    target_agent_id: int | None = None,
    attributes: dict[str, Any] | None = None,
    ts: datetime | None = None,
    machine_reader: Callable[[], str] | None = None,
    capture: Callable[[Event], Event] | None = None,
    producer: Callable[[], EventPipeline] | None = None,
) -> None:
    """Enqueue one event into the unified stream. Never raises — except for a
    contract violation (R2-C): an `event_name` with no `EventSpec` in
    `base/events/declarations`, or a category that contradicts the
    declaration, raises `ValueError` (AGENTS.md "explode on unknown enums").
    An explicitly supplied producer capture callback also propagates its errors
    before observation delivery; no current participant is inferred.
    The loguru adapter wraps its call with `catch=True`, so a logging line
    that drifts off-contract stays visible (JSONL mirror) without crashing
    the producer.

    An event is a named log record (OTel LogRecord semantics: events = logs
    with names); `event_name` is that name — the `event.name` field, and the
    registry key (`EVENTS`).

    `trace_id`/`span_id` are captured from the active OTel span at this point
    (enqueue time — the drain thread runs outside the span context). `agent_id`
    falls back to the process-bound value (init_telemetry); explicit wins.
    `ts` defaults to the PROCESS clock at enqueue time (`datetime.now(UTC)`) —
    the one time source for the entire stream, and an explicit `ts` is
    normalized to UTC before enqueue (`_as_utc`), so every serialization of
    the Event (the JSONL mirror `ts` field, the OTLP body) carries one offset.
    Callers pass an explicit `ts` only for loguru-adapter records (loguru
    stamps local zone — normalized here) and replayed/migrated rows; a
    DB-derived timestamp would silently mix two clocks (W7 rewired the last
    DB-clock writers, heartbeat + delivery watchdog, onto this path)."""
    event = prepare_event(
        category,
        event_name,
        level=level,
        agent_id=agent_id,
        source=source,
        target_agent_id=target_agent_id,
        attributes=attributes,
        ts=ts,
        machine_reader=machine_reader,
    )
    emit_prepared(event, capture=capture, producer=producer)


def prepare_event(
    category: Category,
    event_name: str,
    *,
    level: Level = "info",
    agent_id: int | None = None,
    source: str = "system",
    target_agent_id: int | None = None,
    attributes: dict[str, Any] | None = None,
    ts: datetime | None = None,
    machine_reader: Callable[[], str] | None = None,
) -> Event:
    """Construct one Event without enqueueing it.

    ``machine_reader`` attributes this event to its explicit context without
    rebinding the process or another attachment. Transactional producers stage
    this exact immutable object before their
    database commit, then call :func:`emit_prepared` only after commit.  That
    preserves one canonical byte representation across their manifest receipt
    and the eventually exported telemetry line.
    """
    spec = EVENTS.get(event_name)
    if spec is None:
        raise ValueError(
            f"emit() got unregistered event_name={event_name!r} — declare an "
            "EventSpec in a base/events/declarations module first (base/events/"
            "registry.md is generated from it)"
        )
    if category != spec.category and category not in spec.extra_categories:
        raise ValueError(
            f"emit() category={category!r} contradicts the registry for "
            f"event_name={event_name!r} (declared {spec.category!r})"
        )
    trace_id, span_id = capture_trace_ids()
    return Event(
        ts=_as_utc(ts),
        trace_id=trace_id,
        span_id=span_id,
        agent_id=agent_id if agent_id is not None else ambient_agent_id(),
        machine=(
            resolve_machine(machine_reader)
            if machine_reader is not None
            else process_state["machine"] or resolve_machine()
        ),
        cluster=process_state["cluster"] or cluster_label(),
        process=process_state["process"],
        category=category,
        event_name=event_name,
        level=level,
        source=source,
        target_agent_id=target_agent_id,
        attributes=dict(attributes or {}),
    )


def emit_prepared(
    event: Event,
    *,
    capture: Callable[[Event], Event] | None = None,
    producer: Callable[[], EventPipeline] | None = None,
) -> None:
    """Capture through an explicit producer dependency, then enqueue that exact event."""
    if capture is not None:
        event = capture(event)
    with failure_isolated("emit"):
        pipeline = producer() if producer is not None else process_state["pipeline"]
        if pipeline is None:
            raise RuntimeError(
                "event emission requires an initialized process pipeline or producer"
            )
        pipeline.enqueue(event)


def flush() -> DrainResult:
    """Acknowledge queued and held ordinary records within the default deadline."""
    pipeline = process_state["pipeline"]
    if pipeline is not None:
        return pipeline.flush()
    return DrainResult(DrainStatus.COMPLETED, DrainPhase.DRAIN)


def sync(timeout: float = 5.0) -> DrainResult:
    """Drain and acknowledge the event pipeline within `timeout` seconds."""
    pipeline = process_state["pipeline"]
    if pipeline is not None:
        return pipeline.sync(timeout)
    return DrainResult(DrainStatus.COMPLETED, DrainPhase.DRAIN)


def stop(timeout: float = 5.0) -> DrainResult:
    """Stop the drain thread (process teardown / tests)."""
    pipeline = process_state["pipeline"]
    if pipeline is not None:
        return pipeline.stop(timeout)
    return DrainResult(DrainStatus.COMPLETED, DrainPhase.STOP)
