"""Retired lifecycle ingress cannot enqueue work or rotate a launch attempt."""

from uuid import uuid4

import psycopg
import pytest

from base.db import create_agent
from gateway.app import app
from tests.fixtures.gateway_config import gateway_test_client


@pytest.mark.parametrize(
    "route", ["/api/cancel", "/api/agents/{agent}/compact", "/api/agents/{agent}/retry-launch"]
)
def test_retired_route_has_no_effect(db_conn: psycopg.Connection, route: str) -> None:
    agent = create_agent(db_conn)
    attempt = uuid4()
    db_conn.execute(
        "INSERT INTO agents_meta(id, status, last_launch_attempt_id) VALUES (%s, 'idling', %s)",
        (agent, attempt),
    )
    db_conn.commit()
    with gateway_test_client(app) as client:
        response = client.post(
            route.format(agent=agent),
            json={"agent_id": agent},
            headers={"Idempotency-Key": "retired"},
        )
    assert response.status_code in (404, 405)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (0,)
    assert db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == (attempt,)
