"""A matching marker and successful summary cannot certify an unmaterialized replacement."""

from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import flush_checkpoint
from agent.state import ContextReset
from base.agents.compaction.models import CompactHeldError
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact import apply as compact_apply
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from tests.fixtures.model_catalog import AddBindings


@pytest.mark.parametrize("corruption", ["old_source", "reset_tail"])
async def test_actual_cold_materialization_gap_never_acknowledges_marker_alone(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    corruption: str,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    model = SummaryModel(responses=["Original summary with enough useful content. " * 100])
    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=lambda _: model)}
    )
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    acknowledge = compact_apply.acknowledge
    observed: list[bool] = []

    async def missing_materialization(*args: Any, **kwargs: Any) -> bool:
        snapshot = await cold_reader(accepted.saver).aget_tuple(accepted.config)
        assert snapshot is not None
        if snapshot.checkpoint["channel_values"].get("native_compact") is not None:
            message = HumanMessage(id="source-history", content="Retain this task")
            update = (
                {"messages": [message]}
                if corruption == "old_source"
                else {"context_reset": ContextReset(tail=[message])}
            )
            await accepted.host._graph.aupdate_state(accepted.config, update, as_node="claim")
            await flush_checkpoint(accepted.saver, accepted.agent)
            observed.append(True)
        return await acknowledge(*args, **kwargs)

    monkeypatch.setattr(compact_apply, "acknowledge", missing_materialization)
    with pytest.raises(CompactHeldError, match="materialized application"):
        await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert observed == [True] and model.calls == 1
    assert status["outcome"] == "applying" and status["checkpoint_id"] is None
    assert not status["continuation_released"] and status["result_available"]
    cold = await cold_reader(accepted.saver).aget_tuple(accepted.config)
    assert cold is not None and cold.checkpoint["channel_values"]["native_compact"] is not None
