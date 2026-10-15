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
from base.agents.birth.spawn import create_agent_row
from base.agents.history.delta_read_compat import (
    recovery_reconstruction_scope,
    wrap_saver_reads_with_delta_reconstruction,
)
from base.agents.incarnation.resources import ResourceBirth
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from base.packages.composer_commands import CommandCatalog
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host import runtime as runtime_module
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.runtime import TurnOutcome
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy


def test_unwrapped_saver_does_not_opt_into_reconstruction_cache() -> None:
    saver = Mock()
    with recovery_reconstruction_scope(saver, "no-delta-wrapper") as scope:
        assert scope is None
    assert saver.mock_calls == []


@pytest.mark.parametrize("dangling", [False, True])
async def test_cold_admission_reads_the_history_once_and_never_repairs_it(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    dangling: bool,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent, *_ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_identity.name(),
        catalog=model_catalog,
        authority=config_authority,
    )
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
        if dangling
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
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )

    async def drive(
        _agent: int,
        _runtime: Any,
        _slices: object,
        *,
        incarnation: RuntimeIncarnation | None,
        resources: HostedTurnResources | None,
        checkpoints: Any = None,
    ) -> TurnOutcome:
        assert checkpoints.graph.nodes == graph.nodes
        assert all(checkpoints.graph.nodes[key] is node for key, node in graph.nodes.items())
        assert checkpoints.graph.channels == graph.channels
        assert checkpoints.graph.checkpointer is checkpoints.saver
        assert checkpoints.saver.serde is saver.serde
        assert checkpoints.saver.aput is saver.aput
        with recovery_reconstruction_scope(saver, str(agent), parent=checkpoints.reconstruction):
            await checkpoints.graph.ainvoke({}, config=config)
        return TurnOutcome(exited=False, crashed=False)

    monkeypatch.setattr(host, "_drive_turns", drive)
    await host._run_turn(agent, resources=None)
    assert reads == 1
    assert not any(isinstance(m, ToolMessage) for m in observed)
    assert host.stats.cache_misses == 1
    await graph.aget_state(config)
    assert reads == 2
