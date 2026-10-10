"""A provider failure the host persists before it releases the turn (the circuit breaker's
durable half)."""

from dataclasses import replace
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.llm_errors import FatalProviderError
from agent.ownership.hosted import admit_hosted_runtime
from agent.state import AgentState, CircuitState
from base.agents.context import AvaContext
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.hosted_resources import hosted_scope
from tests.fixtures.units import spawn_agent


def _breaker_ctx(pool: AsyncConnectionPool, *, database_gate: ProcessDbGate) -> AvaContext:
    """Use the original owner's database channel for native failure settlement."""
    return AvaContext(
        ops_pool=pool,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=configured_policy().clock_factory,
    )


@pytest.mark.parametrize("overflow", [False, True])
async def test_host_persists_provider_failure_before_releasing_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    overflow: bool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    """A real graph failure is flushed to PG; a fresh reader sees its breaker."""
    async with hosted_scope(expected_error=FatalProviderError) as resources:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.graph import END, START, StateGraph

        from agent.startup import wrap_saver_writes_with_nstep_interval
        from base.config import settings
        from services.agent_runner.agent_host.host import AgentHost

        agent_id = spawn_agent(
            catalog=model_catalog, authority=config_authority, database_gate=database_gate
        )
        row = db_conn.execute("SELECT machine FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
        assert row is not None
        incarnation = await admit_hosted_runtime(
            aops_pool,
            agent_id,
            row[0],
            uuid4(),
            db=Database.from_settings(gate=database_gate),
            expected_from="idling",
        )
        assert incarnation is not None
        calls = 0
        failure = FatalProviderError(
            "synthetic provider refusal",
            error_class="permanent",
            provider="anthropic",
            status=400 if overflow else 402,
            context_overflow=overflow,
        )

        def reject(state: AgentState) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise failure

        builder = StateGraph(AgentState, context_schema=AvaContext)
        builder.add_node("reject", reject)  # pyright: ignore[reportUnknownMemberType]
        builder.add_edge(START, "reject")
        builder.add_edge("reject", END)
        async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            wrap_saver_writes_with_nstep_interval(saver, 100)
            graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
            host = AgentHost(
                policy=configured_policy(),
                pool=aops_pool,
                checkpointer=saver,
                graph=graph,
                machine="test",
                catalog=model_catalog,
                bus=EventBus.from_settings(),
                db=Database.from_settings(gate=database_gate),
            )
            assert not (
                await host._invoke_until_done(
                    agent_id,
                    replace(
                        _breaker_ctx(aops_pool, database_gate=database_gate),
                        original_incarnation=incarnation,
                        hosted_resources=resources,
                        native_work=None,
                    ),
                )
            ).exited
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
        assert resources.require_service().failures == [(resources, failure)]
