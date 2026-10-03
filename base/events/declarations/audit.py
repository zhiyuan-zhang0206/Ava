"""Audit-category events (append-only operation facts) and the status_change mirror."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, audit_event, telemetry_audit_event

# Functional TypedDict: the producer writes the literal key ``"from"`` (a
# Python keyword, unusable in class-syntax TypedDict fields). The class-syntax
# ``from_`` spelling made the SQL-key derivation read ``attributes->>'from_'``
# — a key that never exists — while the producer wrote ``"from"`` (audit
# 2026-08-08 P2: the registry itself drifting). This form declares the real
# wire key so `_sql_keys`/`payload_keys` derive ``attributes->>'from'``.
StatusChange = TypedDict("StatusChange", {"from": str, "to": str})


class ComputerAction(TypedDict):
    """`computer_action` payload — services/computer/mcp_daemon.py.

    One row per executed-or-refused desktop action. The daily quota reads
    exactly this event name (count by agent_id since local midnight), so the
    payload stays a plain bag: the counter branches on the event_name column,
    never on these keys.
    """

    action: str  # snapshot | click | type | key | scroll | window_info | session_info
    app: str | None  # frontmost window owner at action time, when known
    outcome: str  # ok | denied | error
    error: str | None  # denial/error reason; None on success
    coords: str | None  # compact "x,y" / "x,y,w,h" / key code — for audit replay
    path: str | None  # snapshot PNG path (snapshot actions only) — trace replay
    task_id: int | None  # originating task, when the call carried one


class ComputerSessionStart(TypedDict):
    """`computer_session_start` payload — services/computer/task_sessions.py.

    The envelope opening for a task's desktop actions: the first call carrying
    a task_id emits this; the matching end follows when the task goes idle.
    """

    task_id: int
    first_tool: str  # the tool of the first action in the session
    first_action_at: str  # ISO-8601 UTC


class ComputerSessionEnd(TypedDict):
    """`computer_session_end` payload — services/computer/task_sessions.py.

    The envelope closing: emitted lazily when a task_id sees no action for the
    idle threshold (outcome=idle_timeout), on the next audited call.
    """

    task_id: int
    action_count: int  # actions counted in the session, including the first
    first_action_at: str  # ISO-8601 UTC
    last_action_at: str  # ISO-8601 UTC
    outcome: str  # idle_timeout (explicit end is a phase-3 candidate)


class Spawn(TypedDict):
    """`spawn` payload (audit)."""

    machine: str
    fork_from: int | None
    fork_checkpoint: str | None


class TaskUpdate(TypedDict):
    """`task_update` payload — task_registry.py; `status` only when changed."""

    status: str


EVENTS: dict[str, EventSpec] = {
    "spawn": audit_event(
        "spawn",
        "new agent born",
        payload=Spawn,
        site='ops/agents/spawn.py:349 event_type = "fork" if ... else "spawn"',
    ),
    "fork": audit_event("fork", "agent forked from another"),
    "send_message": audit_event(
        "send_message",
        "message sent to an agent",
        site="base/db/__init__.py:497 inbound kind->event_type mapping value",
    ),
    "terminate": audit_event(
        "terminate", "agent terminated", site="base/db/__init__.py:498 same as above"
    ),
    "restart": audit_event("restart", "agent restart initiated"),
    "cancel": audit_event(
        "cancel", "in-flight turn cancelled", site="base/db/__init__.py:500 same as above"
    ),
    "resurrect": audit_event("resurrect", "terminated agent woken"),
    "billing_resurrect": audit_event(
        "billing_resurrect",
        "billing batch recovery run: billing-class halt victims reinstated after "
        "the provider balance gate passed (task #3919)",
    ),
    "restart_completed": audit_event("restart_completed", "restart finished"),
    "hosted_legacy_adoption": audit_event(
        "hosted_legacy_adoption",
        "a hosted successor replaced a legacy NULL-resource owner before lease "
        "expiry on machine-local evidence (dead predecessor probe set)",
    ),
    "compact": audit_event("compact", "agent context compacted"),
    "circuit_breaker": audit_event(
        "circuit_breaker",
        "heartbeat circuit breaker opened — a permanent provider rejection stopped "
        "heartbeat re-fires (context_overflow reason arms the forced-compact self-rescue)",
    ),
    "report_activity": audit_event(
        "report_activity", "activity report", site="no current producer (DB 5,274 rows)"
    ),
    "status_change": telemetry_audit_event(
        "status_change",
        "agent status transition — both telemetry (loguru) and audit (audit_events) sides emit this name",
        payload=StatusChange,
    ),
    "exit": audit_event("exit", "agent process exited"),
    "label_change": audit_event("label_change", "agent label changed"),
    "skill_invoked": audit_event("skill_invoked", "skill invoked by an agent"),
    "task_create": audit_event("task_create", "task created"),
    "task_update": audit_event("task_update", "task updated", payload=TaskUpdate),
    "report_breached": audit_event(
        "report_breached", "guarantee report breached", site="no current producer (DB 14 rows)"
    ),
    "computer_action": audit_event(
        "computer_action",
        "computer-use desktop action (executed or refused)",
        payload=ComputerAction,
    ),
    "env_write": audit_event(
        "env_write",
        "official .env config write (actor and keys; old/new values for "
        "non-sensitive fields stay in the local record; sensitive values never recorded)",
    ),
    "env_unauthorized_write": audit_event(
        "env_unauthorized_write",
        "out-of-band .env modification detected (no official write recorded)",
        tier="anomaly",
    ),
    "computer_session_start": audit_event(
        "computer_session_start",
        "computer-use task session opened (first action with a task_id)",
        payload=ComputerSessionStart,
    ),
    "computer_session_end": audit_event(
        "computer_session_end",
        "computer-use task session closed (idle timeout)",
        payload=ComputerSessionEnd,
    ),
    "mcp_tool_call": audit_event(
        "mcp_tool_call",
        "MCP tool invoked through the gateway /mcp endpoint (client-scoped, args redacted)",
    ),
}
