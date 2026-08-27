"""Auto-resurrect e2e — sending a chat message to a terminated agent auto-resurrects it.

Full cycle:
  1. agent code: ava.self.terminate() → status='terminated'
  2. test calls POST /api/agents/{id}/messages → deliver_chat_inbound
     → auto-resurrect (resurrect_if_terminated) INSERT 'resurrect' inbound
     + session spawn starts fresh process
  3. new process starts, claim_agent_row UPDATE 'idling'→'running'
     → claim processes 'resurrect', writes marker → idle (status='idling')

Verifies:
- inbound_messages has kind='resurrect' row (auto-resurrect marker)
- agents.status final value='idling'
- agents.pid != first_pid (new PID = fresh process)
"""

from __future__ import annotations

import time

import httpx
import psycopg
import pytest

from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_resurrect:build")
def test_resurrect_brings_back_terminated_agent(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    # capture first process pid for comparison
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT pid FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None and row[0] is not None
    first_pid: int = row[0]

    # turn 1: agent self-terminate
    page.fill('[data-testid="composer-input"]', "\u518d\u89c1")
    page.click('[data-testid="composer-send"]')
    wait_for_status(agent_id, AgentStatus.TERMINATED.value)

    # send a chat message to trigger auto-resurrect — replaces the removed
    # /api/agents/{id}/resurrect endpoint. deliver_chat_inbound automatically calls
    # resurrect_if_terminated to resurrect the terminated agent.
    resp = httpx.post(
        f"{e2e_env.gateway_url}/api/agents/{agent_id}/messages",
        json={"content": "wake up", "source": "user"},
        timeout=90.0,
    )
    resp.raise_for_status()
    assert "status" in resp.json()

    # wait for fresh process to start + idle (90s ceiling same as _db.py wait_for_status —
    # pure headroom, e2e runs serial on dedicated box, not a tight bound)
    deadline = time.monotonic() + 90.0
    last_resurrect = False
    last_status: str | None = None
    last_pid: int | None = None
    while time.monotonic() < deadline:
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM inbound_messages WHERE agent_id = %s AND kind = 'resurrect' LIMIT 1",
                (agent_id,),
            )
            last_resurrect = cur.fetchone() is not None
            cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
            if row:
                last_status, last_pid = row
        if (
            last_resurrect
            and last_status == AgentStatus.IDLING.value
            and last_pid is not None
            and last_pid != first_pid
        ):
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"agent {agent_id} did not complete resurrect cycle within 90s: "
        f"first_pid={first_pid} resurrect={last_resurrect} "
        f"status={last_status!r} pid={last_pid}"
    )
