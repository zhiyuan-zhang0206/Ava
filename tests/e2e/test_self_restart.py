"""ava.self.restart cross-process e2e — agent self-restarts, restarter daemon spawns
a new process.

Full cycle:
  agent code: ava.self.restart() → exec AgentRestart + INSERT 'restart' inbound
  → claim marks agents.status='restarting' + goto END → ainvoke returns → process exits
  → services/restarter daemon 1s poll sees 'restarting' → respawn_agent
  → INSERT 'restart_completed' inbound + status='idling' + session spawn
  → new process starts, claim_agent_row UPDATE 'idling'→'running'
  → claim processes 'restart_completed', writes "[system ts] You have been restarted" marker
  → idles waiting for next inbound (status='idling')

Verifies:
- inbound_messages has kind='restart_completed' row (restarter's sole marker)
- agents.status eventually returns to 'idling'
- agents.pid differs from first process (new PID = new process)
"""

from __future__ import annotations

import time

import psycopg
import pytest

from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._env import E2EEnv


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_restart:build")
def test_self_restart_respawns_process_with_new_pid(e2e_env: E2EEnv, restarter_proc: None) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    # first process must already be IDLING (spawned_agent fixture polled) — capture PID for comparison
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT pid FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None and row[0] is not None, "first process pid should be non-NULL"
    first_pid: int = row[0]

    page.fill('[data-testid="composer-input"]', "重启")
    page.click('[data-testid="composer-send"]')

    # restart cycle: services/restarter daemon 1s poll → respawn → new process IDLING
    # typical total time 2-5s; 90s ceiling same as tests/e2e/_db.py wait_for_status —
    # pure headroom, not a tight bound (e2e runs serial on dedicated box, see _db.py
    # "Why 90s"). A real hang (status never flips) still fails, just later.
    deadline = time.monotonic() + 90.0
    last_completed = False
    last_status: str | None = None
    last_pid: int | None = None
    while time.monotonic() < deadline:
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'restart_completed' LIMIT 1",
                (agent_id,),
            )
            last_completed = cur.fetchone() is not None
            cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
            if row:
                last_status, last_pid = row
        if (
            last_completed
            and last_status == AgentStatus.IDLING.value
            and last_pid is not None
            and last_pid != first_pid
        ):
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"agent {agent_id} did not complete restart cycle within 90s: "
        f"first_pid={first_pid} completed={last_completed} "
        f"status={last_status!r} pid={last_pid}"
    )
