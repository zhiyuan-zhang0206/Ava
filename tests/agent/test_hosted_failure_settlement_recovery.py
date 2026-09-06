"""Database-only abort retries neither invoke the model nor repeat notifications."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent import db as agent_db
from agent import state as states
from agent._runloop import PendingTurnFailure, settle_turn_failure
from agent.graph._claim import claim_node
from agent.graph._llm_errors import FatalProviderError
from agent.hooks.compact import COMPACT_MAX_ATTEMPTS
from agent.impersonation import flush_checkpoint
from agent.startup import _wrap_saver_writes_with_nstep_interval
from services.agent_host.host import AgentHost
from shared.config import settings
from shared.context import AvaContext
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity
from tests.agent.test_inbound_ownership import _admit, _agent


async def _prepare_graph(
    pool: AsyncConnectionPool[Any],
    agent: int,
    model: Callable[[states.AgentState], Awaitable[Command[Any]]],
) -> tuple[Any, AsyncPostgresSaver, RunnableConfig, list[HumanMessage]]:
    saver = AsyncPostgresSaver(pool)
    _wrap_saver_writes_with_nstep_interval(saver, 100)
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("claim", claim_node, destinations=("before_llm", "claim", "__end__"))
    builder.add_node("before_llm", model, destinations=("claim",))
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    history = [HumanMessage(content="Preserve this request")]
    await graph.aupdate_state(config, {"messages": history, "halted": False})
    await flush_checkpoint(saver, agent)
    return graph, saver, config, history


async def _admitted_descendant(
    conn: psycopg.Connection, pool: AsyncConnectionPool[Any]
) -> tuple[int, int, RuntimeIncarnation]:
    ancestor = _agent(conn)
    await _admit(pool, ancestor)
    agent = _agent(conn)
    conn.execute("UPDATE agents_meta SET spawner=%s WHERE id=%s", (f"agent:{ancestor}", agent))
    conn.commit()
    return ancestor, agent, await _admit(pool, agent)


@pytest.mark.parametrize("failure", ["compaction", "provider"])
async def test_abort_survives_database_loss_before_halted_state_write(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    ancestor, agent, owner = await _admitted_descendant(db_conn, aops_pool)
    model_calls: list[str] = []
    summary = AsyncMock(side_effect=RuntimeError("summary unavailable"))
    monkeypatch.setattr("agent.graph._claim_dispatch.generate_summary", summary)

    async def model(state: states.AgentState) -> Command[Any]:
        assert not state.halted
        model_calls.append("called")
        if failure == "provider" and len(model_calls) == 1:
            raise FatalProviderError("invalid credentials", error_class="permanent", status=401)
        return Command(update={"halted": True}, goto="claim")

    graph, saver, config, history = await _prepare_graph(aops_pool, agent, model)
    if failure == "compaction":
        db_conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source) "
            "VALUES(%s,'','compact_request','user')",
            (agent,),
        )
        db_conn.commit()
    broken = await psycopg.AsyncConnection.connect(settings.data_plane.db_url)
    await broken.close()
    original_update = graph.aupdate_state
    outages = 0

    async def interrupted_update(*args: Any, **kwargs: Any) -> Any:
        nonlocal outages
        if outages < 2:
            outages += 1
            await broken.execute("SELECT 1")
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(graph, "aupdate_state", interrupted_update)
    host = AgentHost(pool=aops_pool, checkpointer=saver, graph=graph)
    publisher = MagicMock()
    ctx = AvaContext(ops_pool=aops_pool, event_publisher=publisher, llm=MagicMock())
    with bind_turn_identity(agent, incarnation=owner):
        assert not await host._invoke_until_done(agent, ctx)
    assert outages == 2 and len(model_calls) == (0 if failure == "compaction" else 1)
    errors = [
        json.loads(call.args[0])
        for call in publisher.emit.call_args_list
        if json.loads(call.args[0])["role"] == "error"
    ]
    cold = await AsyncPostgresSaver(aops_pool).aget(config)
    assert cold is not None
    values = cold["channel_values"]
    assert values["halted"] is True and values["messages"] == history
    if failure == "compaction":
        assert summary.await_count == COMPACT_MAX_ATTEMPTS
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='compact_request'",
            (agent,),
        ).fetchone() == ("done",)
    else:
        assert values["circuit"].open and values["circuit"].reason == "auth"
    reports = db_conn.execute(
        "SELECT content,source,payload FROM inbound_messages "
        "WHERE agent_id=%s AND kind='system_note'",
        (ancestor,),
    ).fetchall()
    assert len(reports) == (0 if failure == "compaction" else 1)
    if reports:
        assert reports[0][0].startswith(f"Descendant agent {agent} is blocked")
        assert reports[0][1:] == ("system", {"note_tag": "agent_reply"})
    assert len(errors) == 1 and errors[0]["agent_id"] == agent


@pytest.mark.parametrize("interrupted_at", ["circuit_read", "ancestor_report"])
async def test_interrupted_abort_preparation_does_not_repeat_notifications(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    interrupted_at: str,
) -> None:
    ancestor, agent, owner = await _admitted_descendant(db_conn, aops_pool)
    failure = FatalProviderError("invalid credentials", error_class="permanent", status=401)
    model = AsyncMock(side_effect=failure)
    graph, saver, config, history = await _prepare_graph(aops_pool, agent, model)
    publisher = MagicMock()
    ctx = AvaContext(ops_pool=aops_pool, event_publisher=publisher, llm=MagicMock())
    pending = PendingTurnFailure(failure)
    with bind_turn_identity(agent, incarnation=owner):
        with pytest.raises(FatalProviderError):
            await graph.ainvoke({"turn_active": False}, config, context=ctx)
        await flush_checkpoint(saver, agent)
    entered = asyncio.Event()
    target = graph if interrupted_at == "circuit_read" else agent_db
    method = (
        "aget_state"
        if interrupted_at == "circuit_read"
        else "enqueue_fatal_provider_report_to_nearest_alive_ancestor"
    )
    original = getattr(target, method)

    async def interrupted_prepare(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(target, method, interrupted_prepare)
    with bind_turn_identity(agent, incarnation=owner):
        attempt = asyncio.create_task(
            settle_turn_failure(graph, saver, config, ctx, agent, pending)
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(attempt, 0.01)
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
        assert pending.prepared_update is None and pending.reports_started
        await settle_turn_failure(graph, saver, config, ctx, agent, pending)
    assert model.await_count == 1
    errors = [
        json.loads(call.args[0])
        for call in publisher.emit.call_args_list
        if json.loads(call.args[0])["role"] == "error"
    ]
    reports = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE agent_id=%s AND kind='system_note'",
        (ancestor,),
    ).fetchall()
    assert len(reports) == (0 if interrupted_at == "circuit_read" else 1)
    assert len(errors) == 1 and errors[0]["agent_id"] == agent
    cold = await AsyncPostgresSaver(aops_pool).aget(config)
    assert cold is not None
    values = cold["channel_values"]
    assert values["halted"] is True and values["messages"] == history
    assert values["circuit"].open and values["circuit"].reason == "auth"
