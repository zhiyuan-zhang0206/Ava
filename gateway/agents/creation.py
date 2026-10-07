"""Recovery of a committed keyed agent creation before mutable spawn checks."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request
from psycopg_pool import ConnectionPool

from base.agents.labels import spawn_prompt_with_label
from base.db import Database
from base.events.live.bus import EventBus
from base.log import logger
from gateway.auth.request_principal import PrincipalScopeError, request_key
from ops.agents.creation_identity import (
    CreationConflictError,
    CreationReceipt,
    creation_request_hash,
    find_creation,
)
from ops.rpc_schemas import ConfigNormalization, LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent


def scoped_creation_key(request: Request, key: str | None) -> str | None:
    if key is None:
        return None
    try:
        return request_key(request, key, method="POST", path="/api/agents")
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def creation_receipt(pool: ConnectionPool, key: str, request_hash: str) -> CreationReceipt | None:
    try:
        with pool.connection() as conn:
            return find_creation(conn, key, request_hash)
    except CreationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def create_and_launch_agent(
    body: SpawnAgentRequest,
    target: str,
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    *,
    creation_key: str | None = None,
) -> SpawnedAgent:
    """Gateway-side spawn (Task #1236 follow-up): preflight -> create the agent
    ROW in-process -> forward a launch-only op to the target runner.

    The target runner's ops server runs as the least-privilege `ava_runner`
    role, which by design cannot INSERT agents / agents_meta — so the row is
    created HERE, in the gateway process, as the main data-plane identity. The
    forward op (`kind="spawn-launch-v2"`) validates and wakes the hosted runner.
    The first prompt is committed with the row before this forward.

    Every spawn in the system funnels through this helper (POST /api/agents,
    the guide / packages / schedules draft routers, the MCP tools server), so
    preflight, row creation, and launch stay uniform across entry points.
    """
    from gateway.agents import router as agent_router

    request_hash = creation_request_hash(body.model_dump(mode="json")) if creation_key else None
    if creation_key is not None and request_hash is not None:
        existing = await asyncio.to_thread(creation_receipt, pool, creation_key, request_hash)
        if existing is not None:
            return await recover_launch(pool, db, bus, existing)
    preset_name, tail_skills, model_receipt = await asyncio.to_thread(
        agent_router._spawn_preflight_blocking, db, target, body, pool
    )
    # fork_checkpoint resolution stays gateway-side: LangGraph checkpoints are
    # append-only and "latest" drifts under concurrent writes, so the gateway
    # resolves an explicit id before creating the row.
    fork_checkpoint = await asyncio.to_thread(agent_router.spawn_prechecks_blocking, body, pool)
    new_id, birth_config, prompt_inbound_id, launch_attempt_id = await asyncio.to_thread(
        agent_router.create_agent_row,
        db,
        bus,
        spawner=body.spawner,
        fork_from=body.fork_from,
        fork_checkpoint=fork_checkpoint,
        machine=target,
        config=body.config,
        label=body.label,
        preset_name=preset_name,
        fork_tail_skills=tail_skills,
        prompt=body.prompt,
        prompt_source=body.prompt_source,
        **(
            {"creation_key": creation_key, "creation_request_hash": request_hash}
            if creation_key is not None
            else {}
        ),
    )
    if creation_key is not None and request_hash is not None:
        # A concurrent caller may have won after this caller's preflight. Its
        # committed placement/config/attempt are authoritative for recovery.
        committed = await asyncio.to_thread(creation_receipt, pool, creation_key, request_hash)
        if committed is None:
            raise RuntimeError("committed agent creation receipt is missing")
        if not committed.launch_pending:
            return await agent_router._accepted_launch_receipt(
                pool, SpawnedAgent(id=committed.agent_id)
            )
        target, birth_config, launch_attempt_id = (
            committed.machine,
            committed.birth_config,
            committed.launch_attempt_id,
        )
        body.config = committed.config
    await announce_creation_prompt(bus, new_id, prompt_inbound_id, body)
    launch = LaunchAgentRequest(
        agent_id=new_id,
        launch_attempt_id=launch_attempt_id,
        config=body.config,
        birth_config=birth_config,
    )
    # The endpoint response is the launch op's verdict (the launched agent id —
    # equal to new_id in production; the runner answers for the launch). A
    # withdrawal settlement travels as the spawner's receipt (task #4306).
    spawned = await agent_router._dispatch_committed_launch(pool, db, bus, target, launch)
    if model_receipt is not None:
        spawned = spawned.model_copy(
            update={
                "config_normalized": ConfigNormalization(
                    requested=model_receipt[0], resolved=model_receipt[1]
                )
            }
        )
    return await agent_router._accepted_launch_receipt(pool, spawned)


async def recover_launch(
    pool: ConnectionPool, db: Database, bus: EventBus, existing: CreationReceipt
) -> SpawnedAgent:
    """Recover only an unadmitted birth; a completed incarnation is never revived."""
    from gateway.agents import router as agent_router

    if existing.launch_pending:
        spawned = await agent_router._dispatch_committed_launch(
            pool,
            db,
            bus,
            existing.machine,
            LaunchAgentRequest(
                agent_id=existing.agent_id,
                launch_attempt_id=existing.launch_attempt_id,
                config=existing.config,
                birth_config=existing.birth_config,
            ),
        )
    else:
        spawned = SpawnedAgent(id=existing.agent_id)
    return await agent_router._accepted_launch_receipt(pool, spawned)


async def announce_creation_prompt(
    bus: EventBus, agent_id: int, prompt_inbound_id: int | None, body: SpawnAgentRequest
) -> None:
    """A missed live hint cannot invalidate a committed birth and prompt."""
    from ops.lifecycle.events import publish_inbound_arrived

    if prompt_inbound_id is None or body.prompt_source is None or body.prompt is None:
        return
    try:
        await publish_inbound_arrived(
            bus,
            agent_id,
            prompt_inbound_id,
            "chat",
            body.prompt_source,
            spawn_prompt_with_label(body.prompt, body.label),
        )
    except Exception as exc:
        logger.warning("created agent {} inbound hint failed: {}", agent_id, type(exc).__name__)
