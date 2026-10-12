"""Turn-dispatcher test helpers: the recording turn, stuck-turn driver, fixed clocks and queueing Redis shared by the dispatcher tests."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator

import pytest

from base.events.live.tests.fakes import patch_open_async_redis
from services.agent_runner.agent_host.dispatcher import (
    TurnScheduler,
)


class Recorder:
    """A `run_turn` that records calls and can be released on command."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self.finished: list[int] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.entered: dict[int, asyncio.Event] = {}

    def gate(self, agent_id: int) -> asyncio.Event:
        return self.gates.setdefault(agent_id, asyncio.Event())

    def arrival(self, agent_id: int) -> asyncio.Event:
        return self.entered.setdefault(agent_id, asyncio.Event())

    async def __call__(self, agent_id: int) -> None:
        self.started.append(agent_id)
        self.arrival(agent_id).set()
        gate = self.gates.get(agent_id)
        if gate is not None:
            await gate.wait()
            gate.clear()
        self.finished.append(agent_id)


async def settle() -> None:
    """Let every ready callback run. Two hops: one for the task to be scheduled,
    one for it to run to its next await."""
    for _ in range(6):
        await asyncio.sleep(0)


class StuckTurn:
    """A turn that refuses the cancel, then can be released.

    Swallowing `CancelledError` forever is the faithful stand-in for a task
    blocked in a C call — and it is also unkillable at loop teardown, so a test
    using one hangs the whole run instead of failing. `release()` re-arms
    cancellation so the test can clean up after asserting.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self._released = False
        self._task: asyncio.Task[None] | None = None

    async def __call__(self, _agent_id: int) -> None:
        self._task = asyncio.current_task()
        self.entered.set()
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if self._released:
                    raise
                continue  # refuses to unwind, exactly like a blocked C call

    async def release(self) -> None:
        """Let the turn die, so the loop can close on a clean task set."""
        self._released = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def aclose_without_hanging(sched: TurnScheduler, stuck: StuckTurn) -> bool:
    """Run `sched.aclose()` and report whether it RETURNED, never hanging.

    `asyncio.wait`, not `wait_for`: the pre-bound implementation sat in
    `await task` under a `suppress(CancelledError, ...)` which ATE the
    cancellation `wait_for` sends, so `wait_for` could not rescue the test and
    the whole run hung instead of failing. Waiting without cancelling turns "it
    never returned" into a boolean the caller can assert on.

    Cleanup order is load-bearing: releasing the turn is what lets the pump task
    die, which is what lets a still-parked `aclose` finish. Cancelling `aclose`
    first cannot do it, for the same reason `wait_for` could not.
    """
    closing = asyncio.create_task(sched.aclose())
    try:
        done, _ = await asyncio.wait([closing], timeout=5)
        return closing in done
    finally:
        await stuck.release()
        closing.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await closing


@contextlib.asynccontextmanager
async def releasing_stuck_turn() -> AsyncGenerator[StuckTurn, None]:
    """Release the cancellation-resistant fixture even when an assertion fails."""
    turn = StuckTurn()
    try:
        yield turn
    finally:
        await turn.release()


class QueueingPubSub:
    """Subscription fake that stays live while tests inject wake frames."""

    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.closed = False

    async def psubscribe(self, _pattern: str) -> None:
        return None

    async def get_message(self, *, timeout: float) -> dict[str, object] | None:
        try:
            return await asyncio.wait_for(self.messages.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def aclose(self) -> None:
        self.closed = True


class QueueingRedis:
    def __init__(self, pubsub: QueueingPubSub) -> None:
        self._pubsub = pubsub
        self.closed = False

    def pubsub(self, **_kwargs: object) -> QueueingPubSub:
        return self._pubsub

    async def aclose(self) -> None:
        self.closed = True


def patch_redis(monkeypatch: pytest.MonkeyPatch, pubsub: QueueingPubSub) -> list[QueueingRedis]:
    """Record every client open; a scan failure must never add one."""
    clients: list[QueueingRedis] = []

    def _open(_url: str, **_kw: object) -> QueueingRedis:
        client = QueueingRedis(pubsub)
        clients.append(client)
        return client

    patch_open_async_redis(monkeypatch, _open)
    return clients
