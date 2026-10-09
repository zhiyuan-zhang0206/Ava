"""Agent lifecycle endpoints — /api/agents/* lifecycle surface.

Compact / cancel / terminate / resurrect / restart.

Lifecycle operations that mutate physical host state (session / OS
process) always run on the agent's home machine via its ops server
(`forward_to_home_machine`, `forward.py`) — no local shortcut
even when the target is the co-located box. Operations that are durable
DB-row + event-publish work (cancel) run on whichever
gateway receives them. CRUD + spawn live in `router.py`; message +
state reads in `state.py`.
"""

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
from base.agents.incarnation.native_restart_models import (
    NativeRestartAcceptance,
    NativeRestartAccepted,
    NativeRestartOperation,
    NativeRestartProgress,
    NativeRestartRefused,
    NativeRestartRequest,
)
from base.agents.incarnation.native_work_models import NativeCancelAcceptance, NativeWorkTarget
from base.agents.messages.native_cancel import (
    NativeCancelConflictError,
    accept_native_cancel,
    observe_native_work,
)
from base.agents.messages.native_restart import (
    NativeRestartConflictError,
    lookup_native_restart,
    native_restart_progress,
)
from base.db import publish_inbound_wake
from base.db.transaction import write_transaction
from gateway.agents.forward import forward_to_home_machine
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    PrincipalScopeError,
    request_key,
)
from ops.rpc_schemas import (
    BillingResurrectRequest,
    BillingResurrectResponse,
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

# The open-task hint shows at most this many rows; `more` counts the rest.
_OPEN_TASKS_SHOWN = 5


class ForceExpireImpersonationRequest(BaseModel):
    session_id: int = Field(ge=0)


class ForceExpireImpersonationResponse(BaseModel):
    session_id: int
    status: Literal["expired", "not_open"]


@router.post("/api/agents/{agent_id}/impersonation/force-expire")
async def post_force_expire_impersonation(
    agent_id: int, body: ForceExpireImpersonationRequest, request: Request
) -> ForceExpireImpersonationResponse:
    """End the caller's observed takeover without terminating the native agent."""
    verified_by = getattr(request.state, "source_verified_by", None)
    principal = getattr(request.state, "auth_principal", None)
    subject = getattr(principal, "subject", None)
    actor = (
        f"{verified_by}:{subject}"
        if isinstance(verified_by, str) and isinstance(subject, str)
        else "local_operator"
    )
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


@router.post("/api/agents/{agent_id}/terminate")
async def post_agent_terminate(
    agent_id: int,
    request: Request,
    body: TerminateAgentRequest = Body(default_factory=TerminateAgentRequest),  # noqa: B008
) -> TerminateAgentResponse:
    """Terminate through the home runner's durable native control path.

    Claim returns to END and the host flushes before applying normal termination.
    An optional message is committed as pending work before the command, retained
    for later resurrection without causing another model turn. Force returns
    enqueued while the exact original host settles its task and execution
    resources; acceptance and metadata status are not proof of completed exit.

    Both paths forward to the home runner. A missing agent returns 404; an
    already-terminated identity is a no-op for graceful termination.

    `kill_all_shell_sessions` also kills the agent's shell sessions on its home
    machine, silently; `shell_sessions` in the response says whether that
    already happened (`now`, with the killed ids) or happens right before a
    graceful termination applies (`at_exit`).

    On success the response additionally carries `open_tasks` — the tasks the
    agent still owns (in_progress; at most five, most recently
    updated first) as it goes down. The hint is advisory: a failed read leaves
    it null and never changes the termination result."""
    return await terminate_agent_with_open_tasks(agent_id, body, request.app.state.db_pool)


async def terminate_agent_with_open_tasks(
    agent_id: int, body: TerminateAgentRequest, pool: ConnectionPool
) -> TerminateAgentResponse:
    """Forward the terminate op to the home runner, drop the TTL rows of any
    shell sessions it killed, then attach the agent's open-task hint — read
    once from `agent_tasks` after acceptance.

    Advisory by design: a failed hint read is logged and leaves `open_tasks`
    null, so it can never block or alter the termination itself."""
    forwarded = await forward_to_home_machine(
        agent_id, f"/api/agents/{agent_id}/terminate", body.model_dump()
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
    """Delete the `agent_shell_ttls` rows of sessions a terminate just killed.

    The TTL reaper's own row removal, done up front because the runner that
    killed them holds no DELETE on the table. Sessions a graceful terminate
    kills at exit keep their rows until the reaper retires them at their
    deadline, silently (an absent session never notifies). A failed delete is
    logged and left to that same reaper; it never alters the termination.
    """

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
    """One `agent_tasks` read — the agent's open tasks, most recently updated
    first; None when it owns none."""
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


@router.post("/api/agents/{agent_id}/resurrect")
async def post_agent_resurrect(
    agent_id: int,
    body: ResurrectAgentRequest = Body(default_factory=ResurrectAgentRequest),  # noqa: B008
) -> ResurrectAgentResponse:
    """Resurrect a terminated agent — UPDATE 'terminated' -> 'idling' +
    launch a fresh detached process attached to the same agent_id
    (LangGraph state preserved; the agent wakes and continues from its last
    turn).

    Used by the frontend resurrect button. A bare resurrect is a pure
    lifecycle event with no message — the agent just gets the "you have been
    resurrected" marker; the "resurrect with prompt" path carries a `prompt`
    that is INSERTed as a chat inbound in the **same transaction** as the
    lifecycle 'resurrect' inbound. The detached session may be created before
    commit, but its child blocks on the agent row and cannot claim or process
    either inbound early. `resurrected_by` defaults to
    "user" and is validated against the envelope source whitelist.

    Peer agents have no dedicated resurrect API — they `send_message`, and
    auto-resurrect (`deliver_chat_inbound`) wakes a terminated target. This
    endpoint covers the case auto-resurrect cannot: a resurrect with no
    message to deliver.

    Always runs on the agent's home machine via its ops server
    (`forward_to_home_machine`) — that host starts the new process; launching
    anywhere else would start it on the wrong host.

    404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).
    `already_alive`: agent is still alive
        (running/idling); resurrect does not
        apply — idempotent.
    """
    forwarded = await forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/resurrect-explicit-v2",
        body.model_dump(),
    )
    return ResurrectAgentResponse.model_validate(forwarded)


@router.post("/api/agents/resurrect-billing")
async def post_agents_resurrect_billing(
    request: Request,
    body: BillingResurrectRequest = Body(default_factory=BillingResurrectRequest),  # noqa: B008
) -> BillingResurrectResponse:
    """Billing batch recovery — the explicit, operator-triggered entry (task #3919).

    `execute=false` (the default) is a strictly read-only preview: the
    billing-class halt candidates, the halted-but-alive survey (report-only),
    and the provider balance readout, nothing written. `execute=true` re-checks the provider balance (the run refuses
    below the configured floor) and then resurrects each candidate on its home
    machine through the versioned `resurrect-billing-v1` op, which enforces the
    billing-halt whitelist under the metadata row lock.
    Idempotent: the candidate set self-clears after a run, a repeated run is an
    audited no-op, and a concurrent second run is refused by the run-level
    advisory lock.
    """
    from ops.lifecycle.billing_recovery import run_billing_recovery

    return await run_billing_recovery(
        execute=body.execute,
        pool=request.app.state.db_pool,
        db=request.app.state.db,
        bus=request.app.state.bus,
    )


@router.post("/api/agents/{agent_id}/restart")
async def post_agent_restart(
    agent_id: int,
    body: RestartAgentRequest = Body(default_factory=RestartAgentRequest),  # noqa: B008
) -> RestartAgentResponse:
    """Enqueue native restart on the agent's home runner.

    The current turn reaches claim, returns normally and flushes its checkpoint.
    The host then applies the exact command and releases the incarnation for
    new admission, retaining agent ID and context. Terminated agents require
    resurrection; restart returns already_terminated for them."""
    forwarded = await forward_to_home_machine(
        agent_id, f"/api/agents/{agent_id}/restart", body.model_dump()
    )
    return RestartAgentResponse.model_validate(forwarded)


@router.get("/api/keyed/v1/agents/{agent_id}/native-work")
async def native_work(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], request: Request
) -> NativeWorkTarget:
    """Expose only eligible ACTIVE work backed by actual managed-owner evidence."""
    target = await asyncio.to_thread(observe_native_work, request.app.state.db_pool, agent_id)
    if target is None:
        raise HTTPException(status_code=409, detail="no eligible active native work")
    return target


