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

from ops import agent_launch
from ops import cluster_rpc as _cluster_rpc
from ops.agent_identity import RESIDENT_IDENTITIES, AgentProcessIdentity, probe_agent_process
from ops.agent_wake import ResurrectTriggerStaleError
from ops.agents import (
    get_agent_machine,
    get_agent_status,
    latest_checkpoint_id,
    resurrect_agent,
)
from ops.pages import list_open_page_names
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
    AgentNotFound,
    AgentStatus,
    ForkSourceEmpty,
    ResurrectAlreadyAlive,
)
from shared.audit_events import insert_event_log
from shared.cluster import session_name
from shared.config import settings
from shared.db import insert_inbound_message, publish_inbound_wake
from shared.live_announce import publish_agent_updated_sync
from shared.live_events import (
    InboundArrived,
    NoticePosted,
    NoticeResolved,
    PageClosed,
)
from shared.log import logger
from shared.machine import machine_name
from shared.proc import force_kill
from shared.redis_client import publish_best_effort
from shared.session_backend import native_proc

_log = logging.getLogger(__name__)


async def publish_inbound_arrived(
    agent_id: int, inbound_id: int, kind: str, source: str, content: str
) -> None:
    """Publish InboundArrived to the events channel — frontend live updates."""
    ev = InboundArrived(
        agent_id=agent_id,
        inbound_id=inbound_id,
        kind=kind,
        source=source,
        content=content,
    )
    await publish_best_effort(
        settings.data_plane.events_channel, ev.model_dump_json(), context="inbound_arrived"
    )


async def publish_page_closed(agent_id: int, name: str) -> None:
    """Publish PageClosed to the events channel — frontend popover removes the entry.
    Best-effort, never raises."""
    ev = PageClosed(agent_id=agent_id, name=name)
    await publish_best_effort(
        settings.data_plane.events_channel, ev.model_dump_json(), context="page_closed"
    )


async def publish_notice_resolved(agent_id: int, notice_id: int) -> None:
    """Publish NoticeResolved to the events channel — frontend drops it from the
    unread FYI feed and decrements the agent's unread badge. Best-effort, never raises."""
    ev = NoticeResolved(agent_id=agent_id, notice_id=notice_id)
    await publish_best_effort(
        settings.data_plane.events_channel, ev.model_dump_json(), context="notice_resolved"
    )


async def publish_notice_posted(
    agent_id: int, notice_id: int, priority: str, title: str, task_id: int | None = None
) -> None:
    """Publish NoticePosted to the events channel — frontend adds the notice to
    the unread FYI feed. Best-effort, never raises. `task_id` groups the feed by
    task without a refetch."""
    ev = NoticePosted(
        agent_id=agent_id, notice_id=notice_id, priority=priority, title=title, task_id=task_id
    )
    await publish_best_effort(
        settings.data_plane.events_channel, ev.model_dump_json(), context="notice_posted"
    )


def _force_terminate_transaction(
    agent_id: int,
    db_pool: ConnectionPool,
    *,
    source: str,
    kill_process: bool,
) -> tuple[AgentStatus, int | None, list[str], int]:
    """Fence and mark one explicit kill while holding the agent row lock.

    The newly inserted terminate inbound id is the monotonic intent fence. A
    guarded chat resurrection competing for this row either commits first (and
    this kill follows it) or waits, then observes a fence greater than its
    trigger. When a process should be killed, keep the row lock through the OS
    kill so a post-force chat cannot create a new session in the gap between DB
    intent and session cleanup. The transaction commits on context exit.
    """
    with db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, pid FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        old_status = AgentStatus(row[0])
        pid = row[1]
        page_names = list_open_page_names(conn, agent_id)
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'terminate', %s) RETURNING id",
            (agent_id, source),
        )
        inbound_row = cur.fetchone()
        if inbound_row is None:
            raise RuntimeError("force terminate inbound INSERT returned no id")
        terminate_inbound_id = inbound_row[0]
        cur.execute(
            # termination_source='user': force-kill / a terminate that found the
            # pid already dead. Both are the user's will to end the agent, so it
            # is NOT crash-auto-resurrect-eligible even with a queued inbound.
            "UPDATE agents_meta SET status='terminated', termination_source='user', "
            "heartbeat_paused_until = NULL, last_force_terminate_inbound_id = %s "
            "WHERE id = %s",
            (terminate_inbound_id, agent_id),
        )
        if kill_process:
            agent_session = session_name(f"agent-{agent_id}")
            native_proc().kill_session(agent_session, graceful=False)
            # The exact session name is safe to clear repeatedly. A raw
            # pid can be recycled even while a stale row still says live,
            # so only positive argv identity evidence licenses SIGKILL.
            if (
                pid is not None
                and old_status is not AgentStatus.TERMINATED
                and probe_agent_process(pid, agent_id) is AgentProcessIdentity.OWNED
            ):
                force_kill(pid)
    return old_status, pid, page_names, terminate_inbound_id


