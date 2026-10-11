"""Home-machine launch and supported lifecycle operations, with explicit input wake."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from psycopg_pool import ConnectionPool

from base.agents import (
    AgentStatus,
    InvalidModelConfig,
    MachinePaused,
    RestartResult,
    ResurrectAlreadyAlive,
    ResurrectError,
    ResurrectResult,
    ShellSessionKillTiming,
    TerminateResult,
)
from base.agents.incarnation.native_restart_models import (
    NativeRestartAccepted,
    NativeRestartOperation,
    NativeRestartRefused,
)
from base.agents.messages.inbound import WakeTriggerKind
from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database, insert_inbound_message
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.lm.registry import normalize_overlay_llm_model
from ops.agents import (
    get_agent_machine,
    get_agent_status,
    resurrect_agent,
)
from ops.agents.resurrection_retry import (
    report_auto_resurrect_failure,
    resurrect_refusal_reason,
)
from ops.agents.wake import ResurrectTriggerStaleError
from ops.cluster import rpc as _cluster_rpc
from ops.cluster_status import kill_agent_shells
from ops.lifecycle import termination

# Re-exported from `events`; callers keep module-qualified sites.
from ops.lifecycle.events import (
    publish_inbound_arrived as publish_inbound_arrived,
)
from ops.lifecycle.events import (
    publish_notice_posted as publish_notice_posted,
)
from ops.lifecycle.events import (
    publish_notice_resolved as publish_notice_resolved,
)
from ops.lifecycle.events import (
    publish_page_closed as publish_page_closed,
)

# Re-exported (explicit-alias form) from `launch` so the gateway routers and
# tests keep their module-qualified call sites.
from ops.lifecycle.launch import (
    launch_agent_op as launch_agent_op,
)

# Re-exported (explicit-alias form) from `resurrect_gates` so the
# module-qualified callers — tests — keep their call sites.
from ops.lifecycle.resurrect_gates import (
    clear_wake_suppression as _clear_wake_suppression,
)
from ops.lifecycle.resurrect_gates import (
    recovery_halted as _recovery_halted,
)
from ops.lifecycle.resurrect_gates import (
    system_notice_source_of_trigger as _system_notice_source_of_trigger,
)
from ops.lifecycle.termination import (
    _force_terminate_transaction as _force_terminate_transaction,
)
from ops.lifecycle.termination import (
    _publish_force_terminate_inbound as _publish_force_terminate_inbound,
)
from ops.lifecycle.termination import (
    force_mark_terminated as force_mark_terminated,
)
from ops.rpc_schemas import (
    RestartAgentRequest,
    RestartAgentResponse,
    ResurrectAgentRequest,
    ResurrectAgentResponse,
    TerminateAgentRequest,
    TerminateAgentResponse,
)
from ops.rpc_schemas.terminate import ShellSessionsKill

_log = logging.getLogger(__name__)


async def terminate_agent_op(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: TerminateAgentRequest,
    db_pool: ConnectionPool,
) -> TerminateAgentResponse:
    """Local-target graceful or force terminate. Caller handles cross-machine.

    `body.kill_all_shell_sessions` also kills the agent's shell sessions on
    this, its home machine (docs/decisions/agents/lifecycle/2026-09-27-terminate-has-no-closed-state.md).
    A force terminate kills them right after its fence commits, and so does a
    terminate that finds the agent already terminated. A graceful terminate of
    a live agent only records the request on its terminate command: the home
    runtime attempts older sessions after the termination commits, after the
    agent's last step (`agent.ownership.hosted.apply_hosted_lifecycle`). The
    response's `shell_sessions` reports which of the two happened.

    """
    if body.force:
        _old_status, pid, killed_page_names, command_id, cutoff = await asyncio.to_thread(
            _terminate_force_blocking, db, bus, agent_id, body, db_pool
        )
        await _cancel_hosted_turn_best_effort(agent_id, command_id)
        for page_name in killed_page_names:
            await publish_page_closed(bus, agent_id, page_name)
        _log.info(
            "[gateway] agent %s force requested by %s (pid=%s)",
            agent_id,
            body.source,
            pid,
        )
        # Neither HTTP delivery nor Task.cancel proves resource quiescence.
        return TerminateAgentResponse(
            status=TerminateResult.ENQUEUED,
            shell_sessions=await _kill_shell_sessions_now(
                agent_id, cutoff=cutoff if body.kill_all_shell_sessions else None
            ),
        )

    if (
        not body.kill_all_shell_sessions
        and (await asyncio.to_thread(get_agent_status, db, agent_id)) is AgentStatus.TERMINATED
    ):
        return TerminateAgentResponse(
            status=TerminateResult.ALREADY_TERMINATED,
        )

    iid, cutoff = await asyncio.to_thread(
        _terminate_graceful_blocking, db, bus, agent_id, body, db_pool
    )
    if iid is None:
        # The kill-requesting enqueue found the row terminated under its lock.
        return TerminateAgentResponse(
            status=TerminateResult.ALREADY_TERMINATED,
            shell_sessions=await _kill_shell_sessions_now(agent_id, cutoff=cutoff),
        )
    await publish_inbound_arrived(bus, agent_id, iid, "terminate", body.source, "")
    return TerminateAgentResponse(
        status=TerminateResult.ENQUEUED,
        shell_sessions=ShellSessionsKill(when=ShellSessionKillTiming.AT_EXIT)
        if body.kill_all_shell_sessions
        else None,
    )


async def _kill_shell_sessions_now(
    agent_id: int, *, cutoff: int | None
) -> ShellSessionsKill | None:
    """Attempt this host's older shells when a cutoff was captured; report them.

    A failed kill propagates: the termination itself is already durable, so
    the caller sees the failure and a repeat request retries the kill.
    """
    if cutoff is None:
        return None
    killed = await asyncio.to_thread(kill_agent_shells, agent_id, before_session_index=cutoff)
    _log.info(
        "[gateway] agent %s terminate killed %d shell session(s): %s",
        agent_id,
        len(killed),
        killed,
    )
    return ShellSessionsKill(when=ShellSessionKillTiming.NOW, killed=killed)


async def _cancel_hosted_turn_best_effort(agent_id: int, command_id: int) -> None:
    """Accelerate a hosted force-terminate by cancelling the agent's turn task.

    Dials the local agent-host's loopback health port (`POST /cancel-turn`);
    the host cancels the turn with its bounded unwind and reports a
    C-call-blocked straggler instead of hanging. A transport or HTTP-status failure
    is logged at WARNING with the exception and swallowed. The durable force pointer stays unobserved until
    the original host actually settles; a dead host is not child-exit proof.
    """
    import httpx

    from base.daemon.endpoints import ServiceEndpoints

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
            resp = await client.post(
                f"http://127.0.0.1:{ServiceEndpoints.from_settings().of('agent_host').health_port}/cancel-turn",
                json={"agent_id": agent_id, "command_id": command_id},
            )
            resp.raise_for_status()
    except httpx.HTTPError:
        _log.warning(
            "hosted turn-cancel call for agent %s failed (non-fatal; the durable "
            "force command remains accepted; quiescence is not confirmed)",
            agent_id,
            exc_info=True,
        )


def _terminate_force_blocking(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: TerminateAgentRequest,
    db_pool: ConnectionPool,
) -> tuple[AgentStatus, int | None, list[str], int, int]:
    """Commit the force fence before publishing its wake and lifecycle hint."""
    old_status, pid, killed_page_names, inbound_id, cutoff = _force_terminate_transaction(
        agent_id,
        db_pool,
        source=body.source,
        message=body.message,
        kill_all_shell_sessions=body.kill_all_shell_sessions,
    )
    _publish_force_terminate_inbound(db, bus, agent_id, inbound_id, body.source)
    publish_agent_updated_sync(bus, agent_id)
    return old_status, pid, killed_page_names, inbound_id, cutoff


def _terminate_graceful_blocking(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: TerminateAgentRequest,
    db_pool: ConnectionPool,
) -> tuple[int | None, int | None]:
    """Return a durable command ID, or an already-terminated shell cutoff."""
    return termination._enqueue_termination_inbounds(
        db,
        bus,
        agent_id,
        db_pool,
        source=body.source,
        message=body.message,
        kill_all_shell_sessions=body.kill_all_shell_sessions,
    )


async def resurrect_agent_op(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: ResurrectAgentRequest,
    *,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: WakeTriggerKind | None = None,
) -> ResurrectAgentResponse:
    """Commit hosted resurrection intent, then publish a wake to the home host.

    `trigger_inbound_id` carries the internal auto-resurrect CAS to the home
    runner; manual resurrects omit it and may reopen a closed hosted agent.
    """
    s = await asyncio.to_thread(get_agent_status, db, agent_id)
    if s is not AgentStatus.TERMINATED:
        return ResurrectAgentResponse(status=ResurrectResult.ALREADY_ALIVE)
    try:
        # Keep the synchronous row-lock transaction and wake publication off
        # the gateway event loop. The agent host admits the successor later.
        await asyncio.to_thread(
            resurrect_agent,
            db,
            bus,
            agent_id,
            resurrected_by=body.resurrected_by,
            prompt=body.prompt,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
    except (ResurrectAlreadyAlive, ResurrectTriggerStaleError):
        return ResurrectAgentResponse(status=ResurrectResult.ALREADY_ALIVE)
    return ResurrectAgentResponse(status=ResurrectResult.SPAWNED)


async def resurrect_if_terminated(
    db: Database,
    bus: EventBus,
    agent_id: int,
    *,
    trigger_inbound_id: int,
    trigger_inbound_kind: WakeTriggerKind,
) -> AgentStatus:
    """Try one home-machine wake for an exact newly delivered input.

    Fresh chat can wake a terminated agent after a fix. Background heartbeat and
    other automatic triggers stop after a permanent provider rejection. System
    chat notices cannot resurrect, including old hosted-turn recovery markers.
    The home CAS verifies the pending input is newer than termination and above
    the force fence. No watchdog retries a failed attempt. Unknown errors reach
    the sender; known placement/refusal conditions are logged with the input queued.
    A new message or explicit operator resurrection can try again later."""
    status = await asyncio.to_thread(get_agent_status, db, agent_id)
    if status is not AgentStatus.TERMINATED:
        return status
    if trigger_inbound_kind != "chat" and await asyncio.to_thread(_recovery_halted, db, agent_id):
        _log.warning("automatic wake stopped after a permanent provider error: agent %s", agent_id)
        return status
    notice_source = await asyncio.to_thread(
        _system_notice_source_of_trigger, db, agent_id, trigger_inbound_id
    )
    if notice_source is not None:
        _log.debug(
            "resurrect_if_terminated: trigger %s for agent %s is a system notice "
            "(source=%s); notifications never resurrect a terminated owner — "
            "skipping auto-resurrect",
            trigger_inbound_id,
            agent_id,
            notice_source,
        )
        return status
    body = ResurrectAgentRequest(resurrected_by="system")
    try:
        home = await asyncio.to_thread(get_agent_machine, db, agent_id)
        try:
            lifecycle_payload: dict[str, object] = {
                "path": f"/api/agents/{agent_id}/resurrect-if-pending-work",
                "body": body.model_dump(),
                "trigger_inbound_id": trigger_inbound_id,
                "trigger_inbound_kind": trigger_inbound_kind,
            }
            forwarded = await _cluster_rpc.dispatch_to_machine(
                db,
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
                        db,
                        bus,
                        agent_id,
                        body,
                        trigger_inbound_id=trigger_inbound_id,
                        trigger_inbound_kind=trigger_inbound_kind,
                    )
                ).status
            else:
                raise
        if result_status == ResurrectResult.SPAWNED:
            await asyncio.to_thread(_clear_wake_suppression, db, agent_id)
            status = await asyncio.to_thread(get_agent_status, db, agent_id)
    except _cluster_rpc.ClusterOpUnreachable as exc:
        _log.info(
            "resurrect_if_terminated: agent %s home machine unreachable, skipping "
            "auto-resurrect (%s); inbound queued — the next delivery or a manual "
            "resurrect picks it up once the machine is back",
            agent_id,
            exc,
        )
    except (ResurrectError, MachinePaused) as exc:
        report_auto_resurrect_failure(agent_id, exc)
    except _cluster_rpc.ClusterOpFailed as exc:
        if resurrect_refusal_reason(exc) is None:
            raise
        report_auto_resurrect_failure(agent_id, exc)
    return status


def _restart_blocking(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: RestartAgentRequest,
    db_pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
) -> int | None:
    """Sync restart section — via to_thread. Returns the inbound id, or None
    when the agent is already terminated."""
    from psycopg import sql

    from base.agents.incarnation.lifecycle_acceptance import FAILED_RESTART_FOR_CURRENT_TARGET
    from base.db.transaction import write_transaction
    from base.packages.plugins.config_registration import (
        InvalidConfigOverlay,
        validate_config_overlay,
    )

    if body.config_overlay:
        try:
            validate_config_overlay(
                body.config_overlay,
                models=catalog.models,
                decision_models=catalog.decision_models,
            )
        except InvalidConfigOverlay as exc:
            raise InvalidModelConfig(str(exc)) from exc

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
            from base.lm.model_config import validate_restart_model_config

            # Settle a withdrawn llm_model before the overlay is stored — the
            # provider-outage model-switch channel must not persist a stale id (#4306).
            overlay = dict(body.config_overlay)
            model_receipt = normalize_overlay_llm_model(overlay, models=catalog.models)
            with conn.cursor() as cur:
                validate_restart_model_config(
                    cur,
                    agent_id,
                    overlay,
                    catalog=catalog,
                    llm_override=settings.lm.llm_override,
                    default_model=settings.lm.llm_model,
                )
            if model_receipt is not None:
                _log.warning(
                    "restart overlay llm_model %r is withdrawn; storing the "
                    "registered fallback %r (task #4306)",
                    model_receipt[0],
                    model_receipt[1],
                )
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
                (json.dumps(overlay), agent_id),
            )
            payload = {"config_overlay": overlay}
        # insert_inbound_message commits this connection, so its INSERT commits
        # the preceding overlay UPDATE in the same transaction.
        return insert_inbound_message(
            conn,
            agent_id,
            "",
            source=body.source,
            kind="restart",
            payload=payload,
            database=db,
            bus=bus,
        )


async def restart_agent_op(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: RestartAgentRequest,
    db_pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
) -> RestartAgentResponse:
    """Local-target restart — INSERT one kind='restart' inbound."""
    iid = await asyncio.to_thread(
        _restart_blocking, db, bus, agent_id, body, db_pool, catalog=catalog
    )
    if iid is None:
        return RestartAgentResponse(status=RestartResult.ALREADY_TERMINATED)
    await publish_inbound_arrived(bus, agent_id, iid, "restart", body.source, "")
    return RestartAgentResponse(status=RestartResult.ENQUEUED)


_LIFECYCLE_PATH = re.compile(
    r"^/api/agents/(?P<id>\d+)/"
    r"(?P<action>terminate|resurrect-explicit|"
    r"resurrect-if-pending-work|restart|restart-work)$"
)


async def lifecycle_op(
    db: Database,
    bus: EventBus,
    path: str,
    body: dict[str, Any],
    db_pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
    trigger_inbound_id: int | None = None,
    trigger_inbound_kind: WakeTriggerKind | None = None,
) -> (
    TerminateAgentResponse
    | ResurrectAgentResponse
    | RestartAgentResponse
    | NativeRestartAccepted
    | NativeRestartRefused
):
    """Dispatch terminate, restart and explicit or exact-pending-input resurrection.

    Retired billing recovery and crash-harvest paths fail validation without effects.
    Unknown paths raise ValueError, which the ops server exposes as a failed result."""
    m = _LIFECYCLE_PATH.match(path)
    if m is None:
        raise ValueError(f"lifecycle path not recognized: {path!r}")
    agent_id = int(m.group("id"))
    action = m.group("action")
    if (trigger_inbound_id is None) != (trigger_inbound_kind is None):
        raise ValueError("trigger inbound id and kind must be provided together")
    if action != "resurrect-if-pending-work" and trigger_inbound_id is not None:
        raise ValueError(
            f"trigger inbound is only valid for resurrect-if-pending-work, not {action!r}"
        )
    if action == "restart-work":
        from ops.lifecycle.native_restart import restart_native_work_op

        return await restart_native_work_op(
            db, bus, agent_id, NativeRestartOperation.model_validate(body), db_pool, catalog=catalog
        )
    if action == "terminate":
        return await terminate_agent_op(
            db, bus, agent_id, TerminateAgentRequest.model_validate(body), db_pool
        )
    if action == "resurrect-explicit":
        return await resurrect_agent_op(
            db, bus, agent_id, ResurrectAgentRequest.model_validate(body)
        )
    if action == "resurrect-if-pending-work":
        request = _pending_work_resurrection_request(body, trigger_inbound_id)
        return await resurrect_agent_op(
            db,
            bus,
            agent_id,
            request,
            trigger_inbound_id=trigger_inbound_id,
            trigger_inbound_kind=trigger_inbound_kind,
        )
    if action == "restart":
        return await restart_agent_op(
            db, bus, agent_id, RestartAgentRequest.model_validate(body), db_pool, catalog=catalog
        )
    raise AssertionError(f"unreachable: action={action!r}")


def _pending_work_resurrection_request(
    body: dict[str, Any], trigger_inbound_id: int | None
) -> ResurrectAgentRequest:
    """Keep the guarded resurrection boundary independent of action routing."""
    if trigger_inbound_id is None:
        raise ValueError("resurrect-if-pending-work requires trigger inbound")
    request = ResurrectAgentRequest.model_validate(body)
    if request.resurrected_by != "system":
        raise ValueError("resurrect-if-pending-work requires resurrected_by='system'")
    return request
