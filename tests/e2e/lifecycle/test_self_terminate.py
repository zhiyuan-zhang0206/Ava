from __future__ import annotations

import psycopg
import pytest

from base.api_contracts.agent_contract import AgentStatus
from base.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e.fixture_environment import E2EEnv


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_terminate:build")
def test_self_terminate_marks_agent_terminated(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    page.fill('[data-testid="composer-input"]', "\u518d\u89c1")
    page.click('[data-testid="composer-send"]')

    wait_for_status(agent_id, AgentStatus.TERMINATED.value)

    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'terminate' LIMIT 1",
            (agent_id,),
        )
        row = cur.fetchone()
    assert row is not None, "terminate inbound row missing"
    assert row[0] == "self", (
        f"source should be 'self' (injected by ava.self.terminate), got {row[0]!r}"
    )
