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
from agent.graph.claim.node import claim_node
from agent.hooks.compact import COMPACT_MAX_ATTEMPTS
from agent.impersonation import flush_checkpoint
from agent.startup import wrap_saver_writes_with_nstep_interval
from agent.tests.claim.test_inbound_ownership import _agent
from base.agents.context import AvaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.config import settings
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.events.live.projection import Error
from base.events.live.publisher import AgentEventPublisher
from base.events.live.redis_client import open_async_redis
from base.host.env.agent_slices import AgentSlices

from ...host import AgentHost
from ...runtime import TurnOutcome


def _cold_reader(pool: AsyncConnectionPool[Any]) -> AsyncPostgresSaver:
    """Cold inspector saver that folds delta-written messages (#3180 write switch)."""
    reader = AsyncPostgresSaver(pool)
    wrap_saver_reads_with_delta_reconstruction(reader)
    return reader


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
    wrap_saver_writes_with_nstep_interval(saver, interval)
    # Delta write model (#3180): fold delta-written messages on read (daemon parity).
    wrap_saver_reads_with_delta_reconstruction(saver)
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


def _queue_compact_request(db_conn: psycopg.Connection, agent: int) -> int:
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'','compact_request','user') RETURNING id",
        (agent,),
    ).fetchone()
    assert row is not None
    db_conn.commit()
    return row[0]


def _build_host_driving_invoke_until_done(
    pool: AsyncConnectionPool[Any],
    saver: AsyncPostgresSaver,
    graph: Any,
    ctx: AvaContext,
    monkeypatch: pytest.MonkeyPatch,
) -> AgentHost:
    host = AgentHost(
        pool=pool,
        checkpointer=saver,
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    monkeypatch.setattr(host, "_runtime_for", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        "services.agent_runner.agent_host.runtime.validate_model_config", MagicMock()
    )

    async def drive(target: int, _runtime: object, _slices: object) -> TurnOutcome:
        return await host._invoke_until_done(target, ctx)

    monkeypatch.setattr(host, "_drive_turns", drive)
    return host


def _inbound_status(db_conn: psycopg.Connection, inbound_id: int) -> tuple[Any, ...] | None:
    return db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound_id,)
    ).fetchone()


def _agent_status(db_conn: psycopg.Connection, agent: int) -> tuple[Any, ...] | None:
    return db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone()


async def _assert_failure_is_visible_and_durable(
    db_conn: psycopg.Connection,
    pool: AsyncConnectionPool[Any],
    subscription: PubSub,
    config: RunnableConfig,
    *,
    agent: int,
    compact_id: int,
    history: list[HumanMessage | AIMessage],
) -> None:
    event = await _receive_error(subscription)
    assert event.agent_id == agent
    assert "CompactionFailedError" in event.content and "history was preserved" in event.content
    cold = await _cold_reader(pool).aget_tuple(config)
    assert cold is not None
    persisted = cold.checkpoint["channel_values"]
    assert persisted["halted"] is True
    assert persisted["messages"] == history
    assert _inbound_status(db_conn, compact_id) == ("done",)
    assert _agent_status(db_conn, agent) == ("idling",)


async def _assert_new_inbound_resumes_with_history(
    db_conn: psycopg.Connection,
    pool: AsyncConnectionPool[Any],
    config: RunnableConfig,
    *,
    compact_id: int,
    history: list[HumanMessage | AIMessage],
) -> None:
    resumed = await _cold_reader(pool).aget_tuple(config)
    assert resumed is not None
    messages = resumed.checkpoint["channel_values"]["messages"]
    assert messages[:2] == history
    assert any(message.content == "continued" for message in messages)
    assert _inbound_status(db_conn, compact_id) == ("done",)


@pytest.mark.parametrize("interval", [1, 100])
async def test_compaction_failure_is_visible_durable_and_recovers_on_new_inbound(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    interval: int,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
    summary = AsyncMock(side_effect=RuntimeError("compaction provider unavailable"))
    monkeypatch.setattr("agent.hooks.compact.generate_summary", summary)
    replies: list[str] = []

    graph, saver, config, history = await _prepare_graph(aops_pool, agent, interval, replies)
    compact_id = _queue_compact_request(db_conn, agent)

    redis = open_async_redis(settings.data_plane.redis_url)
    channel = f"{settings.data_plane.events_channel}:compact-proof:{agent}"
    publisher = AgentEventPublisher(redis, channel, agent_id=agent)
    ctx = AvaContext(
        ops_pool=aops_pool,
        event_publisher=publisher,
        llm=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
    )
    host = _build_host_driving_invoke_until_done(aops_pool, saver, graph, ctx, monkeypatch)
    async with asyncio.TaskGroup() as tasks:
        try:
            async with redis.pubsub() as subscription:  # pyright: ignore[reportUnknownMemberType] — redis stubs
                await subscription.subscribe(channel)
                await publisher.start(tasks)
                await asyncio.wait_for(host.run_turn(agent), 5)
                await _assert_failure_is_visible_and_durable(
                    db_conn,
                    aops_pool,
                    subscription,
                    config,
                    agent=agent,
                    compact_id=compact_id,
                    history=history,
                )
                assert not replies
                assert summary.await_count == COMPACT_MAX_ATTEMPTS

                insert_inbound_message(
                    db_conn,
                    agent,
                    "Continue without compacting",
                    "user",
                    bus=event_bus,
                    database=database,
                )
                await asyncio.wait_for(host.run_turn(agent), 5)
                assert replies == ["continued"]
                assert summary.await_count == COMPACT_MAX_ATTEMPTS
                await _assert_new_inbound_resumes_with_history(
                    db_conn, aops_pool, config, compact_id=compact_id, history=history
                )
        finally:
            await publisher.aclose()
            await redis.aclose()
            await host.aclose()