def _publish_force_terminate_inbound(agent_id: int, inbound_id: int, source: str) -> None:
    """Emit the non-transactional audit/wake side effects after fence commit."""
    insert_event_log(
        event_type="terminate",
        agent_id=agent_id,
        source=source,
        payload={"inbound_id": inbound_id},
    )
    publish_inbound_wake(agent_id, str(inbound_id))


def _force_mark_terminated(
    agent_id: int, db_pool: ConnectionPool, *, source: str = "user"
) -> list[str]:
    """Fence an explicit zombie kill and return cascade-closed page names."""
    _, _, page_names, inbound_id = _force_terminate_transaction(
        agent_id,
        db_pool,
        source=source,
        kill_process=False,
    )
    _publish_force_terminate_inbound(agent_id, inbound_id, source)
    return page_names


async def mark_agent_exited_op(agent_id: int, db_pool: ConnectionPool) -> list[str]:
    """Finalize a self-exiting agent process: guarded status flip + events.

    POST /api/agents/{id}/exited calls this when an agent reaches its own
    process-exit finally block (graceful terminate or silent death from a
    SIGHUP/SIGTERM). The agent used to do this inline; it now notifies the
    gateway so it never writes agents_meta or reads the pages table itself.

    Guarded `WHERE status IN ('running','idling')` — the same
    invariant the inline version held:
    - 'restarting' is left untouched (reserved for the restarter daemon; a
      restart goes claim -> 'restarting' -> END -> process exit -> this call,
      and clobbering it to 'terminated' would strand the restart).
    - an unclaimed 'idling' row has no process to send this callback; a normal
      pre-claim death is instead handled by the dead-birth reaper.
    - already 'terminated' -> rowcount 0, idempotent (finally may run twice).

    Differs from `_force_mark_terminated` (used by the force-kill / zombie-reap
    paths), which overwrites status unconditionally — that is wrong here
    because the self-exit path must respect an in-flight restart.

    rowcount==1: really transitioned — publish AgentUpdated + the
    agent_terminated event + PageClosed for each cascade-closed page; returns
    those page names.
    rowcount==0: benign skip; debug-log the actual status to tell
    restarting / already-terminated / idling apart.
    other: the PK guarantees <=1 row; anything else is table corruption -> raise.
    """
    rowcount, page_names, actual_status = await asyncio.to_thread(
        _mark_exited_blocking, agent_id, db_pool
    )
    if rowcount == 1:
        logger.info(
            "agent {agent_id} terminated",
            event="agent_terminated",
            agent_id=agent_id,
        )
        for page_name in page_names:
            await publish_page_closed(agent_id, page_name)
        return page_names
    if rowcount == 0:
        logger.debug(
            "agent {agent_id} mark_agent_exited_op noop (actual status={actual_status!r})",
            agent_id=agent_id,
            actual_status=actual_status,
        )
        return []
    raise RuntimeError(
        f"mark_agent_exited_op agent_id={agent_id} rowcount={rowcount} — "
        f"PK invariant violated; agents_meta table integrity broken"
    )


def _mark_exited_blocking(
    agent_id: int, db_pool: ConnectionPool
) -> tuple[int, list[str], str | None]:
    """Sync DB section of mark_agent_exited_op — via to_thread (the status flip,
    the exit event_log row, the AgentUpdated publish). Returns
    (rowcount, page_names, actual_status)."""
    with db_pool.connection() as conn:
        # SELECT cascade-closable show() pages before UPDATE; daemon-supervised
        # serve() pages stay open and must not emit PageClosed.
        page_names = list_open_page_names(conn, agent_id)
        with conn.cursor() as cur:
            cur.execute(
                # termination_source='exit': the agent's own graceful process-exit
                # finalize (self-terminate, or a caught SIGTERM/SIGHUP that ran the
                # exit finally). Intentional — NOT crash-auto-resurrect-eligible. A
                # SIGKILL/OOM leaves no finally, so it never reaches here; the reaper
                # catches that dead pid and stamps 'reaper' instead.
                "UPDATE agents_meta SET status = %s, termination_source = 'exit', "
                "heartbeat_paused_until = NULL, lease_expires_at = NULL "
                "WHERE id = %s AND status IN (%s, %s)",
                (
                    AgentStatus.TERMINATED,
                    agent_id,
                    AgentStatus.RUNNING,
                    AgentStatus.IDLING,
                ),
            )
            rowcount = cur.rowcount
            if rowcount == 1:
                from shared.audit_events import insert_event_log

                insert_event_log(
                    event_type="exit",
                    agent_id=agent_id,
                    source="system",
                )
            actual_status: str | None = None
            if rowcount == 0:
                cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
                row = cur.fetchone()
                actual_status = row[0] if row is not None else None
    if rowcount == 1:
        with db_pool.connection() as conn:
            publish_agent_updated_sync(conn, agent_id)
    return rowcount, page_names, actual_status


