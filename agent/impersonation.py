"""Cooperative takeover at the native runtime's durable invocation boundary.

The external holder never writes a graph checkpoint. Accepted requests stop
native nodes; only the invocation driver activates after exec closure and the
checkpointer flush. A database lease gates every subsequent invocation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
from shared.runtime_incarnation import current_incarnation
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
            f"ava.impersonation.accept({request_id!r}); this ends your execution. "
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
