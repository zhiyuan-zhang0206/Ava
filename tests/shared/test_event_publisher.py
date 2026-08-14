"""AgentEventPublisher (shared/event_publisher.py) unit tests.

The publisher is the best-effort SSE fan-out for one agent process: callers
`emit(payload)` (synchronous, never blocks, never raises) and a single
background worker serially publishes to the central Redis. Serial = the SSE
ordering contract (Start before Delta, chunk concatenation) is preserved.
A slow/unreachable central Redis must degrade the live view, never stall the
agent's control flow — so a publish that times out or errors drops that event
and the worker keeps going; a full queue drops rather than blocking emit.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import redis.asyncio as aredis

from shared.event_publisher import AgentEventPublisher


class _FakePipeline:
    """Minimal pipeline: collects publish commands, executes them in order on
    execute() — one round-trip, mirroring the real Redis pipeline contract.
    `fail` payloads are command-level errors (like an ACL NOPERM): under
    `raise_on_error=False` they come back as Exception results and the rest of
    the batch still goes out, matching redis-py's real semantics."""

    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._cmds: list[tuple[str, str]] = []

    def publish(self, channel: str, payload: str) -> _FakePipeline:
        self._cmds.append((channel, payload))
        return self

    async def execute(self, raise_on_error: bool = True) -> list[object]:
        from redis.exceptions import ResponseError

        results: list[object] = []
        for channel, payload in self._cmds:
            if payload in self._redis.hang:
                await asyncio.Event().wait()  # block forever — connection-level stall
            if payload in self._redis.fail:
                err = ResponseError("redis down")
                if raise_on_error:
                    raise err
                results.append(err)
                continue
            self._redis.published.append((channel, payload))
            results.append(1)  # subscriber count
        return results


