"""The bulk restart signal wakes a parked listener over Redis."""

from __future__ import annotations

import asyncio
import time

import psycopg

from base import db
from base.config import settings
from base.db.tests.live_agents import seed_agent
from base.events.live.redis_listener import RedisInboundListener


async def test_signal_live_agents_restart_publishes_redis_wake(
    db_conn: psycopg.Connection,
) -> None:
    """The bulk restart wakes each signalled agent over Redis — the same
    per-agent publish as insert_inbound_message — so an idling agent restarts now
    instead of stalling to its SELECT recheck (the quiesce step's convergence
    depends on live agents draining promptly). Park a per-agent listener on the
    agent's channel, fire the bulk signal, assert the parked wait wakes."""
    tid = seed_agent(db_conn, "idling")
    listener = RedisInboundListener(settings.data_plane.redis_url, tid)
    try:
        wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
        await asyncio.sleep(0.2)  # let the subscribe take effect before the publish
        t0 = time.monotonic()
        ids = await asyncio.to_thread(db.signal_live_agents_restart, source="system:update")
        assert tid in ids
        await asyncio.wait_for(wait_task, timeout=5.0)
        assert time.monotonic() - t0 < 5.0, "bulk restart did not wake the parked listener"
    finally:
        await listener.close()
