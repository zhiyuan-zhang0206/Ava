from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Body, HTTPException, Path, Request
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from base.agents.compaction.commands import accept as accept_guarded_compact
from base.agents.compaction.commands import observe as observe_guarded_compact
from base.agents.compaction.commands import status as guarded_compact_status
from base.agents.compaction.models import (
    CompactAcceptance,
    CompactConflictError,
    CompactStatus,
    CompactTarget,
)
from base.agents.impersonation import ImpersonationError
from base.agents.impersonation.maintenance import force_expire_impersonation
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.inbox.native_cancel import (
    NativeCancelConflictError,
    accept_native_cancel,
    observe_native_work,
)
from base.agents.inbox.native_restart import (
    NativeRestartConflictError,
    lookup_native_restart,
    native_restart_progress,
)
from base.agents.incarnation.native_restart_models import (
    NativeRestartAcceptance,
    NativeRestartAccepted,
    NativeRestartOperation,
    NativeRestartProgress,
    NativeRestartRefused,
    NativeRestartRequest,
)
from base.agents.incarnation.native_work_models import NativeCancelAcceptance, NativeWorkTarget
from base.db import Database
from base.db.transaction import write_transaction
from gateway.agents.forward import forward_to_home_machine
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    PrincipalScopeError,
    request_key,
)
from gateway.http.model_catalog import require_model_catalog
from ops.rpc_schemas import (
    OpenTaskRow,
    OpenTasksHint,
    RestartAgentRequest,
    RestartAgentResponse,
    ResurrectAgentRequest,
    ResurrectAgentResponse,
    TerminateAgentRequest,
    TerminateAgentResponse,
)

router = APIRouter()

_log = logging.getLogger(__name__)


_OPEN_TASKS_SHOWN = 5


class ForceExpireImpersonationRequest(BaseModel):
    session_id: int = Field(ge=0)


class ForceExpireImpersonationResponse(BaseModel):
    session_id: int
    status: Literal["expired", "not_open"]


@router.post(
    "/api/agents/{agent_id}/impersonation/force-expire",
    description="End the caller's observed takeover without terminating the native agent.",
)
async def post_force_expire_impersonation(
    agent_id: int, body: ForceExpireImpersonationRequest, request: Request
) -> ForceExpireImpersonationResponse:

    verified_by = getattr(request.state, "source_verified_by", None)
    principal = getattr(request.state, "auth_principal", None)
    subject = getattr(principal, "subject", None)
    if verified_by is None or subject is None:
        raise RuntimeError("force-expire reached its handler without a verified principal")
    actor = f"{verified_by}:{subject}"
    try:
        status = await asyncio.to_thread(
            force_expire_impersonation,
            request.app.state.db_pool,
            request.app.state.db,
            request.app.state.bus,
            agent_id,
            body.session_id,
            actor,
        )
    except ImpersonationError as exc:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found") from exc
    return ForceExpireImpersonationResponse(session_id=body.session_id, status=status)


@router.post(
    "/api/agents/{agent_id}/terminate",
    description="Terminate through the home runner's durable native control path.\n\nClaim returns to END and the host flushes before applying normal termination.\nAn optional message is committed as pending work before the command, retained\nfor later resurrection without causing another model turn. Force returns\nenqueued while the exact original host settles its task and execution\nresources; acceptance and metadata status are not proof of completed exit.\n\nBoth paths forward to the home runner. A missing agent returns 404; an\nalready-terminated identity is a no-op for graceful termination.\n\n`kill_all_shell_sessions` also kills the agent's shell sessions on its home\nmachine, silently; `shell_sessions` in the response says whether that\nalready happened (`now`, with the killed ids) or happens right before a\ngraceful termination applies (`at_exit`).\n\nOn success the response additionally carries `open_tasks` — the tasks the\nagent still owns (in_progress; at most five, most recently\nupdated first) as it goes down. The hint is advisory: a failed read leaves\nit null and never changes the termination result.",
)
async def post_agent_terminate(
    agent_id: int,
    request: Request,
    body: TerminateAgentRequest = Body(default_factory=TerminateAgentRequest),  # noqa: B008
) -> TerminateAgentResponse:

    return await terminate_agent_with_open_tasks(
        agent_id, body, request.app.state.db_pool, db=request.app.state.db
    )


async def terminate_agent_with_open_tasks(
    agent_id: int, body: TerminateAgentRequest, pool: ConnectionPool, *, db: Database
) -> TerminateAgentResponse:

    forwarded = await forward_to_home_machine(
        agent_id, f"/api/agents/{agent_id}/terminate", body.model_dump(), db=db, pool=pool
    )
    response = TerminateAgentResponse.model_validate(forwarded)
    if response.shell_sessions is not None and response.shell_sessions.killed:
        await _drop_killed_shell_ttls(pool, agent_id, response.shell_sessions.killed)
    try:
        hint = await asyncio.to_thread(_open_tasks_hint_blocking, pool, agent_id)
    except (psycopg.Error, PoolTimeout) as exc:
        _log.error(
            "[lifecycle] open-tasks hint for terminate of agent %s failed: %r",
            agent_id,
            exc,
        )
        return response
    return response.model_copy(update={"open_tasks": hint})


