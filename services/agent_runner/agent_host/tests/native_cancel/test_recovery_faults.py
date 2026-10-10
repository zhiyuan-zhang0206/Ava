"""Certified-stop consumer pause precedes its ACK across every database fault."""

from contextlib import asynccontextmanager
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host.invocation import native_work as owner
from services.agent_runner.agent_host.tests.history.test_hosted_compact_failure import (
    _prepare_graph,
)
from services.agent_runner.agent_host.tests.native_cancel.test_transfer import (
    _assert_successor_turns,
    _force_successor,
)


@pytest.mark.parametrize(
    "site", ["before_projection", "before_flush", "after_flush", "before_ack", "after_ack"]
)
async def test_pause_fault_cannot_ack_then_resume_original_conversation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    target, successor, _force = await _force_successor(
        db_conn, aops_pool, database_gate=database_gate
    )
    replies: list[str] = []
    graph, saver, config, _history = await _prepare_graph(aops_pool, target.agent_id, 100, replies)
    injected = False
    actual_update, actual_flush, actual_tx = (
        graph.aupdate_state,
        owner.flush_checkpoint,
        owner.async_write_transaction,
    )

    async def update(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if site == "before_projection" and not injected:
            injected = True
            raise psycopg.OperationalError("test before consumer pause")
        return await actual_update(*args, **kwargs)

    async def flush(*args: Any, **kwargs: Any) -> None:
        nonlocal injected
        if site == "before_flush" and not injected:
            injected = True
            raise psycopg.OperationalError("test before consumer pause flush")
        await actual_flush(*args, **kwargs)
        if site == "after_flush" and not injected:
            injected = True
            raise psycopg.OperationalError("test lost consumer pause flush response")

    @asynccontextmanager
    async def transaction(*args: Any, **kwargs: Any):
        nonlocal injected
        finishing = False
        async with actual_tx(*args, **kwargs) as conn:
            yield conn
            row = await (
                await conn.execute(
                    "SELECT outcome FROM native_cancel_commands WHERE work_id=%s", (target.work_id,)
                )
            ).fetchone()
            finishing = row is not None and row[0] == "recovered_stopped"
            if finishing and site == "before_ack" and not injected:
                injected = True
                raise psycopg.OperationalError("test before recovery ACK commit")
        if finishing and site == "after_ack" and not injected:
            injected = True
            raise psycopg.OperationalError("test lost recovery ACK commit response")

    with monkeypatch.context() as patch:
        patch.setattr(graph, "aupdate_state", update)
        patch.setattr(owner, "flush_checkpoint", flush)
        patch.setattr(owner, "async_write_transaction", transaction)
        with pytest.raises(psycopg.OperationalError):
            await owner.recover_native_cancel(aops_pool, saver, graph, successor, resources=None)
    assert injected and replies == []
    state = db_conn.execute(
        "SELECT outcome,checkpoint_id,recovery_checkpoint_id FROM native_cancel_commands WHERE work_id=%s",
        (target.work_id,),
    ).fetchone()
    assert state is not None and state[1] is None
    assert state[0] == ("recovered_stopped" if site == "after_ack" else "accepted")
    if site != "after_ack":
        assert state[2] is None
    assert await owner.recover_native_cancel(aops_pool, saver, graph, successor, resources=None)
    await _assert_successor_turns(
        db_conn,
        aops_pool,
        target,
        successor,
        graph,
        saver,
        config,
        replies,
        expected_first=["continued"],
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
