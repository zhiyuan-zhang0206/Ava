"""`publish_inbound_wake` is a plain pub/sub publish: no durable wake state is left in Redis."""

from __future__ import annotations

import asyncio
import time

import psycopg
import pytest

from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.inbox.insert import insert_inbound_message
from base.cluster import redis_channel_prefix
from base.config import settings
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener


async def test_publish_leaves_no_redis_key(database: Database, event_bus: EventBus) -> None:
    """A wake published while no listener is subscribed is lost on purpose: the
    claim loop's SELECT recheck is the only recovery, so the publisher must not
    leave a per-agent breadcrumb key behind."""
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        agent_id = create_agent(conn)
        insert_inbound_message(conn, agent_id, "wake", "user", bus=event_bus, database=database)
    r = EventBus.from_settings().sync_redis(decode_responses=True)
    try:
        leftover = [
            str(key)  # pyright: ignore[reportUnknownArgumentType]
            for key in r.scan_iter(match=f"{redis_channel_prefix()}:*{agent_id}*")  # pyright: ignore[reportUnknownMemberType]
        ]
        assert leftover == [], f"publish left Redis keys behind: {leftover}"
    finally:
        r.close()


def test_unknown_wake_error_propagates(
    database: Database, event_bus: EventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.events.live.tests.fakes import patch_sync_redis

    bug = AttributeError("wake bug")

    class BrokenRedis:
        def publish(self, *_args: object, **_kwargs: object) -> int:
            raise bug

        def close(self) -> None:
            pass

    patch_sync_redis(monkeypatch, BrokenRedis)
    with pytest.raises(AttributeError) as raised:
        publish_inbound_wake(database, event_bus, 123, "456")
    assert raised.value is bug


async def test_wake_published_before_subscribe_is_not_replayed(
    database: Database, event_bus: EventBus
) -> None:
    """Pub/sub is fire-and-forget: a wake published while the listener has no
    subscription is not replayed on (re)subscribe, so `wait_one` sleeps out its
    budget and the caller's durable SELECT recheck is the only recovery."""
    agent_id = 6991
    listener = RedisInboundListener(settings.data_plane.redis_url, agent_id=agent_id)
    try:
        # No subscription yet: this publish reaches nobody.
        assert await asyncio.to_thread(publish_inbound_wake, database, event_bus, agent_id, "1")
        started = time.monotonic()
        await listener.wait_one(timeout=1.0)
        elapsed = time.monotonic() - started
        assert elapsed >= 0.9, (
            f"wait_one returned after {elapsed:.2f}s on a wake published before "
            "subscribe — a lost wake is being replayed"
        )
    finally:
        await listener.close()
