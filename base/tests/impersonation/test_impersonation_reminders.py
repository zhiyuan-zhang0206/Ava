"""Reminder lifecycle: insertion with wake, ack suppression, window skipping, dismissal on release and expiry; split from base/tests/impersonation/test_impersonation.py (task #4922)."""

from __future__ import annotations

import psycopg
import pytest

from base.agents import impersonation as leases
from base.db import Database
from base.events.live.bus import EventBus
from base.tests.impersonation._impersonation_helpers import _active, _agent
from tests.impersonation_support import attested_caller


def test_reminder_is_inserted_once_per_lease_and_wakes(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    from base.agents.impersonation.maintenance import remind_expiring_impersonations
    from base.db import pool

    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '4 minutes' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 1
        # Idempotent: a second scan in the same window inserts nothing new.
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 0
    reminder = db_conn.execute(
        "SELECT content,kind,source,status,payload FROM inbound_messages "
        "WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone()
    assert reminder is not None
    assert reminder[1:4] == ("reminder", "system", "pending")
    assert reminder[4]["lease_id"] == str(lease["id"])
    assert f"session {lease['session_id']}" in reminder[0]
    assert f"--agent {owner.agent_id}" in reminder[0]
    assert "renew" in reminder[0] and "release" in reminder[0]


def test_reminder_ack_suppresses_further_reminders(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Issue #2054: an ACKed reminder still counts for the once-per-lease rule.

    A controller that ACKs the reminder without renewing or releasing must not
    get a fresh reminder row on every reaper cycle while the lease stays inside
    the window (the old NOT EXISTS only excluded 'pending' rows, so each cycle
    re-inserted — a nag storm with urgent pushes and re-delivery on top).
    """
    from base.agents.impersonation.maintenance import remind_expiring_impersonations
    from base.db import pool

    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '4 minutes' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 1
    reminder_row = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone()
    assert reminder_row is not None
    reminder_id = reminder_row[0]
    # The relay binds each pushed page to the lease before the controller ACKs.
    db_conn.execute(
        "INSERT INTO agent_impersonation_messages(lease_id,inbound_id) VALUES(%s,%s)",
        (lease["id"], reminder_id),
    )
    db_conn.commit()
    leases.ack(database, event_bus, lease["id"], attested_caller(lease), [reminder_id])
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 0
    assert db_conn.execute(
        "SELECT count(*), max(status) FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == (1, "done")


def test_reminder_skips_leases_outside_the_window(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    from base.agents.impersonation.maintenance import remind_expiring_impersonations
    from base.db import pool

    owner = _agent(db_conn)
    _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '30 minutes' "
        "WHERE agent_id=%s",
        (owner.agent_id,),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 0
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == (0,)


def test_release_dismisses_its_pending_reminder(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    from base.agents.impersonation.maintenance import remind_expiring_impersonations
    from base.db import pool

    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '2 minutes' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 1
    leases.release(database, event_bus, lease["id"], attested_caller(lease), "Done before expiry")
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == ("done",)


def test_expiry_dismisses_its_pending_reminder(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    from base.agents.impersonation import maintenance as maintenance
    from base.db import pool

    owner = _agent(db_conn)
    lease = _active(owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '1 minute' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert maintenance.remind_expiring_impersonations(reaper_pool, database, event_bus) == 1
        db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        assert maintenance.reap_impersonations(reaper_pool, database, event_bus) == 1
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == ("done",)


@pytest.mark.parametrize(
    ("ttl", "remaining", "expected"),
    [
        (1800, 299, 1),
        (1800, 301, 0),
        (3600, 359, 1),
        (3600, 361, 0),
        (86400, 8639, 1),
        (86400, 8641, 0),
        (120, 119, 1),
        (120, 121, 0),
        (3600, -1, 0),
    ],
)
def test_reminder_window_tracks_current_ttl(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    ttl: int,
    remaining: int,
    expected: int,
) -> None:
    from base.agents.impersonation.maintenance import remind_expiring_impersonations
    from base.db import pool

    lease = _active(_agent(db_conn))
    db_conn.execute(
        "UPDATE agent_impersonations SET ttl_seconds=%s, "
        "expires_at=clock_timestamp()+make_interval(secs=>%s) WHERE id=%s",
        (ttl, remaining, lease["id"]),
    )
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == expected


def test_renewal_recomputes_window_and_allows_a_new_reminder(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    from base.agents.impersonation.maintenance import remind_expiring_impersonations
    from base.db import pool

    lease = _active(_agent(db_conn))
    caller = attested_caller(lease)
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 1
        first = leases.inbox(database, lease["id"], caller)
        leases.ack(database, event_bus, lease["id"], caller, [m["id"] for m in first])
        leases.renew(database, event_bus, lease["id"], caller, ttl_seconds=3600)
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 0
        db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '350 seconds' "
            "WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 1
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 0
    rows = db_conn.execute(
        "SELECT status,payload->>'expires_at' FROM inbound_messages "
        "WHERE agent_id=%s AND kind='reminder' ORDER BY id",
        (lease["agent_id"],),
    ).fetchall()
    assert [r[0] for r in rows] == ["done", "pending"]
    assert rows[0][1] != rows[1][1]
