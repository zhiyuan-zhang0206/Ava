"""A durable self-restart never creates a competing agent-side launcher."""

from unittest.mock import Mock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent._starting import claim_agent_row
from agent.db import claim_inbound_batch
from agent.graph._claim_dispatch import _BatchState, _handle_restart
from shared.context import AvaContext
from tests.agent.test_lifecycle_intent import _command
from tests.agent.test_runtime_incarnation import _row


async def test_self_restart_has_no_atexit_or_uncontrolled_process_launch(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    command = _command(db_conn, agent_id, "restart")
    db_conn.execute("UPDATE inbound_messages SET source='self' WHERE id=%s", (command,))
    db_conn.commit()
    batch = await claim_inbound_batch(aops_pool, agent_id)
    register, launch = Mock(), Mock()
    monkeypatch.setattr("atexit.register", register)
    monkeypatch.setattr("subprocess.Popen", launch)
    await _handle_restart(AvaContext(ops_pool=aops_pool), agent_id, batch[0], _BatchState())
    assert register.call_count == 0, "self-restart must not register a competing launcher"
    assert launch.call_count == 0
    assert db_conn.execute(
        "SELECT m.status,i.applied_at IS NOT NULL,i.observed_at "
        "FROM agents_meta m JOIN inbound_messages i ON i.id=m.lifecycle_command_id "
        "WHERE m.id=%s",
        (agent_id,),
    ).fetchone() == ("restarting", True, None)
