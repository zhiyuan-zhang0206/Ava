from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from base.log.tests.log_records import LogRecords

from ..publisher import AgentEventPublisher


def _publisher(maxsize: int = 2) -> AgentEventPublisher:
    redis = MagicMock()
    redis.connection_pool.disconnect = AsyncMock()
    return AgentEventPublisher(
        redis, "ava:events", agent_id=42, maxsize=maxsize, publish_timeout=0.1
    )


def _reports(records: LogRecords) -> list[tuple[str, dict[str, Any]]]:
    return [(r["level"].name, r["extra"]) for r in records.events("sse_drop")]


def test_queue_full_emit_reports_sse_drop_event(loguru_records: LogRecords) -> None:
    pub = _publisher(maxsize=2)
    pub.emit("one")
    pub.emit("two")
    assert _reports(loguru_records) == [], "no drop yet — queue has room"
    pub.emit("three")
    pub.emit("four")

    assert len(_reports(loguru_records)) == 1
    level, kw = _reports(loguru_records)[0]
    assert level == "WARNING"
    assert kw["event"] == "sse_drop"
    assert kw["kind"] == "queue_full"
    assert kw["n"] == 1
    assert kw["aid"] == 42
    assert kw["queue_size"] == 2

    pub._last_warn = 0.0
    pub.emit("five")
    pub.emit("six")
    assert len(_reports(loguru_records)) == 2
    assert _reports(loguru_records)[1][1]["n"] == 2


def test_publish_error_emit_reports_sse_drop_event(loguru_records: LogRecords) -> None:
    pub = _publisher()
    pub._note_drop("publish_error", detail="ConnectionError('boom')")
    assert len(_reports(loguru_records)) == 1
    level, kw = _reports(loguru_records)[0]
    assert level == "INFO"
    assert kw["event"] == "sse_drop"
    assert kw["kind"] == "publish_error"
    assert kw["detail"] == "ConnectionError('boom')"
    assert kw["n"] == 1


async def test_publish_error_detail_carries_the_class_and_message(
    monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords
) -> None:
    async with asyncio.TaskGroup() as tasks:

        def _boom(_batch: list[str]) -> None:
            raise RedisConnectionError("no route to host")

        pub = _publisher()
        monkeypatch.setattr(pub, "_publish_batch", _boom)
        await pub.start(tasks)
        pub.emit("one")
        for _ in range(200):
            if _reports(loguru_records):
                break
            await asyncio.sleep(0.01)
        await pub.aclose()

        assert len(_reports(loguru_records)) == 1
        level, kw = _reports(loguru_records)[0]
        assert level == "INFO"
        assert kw["kind"] == "publish_error"
        assert kw["detail"] == "ConnectionError: no route to host"


async def test_batch_command_failure_detail_carries_the_class_and_message(
    loguru_records: LogRecords,
) -> None:
    failure = RedisConnectionError("broken pipe")

    class _Pipeline:
        def publish(self, *_args: object, **_kwargs: object) -> None: ...

        async def execute(self, *, raise_on_error: bool = False) -> list[object]:
            assert raise_on_error is False
            return [failure]

    redis = MagicMock()
    redis.pipeline.return_value = _Pipeline()
    pub = AgentEventPublisher(redis, "ava:events", agent_id=42, maxsize=2, publish_timeout=0.1)
    await pub._publish_batch(["one"])

    assert len(_reports(loguru_records)) == 1
    level, kw = _reports(loguru_records)[0]
    assert level == "INFO"
    assert kw["kind"] == "publish_error"
    assert kw["detail"] == "ConnectionError: broken pipe"


def test_mixed_window_level_follows_the_worst_drop(loguru_records: LogRecords) -> None:
    pub = _publisher()
    pub._last_warn = time.monotonic()
    pub._note_drop("publish_error", detail="TimeoutError('slow')")
    pub._note_drop("queue_full")
    assert _reports(loguru_records) == []
    pub._flush_warn()
    assert len(_reports(loguru_records)) == 1
    level, kw = _reports(loguru_records)[0]
    assert level == "WARNING"
    assert kw["kind"] == "queue_full"
    assert kw["n"] == 2

    pub._last_warn = time.monotonic()
    pub._note_drop("queue_full")
    pub._note_drop("publish_error", detail="TimeoutError('slow')")
    pub._flush_warn()
    assert len(_reports(loguru_records)) == 2
    level, kw = _reports(loguru_records)[1]
    assert level == "WARNING"
    assert kw["kind"] == "publish_error"
    assert kw["n"] == 2
