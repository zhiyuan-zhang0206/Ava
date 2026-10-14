"""Actual filesystem changes reach Redis, and no watcher setup owns the loop.

The second half is the 2026-10-14 wedge (task #5129): constructing the watcher
over a busy runner takes minutes, so it must run on the watcher's own thread
and a stopping daemon must abandon it rather than join it.
"""

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path

import pytest
from redis.asyncio.client import PubSub
from watchfiles import Change

from base.events.live.bus import EventBus
from base.events.live.projection import ToUserChanged
from services.agent_runner.agent_ops.to_user_watch import watch_to_user


class _FakeWatch:
    """A `watchfiles.watch` stand-in whose setup cost is a sleep on its caller.

    Each call is one watch round and yields the batches given for it, then waits
    for the stop event like a live watch. `calls` records the thread each setup
    ran on, `rebuilt` fires when a round after the first is set up.
    """

    def __init__(self, *rounds: list[set[tuple[Change, str]]], setup_s: float = 0.0) -> None:
        self._rounds = list(rounds)
        self._setup_s = setup_s
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.rebuilt = threading.Event()

    def __call__(self, *_paths: object, **kwargs: object) -> Iterator[set[tuple[Change, str]]]:
        round_index = len(self.calls)
        self.calls.append(threading.current_thread().name)
        self.entered.set()
        if round_index >= 1:
            self.rebuilt.set()
        time.sleep(self._setup_s)
        if round_index < len(self._rounds):
            yield from self._rounds[round_index]
        stop = kwargs.get("stop_event")
        while isinstance(stop, threading.Event) and not stop.is_set():
            time.sleep(0.01)


async def next_event(subscriber: PubSub) -> ToUserChanged:
    async with asyncio.timeout(5):
        while True:
            message = await subscriber.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message:
                payload = message["data"]
                assert isinstance(payload, (str, bytes))
                return ToUserChanged.model_validate(json.loads(payload))


async def agents_until(subscriber: PubSub, expected: set[int], *, timeout: float = 5) -> set[int]:
    """Every agent `expected` names must arrive; a batch may name several at once."""
    seen: set[int] = set()
    async with asyncio.timeout(timeout):
        while not expected <= seen:
            message = await subscriber.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message:
                payload = message["data"]
                assert isinstance(payload, (str, bytes))
                seen.add(ToUserChanged.model_validate(json.loads(payload)).agent_id)
    return seen


@pytest.mark.asyncio
async def test_changes_atomic_replacement_and_new_workspace(
    tmp_path: Path, event_bus: EventBus
) -> None:
    workspace = tmp_path / "7"
    workspace.mkdir()
    async with (
        event_bus.open_async_redis() as client,
        client.pubsub() as subscriber,  # pyright: ignore[reportUnknownMemberType] — Redis kwargs
    ):
        await subscriber.subscribe(event_bus.channel)
        await subscriber.get_message(timeout=1)
        async with asyncio.TaskGroup() as group:
            watcher = group.create_task(watch_to_user(tmp_path, event_bus))
            try:
                # Let the watcher thread reach its first watch round before the
                # first change, and the rebuilt one before the post-rebuild change.
                await asyncio.sleep(0.5)
                (workspace / "to_user.html").write_text("first")
                assert (await next_event(subscriber)).agent_id == 7
                staged = workspace / ".next"
                staged.write_text("second")
                staged.replace(workspace / "to_user.html")
                assert (await next_event(subscriber)).agent_id == 7
                (tmp_path / "8").mkdir()
                (tmp_path / "8" / "to_user.md").write_text("new agent")
                assert (await next_event(subscriber)).agent_id == 8
                await asyncio.sleep(0.5)
                (tmp_path / "8" / "to_user.md").write_text("updated")
                assert (await next_event(subscriber)).agent_id == 8
            finally:
                watcher.cancel()


@pytest.mark.asyncio
async def test_a_blocked_setup_and_rebuild_never_hold_the_loop(
    tmp_path: Path, event_bus: EventBus
) -> None:
    """Setup is paid on the watcher's own thread, and the loop keeps scheduling."""
    (tmp_path / "7").mkdir()
    fake = _FakeWatch(
        [
            {(Change.modified, str(tmp_path / "7" / "to_user.html"))},
            {(Change.added, str(tmp_path / "8"))},
        ],
        [{(Change.added, str(tmp_path / "8"))}],
        setup_s=1.0,
    )
    ticks: list[float] = []

    async def tick() -> None:
        while True:
            ticks.append(time.perf_counter())
            await asyncio.sleep(0.01)

    async with (
        event_bus.open_async_redis() as client,
        client.pubsub() as subscriber,  # pyright: ignore[reportUnknownMemberType] — Redis kwargs
        asyncio.timeout(30),
    ):
        await subscriber.subscribe(event_bus.channel)
        await subscriber.get_message(timeout=1)
        async with asyncio.TaskGroup() as group:
            watcher = group.create_task(watch_to_user(tmp_path, event_bus, watch_source=fake))
            ticker = group.create_task(tick())
            try:
                assert await asyncio.to_thread(fake.rebuilt.wait, 15)
                seen = await agents_until(subscriber, {7, 8})
            finally:
                watcher.cancel()
                ticker.cancel()
        assert seen == {7, 8}

    main_thread = threading.main_thread().name
    assert len(fake.calls) >= 2
    assert all(name != main_thread for name in fake.calls)
    assert fake.calls[0] == "ava-ops-to-user-watch"
    gaps = [later - earlier for earlier, later in pairwise(ticks)]
    assert max(gaps) < 0.5, f"the loop stalled {max(gaps):.2f}s during a {1.0}s setup"


@pytest.mark.asyncio
async def test_a_setup_in_flight_is_abandoned_by_a_stop(
    tmp_path: Path, event_bus: EventBus
) -> None:
    """A stop must not wait for a setup: the real one can take minutes."""
    entered = threading.Event()
    release = threading.Event()

    def stuck_watch(*_paths: object, **_kwargs: object) -> Iterator[set[tuple[Change, str]]]:
        entered.set()
        release.wait(timeout=30)
        return iter(())

    watcher = asyncio.create_task(watch_to_user(tmp_path, event_bus, watch_source=stuck_watch))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        watcher.cancel()
        async with asyncio.timeout(3):  # cancellation plus the watcher's own 1s bounded join
            with pytest.raises(asyncio.CancelledError):
                await watcher
    finally:
        release.set()


@pytest.mark.asyncio
async def test_merged_batches_still_name_every_changed_agent(
    tmp_path: Path, event_bus: EventBus
) -> None:
    """A slow publish merges batches; coalescing must not lose an agent."""
    fake = _FakeWatch(
        [
            {(Change.modified, str(tmp_path / "7" / "to_user.html"))},
            {(Change.modified, str(tmp_path / "8" / "to_user.html"))},
        ]
    )
    async with (
        event_bus.open_async_redis() as client,
        client.pubsub() as subscriber,  # pyright: ignore[reportUnknownMemberType] — Redis kwargs
        asyncio.timeout(30),
    ):
        await subscriber.subscribe(event_bus.channel)
        await subscriber.get_message(timeout=1)
        async with asyncio.TaskGroup() as group:
            watcher = group.create_task(watch_to_user(tmp_path, event_bus, watch_source=fake))
            try:
                seen = await agents_until(subscriber, {7, 8})
            finally:
                watcher.cancel()
        assert seen == {7, 8}