async def _drop_killed_shell_ttls(pool: ConnectionPool, agent_id: int, killed: list[int]) -> None:

    def _delete() -> None:
        with write_transaction(pool) as conn:
            conn.execute(
                "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id = ANY(%s)",
                (agent_id, killed),
            )

    try:
        await asyncio.to_thread(_delete)
    except (psycopg.Error, PoolTimeout) as exc:
        _log.error(
            "[lifecycle] TTL rows of agent %s's killed shell sessions %s were not dropped "
            "(the TTL reaper retires them): %r",
            agent_id,
            killed,
            exc,
        )


def _open_tasks_hint_blocking(pool: ConnectionPool, agent_id: int) -> OpenTasksHint | None:

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, title, status, updated_at FROM agent_tasks "
            "WHERE owner = %s AND status = 'in_progress' "
            "ORDER BY updated_at DESC, id DESC",
            (agent_id,),
        ).fetchall()
    if not rows:
        return None
    shown = rows[:_OPEN_TASKS_SHOWN]
    return OpenTasksHint(
        count=len(rows),
        tasks=[
            OpenTaskRow(id=row[0], title=row[1], status=row[2], updated_at=row[3].isoformat())
            for row in shown
        ],
        more=len(rows) - len(shown),
    )


@router.post(
    "/api/agents/{agent_id}/resurrect",
    description="Resurrect a terminated agent — UPDATE 'terminated' -> 'idling' +\nlaunch a fresh detached process attached to the same agent_id\n(LangGraph state preserved; the agent wakes and continues from its last\nturn).\n\nUsed by the frontend resurrect button. A bare resurrect is a pure\nlifecycle event with no message — the agent just gets the \"you have been\nresurrected\" marker; the \"resurrect with prompt\" path carries a `prompt`\nthat is INSERTed as a chat inbound in the **same transaction** as the\nlifecycle 'resurrect' inbound. The detached session may be created before\ncommit, but its child blocks on the agent row and cannot claim or process\neither inbound early. `resurrected_by` defaults to\n\"user\" and is validated against the envelope source whitelist.\n\nPeer agents have no dedicated resurrect API — they `send_message`, and\nauto-resurrect (`deliver_chat_inbound`) wakes a terminated target. This\nendpoint covers the case auto-resurrect cannot: a resurrect with no\nmessage to deliver.\n\nAlways runs on the agent's home machine via its ops server\n(`forward_to_home_machine`) — that host starts the new process; launching\nanywhere else would start it on the wrong host.\n\n404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).\n`already_alive`: agent is still alive\n    (running/idling); resurrect does not\n    apply — idempotent.",
)
async def post_agent_resurrect(
    agent_id: int,
    request: Request,
    body: ResurrectAgentRequest = Body(default_factory=ResurrectAgentRequest),  # noqa: B008
) -> ResurrectAgentResponse:

    forwarded = await forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/resurrect-explicit",
        body.model_dump(),
        db=request.app.state.db,
        pool=request.app.state.db_pool,
    )
    return ResurrectAgentResponse.model_validate(forwarded)


@router.post(
    "/api/agents/{agent_id}/restart",
    description="Enqueue native restart on the agent's home runner.\n\nThe current turn reaches claim, returns normally and flushes its checkpoint.\nThe host then applies the exact command and releases the incarnation for\nnew admission, retaining agent ID and context. Terminated agents require\nresurrection; restart returns already_terminated for them.",
)
async def post_agent_restart(
    agent_id: int,
    request: Request,
    body: RestartAgentRequest = Body(default_factory=RestartAgentRequest),  # noqa: B008
) -> RestartAgentResponse:

    if body.config_overlay:
        from base.packages.plugins.config_registration import validate_config_overlay
        from base.packages.plugins.plugin_errors import InvalidConfigOverlay

        catalog = require_model_catalog(request)
        try:
            validate_config_overlay(
                body.config_overlay,
                models=catalog.models,
                decision_models=catalog.decision_models,
            )
        except InvalidConfigOverlay as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    forwarded = await forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/restart",
        body.model_dump(),
        db=request.app.state.db,
        pool=request.app.state.db_pool,
    )
    return RestartAgentResponse.model_validate(forwarded)


@router.get(
    "/api/keyed/v1/agents/{agent_id}/native-work",
    description="Expose only eligible ACTIVE work backed by actual managed-owner evidence.",
)
async def native_work(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], request: Request
) -> NativeWorkTarget:

    target = await asyncio.to_thread(observe_native_work, request.app.state.db_pool, agent_id)
    if target is None:
        raise HTTPException(status_code=409, detail="no eligible active native work")
    return target


