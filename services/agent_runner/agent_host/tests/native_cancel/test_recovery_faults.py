"""Interrupted native cancel stays unknown across later inspection attempts."""

import asyncio

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.agents.inbox.native_cancel import accept_native_cancel
from base.db.code_version_gate import ProcessDbGate
from services.agent_runner.agent_host.invocation.native_work import recover_native_cancel
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work


async def test_interrupted_cancel_never_fabricates_completion(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    incarnation, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accepted = await asyncio.to_thread(
            accept_native_cancel, pool, "held-cancel", target.agent_id, target
        )
    for _ in range(2):
        assert not await recover_native_cancel(aops_pool, incarnation)
        assert db_conn.execute(
            "SELECT outcome,checkpoint_id,recovery_checkpoint_id FROM native_cancel_commands WHERE id=%s",
            (accepted.command_id,),
        ).fetchone() == ("uncertain", None, None)
    assert db_conn.execute(
        "SELECT phase FROM native_graph_work WHERE id=%s", (target.work_id,)
    ).fetchone() == ("uncertain",)
