"""Actual filesystem changes reach Redis, including newly created workspaces."""

import asyncio
import json
from pathlib import Path

import pytest
from redis.asyncio.client import PubSub

from base.events.live.bus import EventBus
from base.events.live.projection import ToUserChanged
from services.agent_runner.agent_ops.to_user_watch import watch_to_user


async def next_event(subscriber: PubSub) -> ToUserChanged:
    async with asyncio.timeout(5):
        while True:
            message = await subscriber.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message:
                payload = message["data"]
                assert isinstance(payload, (str, bytes))
                return ToUserChanged.model_validate(json.loads(payload))


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
                await asyncio.sleep(0.2)
                (workspace / "to_user.html").write_text("first")
                assert (await next_event(subscriber)).agent_id == 7
                staged = workspace / ".next"
                staged.write_text("second")
                staged.replace(workspace / "to_user.html")
                assert (await next_event(subscriber)).agent_id == 7
                (tmp_path / "8").mkdir()
                (tmp_path / "8" / "to_user.md").write_text("new agent")
                assert (await next_event(subscriber)).agent_id == 8
                await asyncio.sleep(0.2)
                (tmp_path / "8" / "to_user.md").write_text("updated")
                assert (await next_event(subscriber)).agent_id == 8
            finally:
                watcher.cancel()
