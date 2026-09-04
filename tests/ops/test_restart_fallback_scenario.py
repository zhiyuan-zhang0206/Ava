"""Scenario selection must not repeat restart after a consumed request.

This fixes the fake discriminator, not the production completion protocol.
The durable restarter remains the only process-replacement owner; the fake
scenario only distinguishes a consumed request from a successful completion.
"""

from __future__ import annotations

import psycopg
import pytest

import ava
from ops.agent_spawn import create_agent_row
from shared.machine import machine_name
from tests.e2e.fakes.scenarios import lifecycle_restart


@pytest.mark.parametrize("status", ["claimed", "done"])
def test_consumed_restart_selects_successor_script_without_claiming_completion(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    agent_id, _birth = create_agent_row(spawner="test", machine=machine_name())
    monkeypatch.setattr(ava.self, "AGENT_ID", agent_id)
    initial = lifecycle_restart.build("diagnostic")
    assert initial.script == lifecycle_restart.RESTART_SCRIPT
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,status) "
        "VALUES(%s,'','restart','self',%s) RETURNING id",
        (agent_id, status),
    ).fetchone()
    assert row is not None
    original_request = row[0]
    db_conn.execute("UPDATE agents_meta SET status='restarting' WHERE id=%s", (agent_id,))
    db_conn.commit()
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("restarting",)
    rows = db_conn.execute(
        "SELECT id,kind,source,status FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall()
    assert rows == [(original_request, "restart", "self", status)]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart_completed'",
        (agent_id,),
    ).fetchone() == (0,)
    # Selection cannot manufacture completion: the row stays restarting until
    # the durable restarter admits the successor and writes its completion row.
    successor = lifecycle_restart.build("diagnostic")
    assert successor.cursor == 0
    assert successor.script == lifecycle_restart.IDLE_SCRIPT
    assert not successor.script[0].tool_calls


def test_pending_request_does_not_select_post_request_script(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id, _birth = create_agent_row(spawner="test", machine=machine_name())
    monkeypatch.setattr(ava.self, "AGENT_ID", agent_id)
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,status) "
        "VALUES(%s,'','restart','self','pending')",
        (agent_id,),
    )
    db_conn.commit()
    assert lifecycle_restart.build("diagnostic").script == lifecycle_restart.RESTART_SCRIPT
