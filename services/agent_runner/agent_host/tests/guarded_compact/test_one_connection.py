"""Actual generation and saver paths do not retain a DB connection across network work."""

import asyncio
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from base.lm.plugin_providers import model_catalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from tests.fixtures.model_catalog import AddBindings


async def test_real_chain_pool_one_provider_can_borrow_only_connection(
    db_conn: psycopg.Connection,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
) -> None:
    async with AsyncConnectionPool[psycopg.AsyncConnection](
        db_conn.info.dsn, min_size=1, max_size=1, open=False
    ) as pool:
        queried: list[int] = []

        class BorrowingModel(SummaryModel):
            async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
                async with pool.connection(timeout=1) as conn:
                    row = await (await conn.execute("SELECT 1")).fetchone()
                    assert row is not None and row == (1,)
                    queried.append(row[0])
                return await super().ainvoke(*args, **kwargs)

        model = BorrowingModel(responses=["Original source summary. " * 100])
        binding = model_catalog().bindings["gpt-"]
        add_bindings({"gpt-": replace(binding, build_single_attempt=lambda _: model)})
        accepted = await admit(db_conn, pool, client, monkeypatch)
        await asyncio.wait_for(accepted.host.run_turn(accepted.agent), 5)
        assert accepted.status(client)["outcome"] == "applied"
        assert queried == [1] and model.calls == 1
