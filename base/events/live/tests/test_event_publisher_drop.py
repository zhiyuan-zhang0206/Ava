"""`AgentEventPublisher` sheds → one structured `sse_drop` event.

The ops monitor panel's SSE-backlog metric reads `sse_drop` events from the
event store; this locks the emit contract (event name, kind values, payload
fields, and the cause-routed level) without a live Redis — emit() is
synchronous and the drop report is rate-limited but fires on the first drop
(monotonic clock is far past 0).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from ..publisher import AgentEventPublisher


def _publisher(maxsize: int = 2) -> AgentEventPublisher:
    redis = MagicMock()
    redis.connection_pool.disconnect = AsyncMock()
    return AgentEventPublisher(
        redis, "ava:events", agent_id=42, maxsize=maxsize, publish_timeout=0.1
    )


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    """Capture the drop report's `logger.log(level, ...)` calls as (level, fields)."""
    reports: list[tuple[str, dict[str, object]]] = []
    fake_logger = MagicMock()
    fake_logger.log.side_effect = lambda level, _msg, **kw: reports.append((level, kw))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    monkeypatch.setattr("base.events.live.publisher.logger", fake_logger)
    return reports


def test_queue_full_emit_reports_sse_drop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filling the queue sheds the newest event and logs one structured
    `sse_drop` line with kind=queue_full and the payload fields the panel
    reads (n, aid, queue_size). Local backpressure stays WARNING."""
    reports = _capture(monkeypatch)
    pub = _publisher(maxsize=2)
    pub.emit("one")
    pub.emit("two")
    assert reports == [], "no drop yet — queue has room"
    pub.emit("three")  # queue full -> shed -> first (rate-limited) report
    pub.emit("four")  # still full -> shed, same report window -> no second line

    assert len(reports) == 1  # pyright: ignore[reportUnknownArgumentType]
    level, kw = reports[0]
    assert level == "WARNING"
    assert kw["event"] == "sse_drop"
    assert kw["kind"] == "queue_full"
    assert kw["n"] == 1  # delta since last report — the second shed waits
    assert kw["aid"] == 42
    assert kw["queue_size"] == 2

    # Simulate the rate-limit window elapsing: the next shed reports the
    # accumulated delta (drops 2..3), so no drop is lost to the rate limit.
    pub._last_warn = 0.0
    pub.emit("five")  # drop 3 -> reports delta 3-1=2
    pub.emit("six")  # drop 4 -> same report window, no second line
    assert len(reports) == 2  # pyright: ignore[reportUnknownArgumentType]
    assert reports[1][1]["n"] == 2


def test_publish_error_emit_reports_sse_drop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """A publish failure (redis down / slow) sheds with kind=publish_error and
    carries the exception repr as detail. The transport-side drop is the
    flaky-link norm and reports at INFO (2026-10-03 triage)."""
    reports = _capture(monkeypatch)
    pub = _publisher()
    pub._note_drop("publish_error", detail="ConnectionError('boom')")
    assert len(reports) == 1  # pyright: ignore[reportUnknownArgumentType]
    level, kw = reports[0]
    assert level == "INFO"
    assert kw["event"] == "sse_drop"
    assert kw["kind"] == "publish_error"
    assert kw["detail"] == "ConnectionError('boom')"
    assert kw["n"] == 1


async def test_publish_error_detail_carries_the_class_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with asyncio.TaskGroup() as tasks:
        """A transport failure's detail keeps the class AND the message: redis-py
        8.1's repr reads "network:ConnectionError" — the message that names the
        real fault is gone (task #4964). The report stays INFO: transport-side,
        not local backpressure (2026-10-03 triage #4)."""
        reports = _capture(monkeypatch)

        def _boom(_batch: list[str]) -> None:
            raise RedisConnectionError("no route to host")

        pub = _publisher()
        monkeypatch.setattr(pub, "_publish_batch", _boom)
        await pub.start(tasks)
        pub.emit("one")
        for _ in range(200):
            if reports:
                break
            await asyncio.sleep(0.01)
        await pub.aclose()

        assert len(reports) == 1  # pyright: ignore[reportUnknownArgumentType]
        level, kw = reports[0]
        assert level == "INFO"
        assert kw["kind"] == "publish_error"
        assert kw["detail"] == "ConnectionError: no route to host"


async def test_batch_command_failure_detail_carries_the_class_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-command shed path describes its failing result the same way
    (task #4964)."""
    reports = _capture(monkeypatch)

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

    assert len(reports) == 1  # pyright: ignore[reportUnknownArgumentType]
    level, kw = reports[0]
    assert level == "INFO"
    assert kw["kind"] == "publish_error"
    assert kw["detail"] == "ConnectionError: broken pipe"


def test_mixed_window_level_follows_the_worst_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """One queue_full in the report window wins the level, whichever drop came
    last: a window that shed locally is backpressure, never downplayed by a
    following transport error."""
    reports = _capture(monkeypatch)
    pub = _publisher()
    pub._last_warn = time.monotonic()  # hold the rate limit: both drops in one window
    pub._note_drop("publish_error", detail="TimeoutError('slow')")
    pub._note_drop("queue_full")
    assert reports == []
    pub._flush_warn()
    assert len(reports) == 1  # pyright: ignore[reportUnknownArgumentType]
    level, kw = reports[0]
    assert level == "WARNING"
    assert kw["kind"] == "queue_full"  # the latest cause is still named
    assert kw["n"] == 2

    # The reverse order (last cause a publish error) still reports WARNING.
    pub._last_warn = time.monotonic()
    pub._note_drop("queue_full")
    pub._note_drop("publish_error", detail="TimeoutError('slow')")
    pub._flush_warn()
    assert len(reports) == 2  # pyright: ignore[reportUnknownArgumentType]
    level, kw = reports[1]
    assert level == "WARNING"
    assert kw["kind"] == "publish_error"
    assert kw["n"] == 2
