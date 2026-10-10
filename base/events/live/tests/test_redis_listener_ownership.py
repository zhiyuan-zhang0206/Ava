"""Bounded listener stop observes known work without promising coroutine termination."""

import asyncio
from typing import Any

import pytest

from base.events.live import redis_listener


class LateError(RuntimeError):
    pass


class PubSub:
    def __init__(self) -> None:
        self.subscribed = asyncio.Event()
        self.reading = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.task: asyncio.Task[Any] | None = None
        self.error: Exception | None = None
        self.suppress_subscribe_cancel = False
        self.hold_subscribe = False
        self.closed = False
        self.parent: asyncio.Task[None] | None = None
        self.message: dict[str, str] | None = None

    async def subscribe(self, channel: str) -> None:
        self.task = asyncio.current_task()
        self.subscribed.set()
        if self.hold_subscribe:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if not self.suppress_subscribe_cancel:
                    raise
                await self.release.wait()
        if self.error is not None:
            if self.parent is not None:
                asyncio.get_running_loop().call_soon(self.parent.cancel)
            raise self.error

    async def ping(self) -> None:
        return

    async def get_message(self, **kwargs: object) -> dict[str, str] | None:
        self.task = asyncio.current_task()
        self.reading.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.message

    async def aclose(self) -> None:
        self.closed = True


class Redis:
    def __init__(self, pubsub: PubSub) -> None:
        self.subscription = pubsub
        self.closing = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.closed = False
        self.getdel_entered = asyncio.Event()
        self.getdel_release = asyncio.Event()
        self.getdel_release.set()

    def pubsub(self, **kwargs: object) -> PubSub:
        return self.subscription

    async def getdel(self, key: str) -> None:
        self.getdel_entered.set()
        await self.getdel_release.wait()

    async def aclose(self) -> None:
        self.closing.set()
        await self.close_release.wait()
        self.closed = True


def client(monkeypatch: pytest.MonkeyPatch, *clients: Redis) -> None:
    remaining = iter(clients)

    def from_url(_url: str, **_kwargs: object) -> Redis:
        return next(remaining)

    monkeypatch.setattr(redis_listener.aredis.Redis, "from_url", from_url)


async def finished(task: asyncio.Task[Any]) -> None:
    done, _ = await asyncio.wait({task}, timeout=1)
    assert done, "bounded boundary did not return"


@pytest.fixture
async def reports() -> Any:
    loop = asyncio.get_running_loop()
    old = loop.get_exception_handler()
    captured: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: captured.append(context))
    try:
        yield captured
    finally:
        loop.set_exception_handler(old)


async def test_open_deadline_and_stop_do_not_wait_for_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = PubSub()
    pubsub.hold_subscribe = True
    redis = Redis(pubsub)
    redis.close_release.clear()
    client(monkeypatch, redis)
    listener = redis_listener.RedisInboundListener("redis://unused", 7003)
    waiter = asyncio.create_task(listener.wait_one(0.02))
    try:
        await pubsub.subscribed.wait()
        await finished(waiter)
        waiter.result()
        await redis.closing.wait()
        stopper = asyncio.create_task(listener.stop(timeout=0.02))
        await finished(stopper)
        unfinished = stopper.result()
        assert len(unfinished) == 1 and ":open:" in unfinished[0]
        assert pubsub.closed and not redis.closed
        with pytest.raises(RuntimeError, match="has stopped"):
            await listener.wait_one(1)
    finally:
        pubsub.release.set()
        redis.close_release.set()
        assert pubsub.task is not None
        await asyncio.gather(pubsub.task, return_exceptions=True)
        await listener.stop(timeout=1)
    assert redis.closed and listener.unfinished_work == ()


