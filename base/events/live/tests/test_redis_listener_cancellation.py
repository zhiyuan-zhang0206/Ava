"""Open cancellation rolls back pending handles and preserves completed ownership."""

import asyncio

import pytest

from base.events.live import redis_listener


class _PubSub:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.open_task: asyncio.Task[object] | None = None
        self.parent: asyncio.Task[None] | None = None
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.open_task = asyncio.current_task()
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.parent is not None:
            # Run after this child has returned and attached the handles, but
            # before asyncio.wait's child-completion callback wakes the parent.
            asyncio.get_running_loop().call_soon(self.parent.cancel)

    async def aclose(self) -> None:
        self.closed = True


class _Redis:
    def __init__(self, pubsub: _PubSub) -> None:
        self.subscription = pubsub
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.closed = False

    def pubsub(self, **kwargs: object) -> _PubSub:
        return self.subscription

    async def aclose(self) -> None:
        self.close_entered.set()
        await self.close_release.wait()
        self.closed = True


def _client(monkeypatch: pytest.MonkeyPatch, redis: _Redis) -> None:
    def from_url(url: str, **kwargs: object) -> _Redis:
        return redis

    monkeypatch.setattr(redis_listener.aredis.Redis, "from_url", from_url)


async def test_cancelling_open_wait_cancels_child_without_waiting_for_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = _PubSub()
    redis = _Redis(pubsub)
    _client(monkeypatch, redis)
    listener = redis_listener.RedisInboundListener("redis://unused", 7001)
    parent = asyncio.create_task(listener.wait_one(30))
    try:
        await pubsub.entered.wait()
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(parent, 1)
        await asyncio.wait_for(pubsub.cancelled.wait(), 1)
        await asyncio.wait_for(redis.close_entered.wait(), 1)
        # The existing abandon contract never waits for cancellation cleanup.
        assert parent.done()
        assert not redis.closed
        assert listener._pubsub is None
        assert listener._redis is None
    finally:
        redis.close_release.set()
        assert pubsub.open_task is not None
        if not pubsub.cancelled.is_set():
            pubsub.open_task.cancel()
        await asyncio.gather(pubsub.open_task, return_exceptions=True)
        await listener.close()
    assert redis.closed


async def test_cancelling_parent_after_open_completes_preserves_listener_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = _PubSub()
    redis = _Redis(pubsub)
    redis.close_release.set()
    _client(monkeypatch, redis)
    listener = redis_listener.RedisInboundListener("redis://unused", 7002)
    parent = asyncio.create_task(listener.wait_one(30))
    pubsub.parent = parent
    try:
        await pubsub.entered.wait()
        pubsub.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(parent, 1)
        assert pubsub.open_task is not None
        assert pubsub.open_task.done()
        assert not pubsub.open_task.cancelled()
        assert listener._pubsub is pubsub
        assert listener._redis is redis
        assert not pubsub.closed
        assert not redis.closed
    finally:
        await listener.close()
    assert pubsub.closed
    assert redis.closed
    assert listener._pubsub is None
    assert listener._redis is None
