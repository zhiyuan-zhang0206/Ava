from __future__ import annotations

import asyncio
import time

import psycopg

from base.agents.inbox.live import signal_live_agents_restart
from base.config import settings
from base.db import Database
from base.db.tests.live_agents import seed_agent
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener


async def test_signal_live_agents_restart_publishes_redis_wake(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    tid = seed_agent(db_conn, "idling")
    listener = RedisInboundListener(settings.data_plane.redis_url, tid)
    try:
        wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        ids = await asyncio.to_thread(
            signal_live_agents_restart, database, event_bus, source="system:update"
        )
        assert tid in ids
        await asyncio.wait_for(wait_task, timeout=5.0)
        assert time.monotonic() - t0 < 5.0, "bulk restart did not wake the parked listener"
    finally:
        await listener.close()