async def test_late_consume_bug_is_visible_after_stop_and_raised_on_next_join(
    monkeypatch: pytest.MonkeyPatch,
    reports: list[dict[str, Any]],
) -> None:
    pubsub = PubSub()
    pubsub.error = None
    redis = Redis(pubsub)
    client(monkeypatch, redis)
    listener = redis_listener.RedisInboundListener("redis://unused", 7004)
    waiter = asyncio.create_task(listener.wait_one(30))
    try:
        await pubsub.reading.wait()
        waiter.cancel()
        await finished(waiter)
        with pytest.raises(asyncio.CancelledError):
            waiter.result()
        unfinished = await listener.stop(timeout=0.02)
        assert len(unfinished) == 1 and ":consume:" in unfinished[0]
        error = LateError("late consume")
        pubsub.error = error
        pubsub.release.set()
        assert pubsub.task is not None
        await finished(pubsub.task)
        await asyncio.sleep(0)
        assert reports[0]["exception"] is error
        with pytest.raises(LateError) as captured:
            await listener.stop(timeout=0)
        assert captured.value is error
    finally:
        pubsub.release.set()
        if pubsub.task is not None:
            await asyncio.gather(pubsub.task, return_exceptions=True)


async def test_same_tick_unknown_result_and_caller_cancel_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
    reports: list[dict[str, Any]],
) -> None:
    pubsub = PubSub()
    pubsub.hold_subscribe = True
    error = LateError("subscribe invariant")
    pubsub.error = error
    redis = Redis(pubsub)
    client(monkeypatch, redis)
    listener = redis_listener.RedisInboundListener("redis://unused", 7005)
    waiter = asyncio.create_task(listener.wait_one(30))
    pubsub.parent = waiter
    try:
        await pubsub.subscribed.wait()
        pubsub.release.set()
        await finished(waiter)
        with pytest.raises(asyncio.CancelledError):
            waiter.result()
        assert reports[0]["exception"] is error
        with pytest.raises(LateError) as captured:
            await listener.stop(timeout=1)
        assert captured.value is error
    finally:
        pubsub.release.set()
        if pubsub.task is not None:
            await asyncio.gather(pubsub.task, return_exceptions=True)


async def test_active_unknown_error_reaches_caller_once(
    monkeypatch: pytest.MonkeyPatch,
    reports: list[dict[str, Any]],
) -> None:
    pubsub = PubSub()
    error = LateError("active subscribe")
    pubsub.error = error
    client(monkeypatch, Redis(pubsub))
    listener = redis_listener.RedisInboundListener("redis://unused", 7006)
    with pytest.raises(LateError) as captured:
        await listener.wait_one(1)
    assert captured.value is error
    assert reports == []
    assert await listener.stop(timeout=1) == ()


async def test_abandoned_open_cannot_attach_to_replacement_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = PubSub()
    old.hold_subscribe = old.suppress_subscribe_cancel = True
    new = PubSub()
    first, second = Redis(old), Redis(new)
    client(monkeypatch, first, second)
    listener = redis_listener.RedisInboundListener("redis://unused", 7007)
    waiter = asyncio.create_task(listener.wait_one(0.02))
    try:
        await old.subscribed.wait()
        await finished(waiter)
        await old.cancelled.wait()
        eager = asyncio.create_task(listener.ensure_listening())
        old.release.set()
        await finished(eager)
        eager.result()
        assert listener._pubsub is new and listener._redis is second
        assert old.closed and first.closed
        assert not new.closed and not second.closed
    finally:
        old.release.set()
        new.release.set()
        await listener.stop(timeout=1)
    assert new.closed and second.closed


