"""Immutable PATCH task results committed by the task mutation transaction."""

from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from gateway.schemas.tasks import TaskRow


def existing_task_receipt(
    cur: psycopg.Cursor[Any], path: str, key: str, body: dict[str, Any]
) -> TaskRow | None:
    """Serialize an operation before touching mutable task state."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (path + ":" + key,))
    cur.execute(
        "SELECT request, result FROM task_patch_receipts WHERE path=%s AND operation_key=%s",
        (path, key),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if row[0] != body:
        raise HTTPException(
            status_code=409, detail="idempotency key identifies a different task patch"
        )
    return TaskRow.model_validate(row[1])


def save_task_receipt(
    cur: psycopg.Cursor[Any], path: str, key: str, body: dict[str, Any], task: TaskRow
) -> None:
    """Keep the original response without pinning task or inbound retention."""
    cur.execute(
        "INSERT INTO task_patch_receipts (path, operation_key, request, result) "
        "VALUES (%s, %s, %s, %s)",
        (path, key, Jsonb(body), Jsonb(task.model_dump(mode="json"))),
    )
