"""The real invocation boundary supervises its live-event worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import redis.asyncio as aredis
from redis.exceptions import AuthenticationError, DataError, ResponseError
from redis.exceptions import ConnectionError as RedisConnectionError

from base.agents.context import AvaContext
from base.events.live.publisher import AgentEventPublisher
from base.native_process.runtime_incarnation import RuntimeIncarnation

from ..invocation import driver
from ..runtime import TurnOutcome


class _Redis:
    def __init__(self, fault: str, error: Exception) -> None:
        self.fault = fault
        self.error = error
        self.connection_pool = self
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = asyncio.Event()
        self.published: list[tuple[str, str]] = []
        self.attempts = 0
        self.disconnects = 0

    def pipeline(self, *, transaction: bool) -> _Redis:
        assert not transaction
        return self

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))

    async def execute(self, *, raise_on_error: bool) -> list[object]:
        assert not raise_on_error
        self.entered.set()
        self.attempts += 1
        try:
            if self.fault == "transport" and self.attempts == 1:
                raise self.error
            if self.fault == "pipeline":
                raise self.error
            if self.fault == "result":
                return [self.error]
            if self.fault == "mixed_result":
                return [AuthenticationError("ACL transition"), self.error]
            if self.fault == "disconnect":
                raise RedisConnectionError("broken connection")
            if self.fault == "hang":
                await self.release.wait()
            if self.fault == "cleanup":
                await self.release.wait()
                raise self.error
            return [1]
        finally:
            self.stopped.set()

    async def disconnect(self, *, inuse_connections: bool) -> None:
        assert inuse_connections
        self.disconnects += 1
        if self.fault == "disconnect":
            raise self.error


async def _drive(
    monkeypatch: pytest.MonkeyPatch,
    publisher: AgentEventPublisher,
    invoke: Callable[[int, AvaContext], Awaitable[TurnOutcome]],
) -> TurnOutcome:
    monkeypatch.setattr(driver, "run_compact", AsyncMock(return_value=True))
    monkeypatch.setattr(driver, "settle_original_restart", AsyncMock(return_value=False))
    return await driver.drive_context(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        42,
        AvaContext(
            event_publisher=publisher,
            bus=MagicMock(),
            original_incarnation=RuntimeIncarnation(42, uuid4(), uuid4()),
        ),
        MagicMock(),
        asyncio.Lock(),
        invoke,
        MagicMock(),
    )


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        group = cast(BaseExceptionGroup[BaseException], error)
        return [leaf for child in group.exceptions for leaf in _leaves(child)]
    return [error]


@pytest.mark.parametrize("fault", ["pipeline", "result", "mixed_result", "disconnect"])
@pytest.mark.parametrize("error_type", [ValueError, DataError, ResponseError])
async def test_unknown_worker_error_reaches_invocation_owner(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    error_type: type[Exception],
) -> None:
    error = error_type("publisher defect")
    redis = _Redis(fault, error)
    publisher = AgentEventPublisher(cast(aredis.Redis, redis), "events", agent_id=42)
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def invoke(_agent: int, _ctx: AvaContext) -> TurnOutcome:
        publisher.emit("event")
        try:
            await release.wait()
            return TurnOutcome(exited=False, crashed=False)
        finally:
            cancelled.set()

    owner = asyncio.create_task(_drive(monkeypatch, publisher, invoke))
    try:
        done, _ = await asyncio.wait({owner}, timeout=0.5)
        assert owner in done, "unknown worker failures must interrupt their owner"
        with pytest.raises(ExceptionGroup) as caught:
            await owner
        assert _leaves(caught.value) == [error]
        assert cancelled.is_set()
        assert publisher._task is None or publisher._task.done()
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


async def test_invocation_failure_and_worker_cleanup_failure_are_both_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyError("invocation defect")
    worker = ValueError("publisher defect during drain")
    redis = _Redis("cleanup", worker)
    publisher = AgentEventPublisher(cast(aredis.Redis, redis), "events", agent_id=42)

    async def invoke(_agent: int, _ctx: AvaContext) -> TurnOutcome:
        publisher.emit("event")
        await redis.entered.wait()
        redis.release.set()
        raise primary

    owner = asyncio.create_task(_drive(monkeypatch, publisher, invoke))
    try:
        done, _ = await asyncio.wait({owner}, timeout=0.5)
        assert owner in done
        with pytest.raises(ExceptionGroup) as caught:
            await owner
        leaves = _leaves(caught.value)
        assert primary in leaves
        assert worker in leaves
        assert len(leaves) == 2
    finally:
        redis.release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_invocation_exit_joins_worker_with_bounded_drain(
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    redis = _Redis("hang", ValueError("unused"))
    publisher = AgentEventPublisher(
        cast(aredis.Redis, redis),
        "events",
        agent_id=42,
        publish_timeout=10,
        drain_timeout=0.05,
    )
    release = asyncio.Event()

    async def invoke(_agent: int, _ctx: AvaContext) -> TurnOutcome:
        publisher.emit("event")
        await redis.entered.wait()
        if cancel:
            await release.wait()
        return TurnOutcome(exited=False, crashed=False)

    owner = asyncio.create_task(_drive(monkeypatch, publisher, invoke))
    try:
        await redis.entered.wait()
        if cancel:
            owner.cancel()
        done, _ = await asyncio.wait({owner}, timeout=0.5)
        assert owner in done, "invocation teardown exceeded its drain bound"
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await owner
        else:
            assert not (await owner).crashed
        assert redis.stopped.is_set()
        assert publisher._task is None
    finally:
        redis.release.set()
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("error", [RedisConnectionError("offline"), OSError("socket closed")])
async def test_known_transport_error_recovers_without_interrupting_invocation(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    redis = _Redis("transport", error)
    publisher = AgentEventPublisher(cast(aredis.Redis, redis), "events", agent_id=42)

    async def invoke(_agent: int, _ctx: AvaContext) -> TurnOutcome:
        publisher.emit("first")
        await publisher._queue.join()
        publisher.emit("second")
        return TurnOutcome(exited=False, crashed=False)

    assert not (await _drive(monkeypatch, publisher, invoke)).crashed
    assert redis.attempts == 2
    assert redis.disconnects == 1
    assert publisher._task is None


async def test_parallel_invocations_keep_their_event_identity_and_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _Redis("normal", ResponseError("unused"))
    second = _Redis("normal", ResponseError("unused"))
    publishers = [
        AgentEventPublisher(cast(aredis.Redis, first), "first", agent_id=1),
        AgentEventPublisher(cast(aredis.Redis, second), "second", agent_id=2),
    ]

    async def invoke(_agent: int, ctx: AvaContext) -> TurnOutcome:
        assert ctx.event_publisher is not None
        ctx.event_publisher.emit("one")
        await asyncio.sleep(0)
        ctx.event_publisher.emit("two")
        return TurnOutcome(exited=False, crashed=False)

    await asyncio.gather(*(_drive(monkeypatch, publisher, invoke) for publisher in publishers))
    assert first.published == [("first", "one"), ("first", "two")]
    assert second.published == [("second", "one"), ("second", "two")]
    assert all(publisher._task is None for publisher in publishers)


async def test_invocation_failure_keeps_its_original_type_without_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("turn defect")
    redis = _Redis("normal", ValueError("unused worker defect"))
    publisher = AgentEventPublisher(cast(aredis.Redis, redis), "events", agent_id=42)

    async def invoke(_agent: int, _ctx: AvaContext) -> TurnOutcome:
        publisher.emit("event")
        raise primary

    with pytest.raises(RuntimeError) as caught:
        await _drive(monkeypatch, publisher, invoke)
    assert caught.value is primary
    assert redis.published == [("events", "event")]
    assert publisher._task is None
