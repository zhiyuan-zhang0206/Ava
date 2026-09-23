"""Public SDK retry keeps the committed agent identity."""

from __future__ import annotations

import psycopg
import pytest

import ava
from tests.ava.test_agents_sdk import _sdk_via_inprocess_gateway, _spawn_agent


@pytest.mark.usefixtures(_sdk_via_inprocess_gateway.__name__)
def test_retry_launch_reuses_existing_agent_identity(db_conn: psycopg.Connection) -> None:
    agent_id = _spawn_agent()
    assert ava.agents.retry_launch(agent_id) == agent_id
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents_meta WHERE id=%s", (agent_id,))
        assert cur.fetchone() == (1,)
