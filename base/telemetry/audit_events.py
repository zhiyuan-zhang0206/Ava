"""Audit-event entry point — the category=audit side of the unified event stream.

Every agent operation (spawn, send_message, terminate, compact, status_change,
skill_invoked, ...) is an audit event, and Postgres is its system of record: the
``audit_events`` table is append-only and permanent
(docs/decisions/data/database/2026-10-02-audit-events-in-postgres.md). Loki and the day-stamped
JSONL mirror receive the same event through the unified emitter
(`base.telemetry`) as a projection that sheds under overload, truncates long
lines and expires after 84 hours; losing the projection loses no record.

The primitives that record an event, one of which every audit emit site must use
(`scripts/content_lint/lint_audit_record.py` enforces it):

- :func:`record_audit` / :func:`record_audit_async` — INSERT inside the caller's
  own transaction, so the row commits or rolls back with the business write.
  The caller emits the returned event after its commit.
- :func:`record_audit_standalone` / :func:`record_audit_standalone_async` — for
  a producer that owns no transaction (the effect already happened, or another
  process owns it): one short write transaction, then the emit. A failed write
  raises; it is never degraded to a projection-only emit.
- :func:`record_audit_reported` / :func:`record_audit_reported_async` — the same
  for the few producers that must not fail their caller (an agent-facing tool
  call that already succeeded): a failed write is reported loudly instead.

An event is built with :func:`prepare_event_log`. The one audit event not recorded
here is the `.env` write audit, whose record is the per-home JSONL
(docs/decisions/runtime/config/2026-10-02-env-write-audit-stays-local.md).

Payload tiering
---------------
`payload` is a per-``event_type`` JSON bag whose inner shape is deliberately
left as ``dict`` for most events: they feed display surfaces only (the admin
event log, the FleetView graph — which branches on the ``event_type`` column,
never on payload contents) so a drifted key degrades a rendering, not a
decision. A payload is modeled (a ``BaseModel`` below) only when a downstream
program *branches on its contents*, where a silent key drift would change
behavior. Today that is exactly one event type: ``skill_invoked`` (see
``SkillInvokedPayload``), read by the self-evolution collector for skill
attribution. Add a model here when — and only when — a new consumer starts
branching on a payload's fields; do not model display-only events.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal

import psycopg
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict

from base import telemetry
from base.db import Database
from base.events.contract import EVENTS


class SkillInvokedPayload(BaseModel):
    """Payload of a ``skill_invoked`` audit event — the one payload a
    downstream program branches on.

    The self-evolution collector attributes skills to an agent run by reading
    ``skill`` for rows whose ``invocation_depth`` is ``"loaded"`` (active
    access) versus ``"prompt_injected"`` (baseline system-prompt exposure).
    Producer (``ava.skills``) and that consumer share this one shape instead of
    stringly-typed dict access. See the module docstring for the tiering rule.
    """

    model_config = ConfigDict(frozen=True)

    skill: str
    identifier: str
    invocation_depth: Literal["loaded", "prompt_injected"]


def prepare_event_log(
    *,
    event_type: str,
    agent_id: int | None,
    source: str,
    target_agent_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> telemetry.Event:
    """Construct one audit event (category=audit) without recording or emitting it.

    Pass it, after any session tagging, to one of the `record_audit*` primitives,
    which write it to `audit_events` and emit the same bytes to the unified
    stream after the commit. An event_type with no EventSpec in the registry
    raises ValueError (fail-fast, R2-C).

    Args:
        event_type: a registered category=audit event name.
        agent_id: the primary agent this event is about; None for a
            service-level event with no agent (e.g. an MCP tool call from an
            external client).
        source: who triggered the event — 'agent:<N>', 'user', 'system', 'self'.
        target_agent_id: for directed operations — the other agent (for
            send_message the recipient, for spawn the spawner, for fork the
            FORK SOURCE — the lineage parent, never the executor; the executor
            is `source`, see the fork-lineage ruling 2026-08-28). A dangling
            reference is recorded as-is; readers join against the live agents
            set and drop unknown ids.
        payload: optional JSON-serializable dict with operation-specific data.
            Left untyped on purpose; see the module docstring's payload tiering
            rule for when an event's payload gets a model instead.
    """
    from base.agents.messages.caller_identity import caller_payload

    return telemetry.prepare_event(
        "audit",
        event_type,
        level="info",
        agent_id=agent_id,
        source=source,
        target_agent_id=target_agent_id,
        attributes=caller_payload(source, payload),
    )


_INSERT_AUDIT_EVENT = (
    "INSERT INTO audit_events (event_uid, ts, trace_id, span_id, agent_id, machine, process, "
    "event_name, level, source, target_agent_id, attributes) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
    "ON CONFLICT (event_uid) DO NOTHING"
)


def audit_event_uid(event: telemetry.Event) -> int:
    """The event's stream surrogate id as the signed 64-bit `audit_events.event_uid`.

    The id is the one the JSONL mirror and Loki already carry for the same
    event (`telemetry.event_id`, an unsigned blake2b-64 over the serialized
    line and its nanosecond timestamp). Postgres has no unsigned bigint, so
    values from 2**63 up wrap to the negative half; the mapping is a bijection.
    """
    uid = telemetry.event_id(telemetry.event_line(event), int(event.ts.timestamp() * 1_000_000_000))
    return uid - (1 << 64) if uid >= 1 << 63 else uid


def _audit_row(event: telemetry.Event) -> tuple[Any, ...]:
    """The INSERT parameters of one audit event; ValueError unless it is registered audit."""
    spec = EVENTS.get(event.event_name)
    if (
        event.category != "audit"
        or spec is None
        or "audit" not in {spec.category, *spec.extra_categories}
    ):
        raise ValueError(
            f"record_audit() needs a registered category=audit event, got "
            f"category={event.category!r} event_name={event.event_name!r}"
        )
    return (
        audit_event_uid(event),
        event.ts,
        event.trace_id,
        event.span_id,
        event.agent_id,
        event.machine,
        event.process,
        event.event_name,
        event.level,
        event.source,
        event.target_agent_id,
        json.dumps(event.attributes, default=str, ensure_ascii=False),
    )


def record_audit(conn: psycopg.Connection, event: telemetry.Event) -> telemetry.Event:
    """INSERT one audit event into `audit_events` inside the caller's transaction.

    The row commits or rolls back with the business write that produced the
    fact, so no fact is recorded for an operation that did not happen and none
    is missing for one that did. Pass the exact event that will be emitted
    (after any session tagging), and emit it with `telemetry.emit_prepared`
    only after the transaction commits — Loki then carries the same bytes the
    row holds. Returns the event unchanged so the caller can hand it on.

    Idempotent on the event's stream id: a redelivered identical event inserts
    nothing. A contract violation (not an audit event, or a name the registry
    does not declare as audit) raises ValueError; a database failure
    propagates to the caller's transaction.
    """
    conn.execute(_INSERT_AUDIT_EVENT, _audit_row(event))
    return event


async def record_audit_async(
    conn: psycopg.AsyncConnection[Any], event: telemetry.Event
) -> telemetry.Event:
    """:func:`record_audit` for an async connection inside the caller's transaction."""
    await conn.execute(_INSERT_AUDIT_EVENT, _audit_row(event))
    return event


