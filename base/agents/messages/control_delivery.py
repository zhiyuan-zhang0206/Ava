"""Transactional cancel/compact acceptance, separate from native application."""

from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents import AgentNotFound, AgentStatus
from base.agents.contract import CancelResult
from base.agents.messages.inbound import InboundKind
from base.db import insert_inbound_message_in_transaction
from base.db.transaction import write_transaction
from base.telemetry.audit_events import prepare_event_log, record_audit

ControlKind = Literal[InboundKind.CANCEL, InboundKind.COMPACT_REQUEST]


class ControlConflictError(ValueError):
    """One scoped key already identifies another control request."""


@dataclass(frozen=True)
class ControlAcceptance:
    """Original acceptance plus a current hint, never proof of application."""

    agent_id: int
    kind: ControlKind
    status: CancelResult
    inbound_id: int | None
    pending: bool
    inserted: bool
    event: telemetry.Event | None = None


def _kind(value: str) -> ControlKind:
    kind = InboundKind(value)
    if kind not in (InboundKind.CANCEL, InboundKind.COMPACT_REQUEST):
        raise ValueError(f"unsupported control acceptance kind: {value!r}")
    return kind


def _pending(cur: psycopg.Cursor, agent: int, kind: ControlKind, inbound: int | None) -> bool:
    if inbound is None:
        return False
    cur.execute(
        "SELECT 1 FROM inbound_messages WHERE id=%s AND agent_id=%s AND kind=%s AND status='pending'",
        (inbound, agent, kind.value),
    )
    return cur.fetchone() is not None


def _existing(
    cur: psycopg.Cursor, path: str, key: str, agent: int, kind: ControlKind
) -> ControlAcceptance | None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (path + ":" + key,))
    cur.execute(
        "SELECT agent_id,kind,result,inbound_id FROM agent_control_receipts "
        "WHERE path=%s AND operation_key=%s",
        (path, key),
    )
    row = cur.fetchone()
    if row is None:
        return None
    stored_kind, status = _kind(row[1]), CancelResult(row[2])
    if (row[0], stored_kind) != (agent, kind):
        raise ControlConflictError("idempotency key identifies a different control request")
    return ControlAcceptance(
        agent, kind, status, row[3], _pending(cur, agent, kind, row[3]), inserted=False
    )


def insert_control_in_transaction(
    cur: psycopg.Cursor, agent: int, kind: ControlKind
) -> tuple[int, telemetry.Event | None]:
    """Insert the control inbound and its audit without committing or emitting."""
    kind = _kind(kind)
    inbound, event = insert_inbound_message_in_transaction(cur, agent, "", "user", kind=kind.value)
    if kind is InboundKind.COMPACT_REQUEST:
        event = record_audit(
            cur.connection,
            prepare_event_log(
                event_type="compact",
                agent_id=agent,
                source="user",
                payload={"compact_kind": "request"},
            ),
        )
    return inbound, event


def accept_control(
    pool: ConnectionPool,
    agent_id: int,
    kind: ControlKind,
    *,
    path: str,
    key: str | None = None,
) -> ControlAcceptance:
    """Commit one native inbound or cancel no-op with its optional identity.

    Replay precedes mutable target checks and preserves deleted inbound IDs.
    Only a still-pending original row permits post-commit recovery. Queue
    status is not an execution receipt; claim/application recovery is separate.
    """
    kind = _kind(kind)
    if agent_id <= 0:
        raise ValueError("agent id must be positive")
    if key is not None and not 1 <= len(key) <= 128:
        raise ValueError("operation key must contain 1 to 128 characters")
    with write_transaction(pool) as conn, conn.cursor() as cur:
        if key is not None:
            previous = _existing(cur, path, key, agent_id, kind)
            if previous is not None:
                return previous
        # Termination and resurrection write this same status owner under lock.
        cur.execute("SELECT status FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} not found")
        status = AgentStatus(row[0])
        inbound, event = None, None
        result = CancelResult.ALREADY_TERMINATED
        if kind is InboundKind.COMPACT_REQUEST or status is not AgentStatus.TERMINATED:
            inbound, event = insert_control_in_transaction(cur, agent_id, kind)
            result = CancelResult.ENQUEUED
        if key is not None:
            cur.execute(
                "INSERT INTO agent_control_receipts (path,operation_key,agent_id,kind,result,inbound_id) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (path, key, agent_id, kind.value, result.value, inbound),
            )
        return ControlAcceptance(
            agent_id, kind, result, inbound, inbound is not None, inserted=True, event=event
        )
