"""`insert_inbound_message` leaves the durable wake-key breadcrumb next to its pub/sub wake."""

from __future__ import annotations

import psycopg

from base.cluster import wake_key
from base.config import settings
from base.db import Database, create_agent, insert_inbound_message
from base.events.live.bus import EventBus


async def test_publish_sets_wake_key_too(database: Database, event_bus: EventBus) -> None:
    """The publisher writes the key alongside the pub/sub message (pinned
    at the `base.db.publish_inbound_wake` boundary via
    `insert_inbound_message`), so a wake that the listener DOES receive
    leaves a breadcrumb for the next reconnect window too."""
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        agent_id = create_agent(conn)
        insert_inbound_message(conn, agent_id, "wake", "user", bus=event_bus, database=database)
    r = EventBus.from_settings().sync_redis(decode_responses=True)
    try:
        assert r.get(wake_key(agent_id)) is not None, (
            "insert_inbound_message did not SETEX the wake key"
        )
        r.delete(wake_key(agent_id))
    finally:
        r.close()