@router.post("/api/keyed/v1/agents/{agent_id}/cancel-work")
async def native_cancel(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], body: NativeWorkTarget, request: Request
) -> NativeCancelAcceptance:
    """Accept one exact work intent; acceptance does not prove checkpoint execution."""
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


@router.post("/api/keyed/v1/agents/{agent_id}/restart-work")
async def native_restart(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], body: NativeRestartRequest, request: Request
) -> NativeRestartAcceptance:
    """Accept the original ACTIVE restart once through its versioned executor."""
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
        f"/api/agents/{agent_id}/restart-work-v1",
        operation.model_dump(mode="json"),
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


@router.get("/api/keyed/v1/agents/{agent_id}/restart-commands/{command_id}")
async def native_restart_status(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    command_id: Annotated[int, Path(gt=0, lt=2**63)],
    request: Request,
) -> NativeRestartProgress:
    """Observe retained original execution facts; no mutable current-owner inference."""
    progress = await asyncio.to_thread(
        native_restart_progress, request.app.state.db_pool, agent_id, command_id
    )
    if progress is None:
        raise HTTPException(status_code=404, detail="native restart command not found")
    return progress


@router.get("/api/keyed/v1/agents/{agent_id}/compact-target")
async def guarded_compact_target(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], request: Request
) -> CompactTarget:
    """Observe only actual new-host quiescent, closed-resource source evidence."""
    try:
        return await asyncio.to_thread(observe_guarded_compact, request.app.state.db_pool, agent_id)
    except CompactConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/keyed/v1/agents/{agent_id}/compact-history", status_code=202)
async def guarded_compact_history(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], body: CompactTarget, request: Request
) -> CompactAcceptance:
    """Accept the original observed history; 202 is not summary/application success."""
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


@router.get("/api/keyed/v1/agents/{agent_id}/compact-commands/{command_id}")
async def guarded_compact_command(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)], command_id: UUID, request: Request
) -> CompactStatus:
    """Inspect current execution evidence independently of immutable acceptance."""
    try:
        return await asyncio.to_thread(
            guarded_compact_status, request.app.state.db_pool, agent_id, command_id
        )
    except CompactConflictError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
