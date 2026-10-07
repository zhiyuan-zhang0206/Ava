"""Atomic business acceptance for explicit guarded create-and-assign callers."""

import asyncio
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents import AgentLaunchFailed
from base.agents.tasks.creation import create_task_in_transaction, ensure_parent_exists
from base.api_contracts.idempotency import PRINCIPAL_SCOPE, SCOPE_HEADER
from base.cluster.machine import machine_name
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_spawned_sync, publish_task_created_sync
from base.events.live.bus import EventBus
from base.log import logger
from gateway.agents.creation import recover_launch, scoped_creation_key
from gateway.agents.task_assignment.receipts import existing_assignment, save_assignment
from gateway.agents.task_assignment.schemas import (
    TaskAssignmentAccepted,
    TaskAssignmentIn,
    TaskAssignmentResult,
)
from ops.agents.birth_transaction import insert_agent_birth
from ops.agents.creation_identity import CreationReceipt, creation_request_hash, find_creation
from ops.rpc_schemas import SpawnAgentRequest

router = APIRouter()
PATH = "/api/keyed/v1/task-assignments"


def lookup_assignment(
    pool: ConnectionPool, key: str, request: dict[str, Any]
) -> TaskAssignmentResult | None:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        return existing_assignment(cur, key, request)


def commit_assignment(
    pool: ConnectionPool,
    key: str,
    request: dict[str, Any],
    body: TaskAssignmentIn,
    spawn: SpawnAgentRequest,
    target: str,
    preset_name: str | None,
) -> tuple[TaskAssignmentResult, list[telemetry.Event]]:
    """The second lookup fences concurrent winners after out-of-TX preflight."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        result = existing_assignment(cur, key, request)
        if result is not None:
            return result, []
        try:
            ensure_parent_exists(cur, body.task.parent)
            birth = insert_agent_birth(
                cur,
                machine=target,
                spawner=f"agent:{body.actor_agent_id}",
                config=spawn.config,
                label=spawn.label,
                preset_name=preset_name,
                creation_key=key,
                creation_request_hash=creation_request_hash(request),
            )
            task, task_event, notes = create_task_in_transaction(
                cur,
                body.task.title,
                body.task.description,
                parent=body.task.parent,
                owner=birth.agent_id,
                actor=body.actor_agent_id,
                priority=body.task.priority.value,
                remind_interval_seconds=body.task.remind_interval_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result = TaskAssignmentResult(
            task=task,
            agent_id=birth.agent_id,
            launch_attempt_id=birth.launch_attempt_id,
        )
        save_assignment(cur, key, request, result)
        events = [task_event, *notes]
        if birth.birth_event is not None:
            events.insert(0, birth.birth_event)
        return result, events


def announce_assignment(
    events: list[telemetry.Event], bus: EventBus, actor: int, result: TaskAssignmentResult
) -> None:
    """A missed live/telemetry hint cannot invalidate accepted durable facts."""
    try:
        for event in events:
            telemetry.emit_prepared(event)
        publish_agent_spawned_sync(bus, result.agent_id)
        publish_task_created_sync(bus, actor, result.task.id)
    except Exception:
        logger.exception("accepted task assignment {} announcement failed", result.task.id)


async def observe_launch(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    key: str,
    request: dict[str, Any],
    result: TaskAssignmentResult,
) -> TaskAssignmentAccepted:
    """The retained pair is immutable; native current eligibility controls recovery."""

    def current() -> CreationReceipt | None:
        with pool.connection() as conn:
            return find_creation(conn, key, creation_request_hash(request))

    response = TaskAssignmentAccepted(**result.model_dump())
    try:
        existing = await asyncio.to_thread(current)
        if existing is None or existing.launch_attempt_id != result.launch_attempt_id:
            return response
        response.launch = await recover_launch(pool, db, bus, existing)
    except AgentLaunchFailed as exc:
        response.launch_failure = str(exc)
        response.retry_launch_path = exc.retry_launch_path
    except Exception as exc:
        logger.exception("accepted task assignment {} launch observation failed", result.task.id)
        response.launch_failure = (
            f"Launch observation failed ({type(exc).__name__}); acceptance retained"
        )
    return response


@router.post(PATH, status_code=201)
async def post_task_assignment(
    body: TaskAssignmentIn,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> TaskAssignmentAccepted:
    """Accept one original pair; this response never asserts runner readiness."""
    if idempotency_scope != PRINCIPAL_SCOPE:
        raise HTTPException(status_code=422, detail="task assignment requires principal-v1 scope")
    key = scoped_creation_key(request, idempotency_key, operation_path=PATH)
    if key is None:
        raise RuntimeError("guarded admission requires a principal-bound key")
    pool, db, bus = request.app.state.db_pool, request.app.state.db, request.app.state.bus
    raw = body.model_dump(mode="json")
    result = await asyncio.to_thread(lookup_assignment, pool, key, raw)
    if result is None:
        from gateway.agents import router as agent_router

        target = body.agent.machine if body.agent.machine is not None else machine_name()
        spawn = SpawnAgentRequest(
            spawner=f"agent:{body.actor_agent_id}",
            machine=target,
            label=body.agent.label,
            config=body.agent.config,
        )
        try:
            preset_name, _, _ = await asyncio.to_thread(
                agent_router._spawn_preflight_blocking, db, target, spawn, pool
            )
        except Exception:
            result = await asyncio.to_thread(lookup_assignment, pool, key, raw)
            if result is None:
                raise
        else:
            result, events = await asyncio.to_thread(
                commit_assignment, pool, key, raw, body, spawn, target, preset_name
            )
            if events:
                await asyncio.to_thread(
                    announce_assignment, events, bus, body.actor_agent_id, result
                )
    return await observe_launch(pool, db, bus, key, raw, result)
