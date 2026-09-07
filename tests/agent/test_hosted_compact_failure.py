"""Compaction aborts survive a real hosted turn and its PostgreSQL flush."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from redis.asyncio.client import PubSub

from agent import state as states
from agent.graph._claim import claim_node
from agent.hooks.compact import COMPACT_MAX_ATTEMPTS
from agent.impersonation import flush_checkpoint
from agent.startup import _wrap_saver_writes_with_nstep_interval
from services.agent_host.host import AgentHost
from shared.config import settings
from shared.context import AvaContext
from shared.db import insert_inbound_message
from shared.event_publisher import AgentEventPublisher
from shared.live_events import Error
from shared.redis_client import open_async_redis
from tests.agent.test_inbound_ownership import _agent


async def _prepare_graph(
    pool: AsyncConnectionPool[Any], agent: int, interval: int, replies: list[str]
) -> tuple[Any, AsyncPostgresSaver, RunnableConfig, list[HumanMessage | AIMessage]]:
    async def model(state: states.BaseAgentState) -> Command[Any]:
        assert not state.halted
        replies.append("continued")
        return Command(
            update={"halted": True, "messages": [AIMessage(content="continued")]}, goto="claim"
        )

    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    _wrap_saver_writes_with_nstep_interval(saver, interval)
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("claim", claim_node, destinations=("before_llm", "claim", "__end__"))
    builder.add_node("before_llm", model, destinations=("claim",))
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    history: list[HumanMessage | AIMessage] = [
        HumanMessage(id="original-user", content="Keep this original request"),
        AIMessage(id="original-assistant", content="Keep this original answer"),
    ]
    await graph.aupdate_state(config, {"messages": history, "halted": False})
    await flush_checkpoint(saver, agent)
    return graph, saver, config, history


async def _receive_error(subscription: PubSub) -> Error:
    async with asyncio.timeout(5):
        while True:
            wire = await subscription.get_message(ignore_subscribe_messages=True, timeout=1)
            if wire is None:
                continue
            data = wire["data"]
            assert isinstance(data, (str, bytes))
            if json.loads(data)["role"] == "error":
                return Error.model_validate_json(data)


@pytest.mark.parametrize("interval", [1, 100])
async def test_compaction_failure_is_visible_durable_and_recovers_on_new_inbound(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    interval: int,
) -> None:
    agent = _agent(db_conn)
    summary = AsyncMock(side_effect=RuntimeError("compaction provider unavailable"))
    monkeypatch.setattr("agent.graph._claim_dispatch.generate_summary", summary)
    replies: list[str] = []

    graph, saver, config, history = await _prepare_graph(aops_pool, agent, interval, replies)
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'','compact_request','user') RETURNING id",
        (agent,),
    ).fetchone()
    assert row is not None
    compact_id = row[0]
    db_conn.commit()

    redis = open_async_redis(settings.data_plane.redis_url)
    channel = f"{settings.data_plane.events_channel}:compact-proof:{agent}"
    publisher = AgentEventPublisher(redis, channel, agent_id=agent)
    ctx = AvaContext(ops_pool=aops_pool, event_publisher=publisher, llm=MagicMock())
    host = AgentHost(pool=aops_pool, checkpointer=saver, graph=graph, machine="claim-test")
    monkeypatch.setattr(host, "_runtime_for", AsyncMock(return_value=object()))
    monkeypatch.setattr("services.agent_host.host.validate_model_config", MagicMock())

    async def drive(target: int, _runtime: object) -> bool:
        return await host._invoke_until_done(target, ctx)

    monkeypatch.setattr(host, "_drive_turns", drive)
    try:
        async with redis.pubsub() as subscription:  # pyright: ignore[reportUnknownMemberType] — redis stubs
            await subscription.subscribe(channel)
            await publisher.start()
            await asyncio.wait_for(host.run_turn(agent), 5)
            event = await _receive_error(subscription)
            assert event.agent_id == agent
            assert (
                "CompactionFailedError" in event.content
                and "history was preserved" in event.content
            )
            assert not replies
            assert summary.await_count == COMPACT_MAX_ATTEMPTS
            cold = await AsyncPostgresSaver(aops_pool).aget_tuple(config)
            assert cold is not None
            persisted = cold.checkpoint["channel_values"]
            assert persisted["halted"] is True
            assert persisted["messages"] == history
            assert db_conn.execute(
                "SELECT status FROM inbound_messages WHERE id=%s", (compact_id,)
            ).fetchone() == ("done",)
            assert db_conn.execute(
                "SELECT status FROM agents_meta WHERE id=%s", (agent,)
            ).fetchone() == ("idling",)

            insert_inbound_message(db_conn, agent, "Continue without compacting", "user")
            await asyncio.wait_for(host.run_turn(agent), 5)
            assert replies == ["continued"]
            assert summary.await_count == COMPACT_MAX_ATTEMPTS
            resumed = await AsyncPostgresSaver(aops_pool).aget_tuple(config)
            assert resumed is not None
            messages = resumed.checkpoint["channel_values"]["messages"]
            assert messages[:2] == history
            assert any(message.content == "continued" for message in messages)
            assert db_conn.execute(
                "SELECT status FROM inbound_messages WHERE id=%s", (compact_id,)
            ).fetchone() == ("done",)
    finally:
        await publisher.aclose()
        await redis.aclose()
        await host.aclose()
