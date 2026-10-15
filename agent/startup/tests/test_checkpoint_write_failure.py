from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.startup import wrap_saver_writes_with_loud_failure


class _StubSaver(AsyncPostgresSaver):
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def aput(self, config: RunnableConfig, *_args: Any, **_kwargs: Any) -> Any:
        raise self._error

    async def aput_writes(self, config: RunnableConfig, *_args: Any, **_kwargs: Any) -> Any:
        raise self._error


def _write_failure_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record["extra"]
        for record in records
        if record["extra"].get("event") == "checkpoint_write_failed"
    ]


_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


async def test_cancellation_passes_through_without_a_write_failure_event(
    loguru_records: list[dict[str, Any]],
) -> None:
    saver = _StubSaver(asyncio.CancelledError())
    wrap_saver_writes_with_loud_failure(saver)
    with pytest.raises(asyncio.CancelledError):
        await saver.aput(_CONFIG, None, None, None)
    with pytest.raises(asyncio.CancelledError):
        await saver.aput_writes(_CONFIG, None, None)
    assert _write_failure_events(loguru_records) == []


async def test_real_failures_still_log(
    loguru_records: list[dict[str, Any]],
) -> None:
    saver = _StubSaver(RuntimeError("write failed"))
    wrap_saver_writes_with_loud_failure(saver)
    with pytest.raises(RuntimeError):
        await saver.aput(_CONFIG, None, None, None)
    with pytest.raises(RuntimeError):
        await saver.aput_writes(_CONFIG, None, None)
    assert [event["method"] for event in _write_failure_events(loguru_records)] == [
        "aput",
        "aput_writes",
    ]
