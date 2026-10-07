"""Immutable agent-scoped SDK task creation results."""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ava.sdk_surface import agent_identity
from base.agents.tasks.model import validate_task_snapshot as validate_snapshot


def replay_creation(
    cur: psycopg.Cursor[Any], actor: int, key: str | None, request: dict[str, object]
) -> dict[str, Any] | None:
    """Serialize admission, then return the frozen original result if accepted."""
    if key is None:
        return None
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"task-create:{actor}:{key}",),
    )
    if agent_identity.require_agent_id() != actor:
        raise RuntimeError("task creation actor changed while waiting for operation admission")
    cur.execute(
        "SELECT request, result FROM task_creation_receipts "
        "WHERE actor_agent_id=%s AND operation_key=%s",
        (actor, key),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if row[0] != request:
        raise ValueError("idempotency key identifies a different task creation")
    return validate_snapshot(row[1])


def record_creation(
    cur: psycopg.Cursor[Any],
    actor: int,
    key: str | None,
    request: dict[str, object],
    snapshot: dict[str, object],
) -> None:
    """Commit the original Task snapshot alongside its business effects."""
    if key is None:
        return
    cur.execute(
        "INSERT INTO task_creation_receipts (actor_agent_id, operation_key, request, result) "
        "VALUES (%s, %s, %s, %s)",
        (actor, key, Jsonb(request), Jsonb(validate_snapshot(snapshot))),
    )
