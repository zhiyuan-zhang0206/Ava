"""Agent-scoped task update receipts, owned by the SDK mutation transaction."""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ava.sdk_surface import agent_identity
from base.agents.context import AvaContext
from base.api_contracts.idempotency import validate_idempotency_key

from ._task_update import _UNSET


def update_identity(key: str | None, *, context: AvaContext) -> tuple[int | None, str | None]:
    """Keyless tooling remains compatible; keyed calls need an established actor."""
    if key is None:
        return agent_identity.agent_id(context), None
    validated = validate_idempotency_key(key)
    return agent_identity.require_agent_id(context), validated


def update_request(values: dict[str, object]) -> dict[str, object]:
    """Capture effective inputs before notes acquire a timestamp or parents resolve."""
    return {
        name: value
        for name, value in values.items()
        if value is not _UNSET and (value is not None or name == "parent_id")
    }


def replay_update(
    cur: psycopg.Cursor[Any],
    actor: int | None,
    task_id: int,
    key: str | None,
    request: dict[str, object],
    *,
    context: AvaContext,
) -> bool:
    """Serialize a logical update and recheck identity after any lock wait."""
    if key is None:
        return False
    if actor is None:
        raise RuntimeError("keyed task update requires an established agent identity")
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"task-update:{actor}:{task_id}:{key}",),
    )
    if agent_identity.require_agent_id(context) != actor:
        raise RuntimeError("task update actor changed while waiting for operation admission")
    cur.execute(
        "SELECT request FROM task_update_receipts "
        "WHERE actor_agent_id=%s AND task_id=%s AND operation_key=%s",
        (actor, task_id, key),
    )
    row = cur.fetchone()
    if row is None:
        return False
    if row[0] != request:
        raise ValueError("idempotency key identifies a different task update")
    return True


def record_update(
    cur: psycopg.Cursor[Any],
    actor: int | None,
    task_id: int,
    key: str | None,
    request: dict[str, object],
) -> None:
    """A retained row proves this void-returning mutation committed once."""
    if key is None:
        return
    if actor is None:
        raise RuntimeError("keyed task update requires an established agent identity")
    cur.execute(
        "INSERT INTO task_update_receipts (actor_agent_id, task_id, operation_key, request) "
        "VALUES (%s, %s, %s, %s)",
        (actor, task_id, key, Jsonb(request)),
    )
