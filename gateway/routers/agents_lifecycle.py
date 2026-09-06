"""Agent lifecycle endpoints — /api/agents/* lifecycle surface.

Compact / cancel / terminate / exited / resurrect / restart.

Lifecycle operations that mutate physical host state (session / OS
process) always run on the agent's home machine via its ops server
(`_forward_to_home_machine`, routers/agents_forward.py) — no local shortcut
even when the target is the co-located box. Operations that are durable
DB-row + event-publish work (cancel / exited) run on whichever
gateway receives them. CRUD + spawn live in routers/agents.py; message +
state reads in routers/agents_state.py.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Body, HTTPException, Request, Response
from psycopg_pool import ConnectionPool

from gateway.routers.agents_forward import _forward_to_home_machine
from gateway.schemas import CancelRequest, CompactEnqueued
from ops import ops_lifecycle as _ops
from ops.rpc_schemas import (
    AgentExitedRequest,
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
    """Have the agent gracefully exit — INSERT one kind='terminate'
    source=body.source inbound; after processing the current turn, when
    claim runs, dispatch goto END and the process exits. An optional message
    is committed as pending chat immediately before the terminate command, so
    it is retained for resurrection without causing another LLM turn.

    With `force=true`, request interruption. Hosted force returns `enqueued`
    while the original host drains actual work; acceptance and metadata status
    are not proof of exit. Detached process force returns `force_killed`.

    Smart liveness detection: if the process corresponding to
    agents_meta.pid is gone (zombie row, commonly from early-stage
    EmptyInputError residuals / respawn_agent failures leaving an unclaimed
    row), force UPDATE status='terminated' directly without the
    inbound path — an inbound delivered to a dead process is pending
    forever and the user can never clear it.

    Always runs on the agent's home machine via its ops server
    (`_forward_to_home_machine`). The force path must, because killing the
    detached process / os.kill are physical local-host operations; the graceful
    path must too, because zombie detection `process_alive(pid)` is
    meaningless across machines (probing a cross-machine PID via signal 0
    hits a remote PID space and gives false positives). Both paths run on
    the home machine, symmetric in logic.

    404: agent_id does not exist (AgentNotFound -> handler returns
        404 + reason).
    `already_terminated`: agent was already dead / process detected as
        dead and cleaned up directly.
    `force_killed`: force=true killed the process and marked terminated.
    """
    forwarded = await _forward_to_home_machine(
        agent_id, f"/api/agents/{agent_id}/terminate", body.model_dump()
    )
    return TerminateAgentResponse.model_validate(forwarded)


@router.post("/api/agents/{agent_id}/exited", status_code=204)
async def post_agent_exited(
    agent_id: int, request: Request, body: AgentExitedRequest | None = None
) -> Response:
    """An agent process reports it has reached its own exit finally block —
    finalize its status to 'terminated', close its agent-owned show() pages,
    and keep daemon-supervised serve() pages open.

    Called by the agent itself (`ava.self`'s exit path), not by a user/peer.
    Distinct from `/terminate`, which *initiates* termination (inserts a
    terminate inbound for a live agent, or force-kills): by the time `/exited`
    arrives the process has already stopped, so this only records the
    finalized state. The status flip is guarded (a concurrent restart leaves
    status 'restarting' untouched), so a restart's process-exit hitting this
    endpoint does not strand the restarter.

    No cross-machine forwarding: the work is a status UPDATE + event publish,
    both against the shared DB / events channel, so it runs on whichever
    gateway receives it.
    """
    await _ops.mark_agent_exited_op(
        agent_id,
        request.app.state.db_pool,
        generation=body.generation if body else None,
        owner=body.owner if body else None,
    )
    return Response(status_code=204)


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
    """Have the agent self-restart — INSERT kind='restart' source=body.source
    inbound; after processing the current turn, when claim runs it UPDATE
    status='restarting' + goto END + process exits; the restarter daemon
    sees status='restarting' and auto-respawns a fresh process attached to
    the same agent_id (new PID, LangGraph state preserved).

    Always forwards to the agent's home machine ops server — the restart
    inbound INSERT hits the shared DB regardless of which host runs it, but
    the uniform forwarding path (same as terminate/resurrect) keeps one
    code path with no local shortcut.

    404: agent_id does not exist (AgentNotFound -> handler returns
        404 + reason).
    `already_terminated`: agent is dead; restart does not apply — use
        resurrect.
    """
    forwarded = await _forward_to_home_machine(
        agent_id, f"/api/agents/{agent_id}/restart", body.model_dump()
    )
    return RestartAgentResponse.model_validate(forwarded)
