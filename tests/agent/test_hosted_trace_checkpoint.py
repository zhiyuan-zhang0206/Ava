"""A host turn's trace resolves its final, durably flushed conversation."""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from opentelemetry.sdk.trace import TracerProvider
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import flush_checkpoint
from agent.startup import _wrap_saver_writes_with_nstep_interval
from agent.state import BaseAgentState
from services.agent_host import host as host_module
from shared.checkpoint import load_checkpoint_messages_by_trace
from shared.config import settings
from shared.context import AvaContext
from tests.agent.test_inbound_ownership import _agent


async def test_host_trace_reads_final_messages_after_nstep_flush(
    aops_pool: AsyncConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = _agent(db_conn)
    traces: list[str] = []
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)

    @contextmanager
    def recorded_turn(**_fields: object) -> Generator[None, None, None]:
        with tracer.start_as_current_span("host-turn") as span:
            traces.append(format(span.get_span_context().trace_id, "032x"))
            yield

    monkeypatch.setattr(host_module, "turn_span", recorded_turn)

    def finish(state: BaseAgentState) -> dict[str, object]:
        assert len(state.messages) == 1
        return {
            "messages": [AIMessage(content="final response", id="final")],
            "halted": True,
            "turn_idle": True,
        }

    builder = StateGraph(BaseAgentState, context_schema=AvaContext)
    builder.add_node("finish", finish)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "finish")
    builder.add_edge("finish", END)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        _wrap_saver_writes_with_nstep_interval(saver, 100)
        graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
        await graph.aupdate_state(
            config, {"messages": [HumanMessage(content="prior question", id="prior")]}
        )
        await flush_checkpoint(saver, agent_id)
        host = host_module.AgentHost(
            pool=aops_pool, checkpointer=saver, graph=graph, machine="test"
        )
        assert not await host._invoke_until_done(agent_id, AvaContext(ops_pool=aops_pool))

    assert len(traces) == 1
    # This is the actual gateway trace-content reader, using fresh connections.
    checkpoint_id, messages = await asyncio.to_thread(
        load_checkpoint_messages_by_trace, agent_id, traces[0]
    )
    assert checkpoint_id is not None
    assert [message.text for message in messages] == ["prior question", "final response"]
    provider.shutdown()
