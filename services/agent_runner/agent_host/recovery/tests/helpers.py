"""Public PostgreSQL admission and checkpoint fixtures for host consumers."""

from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.ownership.hosted import admit_hosted_runtime
from base.agents.context import AvaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.incarnation.resources import ResourceBirth
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.agents.spawn import create_agent_row


async def admit(
    pool: AsyncConnectionPool,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentity,
) -> RuntimeIncarnation:
    agent, _, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=local_machine.name(),
        catalog=model_catalog,
        authority=config_authority,
    )
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), agent),
        )
    incarnation = await admit_hosted_runtime(
        pool,
        agent,
        local_machine.name(),
        uuid4(),
        expected_from="idling",
        db=Database.from_settings(gate=database_gate),
    )
    assert incarnation is not None
    return incarnation


async def checkpoint_graph(
    pool: AsyncConnectionPool[Any], agent: int, node: Any
) -> tuple[Any, AsyncPostgresSaver]:
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    # Delta write model (#3180): fold delta-written messages on read (daemon parity).
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("work", node)
    builder.add_edge(START, "work")
    builder.add_edge("work", "__end__")
    graph = builder.compile(checkpointer=saver)
    await graph.aupdate_state(
        {"configurable": {"thread_id": str(agent)}},
        {"messages": [HumanMessage(content="Continue existing work")], "halted": False},
        as_node="work",
    )
    return graph, saver