def record_audit_standalone_many(db: Database, events: Sequence[telemetry.Event]) -> None:
    """Record several audit events in one write transaction, then emit them all.

    All rows commit together or none does; nothing is emitted unless they commit.
    """
    with db.write_transaction() as conn:
        for event in events:
            record_audit(conn, event)
    for event in events:
        telemetry.emit_prepared(event)


def record_audit_standalone(db: Database, event: telemetry.Event) -> None:
    """Record one audit event in its own write transaction, then emit it.

    For a producer that owns no business transaction: the effect already
    happened, or another process owns it. The row is written and committed
    first; only then is the event handed to the emitter as the Loki
    projection. A failed write raises to the caller and nothing is emitted —
    there is no projection-only fallback. The window this does not cover is a
    process that dies after the effect and before this call.
    """
    with db.write_transaction() as conn:
        record_audit(conn, event)
    telemetry.emit_prepared(event)


async def record_audit_standalone_async(pool: AsyncConnectionPool, event: telemetry.Event) -> None:
    """:func:`record_audit_standalone` over an async pool."""
    from base.db.transaction import async_write_transaction

    async with async_write_transaction(pool) as conn:
        await record_audit_async(conn, event)
    telemetry.emit_prepared(event)


def _report_unrecorded(event: telemetry.Event, exc: Exception) -> None:
    """Make a failed audit write loud without failing the caller.

    An error log with the traceback plus an `audit_write_failed` anomaly event.
    The projection still goes out: Loki then carries the fact for 84 hours
    even though the record does not.
    """
    from base.log import logger

    logger.opt(exception=exc).error(
        "audit event {event_name} could not be recorded in audit_events",
        event_name=event.event_name,
    )
    telemetry.emit(
        "telemetry",
        "audit_write_failed",
        level="error",
        agent_id=event.agent_id,
        attributes={
            "event_name": event.event_name,
            "error_class": type(exc).__name__,
            "error": str(exc)[:500],
        },
    )
    telemetry.emit_prepared(event)


def record_audit_reported(db: Database, event: telemetry.Event) -> None:
    """:func:`record_audit_standalone` for a producer that must not fail its caller.

    Used where the operation already succeeded and raising would be wrong:
    an agent-facing tool call (the agent would retry and repeat the side
    effect) or a state transition whose remaining steps must still run. A
    failed write does not raise; it is reported by :func:`_report_unrecorded`.
    """
    try:
        record_audit_standalone(db, event)
    except Exception as exc:
        _report_unrecorded(event, exc)


async def record_audit_reported_async(pool: AsyncConnectionPool, event: telemetry.Event) -> None:
    """:func:`record_audit_reported` over an async pool."""
    try:
        await record_audit_standalone_async(pool, event)
    except Exception as exc:
        _report_unrecorded(event, exc)
