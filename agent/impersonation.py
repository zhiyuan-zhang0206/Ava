"""Cooperative takeover at the native runtime's durable invocation boundary.

The external holder never writes a graph checkpoint. Accepted requests stop
native nodes; only the invocation driver activates after exec closure and the
checkpointer flush. A database lease gates every subsequent invocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent import impersonation_handoff as _handoff
from agent import impersonation_process as _relay_process
from agent import state as _state
from agent.messages import inbound_message
from agent.nodes import BEFORE_LLM, END, NodeName
from base.agents.context import AvaContext, agent_id_from_config
from base.agents.impersonation.notes import HandoffNotes
from base.agents.impersonation.relay import record_relay_failure
from base.agents.impersonation.relay import stamp_relay_failure as _stamp_relay_failure
from base.agents.impersonation.status import OPEN, ImpersonationStatus
from base.agents.messages.envelope import EnvelopeReadInputs, inbound_head
from base.agents.observation.relay_supervision import RelayChild, RelaySupervision, relay_exited
from base.agents.observation.relay_supervision import heartbeat_fresh as _heartbeat_fresh
from base.agents.observation.relay_supervision import terminate_relay as _terminate_relay
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources, hosted_resources_settled


async def native_status(
    db: Database, bus: EventBus, agent_id: int, *, incarnation: RuntimeIncarnation | None
) -> dict[str, Any] | None:
    """Read authoritative lease state only for an admitted native runtime."""
    if incarnation is None:
        return None
    from base.agents.impersonation import native_status as read_status

    return await asyncio.to_thread(read_status, db, bus, agent_id, incarnation)


async def lifecycle_ready(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Native restart/terminate can run while the external owner handles cancel."""
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM inbound_messages i WHERE i.agent_id=%s "
            "AND i.kind IN ('restart','terminate') AND (i.status='pending' "
            "OR (i.status='claimed' AND EXISTS (SELECT 1 FROM agents_meta m "
            "WHERE m.id=i.agent_id AND m.lifecycle_command_id=i.id))) LIMIT 1",
            (agent_id,),
        )
        return await cursor.fetchone() is not None


