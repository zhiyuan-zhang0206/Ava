"""Real PostgreSQL regressions for checkpoint pool isolation and lease cleanup."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.constants import PUSH
from psycopg import AsyncConnection, Capabilities, errors
from psycopg.pq import PipelineStatus, TransactionStatus
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import flush_checkpoint
from services.agent_host.daemon import _build_checkpointer
from services.agent_host.pooled_checkpoint import PooledPostgresSaver
from shared.config import settings

_LOCK_KEY = 918273


@asynccontextmanager
async def _pool(size: int) -> AsyncGenerator[AsyncConnectionPool[AsyncConnection[DictRow]]]:
    async with AsyncConnectionPool[AsyncConnection[DictRow]](
        settings.data_plane.db_url,
        min_size=size,
        max_size=size,
        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        open=False,
    ) as pool:
        await pool.wait()
        yield pool


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _checkpoint() -> Checkpoint:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"value": ["persisted"]}
    checkpoint["channel_versions"] = {"value": "1"}
    return checkpoint


async def _wait_for_blocked_query(conn: AsyncConnection) -> None:
    """Observe PostgreSQL waiting on a lock, not just an asyncio task starting."""
    async with asyncio.timeout(5):
        while True:
            cur = await conn.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                "WHERE NOT l.granted AND a.datname = current_database()"
            )
            row = await cur.fetchone()
            assert row is not None
            if row[0]:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_blocked_agent_checkpoint_does_not_block_another_agent(
    adb_conn: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
    operation: Literal["read", "write"],
) -> None:
    async with _pool(2) as pool:
        saver = await _build_checkpointer(cast(AsyncConnectionPool[AsyncConnection], pool))
        checkpoint = _checkpoint()
        blocked_config = await saver.aput(
            _config("101"), checkpoint, {"source": "input", "step": -1}, {"value": "1"}
        )
        await saver.aput(
            _config("202"), _checkpoint(), {"source": "input", "step": -1}, {"value": "1"}
        )

        if operation == "read":
            # Ordinary reads do not wait on row locks. Add one advisory-lock
            # expression to this real read, evaluated only for the blocked ID.
            monkeypatch.setattr(
                saver,
                "SELECT_SQL",
                saver.SELECT_SQL.replace(
                    "select",
                    f"select CASE WHEN thread_id = '101' "
                    f"THEN pg_advisory_xact_lock({_LOCK_KEY}) END AS test_gate,",
                    1,
                ),
            )
            await adb_conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        else:
            # The upsert must wait for this exact agent's row; another agent's
            # row and every spare pool connection remain available.
            await adb_conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = %s FOR UPDATE",
                ("101",),
            )

        async def run_blocked_operation() -> None:
            if operation == "read":
                await saver.aget_tuple(blocked_config)
            else:
                await saver.aput(
                    blocked_config,
                    checkpoint,
                    {"source": "update", "step": 0},
                    {"value": "1"},
                )

        blocked = asyncio.create_task(run_blocked_operation())
        try:
            await _wait_for_blocked_query(adb_conn)
            async with asyncio.timeout(2):
                free_config = await saver.aput(
                    _config("202"),
                    _checkpoint(),
                    {"source": "update", "step": 0},
                    {"value": "1"},
                )
                await saver.aput_writes(free_config, [(PUSH, "value")], "free-task")
                loaded = await saver.aget_tuple(free_config)
            assert loaded is not None
            assert loaded.checkpoint["channel_values"]["value"] == ["persisted"]
            assert loaded.pending_writes == [("free-task", PUSH, "value")]
            assert not blocked.done()
        finally:
            await adb_conn.rollback()
            await asyncio.wait_for(blocked, timeout=5)


async def test_same_agent_flush_serializes_with_newer_checkpoint(
    adb_conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_interval", 4)
    async with _pool(2) as pool:
        saver = await _build_checkpointer(cast(AsyncConnectionPool[AsyncConnection], pool))
        initial = await saver.aput(
            _config("101"), _checkpoint(), {"source": "input", "step": -1}, {"value": "1"}
        )
        tail, newer = _checkpoint(), _checkpoint()
        await saver.aput(initial, tail, {"source": "loop", "step": 1}, {"value": "1"})
        # Model a save that reached PostgreSQL before its acknowledgement was
        # lost: the buffered flush must retry this existing row. Lock it so
        # the real flush waits while another connection remains available.
        tail_config = await PooledPostgresSaver(pool).aput(
            initial, tail, {"source": "loop", "step": 1}, {"value": "1"}
        )
        await adb_conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = %s AND checkpoint_id = %s FOR UPDATE",
            ("101", tail["id"]),
        )
        flushing = asyncio.create_task(flush_checkpoint(saver, 101))
        started = asyncio.Event()

        async def write_newer() -> None:
            started.set()
            await saver.aput(tail_config, newer, {"source": "loop", "step": 2}, {"value": "1"})

        writing: asyncio.Task[None] | None = None
        try:
            await _wait_for_blocked_query(adb_conn)
            writing = asyncio.create_task(write_newer())
            await asyncio.wait_for(started.wait(), timeout=2)
            # A skipped write has no database await: without the per-thread
            # lock it completes here and replaces the in-flight flush's tail.
            assert not writing.done()
            assert not flushing.done()
        finally:
            await adb_conn.rollback()
            await asyncio.wait_for(flushing, timeout=5)
            if writing is not None:
                await asyncio.wait_for(writing, timeout=5)

        await flush_checkpoint(saver, 101)
        stored = await saver.aget_tuple(_config("101"))
        assert stored is not None
        assert stored.checkpoint["id"] == newer["id"]
        assert stored.parent_config == tail_config
        stored_tail = await saver.aget_tuple(tail_config)
        assert stored_tail is not None
        assert stored_tail.parent_config == initial


@pytest.mark.parametrize("supports_pipeline", [True, False], ids=["pipeline", "transaction"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_failed_cursor_returns_a_clean_reusable_pool_connection(
    adb_conn: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
    supports_pipeline: bool,
    failure: Literal["error", "cancel"],
) -> None:
    # Exercise upstream's transaction fallback as well as real pipeline mode.
    if not supports_pipeline:

        def unavailable(_self: Capabilities) -> bool:
            return False

        monkeypatch.setattr(Capabilities, "has_pipeline", unavailable)

    async with _pool(1) as pool:
        saver = PooledPostgresSaver(pool)
        async with pool.connection() as conn:
            original_backend = conn.info.backend_pid

        async def fail() -> None:
            async with saver._cursor(pipeline=True) as cur:
                if failure == "error":
                    await cur.execute("SELECT 1 / 0")
                else:
                    await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                await cur.fetchone()

        if failure == "error":
            with pytest.raises(errors.DivisionByZero):
                await fail()
        else:
            await adb_conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            pending = asyncio.create_task(fail())
            try:
                await _wait_for_blocked_query(adb_conn)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, timeout=5)
            finally:
                await adb_conn.rollback()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

        async with pool.connection() as conn:
            assert conn.info.backend_pid == original_backend
            assert conn.info.transaction_status == TransactionStatus.IDLE
            assert conn.info.pipeline_status == PipelineStatus.OFF
        saved = await saver.aput(
            _config("after-failure"),
            _checkpoint(),
            {"source": "input", "step": -1},
            {"value": "1"},
        )
        loaded = await saver.aget_tuple(saved)
        assert loaded is not None
        assert loaded.checkpoint["channel_values"]["value"] == ["persisted"]
