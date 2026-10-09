"""Actual inbound-history trigger and application permit order concurrent input."""

import asyncio
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact import apply as compact_apply
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from tests.fixtures.model_catalog import AddBindings


async def wait_for_blocker(conn: psycopg.Connection, blocker: int) -> None:
    async with asyncio.timeout(3):
        while not conn.execute(
            "SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))", (blocker,)
        ).fetchone():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("ordering", ["input_first", "permit_first"])
async def test_actual_trigger_orders_uncommitted_chat_against_application_permit(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    ordering: str,
    model_catalog: ModelCatalog,
) -> None:
    model = SummaryModel(responses=["Original source summary. " * 100])
    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=lambda _: model)}
    )
    accepted = await admit(db_conn, aops_pool, client, monkeypatch, catalog=model_catalog)
    original = compact_apply.authorize
    original_ack = compact_apply.acknowledge
    inserted: list[int] = []
    checked: list[int] = []

    async def authorize(*args: Any, **kwargs: Any) -> bool:
        permitted = False
        with psycopg.connect(db_conn.info.dsn) as late:
            if ordering == "permit_first":
                permitted = await original(*args, **kwargs)
                assert permitted
            row = late.execute(
                "INSERT INTO inbound_messages(agent_id,content,kind,source) "
                "VALUES(%s,'later input must survive','chat','user') RETURNING id",
                (accepted.agent,),
            ).fetchone()
            assert row is not None
            inserted.append(row[0])
            if ordering == "input_first":
                task = asyncio.create_task(original(*args, **kwargs))
                await wait_for_blocker(db_conn, late.info.backend_pid)
                assert not task.done()
                late.commit()
                permitted = await task
                assert not permitted
            else:
                late.commit()
            return permitted

    async def ack(*args: Any, **kwargs: Any) -> bool:
        result = await original_ack(*args, **kwargs)
        if result:
            assert db_conn.execute(
                "SELECT status FROM inbound_messages WHERE id=%s", (inserted[0],)
            ).fetchone() == ("pending",)
            persisted = await cold_reader(accepted.saver).aget_tuple(accepted.config)
            assert persisted is not None
            assert all(
                "later input must survive" not in str(m.content)
                for m in persisted.checkpoint["channel_values"]["messages"]
            )
            checked.append(inserted[0])
        return result

    monkeypatch.setattr(compact_apply, "authorize", authorize)
    monkeypatch.setattr(compact_apply, "acknowledge", ack)
    await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert status["outcome"] == ("rejected" if ordering == "input_first" else "applied")
    assert checked == ([] if ordering == "input_first" else inserted)
    assert model.calls == 1 and len(accepted.ordinary) == 2
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inserted[0],)
    ).fetchone() == ("done",)
    assert db_conn.execute(
        "SELECT phase,ended_at IS NOT NULL FROM native_graph_work WHERE id=%s",
        (status["execution"]["work_id"],),
    ).fetchone() == ("settled", True)
