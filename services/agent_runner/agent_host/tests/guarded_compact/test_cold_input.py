"""Fresh delta reconstruction excludes durable pending task writes and runtime objects."""

from dataclasses import replace
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from tests.fixtures.model_catalog import AddBindings


async def test_actual_persisted_pending_write_is_not_materialized_summary_input(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    model = SummaryModel(responses=["Original source summary. " * 100])
    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=lambda _: model)}
    )
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    before = await cold_reader(accepted.saver).aget_tuple(accepted.config)
    assert before is not None
    raw = AsyncPostgresSaver(cast(Any, aops_pool), serde=accepted.saver.serde)
    await raw.aput_writes(
        before.config,
        [("messages", [HumanMessage(id="pending-proof-gap", content="PENDING NEVER SUMMARIZE")])],
        "unexecuted-provider-task",
    )
    fresh = await cold_reader(accepted.saver).aget_tuple(accepted.config)
    assert fresh is not None and fresh.pending_writes
    assert any(task == "unexecuted-provider-task" for task, _, _ in fresh.pending_writes)
    assert all(m.id != "pending-proof-gap" for m in fresh.checkpoint["channel_values"]["messages"])
    # Mutating a previously read runtime object cannot alter fresh persisted input.
    before.checkpoint["channel_values"]["messages"].append(
        HumanMessage(content="RUNTIME NEVER SUMMARIZE")
    )
    await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert status["outcome"] == "rejected" and status["reason"] == "source_changed"
    assert status["attempt_id"] is None and status["checkpoint_id"] is None
    assert model.calls == 0 and model.inputs == []
    source = await cold_reader(accepted.saver).aget_tuple(before.config)
    assert source is not None and source.pending_writes
    assert any(m.id == "source-history" for m in source.checkpoint["channel_values"]["messages"])
    assert any(task == "unexecuted-provider-task" for task, _, _ in source.pending_writes)
