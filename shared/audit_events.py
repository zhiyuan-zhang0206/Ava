"""Audit-event entry point — the category=audit side of the unified event stream.

Every agent operation (spawn, send_message, terminate, compact, status_change,
skill_invoked, ...) is recorded through these helpers, which enqueue into the
unified emitter (`shared.telemetry`) — audit events via the unified emitter,
the single write path for every event in every process. The emitter
batch-writes the `events` table (the canonical stream; audit rows carry
category=audit, 365d+ retention).

The former contract — "the INSERT rides in the caller's transaction, no
separate commit" — is deliberately gone: the design (event-system refactor,
Layer 1) makes the emitter's batch the single write path for every event, and
the emitter's JSONL mirror is the durable fallback for the window between
enqueue and batch commit. Callers no longer pass a cursor; nothing here
touches the DB on the calling thread.

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

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from shared import telemetry


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


def insert_event_log(
    *,
    event_type: str,
    agent_id: int | None,
    source: str,
    target_agent_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Record one audit event (category=audit) through the unified emitter.

    The emitter enqueues immediately (non-blocking; a broken sink never
    raises into the caller) and the drain thread batch-writes the `events`
    row. The one exception is a contract violation: an event_type with no
    EventSpec in the registry raises ValueError (fail-fast, R2-C), so
    callers must keep event_type inside the registry. The dangling-target guard: a
    target_agent_id that no longer exists is cleared rather than failing the
    write (tests and just-terminated agents produce such references; the event
    is still valid, the source string carries the origin).

    Args:
        event_type: one of 'spawn', 'send_message', 'terminate', 'resurrect',
            'restart', 'restart_completed', 'fork', 'cancel', 'compact',
            'report_activity', 'report_breached', 'status_change', 'exit',
            'label_change', 'skill_invoked', 'task_create', 'task_update',
            'computer_action', 'mcp_tool_call'.
        agent_id: the primary agent this event is about; None for a
            service-level event with no agent (e.g. an MCP tool call from an
            external client — the events table takes NULL agent_id).
        source: who triggered the event — 'agent:<N>', 'user',
            'system', 'self', etc.
        target_agent_id: for directed operations (send_message, spawn, fork)
            — the other agent.
        payload: optional JSON-serializable dict with operation-specific data.
            Left untyped here on purpose; see the module docstring's payload
            tiering rule for when an event's payload gets a model instead.
    """
    telemetry.emit(
        "audit",
        event_type,
        level="info",
        agent_id=agent_id,
        source=source,
        target_agent_id=target_agent_id,
        attributes=payload,
    )


def insert_event_log_many(
    *,
    event_type: str,
    agent_id: int,
    source: str,
    payloads: list[dict[str, Any]],
) -> None:
    """Record one audit event per entry in `payloads`.

    All entries enqueue in one call (the enqueue is a bounded-queue put, so a
    dozen or a hundred payloads cost the caller the same). No
    `target_agent_id` on this path, matching the legacy batch writer.
    `ava.skills` writes per-skill invocation rows through this (one row per
    skill per agent run, dedup'd on the producer side).
    """
    for payload in payloads:
        telemetry.emit(
            "audit",
            event_type,
            level="info",
            agent_id=agent_id,
            source=source,
            attributes=payload,
        )


async def insert_event_log_async(
    *,
    event_type: str,
    agent_id: int,
    source: str,
    target_agent_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Async-code-path alias of `insert_event_log`.

    Enqueueing is synchronous and non-blocking, so there is nothing async
    left; the name is kept so agent-side (async) call sites read the same as
    before. Same contract: never raises, drain thread owns the write.
    """
    insert_event_log(
        event_type=event_type,
        agent_id=agent_id,
        source=source,
        target_agent_id=target_agent_id,
        payload=payload,
    )
