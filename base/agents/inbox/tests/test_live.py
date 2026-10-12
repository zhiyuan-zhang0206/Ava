"""`base/agents/inbox/live.py` — the SQL the fleet update's quiesce step drives.

These are the relocated home of the agents_meta / inbound_messages queries the
gateway CLI used to hand-write inline: signal_live_agents_restart (bulk
restart), list_live_agent_ids. "Live" = status running/idling.
Each helper opens its own connection, so it sees rows committed by the fixture.
"""

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
    """One restart inbound (content='', the given source) per running/idling agent;
    terminated get none. Returns the ids signalled."""
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
    """R1 (Task #1021): a running/idling row WITHOUT a lease (pre-lease code) or
    with an EXPIRED one (a process that stopped renewing) is not alive — the
    lease is the liveness authority, and the quiesce must not signal a zombie."""
    running_no_lease = seed_agent(db_conn, "running", live_lease=False)
    idling_no_lease = seed_agent(db_conn, "idling", live_lease=False)
    running_fresh = seed_agent(db_conn, "running")

    ids = signal_live_agents_restart(database, event_bus, source="system:update")

    assert ids == [running_fresh]
    assert _inbound_rows(db_conn, running_no_lease) == []
    assert _inbound_rows(db_conn, idling_no_lease) == []


def test_agent_is_alive_predicate() -> None:
    """The Python half of the single alive predicate — status AND unexpired
    lease, one definition for row-based checks."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    future = now + timedelta(seconds=600)
    past = now - timedelta(seconds=1)

    assert agent_is_alive("running", future) is True
    assert agent_is_alive("idling", future) is True
    assert agent_is_alive("running", None) is False  # pre-lease row
    assert agent_is_alive("running", past) is False  # expired
    assert agent_is_alive("terminated", future) is False


def test_signal_live_agents_restart_none_live(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    """No live agents → no inbound inserted, returns []."""
    terminated = seed_agent(db_conn, "terminated")
    assert signal_live_agents_restart(database, event_bus, source="system:update") == []
    assert _inbound_rows(db_conn, terminated) == []


def test_signal_live_agents_restart_exclude_ids(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    """exclude_agent_ids agents are skipped even when live — the quiesce
    convergence loop passes its already-signalled set so a pass only signals
    newly-live agents."""
    already = seed_agent(db_conn, "running")
    late = seed_agent(db_conn, "running")

    ids = signal_live_agents_restart(
        database, event_bus, source="system:update", exclude_agent_ids={already}
    )

    assert ids == [late]
    assert _inbound_rows(db_conn, already) == []
    assert _inbound_rows(db_conn, late) == [("restart", "system:update", "")]


def test_list_live_agent_ids(db_conn: psycopg.Connection, database: Database) -> None:
    """list_live_agent_ids lists agents with a LIVE process to act on (quiesce):
    running/idling only."""
    running = seed_agent(db_conn, "running")
    idling = seed_agent(db_conn, "idling")
    seed_agent(db_conn, "terminated")
    assert sorted(list_live_agent_ids(database)) == sorted([running, idling])
