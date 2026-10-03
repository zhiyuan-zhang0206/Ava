"""Reminder lifecycle: insertion with wake, ack suppression, window skipping, dismissal on release and expiry; split from base/tests/test_impersonation.py (task #4922)."""

from __future__ import annotations

import psycopg

from base.agents import impersonation as leases
from base.tests._impersonation_helpers import _active, _agent
from tests.impersonation_support import attested_caller


def test_reminder_is_inserted_once_per_lease_and_wakes(
    db_conn: psycopg.Connection,
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
        assert remind_expiring_impersonations(reaper_pool) == 1
        # Idempotent: a second scan in the same window inserts nothing new.
        assert remind_expiring_impersonations(reaper_pool) == 0
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
        assert remind_expiring_impersonations(reaper_pool) == 1
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
    leases.ack(lease["id"], attested_caller(lease), [reminder_id])
    db_conn.commit()
    with pool(max_size=2) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool) == 0
    assert db_conn.execute(
        "SELECT count(*), max(status) FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == (1, "done")


def test_reminder_skips_leases_outside_the_window(db_conn: psycopg.Connection) -> None:
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
        assert remind_expiring_impersonations(reaper_pool) == 0
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == (0,)


def test_release_dismisses_its_pending_reminder(db_conn: psycopg.Connection) -> None:
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
        assert remind_expiring_impersonations(reaper_pool) == 1
    leases.release(lease["id"], attested_caller(lease), "Done before expiry")
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == ("done",)


def test_expiry_dismisses_its_pending_reminder(db_conn: psycopg.Connection) -> None:
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
        assert maintenance.remind_expiring_impersonations(reaper_pool) == 1
        db_conn.execute(
            "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (lease["id"],),
        )
        db_conn.commit()
        assert maintenance.reap_impersonations(reaper_pool) == 1
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == ("done",)