async def test_consume_grace_returns_while_handle_close_is_unfinished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = PubSub()
    redis = Redis(pubsub)
    redis.close_release.clear()
    client(monkeypatch, redis)
    monkeypatch.setattr(redis_listener, "_CONSUME_ABANDON_GRACE", 0.02)
    listener = redis_listener.RedisInboundListener("redis://unused", 7008)
    waiter = asyncio.create_task(listener.wait_one(0.02))
    try:
        await pubsub.reading.wait()
        await finished(waiter)
        waiter.result()
        await pubsub.cancelled.wait()
        await redis.closing.wait()
        assert listener.wake_state is redis_listener.WakeState.DEGRADED
        unfinished = await listener.stop(timeout=0.02)
        assert any(":consume:" in name for name in unfinished)
        assert any(":consume-cleanup:" in name for name in unfinished)
    finally:
        pubsub.release.set()
        redis.close_release.set()
        assert pubsub.task is not None
        await asyncio.gather(pubsub.task, return_exceptions=True)
        await listener.stop(timeout=1)
    assert listener.unfinished_work == ()


async def test_expected_late_network_error_does_not_fail_stop(
    monkeypatch: pytest.MonkeyPatch,
    reports: list[dict[str, Any]],
) -> None:
    pubsub = PubSub()
    redis = Redis(pubsub)
    client(monkeypatch, redis)
    listener = redis_listener.RedisInboundListener("redis://unused", 7009)
    waiter = asyncio.create_task(listener.wait_one(30))
    try:
        await pubsub.reading.wait()
        waiter.cancel()
        await finished(waiter)
        pubsub.error = OSError("socket already dead")
        pubsub.release.set()
        assert pubsub.task is not None
        await finished(pubsub.task)
        await asyncio.sleep(0)
        assert await listener.stop(timeout=1) == ()
        assert reports == []
    finally:
        pubsub.release.set()
        if pubsub.task is not None:
            await asyncio.gather(pubsub.task, return_exceptions=True)


async def test_stop_during_getdel_prevents_later_consume_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = PubSub()
    redis = Redis(pubsub)
    redis.getdel_release.clear()
    client(monkeypatch, redis)
    listener = redis_listener.RedisInboundListener("redis://unused", 7011)
    waiter = asyncio.create_task(listener.wait_one(30))
    try:
        await asyncio.wait_for(redis.getdel_entered.wait(), timeout=1)
        assert await listener.stop(timeout=0.02) == ()
        assert listener.unfinished_work == ()
        redis.getdel_release.set()
        await finished(waiter)
        with pytest.raises(RuntimeError, match="has stopped"):
            waiter.result()
        assert not pubsub.reading.is_set()
        assert listener.unfinished_work == ()
    finally:
        redis.getdel_release.set()
        pubsub.message = {"type": "message"}
        pubsub.release.set()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await listener.stop(timeout=1)


async def test_close_during_getdel_consumes_only_replacement_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old, new = PubSub(), PubSub()
    first, second = Redis(old), Redis(new)
    first.getdel_release.clear()
    new.message = {"type": "message"}
    new.release.set()
    client(monkeypatch, first, second)
    listener = redis_listener.RedisInboundListener("redis://unused", 7012)
    waiter = asyncio.create_task(listener.wait_one(30))
    try:
        await asyncio.wait_for(first.getdel_entered.wait(), timeout=1)
        await listener.close()
        await listener.ensure_listening()
        assert listener._pubsub is new and listener._redis is second
        first.getdel_release.set()
        await finished(waiter)
        waiter.result()
        assert not old.reading.is_set()
        assert new.reading.is_set()
    finally:
        first.getdel_release.set()
        old.message = {"type": "message"}
        old.release.set()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await listener.stop(timeout=1)


async def test_completed_active_open_stays_owned_until_caller_claims_error(
    monkeypatch: pytest.MonkeyPatch,
    reports: list[dict[str, Any]],
) -> None:
    retained: list[bool] = []
    error = LateError("active result stays with caller")

    class ReceiptPubSub(PubSub):
        async def subscribe(self, channel: str) -> None:
            task = asyncio.current_task()
            assert task is not None

            def receipt(completed: asyncio.Task[Any]) -> None:
                # The listener's callback has run; asyncio.wait has not yet
                # resumed its caller to claim this completed open result.
                retained.append(completed in listener._operations)

            task.add_done_callback(receipt)
            await super().subscribe(channel)

    pubsub = ReceiptPubSub()
    pubsub.error = error
    client(monkeypatch, Redis(pubsub))
    listener = redis_listener.RedisInboundListener("redis://unused", 7013)
    with pytest.raises(LateError) as captured:
        await listener.wait_one(1)
    assert captured.value is error
    assert retained == [True]
    assert reports == []
    assert listener._operations == {}
    assert await listener.stop(timeout=1) == ()


