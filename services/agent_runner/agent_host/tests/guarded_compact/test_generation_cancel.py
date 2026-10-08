"""Actual native cancellation waits for generation unwind and retains unknown result proof."""

import asyncio
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import _insert
from base.lm.plugin_providers import model_catalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from services.agent_runner.agent_host.tests.guarded_compact.test_late_cancel import accept_cancel
from tests.fixtures.model_catalog import AddBindings


async def test_actual_generation_cancel_has_one_attempt_closed_work_and_next_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
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

    binding = model_catalog().bindings["gpt-"]
    add_bindings(
        {
            "gpt-": replace(
                binding, build_single_attempt=lambda _: WaitingModel(responses=["unused"])
            )
        }
    )
    accepted = await admit(db_conn, aops_pool, client, monkeypatch)
    running = asyncio.create_task(accepted.host.run_turn(accepted.agent))
    await asyncio.wait_for(started.wait(), 3)
    cancellation = accept_cancel(client, accepted)
    await asyncio.wait_for(running, 5)
    status = accepted.status(client)
    assert ended.is_set() and calls == ["original"]
    assert status["outcome"] == "uncertain" and status["recovery_checkpoint_id"]
    assert status["checkpoint_id"] is None and not status["result_available"]
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE id=%s", (cancellation["command_id"],)
    ).fetchone() == ("applied",)
    assert db_conn.execute(
        "SELECT phase,ended_at IS NOT NULL FROM native_graph_work WHERE id=%s",
        (status["execution"]["work_id"],),
    ).fetchone() == ("settled", True)
    _insert(db_conn, accepted.agent)
    await accepted.host.run_turn(accepted.agent)
    assert calls == ["original"] and len(accepted.ordinary) == 2
