from __future__ import annotations

import psycopg

from base.agents.inbox.live import (
    agent_is_alive,
    list_live_agent_ids,
    signal_live_agents_restart,
)
from base.db import Database
from base.db.tests.live_agents import seed_agent
from base.events.live.bus import EventBus


def _inbound_rows(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, source, content FROM inbound_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return cur.fetchall()


def test_signal_live_agents_restart_only_live(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    running = seed_agent(db_conn, "running")
    idling = seed_agent(db_conn, "idling")
    terminated = seed_agent(db_conn, "terminated")

    ids = signal_live_agents_restart(database, event_bus, source="system:update")

    assert sorted(ids) == sorted([running, idling])
    assert _inbound_rows(db_conn, running) == [("restart", "system:update", "")]
    assert _inbound_rows(db_conn, idling) == [("restart", "system:update", "")]
    assert _inbound_rows(db_conn, terminated) == []


def test_signal_live_agents_restart_requires_an_unexpired_lease(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    running_no_lease = seed_agent(db_conn, "running", live_lease=False)
    idling_no_lease = seed_agent(db_conn, "idling", live_lease=False)
    running_fresh = seed_agent(db_conn, "running")

    ids = signal_live_agents_restart(database, event_bus, source="system:update")

    assert ids == [running_fresh]
    assert _inbound_rows(db_conn, running_no_lease) == []
    assert _inbound_rows(db_conn, idling_no_lease) == []


def test_agent_is_alive_predicate() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    future = now + timedelta(seconds=600)
    past = now - timedelta(seconds=1)

    assert agent_is_alive("running", future) is True
    assert agent_is_alive("idling", future) is True
    assert agent_is_alive("running", None) is False
    assert agent_is_alive("running", past) is False
    assert agent_is_alive("terminated", future) is False


def test_signal_live_agents_restart_none_live(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    terminated = seed_agent(db_conn, "terminated")
    assert signal_live_agents_restart(database, event_bus, source="system:update") == []
    assert _inbound_rows(db_conn, terminated) == []


def test_signal_live_agents_restart_exclude_ids(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    already = seed_agent(db_conn, "running")
    late = seed_agent(db_conn, "running")

    ids = signal_live_agents_restart(
        database, event_bus, source="system:update", exclude_agent_ids={already}
    )

    assert ids == [late]
    assert _inbound_rows(db_conn, already) == []
    assert _inbound_rows(db_conn, late) == [("restart", "system:update", "")]


def test_list_live_agent_ids(db_conn: psycopg.Connection, database: Database) -> None:
    running = seed_agent(db_conn, "running")
    idling = seed_agent(db_conn, "idling")
    seed_agent(db_conn, "terminated")
    assert sorted(list_live_agent_ids(database)) == sorted([running, idling])