async def test_unknown_cleanup_typeerror_is_not_the_redis_transport_quirk(
    monkeypatch: pytest.MonkeyPatch,
    reports: list[dict[str, Any]],
) -> None:
    error = TypeError("cleanup invariant")

    class FailingClosePubSub(PubSub):
        async def aclose(self) -> None:
            raise error

    pubsub = FailingClosePubSub()
    redis = Redis(pubsub)
    client(monkeypatch, redis)
    listener = redis_listener.RedisInboundListener("redis://unused", 7014)
    await listener.ensure_listening()
    with pytest.raises(TypeError) as captured:
        await listener.stop(timeout=1)
    assert captured.value is error
    assert redis.closed, "the second handle must still be attempted"
    assert reports[0]["exception"] is error
    assert listener.unfinished_work == ()


@pytest.mark.parametrize("boundary", ["probe", "wake_key"])
async def test_unknown_probe_or_wake_key_error_reaches_active_caller(
    monkeypatch: pytest.MonkeyPatch,
    reports: list[dict[str, Any]],
    boundary: str,
) -> None:
    error = LateError(f"{boundary} invariant")

    class FailingProbe(PubSub):
        async def ping(self) -> None:
            raise error

    class FailingWakeKey(Redis):
        async def getdel(self, key: str) -> None:
            raise error

    pubsub = FailingProbe() if boundary == "probe" else PubSub()
    pubsub.message = {"type": "message"}
    pubsub.release.set()
    redis = FailingWakeKey(pubsub) if boundary == "wake_key" else Redis(pubsub)
    client(monkeypatch, redis)
    listener = redis_listener.RedisInboundListener("redis://unused", 7015)
    try:
        await listener.ensure_listening()
        with pytest.raises(LateError) as captured:
            await listener.wait_one(1)
        assert captured.value is error
        assert reports == []
        assert listener._operations == {}
    finally:
        await listener.stop(timeout=1)


@pytest.mark.parametrize("boundary", ["consume_timeout", "consume_transport", "wake_key_timeout"])
async def test_old_phase_cleanup_preserves_replacement_subscription(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    old, new = PubSub(), PubSub()
    first, second = Redis(old), Redis(new)
    if boundary == "wake_key_timeout":
        first.getdel_release.clear()
    client(monkeypatch, first, second)
    monkeypatch.setattr(redis_listener, "_CONSUME_ABANDON_GRACE", 0.02)
    listener = redis_listener.RedisInboundListener("redis://unused", 7016)
    waiter = asyncio.create_task(listener.wait_one(0.3))
    try:
        started = first.getdel_entered if boundary == "wake_key_timeout" else old.reading
        await asyncio.wait_for(started.wait(), timeout=1)
        await listener.close()
        await listener.ensure_listening()
        assert old.closed and first.closed
        assert listener._pubsub is new and listener._redis is second
        if boundary == "consume_transport":
            old.error = OSError("the replaced socket failed")
            old.release.set()
        await finished(waiter)
        waiter.result()
        await asyncio.sleep(0)
        assert listener._pubsub is new and listener._redis is second
        assert not new.closed and not second.closed
        if boundary == "consume_timeout":
            assert old.task is not None and not old.task.done()
            assert any(":consume:" in name for name in listener.unfinished_work)
    finally:
        first.getdel_release.set()
        old.release.set()
        new.release.set()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        if old.task is not None:
            await asyncio.gather(old.task, return_exceptions=True)
        await listener.stop(timeout=1)
