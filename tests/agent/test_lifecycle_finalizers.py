"""Generic dead-letter cleanup cannot overwrite a durable lifecycle command."""

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.tests.test_lifecycle_intent import _command
from agent.tests.test_inbound_ownership import _admit, _agent
from base.config import settings
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.native_process.turn_identity import bind_turn_identity
from services.wake.delivery_watchdog.daemon import dead_letter_stale_claimed


async def test_generic_dead_letter_leaves_fixed_command_claimed(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    incarnation = await _admit(aops_pool, agent_id)
    command_id = _command(db_conn, agent_id, "restart")
    with bind_turn_identity(agent_id, incarnation=incarnation):
        assert [i.id for i in await claim_inbound_batch(aops_pool, agent_id)] == [command_id]
    chat_id = insert_inbound_message(
        db_conn, agent_id, "old chat", "cli", bus=event_bus, database=database
    )
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='exit' WHERE id=%s",
        (agent_id,),
    )
    db_conn.execute(
        "UPDATE inbound_messages SET status='claimed',claimed_at=clock_timestamp()-interval '1 day' WHERE agent_id=%s",
        (agent_id,),
    )
    db_conn.commit()
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=2
    ) as pool:
        assert dead_letter_stale_claimed(pool, 60, 7200.0) == 1
    assert db_conn.execute(
        "SELECT id,status FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall() == [(command_id, "claimed"), (chat_id, "done")]
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (command_id,)
