"""Cooperative takeover at the native runtime's durable invocation boundary.

The external holder never writes a graph checkpoint. Accepted requests stop
native nodes; only the invocation driver activates after exec closure and the
checkpointer flush. A database lease gates every subsequent invocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent import state as _state
from agent.nodes import BEFORE_LLM, END, NodeName
from shared.context import AvaContext, agent_id_from_config
from shared.envelope import wrap_inbound
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation
from shared.turn_identity import hosted_resources_settled


async def native_status(agent_id: int) -> dict[str, Any] | None:
    """Read authoritative lease state only for an admitted native runtime."""
    incarnation = current_incarnation(agent_id)
    if incarnation is None:
        return None
    from shared.impersonation import native_status as read_status

    return await asyncio.to_thread(read_status, agent_id, incarnation)


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
    state: _state.AgentState, agent_id: int, _ctx: AvaContext | None = None
) -> Command[NodeName] | None:
    """Present consent once, or end the invocation without claiming input."""
    session = await native_status(agent_id)
    await supervise_relay(session, agent_id)
    if session is None:
        return None
    if session["status"] == "requested":
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
        message = HumanMessage(
            id=f"impersonation-request:{request_receipt}",
            content=wrap_inbound(content, session["source"]),
            additional_kwargs={"ava_msg_type": "inbound", "ava_source": session["source"]},
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
            session = await native_status(agent_id_from_config(config))
            if session is not None and session["status"] != "requested":
                return Command(update={"turn_idle": True}, goto=END)
        return await runner(state, runtime, config)

    return guarded


async def flush_checkpoint(checkpointer: object, agent_id: int) -> None:
    """Flush the optional buffered saver without probing dynamic attributes."""
    attributes = getattr(checkpointer, "__dict__", {})
    if "_ava_nstep_flush" in attributes:
        flush = cast(Callable[[str], Awaitable[None]], attributes["_ava_nstep_flush"])
        await flush(str(agent_id))


async def settle_checkpoint(
    graph: CompiledStateGraph[
        _state.BaseAgentState, AvaContext, _state.BaseAgentState, _state.BaseAgentState
    ],
    agent_id: int,
    *,
    activate_accepted: bool = True,
) -> bool:
    """After invocation+flush, activate; apply terminal deltas exactly once."""
    session = await native_status(agent_id)
    if session is None or session["status"] == "requested":
        return False
    incarnation = current_incarnation(agent_id)
    assert incarnation is not None  # noqa: S101 — native_status requires it
    from shared.impersonation import activate, mark_plugin_applied

    if session["status"] == "accepted":
        if not activate_accepted:
            return False
        # Continuations and managed exec resources must settle before activation.
        if not hosted_resources_settled():
            raise RuntimeError(
                "cannot activate impersonation with unresolved native exec resources"
            )
        # The bound relay must be live before the takeover stands. On failure
        # the lease is rolled back to 'rejected' with a loud reason and the
        # native agent resumes — no silent half-takeover.
        if not await asyncio.to_thread(establish_relay, session, incarnation):
            return False
        session = await asyncio.to_thread(activate, session["id"], incarnation)
    if session["status"] == "active":
        return True
    from ava._external_state import decode_plugin_delta

    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    snapshot = await graph.aget_state(config)
    receipt = snapshot.values.get("impersonation_applied", {})
    for version in range(session["applied_version"] + 1, session["delta_version"] + 1):
        expected = {"lease_id": session["id"], "version": version}
        recorded_version = (
            receipt.get("version", 0) if receipt.get("lease_id") == session["id"] else 0
        )
        if recorded_version < version:
            delta = decode_plugin_delta(session["plugin_delta"][version - 1])
            await graph.aupdate_state(config, {**delta, "impersonation_applied": expected})
            await flush_checkpoint(graph.checkpointer, agent_id)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            receipt = expected
        await asyncio.to_thread(mark_plugin_applied, session["id"], version, incarnation)
    return False


# ── Bound relay process: establishment, supervision, teardown ─────────────────

_RELAY_READY_TIMEOUT_S = 30.0
_RELAY_READY_POLL_S = 1.0
_RELAY_RESPAWN_MIN_SECONDS = 60.0


class _RelayChild:
    """The native runtime's bound relay for one lease, spawned at activation."""

    __slots__ = ("last_spawn_attempt", "lease_id", "process", "spawned_at", "token")

    def __init__(
        self,
        lease_id: str,
        process: subprocess.Popen[bytes],
        token: str,
        spawned_at: float,
    ) -> None:
        self.lease_id = lease_id
        self.process = process
        self.token = token
        self.spawned_at = spawned_at
        self.last_spawn_attempt = spawned_at


