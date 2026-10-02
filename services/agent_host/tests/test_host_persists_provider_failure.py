"""A provider failure the host persists before it releases the turn (the circuit breaker's
durable half)."""

from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.llm_errors import FatalProviderError
from agent.state import AgentState, CircuitState
from base.agents.context import AvaContext
from base.db import Database
from base.events.live.bus import EventBus
from tests.fixtures.units import spawn_agent


def _breaker_ctx() -> AvaContext:
    """An AvaContext whose event-log write is skipped (no ops_pool)."""
    return AvaContext(ops_pool=None, llm=MagicMock(), event_publisher=MagicMock())


@pytest.mark.parametrize("overflow", [False, True])
async def test_host_persists_provider_failure_before_releasing_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    overflow: bool,
) -> None:
    """A real graph failure is flushed to PG; a fresh reader sees its breaker."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    from agent.startup import wrap_saver_writes_with_nstep_interval
    from base.config import settings
    from services.agent_host.host import AgentHost

    agent_id = spawn_agent()
    calls = 0

    def reject(state: AgentState) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise FatalProviderError(
            "synthetic provider refusal",
            error_class="permanent",
            provider="anthropic",
            status=400 if overflow else 402,
            context_overflow=overflow,
        )

    builder = StateGraph(AgentState, context_schema=AvaContext)
    builder.add_node("reject", reject)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "reject")
    builder.add_edge("reject", END)
    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        wrap_saver_writes_with_nstep_interval(saver, 100)
        graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
        host = AgentHost(
            pool=aops_pool,
            checkpointer=saver,
            graph=graph,
            machine="test",
            bus=EventBus.from_settings(),
            db=Database.from_settings(),
        )
        assert not (await host._invoke_until_done(agent_id, _breaker_ctx())).exited
    # New saver/connection prevents in-memory buffered state from faking success.
    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as reader:
        stored = await reader.aget_tuple({"configurable": {"thread_id": str(agent_id)}})
    assert stored is not None
    values = stored.checkpoint["channel_values"]
    assert values["halted"] is True
    circuit = CircuitState.model_validate(values["circuit"])
    assert circuit.open is True
    assert circuit.reason == ("context_overflow" if overflow else "billing")
    assert circuit.opened_at is not None
    assert calls == 1
