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
import logging
import re
from typing import Any, Literal

from psycopg_pool import ConnectionPool

from ops import agent_launch, runner_mode
from ops import cluster_rpc as _cluster_rpc
from ops.agent_identity import RESIDENT_IDENTITIES, probe_agent_process
from ops.agent_wake import ResurrectTriggerStaleError
from ops.agents import (
    get_agent_machine,
    get_agent_status,
    latest_checkpoint_id,
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
from ops.ops_exit import (
    mark_agent_hibernating_op as mark_agent_hibernating_op,
)
from ops.rpc_schemas import (
    CancelRequested,
    LaunchAgentRequest,
    RestartAgentRequest,
    RestartAgentResponse,
    ResurrectAgentRequest,
    ResurrectAgentResponse,
    SpawnAgentRequest,
    SpawnedAgent,
    TerminateAgentRequest,
    TerminateAgentResponse,
)
from shared.agents import (
    AgentStatus,
    ForkSourceEmpty,
    ResurrectAlreadyAlive,
)
from shared.config import settings
from shared.db import insert_inbound_message, publish_inbound_wake
from shared.live_announce import publish_agent_updated_sync
from shared.machine import machine_name

_log = logging.getLogger(__name__)


async def launch_agent_op(body: LaunchAgentRequest, db_pool: ConnectionPool) -> SpawnedAgent:
    """Launch a PRE-CREATED agent row on this machine (Task #1236 follow-up).

    The gateway created the row (agents + agents_meta, as the main identity)
    and forwards only the launch here — this ops server runs as the
    least-privilege `ava_runner` role, which by design cannot INSERT agents.
    Everything this op does is within the runner role: model-config validation,
    the detached child launch (OS-level), the launch-confirm (agents_meta
    UPDATE), and the plain-spawn first prompt (inbound INSERT).
    """
    # Validate model config before launching — the runner owns the LLM keys and
    # is authoritative (defense-in-depth on top of the gateway-side check). Off
    # the event loop: may read provider API keys.
    from shared.lm.factory import validate_model_config

    await asyncio.to_thread(validate_model_config, model=settings.lm.llm_model, config=body.config)
    if runner_mode.is_hosted():
        # Hosted: the row the gateway created IS the agent. No fork, no
        # launch-confirm (there is no pid to wait for — the claim CAS of the
        # dispatcher's turn task is the confirmation). The prompt INSERT below
        # publishes its own wake; the explicit wake at the end covers the fork,
        # whose inbounds are raw SQL with no publish, and gives a plain spawn
        # one cheap no-op turn that doubles as a spawn health check.
        if body.prompt is not None:
            assert body.prompt_source is not None  # narrowed by the caller  # noqa: S101
            prompt = body.prompt
            if body.label:
                prompt = f"{prompt}\n\nYour label has been set to {body.label}."
            iid = await asyncio.to_thread(
                _insert_prompt_blocking, db_pool, body.agent_id, prompt, body.prompt_source
            )
            await publish_inbound_arrived(body.agent_id, iid, "chat", body.prompt_source, prompt)
        publish_inbound_wake(body.agent_id, "0")
        return SpawnedAgent(id=body.agent_id)
    # _launch_agent_process is synchronous (detached-process launch). Run it off
    # the event loop so a launch never blocks the ops dispatch loop and starves
    # concurrent requests (status probes / other ops) while it runs.
    await asyncio.to_thread(
        agent_launch._launch_agent_process,
        body.agent_id,
        body.config,
        birth_config=body.birth_config,
        confirm=False,
    )
    # Confirm the launched child claimed its row off this response path; a launch
    # that never claims is forced 'terminated' there (reaper backstops).
    agent_launch.schedule_launch_confirm(body.agent_id)
    if body.prompt is not None:
        assert body.prompt_source is not None  # narrowed by the caller  # noqa: S101
        prompt = body.prompt
        if body.label:
            prompt = f"{prompt}\n\nYour label has been set to {body.label}."
        iid = await asyncio.to_thread(
            _insert_prompt_blocking, db_pool, body.agent_id, prompt, body.prompt_source
        )
        await publish_inbound_arrived(body.agent_id, iid, "chat", body.prompt_source, prompt)
    return SpawnedAgent(id=body.agent_id)


def _spawn_prechecks_blocking(body: SpawnAgentRequest, db_pool: ConnectionPool) -> str | None:
    """Sync spawn pre-checks — via to_thread: model-config validation (may read
    provider API keys) + fork checkpoint lookup. Returns the fork checkpoint
    (None for a plain spawn)."""
    # Validate model config before any DB work — defense-in-depth on top of the
    # gateway-side check in post_agents. The gateway may be on a different machine
    # without API keys; the runner always has its own settings and is authoritative.
    from shared.lm.factory import validate_model_config

    validate_model_config(model=settings.lm.llm_model, config=body.config)
    fork_checkpoint: str | None = None
    if body.fork_from is not None:
        with db_pool.connection() as conn, conn.cursor() as cur:
            fork_checkpoint = latest_checkpoint_id(cur, body.fork_from)
        if fork_checkpoint is None:
            raise ForkSourceEmpty(
                f"agent {body.fork_from} has no checkpoint — it may not have run any LLM/exec step yet"
            )
    if body.prompt is not None and body.prompt_source is None:
        raise RuntimeError("prompt_source missing despite schema validator")
    return fork_checkpoint


def _insert_prompt_blocking(
    db_pool: ConnectionPool, agent_id: int, prompt: str, source: str
) -> int:
    """Sync first-prompt inbound INSERT for a plain spawn — via to_thread."""
    with db_pool.connection() as conn:
        return insert_inbound_message(conn, agent_id, prompt, source=source)


# ─── lifecycle: terminate / resurrect / restart ─────────────────────────────


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
        old_status, pid, killed_page_names = await asyncio.to_thread(
            _terminate_force_blocking, agent_id, body, db_pool, kill_process=not hosted
        )
        if old_status is AgentStatus.TERMINATED:
            return TerminateAgentResponse(status="already_terminated")
        if hosted:
            # No process to SIGKILL — the row is terminated and the durable
            # terminate inbound (inserted inside the force transaction) is the
            # correctness mechanism: a running turn claims it and exits. This
            # call only ACCELERATES a turn wedged inside a long await, and it
            # is best-effort by construction: a host that is down or
            # restarting means the turn is already gone (checkpointed, and no
            # wake will resurrect a terminated row).
            await _cancel_hosted_turn_best_effort(agent_id)
        for page_name in killed_page_names:
            await publish_page_closed(agent_id, page_name)
        _log.info(
            "[gateway] agent %s force-killed by %s (pid=%s)",
            agent_id,
            body.source,
            pid,
        )
        return TerminateAgentResponse(status="force_killed")

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


async def _cancel_hosted_turn_best_effort(agent_id: int) -> None:
    """Accelerate a hosted force-terminate by cancelling the agent's turn task.

    Dials the local agent-host's loopback health port (`POST /cancel-turn`);
    the host cancels the turn with its bounded unwind and reports a
    C-call-blocked straggler instead of hanging. Every failure is swallowed at
    INFO with the exception — the durable terminate inbound already inserted by
    the force transaction is what makes the row dead; this hop only shortens
    the window for a turn stuck inside a long await, and a host that is down /
    restarting means the turn is already gone.
    """
    import httpx

    from shared.daemon_health import health_port

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
            resp = await client.post(
                f"http://127.0.0.1:{health_port('agent_host')}/cancel-turn",
                json={"agent_id": agent_id},
            )
            resp.raise_for_status()
    except Exception:
        _log.info(
            "hosted turn-cancel call for agent %s failed (non-fatal; the durable "
            "terminate inbound already finalized the row)",
            agent_id,
            exc_info=True,
        )


def _terminate_force_blocking(
    agent_id: int,
    body: TerminateAgentRequest,
    db_pool: ConnectionPool,
    *,
    kill_process: bool,
) -> tuple[AgentStatus, int | None, list[str]]:
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
    return old_status, pid, killed_page_names


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
    if s is not AgentStatus.TERMINATED:
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
    resurrect with a warning — the queued inbound is picked up when the machine
    is back (next delivery re-triggers this, or a manual resurrect).

    This is deliberately NOT wired into the terminate / cancel / restart ops:
    those signals are no-ops on an already-dead agent (reviving an agent only to
    kill or pause it would reverse the caller's intent), so they short-circuit on
    TERMINATED instead.
    """
    status = await asyncio.to_thread(get_agent_status, agent_id)
    if status is not AgentStatus.TERMINATED:
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
            status = await asyncio.to_thread(get_agent_status, agent_id)
    except _cluster_rpc.ClusterOpUnreachable as exc:
        _log.warning(
            "resurrect_if_terminated: agent %s home machine unreachable, skipping "
            "auto-resurrect (%s); inbound queued — the next delivery or a manual "
            "resurrect picks it up once the machine is back",
            agent_id,
            exc,
        )
    except Exception:
        _log.warning(
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
    if get_agent_status(agent_id) is AgentStatus.TERMINATED:
        return None
    with db_pool.connection() as conn:
        return insert_inbound_message(conn, agent_id, "", source=body.source, kind="restart")


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