async def mark_agent_hibernating_op(agent_id: int, db_pool: ConnectionPool) -> None:
    """Finalize a hibernating agent process: guarded park to 'hibernating'.

    POST /api/agents/{id}/hibernating calls this when an agent's process reaches
    its exit finally block after the hibernation swap-out signal (SIGUSR1). It is
    the swap-out twin of `mark_agent_exited_op`, differing in three deliberate
    ways: the row lands 'hibernating' not 'terminated'; the agent's pages stay
    OPEN (the cascade_close_agent_pages trigger fires only on 'terminated', so a
    hibernating flip leaves them); and NO exit event_log is written (the agent is
    not dying, just swapped out). `heartbeat_paused_until` is deliberately left
    untouched — hibernation (free RAM) is orthogonal to a self-requested pause
    (do-not-nudge); the pause window must survive a swap-out.

    Guarded `WHERE status IN ('running','idling')`:
    - 'idling' is the state the swap-out controller selects; the process was
      parked in the inbound wait when SIGUSR1 landed.
    - 'running' covers the phantom-running the idle-wait's own finally leaves:
      SIGUSR1 delivered during `_wait_for_batch`'s inbound wait unwinds through
      that finally, which flips IDLING->RUNNING before main()'s finally reaches
      here — the same reason `mark_agent_exited_op` accepts 'running'.
    - 'restarting' / 'terminated' / already-'hibernating' -> rowcount 0, benign
      no-op (a concurrent restart / terminate / double-finalize won). An
      unclaimed `idling` row cannot normally send this process callback.

    rowcount==1: publish AgentUpdated so ops/frontend SSE reflect the parked row
    (the frontend projects 'hibernating' -> 'idling', so no visible change). No
    agent_terminated event, no PageClosed. rowcount==0: benign skip. other: the
    PK guarantees <=1 row; anything else is table corruption -> raise.
    """
    rowcount = await asyncio.to_thread(_mark_hibernating_blocking, agent_id, db_pool)
    if rowcount == 1:
        logger.info(
            "agent {agent_id} hibernating",
            event="agent_hibernating",
            agent_id=agent_id,
        )
        return
    if rowcount == 0:
        logger.debug(
            "agent {agent_id} mark_agent_hibernating_op noop (not running/idling)",
            agent_id=agent_id,
        )
        return
    raise RuntimeError(
        f"mark_agent_hibernating_op agent_id={agent_id} rowcount={rowcount} — "
        f"PK invariant violated; agents_meta table integrity broken"
    )


def _mark_hibernating_blocking(agent_id: int, db_pool: ConnectionPool) -> int:
    """Sync DB section of mark_agent_hibernating_op — via to_thread (guarded
    park UPDATE + AgentUpdated publish). Returns the rowcount."""
    with db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s, lease_expires_at = NULL "
            "WHERE id = %s AND status IN (%s, %s)",
            (
                AgentStatus.HIBERNATING,
                agent_id,
                AgentStatus.RUNNING,
                AgentStatus.IDLING,
            ),
        )
        rowcount = cur.rowcount
    if rowcount == 1:
        with db_pool.connection() as conn:
            publish_agent_updated_sync(conn, agent_id)
    return rowcount


# ─── spawn ─────────────────────────────────────────────────────────────────


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
        old_status, pid, killed_page_names = await asyncio.to_thread(
            _terminate_force_blocking, agent_id, body, db_pool
        )
        if old_status is AgentStatus.TERMINATED:
            return TerminateAgentResponse(status="already_terminated")
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


def _terminate_force_blocking(
    agent_id: int, body: TerminateAgentRequest, db_pool: ConnectionPool
) -> tuple[AgentStatus, int | None, list[str]]:
    """Sync force-kill section — via to_thread: session kill + SIGKILL + status
    flip + AgentUpdated + terminate inbound. Returns (old status, pid, page names)."""
    old_status, pid, killed_page_names, inbound_id = _force_terminate_transaction(
        agent_id,
        db_pool,
        source=body.source,
        kill_process=True,
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
