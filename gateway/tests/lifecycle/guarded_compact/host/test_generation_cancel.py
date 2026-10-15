import asyncio
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.graph.interrupt import ModelInterruptedError
from agent.tests.claim.test_inbound_ownership import insert
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import AddBindings
from gateway.tests.lifecycle.guarded_compact.host.admission import admit
from gateway.tests.lifecycle.guarded_compact.host.test_late_cancel import accept_cancel
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact_support import SummaryModel


async def test_actual_generation_cancel_has_one_attempt_closed_work_and_next_chat(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    started = asyncio.Event()
    ended = asyncio.Event()
    calls: list[str] = []

    class WaitingModel(SummaryModel):
        async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("original")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog,
        {
            "gpt-": replace(
                binding, build_single_attempt=lambda _: WaitingModel(responses=["unused"])
            )
        },
    )
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    running = asyncio.create_task(accepted.host.run_turn(accepted.agent))
    await asyncio.wait_for(started.wait(), 3)
    cancellation = accept_cancel(client, accepted)
    with pytest.raises(ModelInterruptedError):
        await asyncio.wait_for(running, 5)
    status = accepted.status(client)
    assert ended.is_set() and calls == ["original"]
    assert status["outcome"] == "uncertain" and status["recovery_checkpoint_id"] is None
    assert status["checkpoint_id"] is None and not status["result_available"]
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE id=%s", (cancellation["command_id"],)
    ).fetchone() == ("accepted",)
    assert db_conn.execute(
        "SELECT phase,ended_at IS NOT NULL FROM native_graph_work WHERE id=%s",
        (status["execution"]["work_id"],),
    ).fetchone() == ("active", False)
    insert(db_conn, accepted.agent)
    await accepted.host.run_turn(accepted.agent)
    assert calls == ["original"] and len(accepted.ordinary) == 1
