"""An incomplete retry identity cannot rotate a committed agent's attempt."""

from __future__ import annotations

import psycopg
import pytest

import ava
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from gateway.tests.agents.sdk_support import sdk_via_gateway, spawn_agent


@pytest.mark.usefixtures(sdk_via_gateway.__name__)
def test_retry_launch_requires_observed_operation_identity(
    db_conn: psycopg.Connection, *, config_authority: ConfigAuthority, database_gate: ProcessDbGate
) -> None:
    agent_id = spawn_agent(config_authority=config_authority, database_gate=database_gate)
    prior = ava.agents.get_launch_attempt(agent_id)
    with pytest.raises(TypeError):
        ava.agents.retry_launch(agent_id)  # pyright: ignore[reportCallIssue]
    row = db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert row == (prior,)
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (0,)
