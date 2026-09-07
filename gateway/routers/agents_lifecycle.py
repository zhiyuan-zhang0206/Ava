"""Agent lifecycle endpoints — /api/agents/* lifecycle surface.

Compact / cancel / terminate / resurrect / restart.

Lifecycle operations that mutate physical host state (session / OS
process) always run on the agent's home machine via its ops server
(`_forward_to_home_machine`, routers/agents_forward.py) — no local shortcut
even when the target is the co-located box. Operations that are durable
DB-row + event-publish work (cancel) run on whichever
gateway receives them. CRUD + spawn live in routers/agents.py; message +
state reads in routers/agents_state.py.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Body, HTTPException, Request
from psycopg_pool import ConnectionPool

from gateway.routers.agents_forward import _forward_to_home_machine
from gateway.schemas import CancelRequest, CompactEnqueued
from ops import ops_lifecycle as _ops
from ops.rpc_schemas import (
    CancelRequested,
    RestartAgentRequest,
    RestartAgentResponse,
    ResurrectAgentRequest,
    ResurrectAgentResponse,
    TerminateAgentRequest,
    TerminateAgentResponse,
)
from shared.db import agent_exists, insert_compact_request_inbound

router = APIRouter()


@router.post("/api/agents/{agent_id}/compact")
async def post_compact(
    agent_id: int,
    request: Request,
    mode: Literal["framework", "agent"] = "framework",
) -> CompactEnqueued:
    """Trigger compact — INSERT kind='compact_request' inbound; the claim
    Node takes over, runs the backend Compaction LLM to generate a summary
    that replaces messages, and publishes a `compact_done` event to notify
    UI.

    `mode` query parameter is preserved for backward compat with old
    frontends but is ignored — the new design uniformly uses backend LLM
    summary generation (see
    decisions/2026-05-02-self-cycling-langgraph.md). Agent-initiated compact still goes through
    ava.self.compact() -> kind='compact_summary'; this is a separate signal
    from UI-triggered compact_request.

    A compact targeting a terminated agent auto-resurrects it (shared with the
    chat path): otherwise the compact_request row would sit pending with no live
    process to claim it. The co-batched resurrect wins the claim node's recency
    routing, so the agent wakes and the requested compaction still runs.
    """
    inbound_id = await asyncio.to_thread(
        _compact_request_blocking,
        request.app.state.db_pool,
        agent_id,
    )
    await _ops.resurrect_if_terminated(
        agent_id,
        trigger_inbound_id=inbound_id,
        trigger_inbound_kind="compact_request",
    )

    return CompactEnqueued(mode=mode, agent_id=agent_id, status="enqueued")


@router.post("/api/cancel")
async def post_cancel(body: CancelRequest, request: Request) -> CancelRequested:
    """Pause/stop the agent — INSERT a durable kind='cancel' inbound.

    Durable, not fire-and-forget: a running llm/exec node interrupts on the
    row immediately (it watches the inbound Redis pub/sub path); if the agent
    is between actions when the cancel lands, the row stays pending and the
    next claim pass halts it to idle. Either way the agent stops and stays
    alive (resumable by the next message). Enqueue-and-return like `/messages`;
    the kernel emits a `cancelled` SSE event when it actually stops.

    No cross-machine forwarding: the cancel is a durable row in the shared DB
    (plus a Redis wake), delivered regardless of which host runs the agent.
    """
    return await _ops.cancel_agent_op(body.agent_id, request.app.state.db_pool)


def _compact_request_blocking(pool: ConnectionPool, agent_id: int) -> int:
    """Sync compact-request INSERT + 404 guard — via to_thread."""
    with pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        return insert_compact_request_inbound(conn, agent_id)


@router.post("/api/agents/{agent_id}/terminate")
async def post_agent_terminate(
    agent_id: int,
    body: TerminateAgentRequest = Body(default_factory=TerminateAgentRequest),  # noqa: B008
) -> TerminateAgentResponse:
    """Terminate through the home runner's durable native control path.

    Claim returns to END and the host flushes before applying normal termination.
    An optional message is committed as pending work before the command, retained
    for later resurrection without causing another model turn. Force returns
    enqueued while the exact original host settles its task and execution
    resources; acceptance and metadata status are not proof of completed exit.

    Both paths forward to the home runner. A missing agent returns 404; an
    already-terminated identity is a no-op for graceful termination."""
    forwarded = await _forward_to_home_machine(
        agent_id, f"/api/agents/{agent_id}/terminate", body.model_dump()
    )
    return TerminateAgentResponse.model_validate(forwarded)


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
    (`_forward_to_home_machine`) — that host starts the new process; launching
    anywhere else would start it on the wrong host.

    404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).
    `already_alive`: agent is still alive
        (running/idling/restarting); resurrect does not
        apply — idempotent.
    """
    forwarded = await _forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/resurrect-explicit-v2",
        body.model_dump(),
    )
    return ResurrectAgentResponse.model_validate(forwarded)


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
    forwarded = await _forward_to_home_machine(
        agent_id, f"/api/agents/{agent_id}/restart", body.model_dump()
    )
    return RestartAgentResponse.model_validate(forwarded)