class _FakeRedis:
    """Records publish(channel, payload) calls in arrival order. A payload in
    `fail` raises; one in `hang` never returns (to exercise the publish timeout).
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.fail: set[str] = set()
        self.hang: set[str] = set()
        self.connection_pool: object | None = None  # real clients carry a pool; see reconnect test

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    async def publish(self, channel: str, payload: str) -> None:
        if payload in self.hang:
            await asyncio.Event().wait()  # block forever
        if payload in self.fail:
            raise ConnectionError("redis down")
        self.published.append((channel, payload))


def _pub(redis: _FakeRedis, **kwargs: Any) -> AgentEventPublisher:
    """Build a publisher over the fake redis. The cast bridges `_FakeRedis`
    (which only duck-types the `publish()` the worker calls) to the declared
    `redis_async: aredis.Redis` parameter — production always passes a real one."""
    return AgentEventPublisher(cast(aredis.Redis, redis), "ch", agent_id=1, **kwargs)


async def test_emits_in_fifo_order() -> None:
    redis = _FakeRedis()
    pub = _pub(redis)
    await pub.start()
    for i in range(5):
        pub.emit(f"e{i}")
    await pub.aclose()  # bounded drain flushes the queue
    assert [p for _, p in redis.published] == ["e0", "e1", "e2", "e3", "e4"]


async def test_emit_never_blocks_or_raises_even_unstarted() -> None:
    # No worker started: emit past maxsize must neither block nor raise.
    pub = _pub(_FakeRedis(), maxsize=2)
    for i in range(10):
        pub.emit(f"e{i}")  # must not raise QueueFull


async def test_sheds_oldest_when_queue_full() -> None:
    # Worker not started, so nothing drains: past capacity the OLDEST buffered
    # event is shed to keep the newest — the live view is worth the most-recent
    # state; the turn-end snapshot repairs any gap in the past.
    pub = _pub(_FakeRedis(), maxsize=3)
    for i in range(5):
        pub.emit(f"e{i}")
    drained: list[str] = []
    while not pub._queue.empty():
        drained.append(pub._queue.get_nowait())
    assert drained == ["e2", "e3", "e4"]


async def test_publish_failure_keeps_worker_alive() -> None:
    redis = _FakeRedis()
    redis.fail.add("bad")
    pub = _pub(redis)
    await pub.start()
    pub.emit("bad")  # publish raises -> dropped, worker survives
    pub.emit("good")
    await pub.aclose()
    assert ("ch", "good") in redis.published
    assert ("ch", "bad") not in redis.published


async def test_publish_timeout_drops_and_continues() -> None:
    redis = _FakeRedis()
    redis.hang.add("slow")
    pub = _pub(redis, publish_timeout=0.05)
    await pub.start()
    pub.emit("slow")  # hangs -> times out -> the whole batch is dropped
    await pub._queue.join()  # slow's batch is processed (and shed)
    pub.emit("fast")  # next batch publishes normally
    await pub.aclose()
    assert ("ch", "fast") in redis.published
    assert ("ch", "slow") not in redis.published


async def test_aclose_drain_is_bounded() -> None:
    # A publish that never returns must not make aclose hang: the drain is
    # time-boxed, then the worker is cancelled.
    redis = _FakeRedis()
    redis.hang.add("stuck")
    pub = _pub(redis, publish_timeout=10.0, drain_timeout=0.1)
    await pub.start()
    pub.emit("stuck")
    await asyncio.wait_for(pub.aclose(), timeout=1.0)


async def test_aclose_is_idempotent_and_safe_without_start() -> None:
    pub = _pub(_FakeRedis())
    await pub.aclose()  # never started — must not raise
    await pub.start()
    await pub.aclose()
    await pub.aclose()  # second close is a no-op


async def test_drains_in_batches_preserving_order() -> None:
    # Many queued events go out in one pipeline round-trip, FIFO preserved.
    redis = _FakeRedis()
    pub = _pub(redis)
    await pub.start()
    for i in range(150):
        pub.emit(f"e{i}")
    await pub.aclose()
    assert [p for _, p in redis.published] == [f"e{i}" for i in range(150)]


async def test_batch_publish_failure_sheds_batch_and_keeps_worker_alive() -> None:
    # A pipeline failure drops the whole batch; the worker survives and the
    # next batch publishes normally.
    redis = _FakeRedis()
    redis.fail.add("bad")
    pub = _pub(redis)
    await pub.start()
    pub.emit("bad")
    pub.emit("good")
    await pub.aclose()
    assert ("ch", "good") in redis.published
    assert ("ch", "bad") not in redis.published


async def test_batch_publish_timeout_sheds_batch_and_continues() -> None:
    redis = _FakeRedis()
    redis.hang.add("slow")
    pub = _pub(redis, publish_timeout=0.05)
    await pub.start()
    pub.emit("slow")  # connection-level stall -> batch shed
    await pub._queue.join()  # slow's batch is processed (and shed)
    pub.emit("fast")  # next batch publishes normally
    await pub.aclose()
    assert ("ch", "fast") in redis.published
    assert ("ch", "slow") not in redis.published


async def test_connection_level_failure_tears_down_pool_for_reconnect() -> None:
    # A connection-level failure (timeout) sheds the batch AND disconnects the
    # pool, so the next batch reconnects fresh instead of riding a half-dead
    # socket (keepalive/health-check would otherwise take up to ~30s).
    redis = _FakeRedis()

    class _Pool:
        def __init__(self) -> None:
            self.disconnects = 0

        async def disconnect(self, inuse_connections: bool = False) -> None:
            # async like redis-py's real ConnectionPool.disconnect: the worker
            # must await it or the tear-down never runs (an async method called
            # without await is a discarded coroutine).
            self.disconnects += 1

    pool = _Pool()
    redis.connection_pool = pool
    redis.hang.add("slow")
    pub = _pub(redis, publish_timeout=0.05)
    await pub.start()
    pub.emit("slow")
    await pub._queue.join()
    assert pool.disconnects == 1
    pub.emit("fast")
    await pub.aclose()
    assert ("ch", "fast") in redis.published


async def test_aclose_join_completes_after_queue_full_shed() -> None:
    # A queue-full shed must account the shedded event's task_done: an
    # asyncio.Queue's unfinished-task count is released only by task_done (get
    # does not touch it) and the worker never dequeues a shedded event, so
    # without the accounting join() — aclose's bounded drain — can never
    # complete once the queue has ever been full and aclose always waits the
    # full drain_timeout before cancelling the worker.
    redis = _FakeRedis()
    pub = _pub(redis, maxsize=2)
    await pub.start()
    for i in range(20):
        pub.emit(f"e{i}")  # repeatedly full -> sheds the oldest each time
    # join() completes once the worker has drained; with the task_done bug it
    # never completes and this wait_for times out.
    await asyncio.wait_for(pub._queue.join(), timeout=2.0)
    await pub.aclose()
    assert ("ch", "e19") in redis.published