# Keyed by agent: the agent-host service drives several agents in one process.
# A module slot per agent keeps each bound relay separate. Entries die with the
# process or are popped when the native agent regains control.
_relay_children: dict[int, _RelayChild] = {}


def drop_relay_supervision(agent_id: int) -> None:
    """Host drop_agent hook: forget supervision state for a departed agent.

    The relay process itself is not killed here — it self-exits as soon as the
    lease reaches a terminal status (the terminate trigger revokes it), and a
    relay that keeps running until then is still delivering real messages.
    """
    _relay_children.pop(agent_id, None)


def _spawn_codex_relay(
    agent_id: int,
    lease_id: str,
    relay_token: str,
    thread_id: str,
    codex_remote: str | None,
) -> subprocess.Popen[bytes]:
    """Spawn the bound relay; the scoped credential travels over a private pipe.

    The token never appears in argv or the environment. The child inherits the
    runtime's own environment (DB reachability), stdout is discarded (hints
    travel through `codex queue`), stderr flows into this process's log.
    """
    argv = [
        sys.executable,
        "-m",
        "cli",
        "impersonate",
        "relay",
        str(agent_id),
        "--lease-id",
        lease_id,
        "--provider",
        "codex",
        "--thread-id",
        thread_id,
        "--token-stdin",
    ]
    if codex_remote is not None:
        argv.extend(["--codex-remote", codex_remote])
    process = subprocess.Popen(  # noqa: S603 — fixed argv built from constants and lease-row fields
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert process.stdin is not None  # noqa: S101 — PIPE requested above
        with process.stdin:
            process.stdin.write(relay_token.encode() + b"\n")
    except (BrokenPipeError, OSError):  # fail-fast-ok: child already gone; poll reports it
        pass
    return process


def _terminate_relay(child: _RelayChild) -> None:
    process = child.process
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _heartbeat_fresh(heartbeat: datetime | None, *, now: datetime | None = None) -> bool:
    from shared.impersonation import RELAY_HEARTBEAT_STALE_SECONDS

    current = now or datetime.now(UTC)
    if heartbeat is None:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    return heartbeat >= current - timedelta(seconds=RELAY_HEARTBEAT_STALE_SECONDS)


def establish_relay(session: dict[str, Any], incarnation: RuntimeIncarnation) -> bool:
    """Activation gate: the bound relay must be up before the takeover stands.

    codex — provision the scoped credential, spawn the relay here, and wait for
    its first heartbeat. claude — its relay runs inside the controller's own
    session; require a fresh heartbeat instead. Any failure rolls the lease to
    'rejected' with a loud reason (fail_acceptance) and returns False; native
    control resumes. A lease that is no longer 'accepted' returns False without
    a transition — someone else already ended it.
    """
    from shared.impersonation import ImpersonationError, fail_acceptance, provision_relay, relay_get

    provider = session["relay_provider"]
    if provider == "claude":
        if _heartbeat_fresh(session["relay_heartbeat_at"]):
            return True
        return _roll_back_relay_failure(
            session,
            incarnation,
            fail_acceptance,
            "the controller relay is not running (no fresh heartbeat)",
        )
    if provider != "codex":
        # Defensive: accept() already rejects leases without a relay binding.
        return _roll_back_relay_failure(
            session, incarnation, fail_acceptance, "request has no relay binding"
        )
    relay_token = secrets.token_urlsafe(32)
    try:
        provision_relay(session["id"], incarnation, relay_token)
    except ImpersonationError:
        return False  # the lease is no longer native-held accepted state
    child = _RelayChild(
        session["id"],
        _spawn_codex_relay(
            incarnation.agent_id,
            session["id"],
            relay_token,
            session["relay_thread_id"],
            session["relay_codex_remote"],
        ),
        relay_token,
        time.monotonic(),
    )
    _relay_children[incarnation.agent_id] = child
    deadline = time.monotonic() + _RELAY_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if child.process.poll() is not None:
            _relay_children.pop(incarnation.agent_id, None)
            return _roll_back_relay_failure(
                session,
                incarnation,
                fail_acceptance,
                f"relay process exited during startup (exit code {child.process.returncode})",
            )
        try:
            latest = relay_get(session["id"], relay_token)
        except ImpersonationError:
            # Token mismatch means a re-provision raced in; treat as ended here.
            return _roll_back_relay_failure(
                session, incarnation, fail_acceptance, "relay credential was re-provisioned"
            )
        if latest["status"] != "accepted":
            return False  # released/expired/rejected while starting; no relay needed
        if _heartbeat_fresh(latest["relay_heartbeat_at"]):
            return True
        time.sleep(_RELAY_READY_POLL_S)
    _terminate_relay(child)
    _relay_children.pop(incarnation.agent_id, None)
    return _roll_back_relay_failure(
        session, incarnation, fail_acceptance, "relay did not become ready in time"
    )


def _roll_back_relay_failure(
    session: dict[str, Any],
    incarnation: RuntimeIncarnation,
    fail_acceptance: Callable[[str, RuntimeIncarnation, str], dict[str, Any]],
    reason: str,
) -> bool:
    from shared.impersonation import ImpersonationError

    with contextlib.suppress(ImpersonationError):
        fail_acceptance(session["id"], incarnation, reason)  # already ended: no-op
    return False


async def supervise_relay(session: dict[str, Any] | None, agent_id: int) -> None:
    """Respawn a dead bound relay, or tear it down — the native supervision seam.

    Two call sites: the claim gate (native loop paused or resuming) and the
    held-controls pass while an active lease parks the agent outside the graph
    (services/agent_host/host.py `_apply_held_controls`). The claim gate never
    runs during an active lease, so the held-controls path is the lease's only
    native supervision point there; the dispatcher's pending scan keeps waking
    agents with an open lease, giving it a periodic trigger even without
    inbound traffic.

    Hot-path cost (fresh heartbeat): one `native_status` read plus a datetime
    comparison — no model calls, no graph work, no polling. Only a stale
    heartbeat escalates: at most one provision write, one relay spawn, and the
    rate-limited failure stamp. The relay itself heartbeats the lease row; a
    stale heartbeat with pending inbound must not be silent.
    """
    from shared.impersonation import (
        ImpersonationError,
        provision_relay,
        record_relay_failure,
    )

    child = _relay_children.get(agent_id)
    if session is None:
        # Native control returned (release/expiry): the relay self-exits on
        # terminal status; this kill is the explicit symmetric teardown.
        if child is not None:
            _relay_children.pop(agent_id, None)
            await asyncio.to_thread(_terminate_relay, child)
        return
    if session["status"] != "active":
        return
    if _heartbeat_fresh(session["relay_heartbeat_at"]):
        return
    if session["relay_provider"] != "codex":
        # A controller-session relay (claude) cannot be respawned from here:
        # stamp the failure durably and let the controller's own supervision
        # see it. The wake path also alerts on delivery.
        await asyncio.to_thread(_stamp_relay_failure, session, agent_id, record_relay_failure)
        return
    now = time.monotonic()
    if child is not None and child.process.poll() is None:
        if now - child.spawned_at < _RELAY_READY_TIMEOUT_S:
            return  # startup grace: a fresh spawn may not have heartbeated yet
        await asyncio.to_thread(_terminate_relay, child)  # alive but silent: hung
    if child is not None and now - child.last_spawn_attempt < _RELAY_RESPAWN_MIN_SECONDS:
        await asyncio.to_thread(_stamp_relay_failure, session, agent_id, record_relay_failure)
        return
    token = child.token if child is not None else None
    if token is None:
        # This process did not mint the credential (restarted mid-lease):
        # re-provision, which also revokes any lingering duplicate relay.
        incarnation = current_incarnation(agent_id)
        if incarnation is None:
            await asyncio.to_thread(_stamp_relay_failure, session, agent_id, record_relay_failure)
            return
        token = secrets.token_urlsafe(32)
        try:
            await asyncio.to_thread(provision_relay, session["id"], incarnation, token)
        except (ImpersonationError, RuntimeError):
            await asyncio.to_thread(_stamp_relay_failure, session, agent_id, record_relay_failure)
            return
    spawned_at = time.monotonic()
    _relay_children[agent_id] = _RelayChild(
        session["id"],
        _spawn_codex_relay(
            agent_id,
            session["id"],
            token,
            session["relay_thread_id"],
            session["relay_codex_remote"],
        ),
        token,
        spawned_at,
    )
    from shared.log import logger

    logger.warning(
        "respawning impersonation relay after a stale heartbeat",
        agent_id=agent_id,
        lease_id=str(session["id"]),
    )


def _stamp_relay_failure(
    session: dict[str, Any],
    agent_id: int,
    record_relay_failure: Callable[[str, RuntimeIncarnation], bool],
) -> None:
    from shared.impersonation import ImpersonationError
    from shared.log import logger

    incarnation = current_incarnation(agent_id)
    if incarnation is None:
        return
    try:
        stamped = record_relay_failure(session["id"], incarnation)
    except (ImpersonationError, RuntimeError):
        return
    if stamped:
        logger.error(
            "impersonation relay heartbeat stale and it cannot be respawned here",
            agent_id=agent_id,
            lease_id=str(session["id"]),
            relay_provider=session["relay_provider"],
        )
