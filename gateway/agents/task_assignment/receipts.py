"""Retained compound acceptance tombstones; no FK or mutable-state replay."""

from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from base.agents.tasks.model import validate_task_snapshot
from gateway.agents.task_assignment.schemas import TaskAssignmentResult


def validate_result(value: dict[str, Any]) -> TaskAssignmentResult:
    """Validate complete native Task snapshots before dataclass defaults can apply."""
    value = {**value, "task": validate_task_snapshot(value["task"])}
    return TaskAssignmentResult.model_validate(value)


def existing_assignment(
    cur: psycopg.Cursor[Any], key: str, request: dict[str, Any]
) -> TaskAssignmentResult | None:
    """Wait for the logical operation before inspecting its immutable result."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
    cur.execute(
        "SELECT request,result FROM task_assignment_receipts WHERE operation_key=%s", (key,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    if row[0] != request:
        raise HTTPException(
            status_code=409, detail="idempotency key identifies a different task assignment"
        )
    return validate_result(row[1])


def save_assignment(
    cur: psycopg.Cursor[Any], key: str, request: dict[str, Any], result: TaskAssignmentResult
) -> None:
    """Commit the original pair with birth, task, audit and durable assignment."""
    cur.execute(
        "SELECT jsonb_build_object('machine',machine,'config_overlay',config_overlay,"
        "'birth_config',birth_config,'preset_name',preset_name,"
        "'launch_attempt_id',last_launch_attempt_id) FROM agents_meta WHERE id=%s",
        (result.agent_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("accepted birth metadata is missing")
    cur.execute(
        "INSERT INTO task_assignment_receipts(operation_key,request,result,birth_snapshot) "
        "VALUES (%s,%s,%s,%s)",
        (key, Jsonb(request), Jsonb(result.model_dump(mode="json")), Jsonb(row[0])),
    )
