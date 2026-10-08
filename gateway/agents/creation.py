"""Recovery of a committed keyed agent creation before mutable spawn checks."""

from __future__ import annotations

import asyncio
from typing import TypedDict

from fastapi import Header, HTTPException, Request
from psycopg_pool import ConnectionPool

from base.agents.labels import spawn_prompt_with_label
from base.db import Database
from base.events.live.bus import EventBus
from base.log import logger
from gateway.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    PrincipalScopeError,
    request_key,
)
from ops.agents.creation_identity import (
    CreationConflictError,
    CreationReceipt,
    creation_request_hash,
    find_creation,
)
from ops.rpc_schemas import ConfigNormalization, LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent


class CreationLaunchArguments(TypedDict, total=False):
    """Typed opt-in arguments; legacy handler calls pass no new keywords."""

    creation_key: str
    creation_identity: dict[str, object]
    immutable_birth: bool


class _CreationArguments(TypedDict, total=False):
    creation_key: str
    creation_request_hash: str
    immutable_creation_snapshot: bool


def scoped_creation_key(
    request: Request, key: str | None, *, operation_path: str = "/api/agents"
) -> str | None:
    """Preserve the canonical legacy namespace unless a guarded entry names its own."""
    if key is None:
        return None
    try:
        return request_key(request, key, method="POST", path=operation_path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def creation_receipt(
    pool: ConnectionPool, key: str, request_hash: str, *, immutable_snapshot: bool = False
) -> CreationReceipt | None:
    try:
        with pool.connection() as conn:
            return find_creation(conn, key, request_hash, immutable_snapshot=immutable_snapshot)
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
    creation_identity: dict[str, object] | None = None,
    immutable_birth: bool = False,
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

    snapshot = immutable_birth or creation_identity is not None
    arguments = _creation_arguments(
        body, creation_key, creation_identity, immutable_birth=immutable_birth
    )
    request_hash = arguments.get("creation_request_hash")
    if creation_key is not None and request_hash is not None:
        existing = await asyncio.to_thread(
            creation_receipt, pool, creation_key, request_hash, immutable_snapshot=snapshot
        )
        if existing is not None:
            return await recover_launch(pool, db, bus, existing, immutable_snapshot=snapshot)
    preset_name, tail_skills, model_receipt = await asyncio.to_thread(
        agent_router._spawn_preflight_blocking, db, target, body, pool
    )
    # fork_checkpoint resolution stays gateway-side: LangGraph checkpoints are
    # append-only and "latest" drifts under concurrent writes, so the gateway
    # resolves an explicit id before creating the row.
    fork_checkpoint = await asyncio.to_thread(agent_router.spawn_prechecks_blocking, body, pool)
    try:
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
            **arguments,
        )
    except CreationConflictError as exc:
        if not snapshot:
            raise
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if creation_key is not None and request_hash is not None:
        # A concurrent caller may have won after this caller's preflight. Its
        # committed placement/config/attempt are authoritative for recovery.
        committed = await asyncio.to_thread(
            creation_receipt, pool, creation_key, request_hash, immutable_snapshot=snapshot
        )
        if committed is None:
            raise RuntimeError("committed agent creation receipt is missing")
        if not committed.launch_pending:
            return await recover_launch(pool, db, bus, committed, immutable_snapshot=snapshot)
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
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    existing: CreationReceipt,
    *,
    immutable_snapshot: bool = False,
) -> SpawnedAgent:
    """Recover only an unadmitted birth; a completed incarnation is never revived."""
    from gateway.agents import router as agent_router

    if immutable_snapshot and not existing.launch_pending:
        return SpawnedAgent(id=existing.agent_id, accepted=True, execution_observed=False)
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


def guarded_draft_key(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> str:
    """Require a verified principal and the exact versioned operation namespace."""
    if idempotency_scope != PRINCIPAL_SCOPE:
        raise HTTPException(status_code=422, detail="guarded draft requires principal-v1 scope")
    key = scoped_creation_key(request, idempotency_key, operation_path=request.url.path)
    if key is None:
        raise RuntimeError("required draft key is missing")
    return key


def _creation_arguments(
    body: SpawnAgentRequest,
    key: str | None,
    identity: dict[str, object] | None,
    *,
    immutable_birth: bool = False,
) -> _CreationArguments:
    if key is None:
        if immutable_birth or identity is not None:
            raise ValueError("immutable birth requires a scoped key")
        return {}
    arguments: _CreationArguments = {
        "creation_key": key,
        "creation_request_hash": creation_request_hash(
            identity if identity is not None else body.model_dump(mode="json")
        ),
    }
    if immutable_birth or identity is not None:
        arguments["immutable_creation_snapshot"] = True
    return arguments
