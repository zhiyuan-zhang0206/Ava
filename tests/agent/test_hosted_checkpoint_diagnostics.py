"""Shared saver failures keep their actual checkpoint identity and propagate."""

import asyncio
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from psycopg_pool import AsyncConnectionPool, PoolClosed

from agent.impersonation import flush_checkpoint
from services.agent_host.daemon import _build_checkpointer
from shared.config import settings
from shared.log import logger
from shared.turn_identity import bind_turn_identity


@pytest.mark.parametrize("method", ["aput", "aput_writes", "flush"])
async def test_shared_saver_reports_each_failed_write_owner_without_swallowing(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    monkeypatch.setattr(settings.agent, "checkpoint_interval", 100)
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        settings.data_plane.db_url, min_size=0, max_size=2, open=False
    )
    await pool.open()
    saver = await _build_checkpointer(pool)
    configs: list[RunnableConfig] = []
    records: list[dict[str, Any]] = []

    def capture(message: Any) -> None:
        record = message.record
        if record["extra"].get("event") == "checkpoint_write_failed":
            records.append(record["extra"])

    sink = logger.add(capture, level="ERROR")
    try:
        for agent_id in (101, 202):
            config: RunnableConfig = {
                "configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}
            }
            saved = await saver.aput(
                config, empty_checkpoint(), {"source": "input", "step": -1, "parents": {}}, {}
            )
            configs.append(saved)
            if method == "flush":
                await saver.aput(
                    saved, empty_checkpoint(), {"source": "loop", "step": 1, "parents": {}}, {}
                )
        # A real pool/storage boundary fails, without replacing the saver methods.
        await pool.close()

        async def fail(config: RunnableConfig) -> None:
            if method == "aput":
                await saver.aput(
                    config, empty_checkpoint(), {"source": "input", "step": 0, "parents": {}}, {}
                )
            elif method == "aput_writes":
                await saver.aput_writes(config, [("messages", "unsaved")], "failed-task")
            else:
                assert "configurable" in config
                await flush_checkpoint(saver, int(config["configurable"]["thread_id"]))

        # A misleading enclosing turn must not replace either write's owner.
        with bind_turn_identity(999):
            errors = await asyncio.gather(
                *(fail(config) for config in configs), return_exceptions=True
            )
        assert len(errors) == 2 and all(isinstance(error, PoolClosed) for error in errors)
        assert sorted(record["agent_id"] for record in records) == [101, 202]
        assert {record["method"] for record in records} == {
            "aput_writes" if method == "aput_writes" else "aput"
        }
    finally:
        logger.remove(sink)
        await pool.close()
