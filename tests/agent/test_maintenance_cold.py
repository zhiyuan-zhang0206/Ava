"""Cold hosted metadata is preserved only behind an absent-host proof."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from shared import maintenance_cohort, pause_owner
from shared.db import insert_inbound_message
from shared.machine import machine_name
from tests.agent.test_maintenance import WHEN, _agent
from tests.agent.test_maintenance import isolate as isolate


@pytest.mark.parametrize("observed", [False, True])
def test_cold_hosted_restart_tails_and_claimed_chat_are_preserved(
    db_conn: psycopg.Connection[Any], observed: bool
) -> None:
    agent = _agent(db_conn)
    owner, generation = uuid4(), uuid4()
    chat = insert_inbound_message(db_conn, agent, "unfinished action", "user")
    restart = insert_inbound_message(db_conn, agent, "", "system:update", kind="restart")
    db_conn.execute("UPDATE inbound_messages SET status='claimed' WHERE id=%s", (chat,))
    db_conn.execute(
        "UPDATE inbound_messages SET status=%s,applied_at=clock_timestamp(),observed_at=%s,"
        "target_owner=%s,target_generation=%s,claimed_at=clock_timestamp() WHERE id=%s",
        ("done" if observed else "claimed", WHEN if observed else None, owner, generation, restart),
    )
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind=%s,runtime_owner=%s,runtime_generation=%s,"
        "lifecycle_command_id=%s WHERE id=%s",
        (
            "hosted" if observed else None,
            owner if observed else None,
            generation if observed else None,
            None if observed else restart,
            agent,
        ),
    )
    db_conn.commit()
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    db_conn.commit()
    pause_owner.begin_maintenance("cold", WHEN)
    with pytest.raises(RuntimeError):
        maintenance_cohort.prepare(
            db_conn, machine=machine_name(), host_owner=None, holder="cold", acquired_at=WHEN
        )
    db_conn.rollback()
    held = maintenance_cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=None,
        holder="cold",
        acquired_at=WHEN,
        host_absent=True,
    )
    assert held.parked == (agent,) and held.commands == {} and held.drained == ()
    maintenance_cohort.verify_drained(db_conn, held)
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (chat,)
    ).fetchone() == ("claimed",)


def test_expired_host_lease_does_not_prove_normal_shutdown(
    db_conn: psycopg.Connection[Any],
) -> None:
    agent = _agent(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind='hosted',runtime_owner=%s,runtime_generation=%s,"
        "lease_expires_at=%s WHERE id=%s",
        (uuid4(), uuid4(), WHEN - timedelta(days=1), agent),
    )
    db_conn.commit()
    pause_owner.begin_maintenance("cold", WHEN)
    with pytest.raises(RuntimeError, match="live original native"):
        maintenance_cohort.prepare(
            db_conn,
            machine=machine_name(),
            host_owner=None,
            holder="cold",
            acquired_at=WHEN,
            host_absent=True,
        )
