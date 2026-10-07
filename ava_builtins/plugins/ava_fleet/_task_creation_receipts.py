"""Immutable agent-scoped SDK task creation results."""

import json
from dataclasses import asdict, fields
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from ava.sdk_surface import agent_identity
from base.agents.tasks.priority import validate_priority

from ._task_update import _validate_status


def validate_snapshot(snapshot: dict[str, object]) -> dict[str, Any]:
    """Validate the complete public dataclass without defaulting absent fields."""
    from .task_registry import Task

    if set(snapshot) != {field.name for field in fields(Task)}:
        raise ValueError("task creation snapshot has missing or unknown fields")
    task = TypeAdapter(Task).validate_json(json.dumps(snapshot), strict=True)
    _validate_status(task.status)
    validate_priority(task.priority)
    return asdict(task)


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
