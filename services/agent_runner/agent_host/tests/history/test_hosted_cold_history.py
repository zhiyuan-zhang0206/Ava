"""A cold admission repairs and invokes from one unchanged history read."""

from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from base.agents.history.delta_read_compat import (
    recovery_reconstruction_scope,
    wrap_saver_reads_with_delta_reconstruction,
)
from base.agents.incarnation.resources import ResourceBirth
from base.cluster.machine import machine_name
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from ops.agents.spawn import create_agent_row
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host import runtime as runtime_module
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.runtime import TurnOutcome


def test_unwrapped_saver_does_not_opt_into_reconstruction_cache() -> None:
    saver = Mock()
    with recovery_reconstruction_scope(saver, "no-delta-wrapper") as scope:
        assert scope is None
    assert saver.mock_calls == []


@pytest.mark.parametrize("needs_repair", [False, True])
async def test_cold_repair_and_invocation_share_only_unchanged_messages(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    needs_repair: bool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent, *_ = create_agent_row(database, event_bus, spawner="user", machine=machine_name())
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), agent),
        )
    saver = AsyncPostgresSaver(cast(Any, aops_pool))
    wrap_saver_reads_with_delta_reconstruction(saver)
    observed: list[Any] = []

    async def work(state: states.AgentState) -> dict[str, Any]:
        observed.extend(state.messages)
        return {"halted": True, "turn_idle": True}

    builder: Any = StateGraph(states.AgentState)
    builder.add_node("work", work)
    builder.add_edge(START, "work")
    builder.add_edge("work", "__end__")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    await graph.ainvoke({"messages": [HumanMessage(id="seed", content="Seed")]}, config=config)
    observed.clear()
    tail = (
        AIMessage(
            id="tail",
            content="",
            tool_calls=[{"id": "unfinished", "name": "execute_code", "args": {}}],
        )
        if needs_repair
        else HumanMessage(id="tail", content="Continue")
    )
    await graph.aupdate_state(config, {"messages": [tail]}, as_node="work")
    raw = await AsyncPostgresSaver.aget_tuple(saver, config)
    assert raw is not None and "messages" not in raw.checkpoint["channel_values"]
    original = saver.aget_delta_channel_history
    reads = 0

    async def counted(**kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        return await original(**kwargs)

    monkeypatch.setattr(saver, "aget_delta_channel_history", counted)
    monkeypatch.setattr(host_module, "admit_stored_model", Mock(return_value=True))
    monkeypatch.setattr(host_module, "publish_agent_updated", AsyncMock())
    monkeypatch.setattr(
        runtime_module, "boot_agent_scope", AsyncMock(return_value=(object(), None))
    )
    monkeypatch.setattr(host_module, "close_hosted_turn", AsyncMock())
    host = AgentHost(
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )

    async def drive(
        _agent: int,
        _runtime: Any,
        _slices: object,
        *,
        incarnation: RuntimeIncarnation | None,
        resources: HostedTurnResources | None,
    ) -> TurnOutcome:
        # Recovery can nest inside this turn without dropping or duplicating its cache.
        with recovery_reconstruction_scope(saver, str(agent)):
            await graph.ainvoke({}, config=config)
        return TurnOutcome(exited=False, crashed=False)

    monkeypatch.setattr(host, "_drive_turns", drive)
    await host._run_turn(agent, resources=None)
    assert reads == (2 if needs_repair else 1)
    assert any(isinstance(m, ToolMessage) for m in observed) is needs_repair
    assert host.stats.cache_misses == 1
    # The retained entry belongs to admission, not the warm runtime or next reader.
    await graph.aget_state(config)
    assert reads == (3 if needs_repair else 2)
