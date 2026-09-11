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
from pydantic import ValidationError

from agent.impersonation import flush_checkpoint
from agent.state import build_checkpoint_serde
from services.agent_host import pooled_checkpoint as pooled_ckpt
from services.agent_host.daemon import _build_checkpointer
from services.agent_host.pooled_checkpoint import PooledPostgresSaver
from shared.config import settings
from shared.config.agent_runtime import AgentRuntimeSettings

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


def test_checkpoint_max_blob_bytes_config_default_and_metadata() -> None:
    field = AgentRuntimeSettings.model_fields["checkpoint_max_blob_bytes"]
    assert field.default == 16 * 1024 * 1024
    assert field.alias == "AVA_CHECKPOINT_MAX_BLOB_BYTES"
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert extra["scope"] == "cluster-pinned"
    assert extra["restart_required"] == "agent"
    assert extra["writable"] is True

    overrides_field = AgentRuntimeSettings.model_fields["checkpoint_max_blob_bytes_overrides"]
    assert overrides_field.alias == "AVA_CHECKPOINT_MAX_BLOB_BYTES_OVERRIDES"
    assert overrides_field.default_factory is not None
    overrides_extra = overrides_field.json_schema_extra
    assert isinstance(overrides_extra, dict)
    assert overrides_extra["scope"] == "cluster-pinned"
    assert overrides_extra["restart_required"] == "agent"


def test_blob_limit_overrides_config_parses_and_validates() -> None:
    assert AgentRuntimeSettings.model_validate({}).checkpoint_max_blob_bytes_overrides == {}

    # The operator path: the JSON-object string an env var carries.
    configured = AgentRuntimeSettings.model_validate(
        {"AVA_CHECKPOINT_MAX_BLOB_BYTES_OVERRIDES": '{"6093": 33554432}'}
    )
    assert configured.checkpoint_max_blob_bytes_overrides == {"6093": 33554432}

    with pytest.raises(ValidationError, match="numeric agent id"):
        AgentRuntimeSettings.model_validate(
            {"AVA_CHECKPOINT_MAX_BLOB_BYTES_OVERRIDES": '{"abc": 1}'}
        )
    with pytest.raises(ValidationError, match="must be positive"):
        AgentRuntimeSettings.model_validate(
            {"AVA_CHECKPOINT_MAX_BLOB_BYTES_OVERRIDES": '{"6093": 0}'}
        )


def _guarded_saver() -> PooledPostgresSaver:
    """The real saver without a database: _dump_blobs/_dump_writes never touch conn."""
    return PooledPostgresSaver(
        conn=cast("AsyncConnectionPool[AsyncConnection[DictRow]]", None),
        serde=build_checkpoint_serde(),
    )


async def test_dump_blobs_passes_blobs_under_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_max_blob_bytes", 1024 * 1024)
    saver = _guarded_saver()

    rows = saver._dump_blobs("t", "", {"messages": ["small"]}, {"messages": "1"})

    assert len(rows) == 1
    assert rows[0][2] == "messages"
    assert isinstance(rows[0][-1], bytes)


async def test_dump_blobs_refuses_a_blob_over_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_max_blob_bytes", 4096)
    saver = _guarded_saver()

    with pytest.raises(Exception, match="checkpoint write refused") as excinfo:
        saver._dump_blobs("t", "", {"messages": ["x" * 8192]}, {"messages": "1"})

    assert isinstance(excinfo.value, pooled_ckpt.CheckpointBlobTooLargeError)
    assert "'messages'" in str(excinfo.value)
    assert "AVA_CHECKPOINT_MAX_BLOB_BYTES" in str(excinfo.value)


async def test_dump_writes_refuses_a_value_over_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_max_blob_bytes", 4096)
    saver = _guarded_saver()

    with pytest.raises(Exception, match="checkpoint write refused") as excinfo:
        saver._dump_writes("t", "", "ckpt", "task", "", [("messages", ["x" * 8192])])

    assert isinstance(excinfo.value, pooled_ckpt.CheckpointBlobTooLargeError)


async def test_oversized_checkpoint_write_is_refused_and_writes_nothing(
    adb_conn: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_interval", 1)
    monkeypatch.setattr(settings.agent, "checkpoint_max_blob_bytes", 64 * 1024)
    async with _pool(2) as pool:
        saver = await _build_checkpointer(cast(AsyncConnectionPool[AsyncConnection], pool))
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": ["x" * (256 * 1024)]}
        checkpoint["channel_versions"] = {"messages": "1"}

        with pytest.raises(Exception, match="checkpoint write refused") as excinfo:
            await saver.aput(
                _config("6094"), checkpoint, {"source": "update", "step": 0}, {"messages": "1"}
            )
        assert isinstance(excinfo.value, pooled_ckpt.CheckpointBlobTooLargeError)

        cursor = await adb_conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", ("6094",)
        )
        row = await cursor.fetchone()
        assert row is not None and row[0] == 0
        cursor = await adb_conn.execute(
            "SELECT count(*) FROM checkpoint_blobs WHERE thread_id = %s", ("6094",)
        )
        row = await cursor.fetchone()
        assert row is not None and row[0] == 0

        # The same payload stores once an operator raises the limit.
        monkeypatch.setattr(settings.agent, "checkpoint_max_blob_bytes", 1024 * 1024)
        saved = await saver.aput(
            _config("6094"), checkpoint, {"source": "update", "step": 0}, {"messages": "1"}
        )
        saved_config = cast("dict[str, str]", saved.get("configurable"))
        assert saved_config["checkpoint_id"] == checkpoint["id"]


async def test_blob_limit_override_lifts_the_limit_for_one_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_max_blob_bytes", 4096)
    monkeypatch.setattr(
        settings.agent, "checkpoint_max_blob_bytes_overrides", {"6093": 1024 * 1024}
    )
    saver = _guarded_saver()

    rows = saver._dump_blobs("6093", "", {"messages": ["x" * 8192]}, {"messages": "1"})
    assert len(rows) == 1

    with pytest.raises(Exception, match="checkpoint write refused") as excinfo:
        saver._dump_blobs("6094", "", {"messages": ["x" * 8192]}, {"messages": "1"})
    assert isinstance(excinfo.value, pooled_ckpt.CheckpointBlobTooLargeError)
    assert "AVA_CHECKPOINT_MAX_BLOB_BYTES" in str(excinfo.value)


async def test_blob_limit_override_can_lower_one_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_max_blob_bytes", 1024 * 1024)
    monkeypatch.setattr(settings.agent, "checkpoint_max_blob_bytes_overrides", {"6093": 4096})
    saver = _guarded_saver()

    with pytest.raises(Exception, match="checkpoint write refused") as excinfo:
        saver._dump_blobs("6093", "", {"messages": ["x" * 8192]}, {"messages": "1"})
    assert isinstance(excinfo.value, pooled_ckpt.CheckpointBlobTooLargeError)
    assert "AVA_CHECKPOINT_MAX_BLOB_BYTES_OVERRIDES" in str(excinfo.value)


async def test_blob_limit_override_applies_to_the_writes_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_max_blob_bytes", 4096)
    monkeypatch.setattr(
        settings.agent, "checkpoint_max_blob_bytes_overrides", {"6093": 1024 * 1024}
    )
    saver = _guarded_saver()

    rows = saver._dump_writes("6093", "", "ckpt", "task", "", [("messages", ["x" * 8192])])
    assert len(rows) == 1
