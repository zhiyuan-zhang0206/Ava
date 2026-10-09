"""Scenario selection must not repeat restart after a consumed request.

This fixes the fake discriminator, not the production completion protocol.
The durable restarter remains the only process-replacement owner; the fake
scenario only distinguishes a consumed request from a successful completion.
"""

from __future__ import annotations

import psycopg
import pytest

from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.plugin_providers import build_model_catalog
from ops.agents.spawn import create_agent_row
from tests.e2e.fakes.scenarios import lifecycle_restart


@pytest.mark.parametrize("status", ["claimed", "done"])
def test_consumed_restart_selects_successor_script_without_claiming_completion(
    db_conn: psycopg.Connection,
    status: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    agent_id, _birth, _prompt_id, _attempt_id = create_agent_row(
        database,
        event_bus,
        spawner="test",
        machine=machine_name(),
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    initial = lifecycle_restart.build("diagnostic", agent_id=agent_id)
    assert initial.script == lifecycle_restart.RESTART_SCRIPT
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,status) "
        "VALUES(%s,'','restart','self',%s) RETURNING id",
        (agent_id, status),
    ).fetchone()
    assert row is not None
    original_request = row[0]
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent_id,))
    db_conn.commit()
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
    rows = db_conn.execute(
        "SELECT id,kind,source,status FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall()
    assert rows == [(original_request, "restart", "self", status)]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart_completed'",
        (agent_id,),
    ).fetchone() == (0,)
    # Selection cannot manufacture completion: the row stays idling until
    # the durable restarter admits the successor and writes its completion row.
    successor = lifecycle_restart.build("diagnostic", agent_id=agent_id)
    assert successor.cursor == 0
    assert successor.script == lifecycle_restart.IDLE_SCRIPT
    assert not successor.script[0].tool_calls


def test_pending_request_does_not_select_post_request_script(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    agent_id, _birth, _prompt_id, _attempt_id = create_agent_row(
        database,
        event_bus,
        spawner="test",
        machine=machine_name(),
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,status) "
        "VALUES(%s,'','restart','self','pending')",
        (agent_id,),
    )
    db_conn.commit()
    assert (
        lifecycle_restart.build("diagnostic", agent_id=agent_id).script
        == lifecycle_restart.RESTART_SCRIPT
    )
