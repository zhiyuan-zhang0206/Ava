"""Agent lifecycle + event-publish RPC ops.

spawn / terminate / resurrect / restart / self-exit finalize, plus the
InboundArrived / PageClosed event publishes the agent-runner emits. One of the
four op clusters split out of the former single `ops/operations.py` (the
others are ops_cluster / ops_config / ops_inventory); each cluster is
self-contained — no op here calls an op in another cluster.

Both the gateway FastAPI handlers (`gateway/routers/agents_lifecycle.py`, `uploads.py`)
and the agent-runner ops server (`services/agent_ops/daemon.py:_dispatch`) call
these directly; the ops server runs them in-process. Cross-machine routing stays
in the FastAPI handler wrappers — forwarding never recurses inside an op. The
one forwarding call site in this module is `resurrect_if_terminated`, which is
NOT an op (only the gateway routers call it, never the ops server dispatch), so
its home-machine forward cannot loop either.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from psycopg_pool import ConnectionPool

import shared.db
from ops import cluster_rpc as _cluster_rpc
from ops import runner_mode
from ops.agent_identity import RESIDENT_IDENTITIES, probe_agent_process
from ops.agent_wake import ResurrectTriggerStaleError
from ops.agents import (
    get_agent_machine,
    get_agent_status,
    resurrect_agent,
)

# Re-exported (explicit-alias form) so the module-qualified callers —
# gateway/routers/notices.py, the ops server, tests — keep their call sites
# unchanged after the Task #1999 split: these helpers now live in
# `ops/ops_events.py`.
from ops.ops_events import (
    publish_inbound_arrived as publish_inbound_arrived,
)
from ops.ops_events import (
    publish_notice_posted as publish_notice_posted,
)
from ops.ops_events import (
    publish_notice_resolved as publish_notice_resolved,
)
from ops.ops_events import (
    publish_page_closed as publish_page_closed,
)
from ops.ops_exit import (
    _force_mark_terminated as _force_mark_terminated,
)
from ops.ops_exit import (
    _force_terminate_transaction as _force_terminate_transaction,
)
from ops.ops_exit import (
    _publish_force_terminate_inbound as _publish_force_terminate_inbound,
)
from ops.ops_exit import (
    mark_agent_exited_op as mark_agent_exited_op,
)

# Re-exported (explicit-alias form) so the gateway routers and tests keep their
# call sites unchanged after the Task #1999 split: the launch op cluster now
# lives in `ops/ops_launch.py`.
# Re-exported (explicit-alias form) so the gateway routers and tests keep their
# call sites unchanged after the Task #1999 split: the launch op cluster now
# lives in `ops/ops_launch.py`.
from ops.ops_launch import (
    _insert_prompt_blocking as _insert_prompt_blocking,
)
from ops.ops_launch import (
    _spawn_prechecks_blocking as _spawn_prechecks_blocking,
)
from ops.ops_launch import (
    launch_agent_op as launch_agent_op,
)
from ops.rpc_schemas import (
    CancelRequested,
    RestartAgentRequest,
    RestartAgentResponse,
    ResurrectAgentRequest,
    ResurrectAgentResponse,
    TerminateAgentRequest,
    TerminateAgentResponse,
)
from shared.agents import (
    AgentNotFound,
    AgentStatus,
    ResurrectAlreadyAlive,
)
from shared.db import insert_inbound_message
from shared.live_announce import publish_agent_updated_sync
from shared.machine import machine_name

_log = logging.getLogger(__name__)


def _cancel_blocking(agent_id: int, db_pool: ConnectionPool) -> int | None:
    """Sync cancel section — via to_thread. Returns the inbound id, or None
    when the agent is already terminated (the cancel would sit pending
    forever / fire a spurious pause on a future resurrect)."""
    if get_agent_status(agent_id) is AgentStatus.TERMINATED:
        return None
    with db_pool.connection() as conn:
        return insert_inbound_message(conn, agent_id, "", source="user", kind="cancel")


async def cancel_agent_op(agent_id: int, db_pool: ConnectionPool) -> CancelRequested:
    """Pause/stop the agent: INSERT a durable kind='cancel' inbound.

    No cross-machine forwarding — the INSERT lands in the shared DB and the
    agent (wherever it runs) is woken by the Redis pub/sub wake publish (PG
    LISTEN/NOTIFY was retired: PgBouncer transaction pooling breaks
    session-scoped LISTEN); the in-flight node interrupts on it, or the next
    claim pass halts to idle. A cancel for an already-dead agent is a no-op
    (the row would sit pending forever / fire a spurious pause on a future
    resurrect), so short-circuit on TERMINATED.
    """
    iid = await asyncio.to_thread(_cancel_blocking, agent_id, db_pool)
    if iid is None:
        return CancelRequested(status="already_terminated")
    await publish_inbound_arrived(agent_id, iid, "cancel", "user", "")
    return CancelRequested(status="enqueued")


async def terminate_agent_op(
    agent_id: int, body: TerminateAgentRequest, db_pool: ConnectionPool
) -> TerminateAgentResponse:
    """Local-target graceful or force terminate. Caller handles cross-machine."""
    if body.force:
        hosted = runner_mode.is_hosted()
        old_status, pid, killed_page_names, command_id = await asyncio.to_thread(
            _terminate_force_blocking, agent_id, body, db_pool, kill_process=not hosted
        )
        if old_status is AgentStatus.TERMINATED and not hosted:
            return TerminateAgentResponse(status="already_terminated")
        if hosted:
            await _cancel_hosted_turn_best_effort(agent_id, command_id)
        for page_name in killed_page_names:
            await publish_page_closed(agent_id, page_name)
        _log.info(
            "[gateway] agent %s force requested by %s (pid=%s)",
            agent_id,
            body.source,
            pid,
        )
        # Neither HTTP delivery nor Task.cancel proves resource quiescence.
        return TerminateAgentResponse(status="enqueued" if hosted else "force_killed")

    s = await asyncio.to_thread(get_agent_status, agent_id)
    if s is AgentStatus.TERMINATED:
        return TerminateAgentResponse(status="already_terminated")

    status, iid, zombie_closed_page_names = await asyncio.to_thread(
        _terminate_graceful_blocking, agent_id, body, db_pool
    )
    if status == "already_terminated":
        for page_name in zombie_closed_page_names:
            await publish_page_closed(agent_id, page_name)
        return TerminateAgentResponse(status="already_terminated")
    assert iid is not None  # status == "enqueued" implies the inbound was inserted  # noqa: S101
    await publish_inbound_arrived(agent_id, iid, "terminate", body.source, "")
    return TerminateAgentResponse(status="enqueued")


async def _cancel_hosted_turn_best_effort(agent_id: int, command_id: int) -> None:
    """Accelerate a hosted force-terminate by cancelling the agent's turn task.

    Dials the local agent-host's loopback health port (`POST /cancel-turn`);
    the host cancels the turn with its bounded unwind and reports a
    C-call-blocked straggler instead of hanging. Every failure is swallowed at
    INFO with the exception. The durable force pointer stays unobserved until
    the original host actually settles; a dead host is not child-exit proof.
    """
    import httpx

    from shared.daemon_health import health_port

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
            resp = await client.post(
                f"http://127.0.0.1:{health_port('agent_host')}/cancel-turn",
                json={"agent_id": agent_id, "command_id": command_id},
            )
            resp.raise_for_status()
    except Exception:
        _log.info(
            "hosted turn-cancel call for agent %s failed (non-fatal; the durable "
            "force command remains accepted; quiescence is not confirmed)",
            agent_id,
            exc_info=True,
        )


def _terminate_force_blocking(
    agent_id: int,
    body: TerminateAgentRequest,
    db_pool: ConnectionPool,
    *,
    kill_process: bool,
) -> tuple[AgentStatus, int | None, list[str], int]:
    """Sync force-kill section — via to_thread: session kill + SIGKILL + status
    flip + AgentUpdated + terminate inbound. Returns (old status, pid, page names).

    `kill_process` is False in hosted mode: there is no process to kill, and
    the turn-cancel acceleration happens asynchronously after the transaction
    (the DB fence must commit first so a post-force chat cannot resurrect the
    row between the kill and the cancel)."""
    old_status, pid, killed_page_names, inbound_id = _force_terminate_transaction(
        agent_id,
        db_pool,
        source=body.source,
        kill_process=kill_process,
    )
    _publish_force_terminate_inbound(agent_id, inbound_id, body.source)
    with db_pool.connection() as conn:
        publish_agent_updated_sync(conn, agent_id)
    return old_status, pid, killed_page_names, inbound_id


def _terminate_graceful_blocking(
    agent_id: int, body: TerminateAgentRequest, db_pool: ConnectionPool
) -> tuple[str, int | None, list[str]]:
    """Sync graceful-terminate section — via to_thread: zombie check + optional
    zombie finalize, else the terminate inbound INSERT. Returns
    (status, inbound_id, zombie_closed_page_names); status is
    'already_terminated' (zombie) or 'enqueued'."""
    with db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pid FROM agents_meta WHERE id=%s", (agent_id,))
        row = cur.fetchone()
    pid = row[0] if row else None
    if pid is not None and probe_agent_process(pid, agent_id) not in RESIDENT_IDENTITIES:
        zombie_closed_page_names = _force_mark_terminated(agent_id, db_pool, source=body.source)
        with db_pool.connection() as conn:
            publish_agent_updated_sync(conn, agent_id)
        return "already_terminated", None, zombie_closed_page_names
    with db_pool.connection() as conn:
        iid = insert_inbound_message(conn, agent_id, "", source=body.source, kind="terminate")
    return "enqueued", iid, []


def _pending_allocation_can_resume(agent_id: int, trigger_inbound_id: int | None) -> bool:
    from shared.db import connect
    from shared.resurrection_launch import pending_allocation

    if trigger_inbound_id is None:
        return False
    with connect() as conn:
        return pending_allocation(conn, agent_id, trigger_inbound_id)


def _wake_suppression_active(agent_id: int) -> bool:
    with shared.db.connect() as conn:
        row = conn.execute(
            "SELECT wake_suppressed_until >= now() FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row[0] is True


def _clear_wake_suppression(agent_id: int) -> None:
    with shared.db.connect() as conn:
        conn.execute(
            "UPDATE agents_meta SET wake_suppressed_until=NULL, wake_suppress_reason=NULL "
            "WHERE id=%s AND wake_suppressed_until IS NOT NULL",
            (agent_id,),
        )


async def resurrect_agent_op(
    agent_id: int,
    body: ResurrectAgentRequest,
    *,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None = None,
) -> ResurrectAgentResponse:
    """Local-target resurrect (UPDATE terminated -> idling + detached process launch).

    `trigger_inbound_id` carries the internal auto-resurrect CAS to the home
    runner; manual resurrects omit it and retain their unconditional contract.
    """
    s = await asyncio.to_thread(get_agent_status, agent_id)
    if s is not AgentStatus.TERMINATED and (
        s is not AgentStatus.IDLING
        or not await asyncio.to_thread(_pending_allocation_can_resume, agent_id, trigger_inbound_id)
    ):
        return ResurrectAgentResponse(status="already_alive")
    try:
        # resurrect_agent synchronously launches the agent and polls up to
        # launch_confirm_timeout_seconds for it to claim. Run it off the event loop: a
        # resurrected agent self-fetches its config from THIS gateway at boot, so
        # blocking the loop here would deadlock that fetch. (spawn avoids this by
        # always dispatching to the ops daemon; a local resurrect runs in-process.)
        await asyncio.to_thread(
            resurrect_agent,
            agent_id,
            resurrected_by=body.resurrected_by,
            prompt=body.prompt,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
    except (ResurrectAlreadyAlive, ResurrectTriggerStaleError):
        return ResurrectAgentResponse(status="already_alive")
    return ResurrectAgentResponse(status="spawned")


async def resurrect_if_terminated(
    agent_id: int,
    *,
    trigger_inbound_id: int,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"],
) -> AgentStatus:
    """Resurrect `agent_id` when it is terminated, so a just-delivered inbound is
    handled by a live process instead of sitting unclaimed forever.

    Call this right after an inbound has been persisted for an agent that may be
    terminated (a chat delivery, a user-triggered compact). `resurrect_agent_op`
    inserts its own newer 'resurrect' inbound; the claim node resolves a batch by
    recency, so the resurrect wins over any stale prior-life terminate sharing
    the batch and the just-delivered inbound survives to be processed.

    Chat delivery, compact, and the delivery watchdog pass an exact
    `trigger_inbound_id` plus its expected kind. They travel on the distinct
    internal `resurrect-if-pending-work-v2` lifecycle path so an older runner
    rejects the unknown path (version skew fails closed), then the home runner's
    final terminated -> idling UPDATE verifies that exact work is still
    pending, newer than the current termination, and above the force-intent
    fence. Explicit manual resurrection uses `resurrect-explicit-v2`; crash and
    wedged recovery use their exact controller claims in the local runner.

    Returns the agent's status after the attempt: the post-resurrect status when
    a process was spawned, otherwise the unchanged status (a non-terminated agent
    is returned untouched). A resurrect failure (e.g. the launch path is
    unreachable) is logged and swallowed — the inbound is already queued, so a
    later manual resurrect picks it up.

    The process must start on the agent's home machine (`agents_meta.machine`)
    — launching it here when the agent lives elsewhere trips the boot placement
    gate and crash-loops (agent 1513 incident). A local-homed agent resurrects
    in-process; a remote-homed one is forwarded as a 'lifecycle' op to its home
    machine's ops server, the same dispatch the gateway's /resurrect route uses
    (`_forward_to_home_machine`). An unreachable home machine skips the
    resurrect with an INFO record — the queued inbound is picked up when the machine
    is back (next delivery re-triggers this, or a manual resurrect).

    This is deliberately NOT wired into the terminate / cancel / restart ops:
    those signals are no-ops on an already-dead agent (reviving an agent only to
    kill or pause it would reverse the caller's intent), so they short-circuit on
    TERMINATED instead.
    """
    status = await asyncio.to_thread(get_agent_status, agent_id)
    if status is not AgentStatus.TERMINATED and (
        status is not AgentStatus.IDLING
        or not await asyncio.to_thread(_pending_allocation_can_resume, agent_id, trigger_inbound_id)
    ):
        return status
    if await asyncio.to_thread(_wake_suppression_active, agent_id):
        _log.debug(
            "resurrect_if_terminated: automatic wake suppressed for agent %s; "
            "skipping auto-resurrect",
            agent_id,
        )
        return status
    body = ResurrectAgentRequest(resurrected_by="system")
    try:
        home = await asyncio.to_thread(get_agent_machine, agent_id)
        try:
            lifecycle_payload: dict[str, object] = {
                "path": f"/api/agents/{agent_id}/resurrect-if-pending-work-v2",
                "body": body.model_dump(),
                "trigger_inbound_id": trigger_inbound_id,
                "trigger_inbound_kind": trigger_inbound_kind,
            }
            forwarded = await _cluster_rpc.dispatch_to_machine(
                target_machine=home,
                kind="lifecycle",
                payload=lifecycle_payload,
            )
            result_status = ResurrectAgentResponse.model_validate(forwarded).status
        except _cluster_rpc.ClusterOpUnreachable:
            if home == machine_name():
                # Local ops server not reachable (test / single-process):
                # fall back to in-process resurrect.
                result_status = (
                    await resurrect_agent_op(
                        agent_id,
                        body,
                        trigger_inbound_id=trigger_inbound_id,
                        trigger_inbound_kind=trigger_inbound_kind,
                    )
                ).status
            else:
                raise
        if result_status == "spawned":
            await asyncio.to_thread(_clear_wake_suppression, agent_id)
            status = await asyncio.to_thread(get_agent_status, agent_id)
    except _cluster_rpc.ClusterOpUnreachable as exc:
        _log.info(
            "resurrect_if_terminated: agent %s home machine unreachable, skipping "
            "auto-resurrect (%s); inbound queued — the next delivery or a manual "
            "resurrect picks it up once the machine is back",
            agent_id,
            exc,
        )
    except Exception:
        _log.info(
            "resurrect_if_terminated: auto-resurrect agent %s failed; "
            "inbound queued, manual resurrect will pick it up",
            agent_id,
            exc_info=True,
        )
    return status


def _restart_blocking(
    agent_id: int, body: RestartAgentRequest, db_pool: ConnectionPool
) -> int | None:
    """Sync restart section — via to_thread. Returns the inbound id, or None
    when the agent is already terminated."""
    from psycopg import sql

    from shared.db_transaction import write_transaction
    from shared.lifecycle_acceptance import FAILED_RESTART_FOR_CURRENT_TARGET

    with write_transaction(db_pool) as conn:
        row = conn.execute(
            sql.SQL("SELECT status,{} FROM agents_meta WHERE id=%s FOR UPDATE").format(
                sql.SQL(FAILED_RESTART_FOR_CURRENT_TARGET)
            ),
            (agent_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"agent {agent_id} does not exist")
        if row[0] == "terminated" and row[1] is not True:
            return None
        payload: dict[str, object] | None = None
        if body.config_overlay:
            # This overlay write must not depend on borrowed-backend session
            # state: PgBouncer can hand us a backend whose session a previous
            # client left with default_transaction_read_only=on, and this
            # emergency channel (provider-outage model switch) is exactly when
            # the pool is most likely poisoned. Declare the transaction
            # writable before the DML (same posture as #1428/#1436).
            conn.execute(
                "UPDATE agents_meta "
                "SET config_overlay = COALESCE(config_overlay, '{}'::jsonb) || %s::jsonb "
                "WHERE id = %s",
                (json.dumps(dict(body.config_overlay)), agent_id),
            )
            payload = {"config_overlay": dict(body.config_overlay)}
        # insert_inbound_message commits this connection, so its INSERT commits
        # the preceding overlay UPDATE in the same transaction.
        return insert_inbound_message(
            conn, agent_id, "", source=body.source, kind="restart", payload=payload
        )


async def restart_agent_op(
    agent_id: int, body: RestartAgentRequest, db_pool: ConnectionPool
) -> RestartAgentResponse:
    """Local-target restart — INSERT one kind='restart' inbound."""
    iid = await asyncio.to_thread(_restart_blocking, agent_id, body, db_pool)
    if iid is None:
        return RestartAgentResponse(status="already_terminated")
    await publish_inbound_arrived(agent_id, iid, "restart", body.source, "")
    return RestartAgentResponse(status="enqueued")


_LIFECYCLE_PATH = re.compile(
    r"^/api/agents/(?P<id>\d+)/"
    r"(?P<action>terminate|resurrect|resurrect-explicit-v2|"
    r"resurrect-if-pending-work-v2|restart)$"
)


async def lifecycle_op(
    path: str,
    body: dict[str, Any],
    db_pool: ConnectionPool,
    *,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: Literal["chat", "compact_request", "system_note"] | None = None,
) -> TerminateAgentResponse | ResurrectAgentResponse | RestartAgentResponse:
    """Parse the lifecycle path from a 'lifecycle' op payload and dispatch to
    the appropriate per-action op. Returns the per-action response model (the
    ops server serializes it into the /ops response).

    Path shapes accepted: terminate/restart plus the versioned internal
    `resurrect-explicit-v2` and guarded `resurrect-if-pending-work-v2` paths.
    Legacy `resurrect` is recognized only to reject it. This is the
    version-skew fail-closed boundary: an old runner rejects the v2 paths and a
    new runner rejects every old gateway resurrection instead of guessing its
    intent from the request body.
    Anything else raises ValueError — the ops server converts that to a failed
    op result.
    """
    m = _LIFECYCLE_PATH.match(path)
    if m is None:
        raise ValueError(f"lifecycle path not recognized: {path!r}")
    agent_id = int(m.group("id"))
    action = m.group("action")
    if (trigger_inbound_id is None) != (trigger_inbound_kind is None):
        raise ValueError("trigger inbound id and kind must be provided together")
    if action != "resurrect-if-pending-work-v2" and trigger_inbound_id is not None:
        raise ValueError(
            f"trigger inbound is only valid for resurrect-if-pending-work-v2, not {action!r}"
        )
    if action == "terminate":
        return await terminate_agent_op(
            agent_id, TerminateAgentRequest.model_validate(body), db_pool
        )
    if action == "resurrect":
        raise ValueError("legacy /resurrect is refused; use a versioned internal path")
    if action == "resurrect-explicit-v2":
        return await resurrect_agent_op(agent_id, ResurrectAgentRequest.model_validate(body))
    if action == "resurrect-if-pending-work-v2":
        if trigger_inbound_id is None:
            raise ValueError("resurrect-if-pending-work-v2 requires trigger inbound")
        request = ResurrectAgentRequest.model_validate(body)
        if request.resurrected_by != "system":
            raise ValueError("resurrect-if-pending-work-v2 requires resurrected_by='system'")
        return await resurrect_agent_op(
            agent_id,
            request,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
    if action == "restart":
        return await restart_agent_op(agent_id, RestartAgentRequest.model_validate(body), db_pool)
    raise AssertionError(f"unreachable: action={action!r}")