@router.post(
    "/api/keyed/v1/agents/{agent_id}/cancel-work",
    description="Accept one exact work intent; acceptance does not prove checkpoint execution.",
)
async def native_cancel(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], body: NativeWorkTarget, request: Request
) -> NativeCancelAcceptance:

    scoped = _native_control_key(request)
    try:
        return await asyncio.to_thread(
            accept_native_cancel, request.app.state.db_pool, scoped, agent_id, body
        )
    except NativeCancelConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _native_control_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if (
        key is None
        or request.headers.get(SCOPE_HEADER) != PRINCIPAL_SCOPE
        or not isinstance(getattr(request.state, "auth_principal", None), AuthPrincipal)
    ):
        raise HTTPException(
            status_code=400,
            detail="guarded native control requires a key and verified principal-v1 scope",
        )
    try:
        return request_key(request, key, method="POST", path=request.url.path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/api/keyed/v1/agents/{agent_id}/restart-work",
    description="Accept the original ACTIVE restart once through its dedicated executor.",
)
async def native_restart(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], body: NativeRestartRequest, request: Request
) -> NativeRestartAcceptance:

    key = _native_control_key(request)
    try:
        previous = await asyncio.to_thread(
            lookup_native_restart, request.app.state.db_pool, key, agent_id, body
        )
    except NativeRestartConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if previous is not None:
        return previous
    operation = NativeRestartOperation(operation_key=key, request=body)
    reply = await forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/restart-work",
        operation.model_dump(mode="json"),
        db=request.app.state.db,
        pool=request.app.state.db_pool,
        idempotency_key=key,
    )
    try:
        adapter: TypeAdapter[NativeRestartAccepted | NativeRestartRefused] = TypeAdapter(
            NativeRestartAccepted | NativeRestartRefused
        )
        result = adapter.validate_python(reply)
    except ValidationError as exc:
        raise HTTPException(
            status_code=502, detail="native restart executor protocol is unsupported"
        ) from exc
    if isinstance(result, NativeRestartRefused):
        raise HTTPException(
            status_code=409 if result.reason == "identity_conflict" else 422, detail=result.detail
        )
    stored = await asyncio.to_thread(
        lookup_native_restart, request.app.state.db_pool, key, agent_id, body
    )
    if stored is None or stored != result.acceptance:
        raise HTTPException(
            status_code=502, detail="native restart executor has no matching durable acceptance"
        )
    return stored


@router.get(
    "/api/keyed/v1/agents/{agent_id}/restart-commands/{command_id}",
    description="Observe retained original execution facts; no mutable current-owner inference.",
)
async def native_restart_status(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    command_id: Annotated[int, Path(gt=0, lt=2**63)],
    request: Request,
) -> NativeRestartProgress:

    progress = await asyncio.to_thread(
        native_restart_progress, request.app.state.db_pool, agent_id, command_id
    )
    if progress is None:
        raise HTTPException(status_code=404, detail="native restart command not found")
    return progress


@router.get(
    "/api/keyed/v1/agents/{agent_id}/compact-target",
    description="Observe only actual new-host quiescent, closed-resource source evidence.",
)
async def guarded_compact_target(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], request: Request
) -> CompactTarget:

    try:
        return await asyncio.to_thread(observe_guarded_compact, request.app.state.db_pool, agent_id)
    except CompactConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/keyed/v1/agents/{agent_id}/compact-history",
    status_code=202,
    description="Accept the original observed history; 202 is not summary/application success.",
)
async def guarded_compact_history(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], body: CompactTarget, request: Request
) -> CompactAcceptance:

    key = request.headers.get("Idempotency-Key")
    if (
        key is None
        or request.headers.get(SCOPE_HEADER) != PRINCIPAL_SCOPE
        or not isinstance(getattr(request.state, "auth_principal", None), AuthPrincipal)
    ):
        raise HTTPException(
            status_code=400, detail="guarded compact requires a key and verified principal-v1 scope"
        )
    try:
        scoped = request_key(request, key, method="POST", path=request.url.path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        accepted = await asyncio.to_thread(
            accept_guarded_compact, request.app.state.db_pool, scoped, agent_id, body
        )
    except CompactConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    current = await asyncio.to_thread(
        guarded_compact_status, request.app.state.db_pool, agent_id, accepted.command_id
    )
    if not current.continuation_released:
        await asyncio.to_thread(
            publish_inbound_wake,
            request.app.state.db,
            request.app.state.bus,
            agent_id,
            str(accepted.command_id),
        )
    return accepted


@router.get(
    "/api/keyed/v1/agents/{agent_id}/compact-commands/{command_id}",
    description="Inspect current execution evidence independently of immutable acceptance.",
)
async def guarded_compact_command(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], command_id: UUID, request: Request
) -> CompactStatus:

    try:
        return await asyncio.to_thread(
            guarded_compact_status, request.app.state.db_pool, agent_id, command_id
        )
    except CompactConflictError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
