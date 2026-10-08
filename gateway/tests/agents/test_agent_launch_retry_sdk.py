"""An incomplete retry identity cannot rotate a committed agent's attempt."""

from __future__ import annotations

import psycopg
import pytest

import ava
from gateway.tests.agents.test_agents_sdk import _sdk_via_inprocess_gateway, _spawn_agent


@pytest.mark.usefixtures(_sdk_via_inprocess_gateway.__name__)
def test_retry_launch_requires_observed_operation_identity(db_conn: psycopg.Connection) -> None:
    agent_id = _spawn_agent()
    prior = ava.agents.get_launch_attempt(agent_id)
    with pytest.raises(TypeError):
        ava.agents.retry_launch(agent_id)  # pyright: ignore[reportCallIssue]
    row = db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert row == (prior,)
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (0,)