async def active_lease(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Pre-admission host gate; accepted rows must reach a durable boundary."""
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM agent_impersonations WHERE agent_id=%s "
            "AND status='active' AND expires_at>clock_timestamp() LIMIT 1",
            (agent_id,),
        )
        return await cursor.fetchone() is not None


async def claim_gate(
    state: _state.AgentState, agent_id: int, ctx: AvaContext
) -> Command[NodeName] | None:
    """Present consent once, or end the invocation without claiming input."""
    incarnation = ctx.original_incarnation
    db, bus = ctx.require_db(), ctx.require_bus()
    session = await native_status(db, bus, agent_id, incarnation=incarnation)
    await supervise_relay(
        db,
        bus,
        session,
        agent_id,
        ctx.relays,
        incarnation=incarnation,
    )
    if session is None:
        return None
    if session["status"] == ImpersonationStatus.REQUESTED and session["automatic"]:
        from base.agents.impersonation import accept

        assert incarnation is not None  # noqa: S101 — native_status requires it
        brief = session["reason"] or "Continue the agent's work from its saved state."
        await asyncio.to_thread(
            accept,
            db,
            bus,
            session["id"],
            agent_id,
            incarnation,
            brief,
            local_machine=ctx.require_machine().name(),
        )
        return Command(
            update={
                **_handoff.start_update(
                    session,
                    introduced=state.impersonation_introduced,
                    notes=HandoffNotes(
                        ctx.require_clock,
                        lambda: ctx.require_agent().read("general", "message_timestamps"),
                    ),
                ),
                "turn_active": False,
                "turn_idle": True,
            },
            goto=END,
        )
    if session["status"] == ImpersonationStatus.REQUESTED:
        request_receipt = f"{session['id']}:{session['consent_version']}"
        if state.impersonation_request_id == request_receipt:
            return None
        request_id = session["id"]
        content = (
            f"External agent requests to impersonate you. Request: {request_id}.\n"
            f"Reason: {session['reason']}\n"
            "To accept, save your working state and call "
            f"ava.impersonation.accept({request_id!r}, start_message='your handoff "
            "brief for the external session'); this ends your execution. The brief "
            "is delivered to the external session when the takeover starts and is "
            "required: cover the current work, the context it needs, and how to "
            "acknowledge incoming messages. "
            f"To decline, call ava.impersonation.reject({request_id!r}, reason=...). "
            "Acceptance pauses your native loop until release or lease expiry."
        )
        head = inbound_head(
            session["source"],
            inputs=EnvelopeReadInputs(
                ctx.require_clock, lambda: ctx.require_agent().read("general", "message_timestamps")
            ),
        )
        message = inbound_message(
            message_id=f"impersonation-request:{request_receipt}",
            content=head + content,
            source=session["source"],
            inbound_id=None,
            body_start=len(head),
            created_at=datetime.now(UTC),
        )
        return Command(
            update={
                "messages": [message],
                "impersonation_request_id": request_receipt,
                "halted": False,
                "turn_active": True,
            },
            goto=BEFORE_LLM,
        )
    # Terminal rows with an unapplied delta also stop here: the invocation
    # driver applies the ordered log before allowing another model decision.
    return Command(update={"turn_active": False, "turn_idle": True}, goto=END)


def protect_native_hooks(
    runner: Callable[..., Awaitable[Command[NodeName]]],
) -> Callable[..., Awaitable[Command[NodeName]]]:
    """Fence automatic compaction and plugin hooks before the LLM node."""

    async def guarded(
        state: _state.AgentState, runtime: Runtime[AvaContext], config: RunnableConfig
    ) -> Command[NodeName]:
        if runtime.context.ops_pool is not None:
            session = await native_status(
                runtime.context.require_db(),
                runtime.context.require_bus(),
                agent_id_from_config(config),
                incarnation=runtime.context.original_incarnation,
            )
            if session is not None and session["status"] != ImpersonationStatus.REQUESTED:
                return Command(update={"turn_idle": True}, goto=END)
        return await runner(state, runtime, config)

    return guarded


async def _activate_accepted(
    graph: CompiledStateGraph[
        _state.BaseAgentState, AvaContext, _state.BaseAgentState, _state.BaseAgentState
    ],
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    resources: HostedTurnResources | None,
    notes: HandoffNotes,
    read_local_machine: Callable[[], str],
) -> dict[str, Any] | None:
    """Activate an accepted lease; None when the relay gate failed and native control resumes."""
    from base.agents.impersonation import activate

    # Continuations and managed exec resources must settle before activation.
    if not hosted_resources_settled(resources):
        raise RuntimeError("cannot activate impersonation with unresolved native exec resources")
    await _handoff.ensure_start_marker(graph, session, notes=notes)
    # The bound relay must be live before the takeover stands. On failure
    # the lease is rolled back to 'rejected' with a loud reason and the
    # native agent resumes — no silent half-takeover.
    if not await asyncio.to_thread(
        establish_relay,
        db,
        bus,
        session,
        incarnation,
        relays,
        read_local_machine=read_local_machine,
    ):
        return None
    return await asyncio.to_thread(activate, db, bus, session["id"], incarnation)


async def _apply_plugin_deltas(
    graph: CompiledStateGraph[
        _state.BaseAgentState, AvaContext, _state.BaseAgentState, _state.BaseAgentState
    ],
    db: Database,
    session: dict[str, Any],
    agent_id: int,
    incarnation: RuntimeIncarnation,
) -> None:
    """Apply each unapplied plugin delta exactly once, recording its receipt."""
    from agent.extensions.plugin_state_codec import decode_plugin_delta
    from base.agents.impersonation import mark_plugin_applied

    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    snapshot = await graph.aget_state(config)
    receipt = snapshot.values.get("impersonation_applied", {})
    for version in range(session["applied_version"] + 1, session["delta_version"] + 1):
        expected = {"lease_id": session["id"], "version": version}
        recorded_version = (
            receipt.get("version", 0) if receipt.get("lease_id") == session["id"] else 0
        )
        if recorded_version < version:
            delta = decode_plugin_delta(
                session["plugin_delta"][version - 1], graph.builder.state_schema
            )
            await graph.aupdate_state(config, {**delta, "impersonation_applied": expected})
            await _handoff.flush_checkpoint(graph.checkpointer, agent_id)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            receipt = expected
        await asyncio.to_thread(mark_plugin_applied, db, session["id"], version, incarnation)


async def settle_checkpoint(
    graph: CompiledStateGraph[
        _state.BaseAgentState, AvaContext, _state.BaseAgentState, _state.BaseAgentState
    ],
    db: Database,
    bus: EventBus,
    agent_id: int,
    relays: RelaySupervision,
    *,
    activate_accepted: bool = True,
    incarnation: RuntimeIncarnation | None,
    resources: HostedTurnResources | None,
    notes: HandoffNotes,
    read_local_machine: Callable[[], str],
) -> bool:
    """After invocation+flush, activate; apply terminal deltas exactly once."""
    session = await native_status(db, bus, agent_id, incarnation=incarnation)
    if session is None or session["status"] == ImpersonationStatus.REQUESTED:
        return False
    assert incarnation is not None  # noqa: S101 — native_status requires it
    if session["status"] == ImpersonationStatus.ACCEPTED:
        if not activate_accepted:
            return False
        activated = await _activate_accepted(
            graph,
            db,
            bus,
            session,
            incarnation,
            relays,
            resources=resources,
            notes=notes,
            read_local_machine=read_local_machine,
        )
        if activated is None:
            return False
        session = activated
    if session["status"] == ImpersonationStatus.ACTIVE:
        return True
    await _apply_plugin_deltas(graph, db, session, agent_id, incarnation)
    if session["automatic"] and session["handoff_applied_at"] is None:
        from base.agents.impersonation import aborted_detail

        # A supervisor-aborted lease (task #3998) carries its death cause as
        # "aborted: <detail>" in rejection_reason; the end note names it.
        await _handoff.deliver_handoff(
            graph,
            db,
            bus,
            session,
            incarnation,
            notes=notes,
            reason=aborted_detail(session.get("rejection_reason")),
        )
    return False


# ── Bound relay process: establishment, supervision, teardown ─────────────────

_RELAY_READY_TIMEOUT_S = 30.0
_RELAY_READY_POLL_S = 1.0


def establish_relay(
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    read_local_machine: Callable[[], str],
) -> bool:
    """Activation gate: the bound relay must be up before the takeover stands.

    codex — provision the scoped credential, spawn the relay here, and wait for
    its first heartbeat. claude / dsh — the relay runs inside the controller's
    own session; require a fresh heartbeat instead. Any failure rolls the lease to
    'rejected' with a loud reason (fail_acceptance) and returns False; native
    control resumes. A lease that is no longer 'accepted' returns False without
    a transition — someone else already ended it.
    """
    from base.agents.impersonation import SESSION_RELAY_PROVIDERS, fail_acceptance

    provider = session["relay_provider"]
    if provider in SESSION_RELAY_PROVIDERS:
        return _await_session_relay(db, bus, session, incarnation)
    if provider != "codex":
        # Defensive: accept() already rejects leases without a relay binding.
        return _roll_back_relay_failure(
            db, bus, session, incarnation, fail_acceptance, "request has no relay binding"
        )
    return _establish_codex_relay(
        db, bus, session, incarnation, relays, read_local_machine=read_local_machine
    )


def _await_session_relay(
    db: Database, bus: EventBus, session: dict[str, Any], incarnation: RuntimeIncarnation
) -> bool:
    """claude / dsh: the relay runs in the controller's own session; require a fresh heartbeat."""
    from base.agents.impersonation import fail_acceptance

    if session["automatic"]:
        from base.agents.impersonation import native_status as read_status

        deadline = time.monotonic() + _RELAY_READY_TIMEOUT_S
        while not _heartbeat_fresh(session["relay_heartbeat_at"]) and time.monotonic() < deadline:
            time.sleep(_RELAY_READY_POLL_S)
            latest = read_status(db, bus, incarnation.agent_id, incarnation)
            if (
                latest is None
                or latest["id"] != session["id"]
                or latest["status"] != ImpersonationStatus.ACCEPTED
            ):
                return False
            session = latest
    if _heartbeat_fresh(session["relay_heartbeat_at"]):
        return True
    return _roll_back_relay_failure(
        db,
        bus,
        session,
        incarnation,
        fail_acceptance,
        "the controller relay is not running (no fresh heartbeat)",
    )


def _establish_codex_relay(
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    read_local_machine: Callable[[], str],
) -> bool:
    """codex: provision the scoped credential, spawn the relay here, wait for its first heartbeat."""
    from base.agents.impersonation import (
        ImpersonationError,
        fail_acceptance,
        provision_relay,
        relay_get,
    )

    relay_token = secrets.token_urlsafe(32)
    try:
        minted = provision_relay(db, session["id"], incarnation, relay_token)
        if minted is None:
            return False
    except ImpersonationError:
        return False  # the lease is no longer native-held accepted state
    child = RelayChild(
        session["id"],
        _relay_process.spawn_codex_relay(
            incarnation.agent_id,
            session["id"],
            relay_token,
            session["relay_thread_id"],
            session["relay_codex_remote"],
            session["process_metadata"].get("codex_home"),
        ),
        relay_token,
        time.monotonic(),
        minted["relay_generation"],
    )
    relays.children[incarnation.agent_id] = child
    deadline = time.monotonic() + _RELAY_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if child.process.poll() is not None:
            relays.children.pop(incarnation.agent_id, None)
            return _roll_back_relay_failure(
                db,
                bus,
                session,
                incarnation,
                fail_acceptance,
                f"relay process exited during startup (exit code {child.process.returncode})",
            )
        try:
            latest = relay_get(
                db, bus, session["id"], relay_token, local_machine=read_local_machine()
            )
        except ImpersonationError:
            # Token mismatch means a re-provision raced in; treat as ended here.
            return _roll_back_relay_failure(
                db,
                bus,
                session,
                incarnation,
                fail_acceptance,
                "relay credential was re-provisioned",
            )
        if latest["status"] != ImpersonationStatus.ACCEPTED:
            return False  # released/expired/rejected while starting; no relay needed
        if _heartbeat_fresh(latest["relay_heartbeat_at"]):
            return True
        time.sleep(_RELAY_READY_POLL_S)
    _terminate_relay(child)
    relays.children.pop(incarnation.agent_id, None)
    return _roll_back_relay_failure(
        db, bus, session, incarnation, fail_acceptance, "relay did not become ready in time"
    )


def _roll_back_relay_failure(
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    fail_acceptance: Callable[[Database, EventBus, str, RuntimeIncarnation, str], dict[str, Any]],
    reason: str,
) -> bool:
    from base.agents.impersonation import ImpersonationError

    with contextlib.suppress(ImpersonationError):
        fail_acceptance(db, bus, session["id"], incarnation, reason)  # already ended: no-op
    return False


def _provider_anchor_states(process_metadata: object) -> list[str]:
    """The lease's recorded provider anchors classified against the live table."""
    from base.agents.impersonation import provider_anchor_states

    return provider_anchor_states(process_metadata)


async def _abort_for_death(
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    agent_id: int,
    component: str,
    detail: str,
    *,
    incarnation: RuntimeIncarnation | None,
) -> bool:
    """Stop the lease after a core-component death; emits impersonation_aborted."""
    from base.agents.impersonation import ImpersonationError, abort_lease

    if incarnation is None:
        return False
    try:
        ended = await asyncio.to_thread(abort_lease, db, bus, session["id"], incarnation, detail)
    except (ImpersonationError, RuntimeError):
        return False
    if ended is None:
        return False
    from base.log import logger

    logger.warning(
        "impersonation stopped after a core-component death: {detail}",
        event="impersonation_aborted",
        agent_id=agent_id,
        lease_id=str(session["id"]),
        session_id=session.get("session_id"),
        component=component,
        detail=detail,
    )
    return True


async def supervise_relay(
    db: Database,
    bus: EventBus,
    session: dict[str, Any] | None,
    agent_id: int,
    relays: RelaySupervision,
    *,
    incarnation: RuntimeIncarnation | None,
) -> None:
    """Supervise executor authority and observe relay delivery.

    Only confirmed executor death ends a valid lease. Uncertain evidence keeps
    the original TTL unchanged. A stale or exited relay is recorded as degraded
    and never replaced: the lease runs to its TTL or the executor's death. When
    the host first sees its relay's lease ended, it submits the ended-lease
    notice once.
    """
    child = relays.children.get(agent_id)
    if (
        child is not None
        and session is not None
        and (child.lease_id, child.generation) != (session["id"], session["relay_generation"])
    ):
        return  # A delayed predecessor snapshot cannot retire a replacement's sender.
    if session is None or session["status"] not in OPEN:
        if child is not None:
            with db.connect() as conn:
                terminal = conn.execute(
                    "SELECT status FROM agent_impersonations WHERE id=%s", (child.lease_id,)
                ).fetchone()
            if terminal is not None and ImpersonationStatus(terminal[0]) not in OPEN:
                if relays.children.get(agent_id) is child:
                    relays.children.pop(agent_id, None)
                await asyncio.to_thread(_terminate_relay, child)
                await _send_ended_notice(db, agent_id, child.lease_id)
        return
    if session["status"] != ImpersonationStatus.ACTIVE:
        return
    if await _executor_verdict_stops(db, bus, session, agent_id, relays, incarnation=incarnation):
        return
    exited = relay_exited(child)
    if not exited and _heartbeat_fresh(session["relay_heartbeat_at"]):
        return
    await _record_stale_relay(
        db, session, agent_id, child, confirmed_exit=exited, incarnation=incarnation
    )


async def _executor_verdict_stops(
    db: Database,
    bus: EventBus,
    session: dict[str, Any],
    agent_id: int,
    _relays: RelaySupervision,
    *,
    incarnation: RuntimeIncarnation | None,
) -> bool:
    """Component A: the executor's recorded process chain. True when supervision ends here."""
    states = _provider_anchor_states(session.get("process_metadata")) or ["unknown"]
    if "alive" in states:
        return False
    if set(states) <= {"dead", "reused"}:
        await _abort_for_death(
            db,
            bus,
            session,
            agent_id,
            "executor",
            "the executor process is gone",
            incarnation=incarnation,
        )
        return True
    from base.agents.impersonation.relay import record_degradation

    if incarnation is not None:
        await asyncio.to_thread(
            record_degradation,
            db,
            session["id"],
            incarnation,
            "executor liveness unknown; original lease TTL remains authoritative",
        )
    return True


async def _record_stale_relay(
    db: Database,
    session: dict[str, Any],
    agent_id: int,
    child: RelayChild | None,
    *,
    confirmed_exit: bool = False,
    incarnation: RuntimeIncarnation | None,
) -> None:
    """Record a stale or exited relay as degraded; nothing replaces it.

    A relay still inside its startup window is left alone. The lease keeps its
    original TTL: messages stay in the inbox and the liveness alert names the
    gap until the controller re-requests or releases.
    """
    from base.agents.impersonation.relay import record_degradation

    if incarnation is None:
        return
    if (
        not confirmed_exit
        and child is not None
        and child.process.poll() is None
        and time.monotonic() - child.spawned_at < _RELAY_READY_TIMEOUT_S
    ):
        return
    minted_at = session.get("relay_minted_at")
    if (
        not confirmed_exit
        and minted_at is not None
        and (datetime.now(UTC) - minted_at).total_seconds() < _RELAY_READY_TIMEOUT_S
    ):
        return
    await asyncio.to_thread(
        record_degradation,
        db,
        session["id"],
        incarnation,
        "relay unavailable; automatic recovery unsupported",
    )
    await asyncio.to_thread(
        _stamp_relay_failure,
        db,
        session,
        agent_id,
        record_relay_failure,
        incarnation=incarnation,
    )


async def _send_ended_notice(db: Database, agent_id: int, lease_id: str) -> None:
    """Submit the ended lease's notice once; a failure is logged and never retried."""
    from base.agents.impersonation.terminal_notices import send_ended_notice
    from base.log import logger

    error = await asyncio.to_thread(send_ended_notice, db, lease_id)
    if error is not None:
        logger.error(
            "ended impersonation lease notice was not delivered and will not be retried",
            event="impersonation_terminal_notice_failed",
            agent_id=agent_id,
            lease_id=str(lease_id),
            error=error,
        )
