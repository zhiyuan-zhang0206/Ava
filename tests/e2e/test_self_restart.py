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

import httpx
import psycopg
import pytest
from playwright.sync_api import Page

from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.e2e.conftest import _HOSTED


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_restart:build")
def test_self_restart_respawns_process_with_new_pid(e2e_env: E2EEnv, restarter_proc: None) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    if _HOSTED:
        _assert_hosted_self_restart(e2e_env, page, agent_id)
        return

    # first process must already be IDLING (spawned_agent fixture polled) — capture PID for comparison
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT pid FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None and row[0] is not None, "first process pid should be non-NULL"
    first_pid: int = row[0]

    page.fill('[data-testid="composer-input"]', "\u91cd\u542f")
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


def _assert_hosted_self_restart(e2e_env: E2EEnv, page: Page, agent_id: int) -> None:
    """Hosted self-restart: no process to respawn and no restarter to INSERT
    'restart_completed' — the claim renders the restart marker in the turn's
    state, the restart_requested channel drops the cached runtime inside the
    agent-host. The host settles its running status after the marker is
    published, so the test waits for idling before asserting the row remains
    runnable with a NULL pid and no restarter-style inbound row."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None, "spawned row missing"
    assert row[0] == AgentStatus.IDLING.value, f"expected idling before restart, got {row[0]!r}"
    assert row[1] is None, "hosted rows never carry a pid"

    page.fill('[data-testid="composer-input"]', "\u91cd\u542f")
    page.click('[data-testid="composer-send"]')

    deadline = time.monotonic() + 90.0
    seen_marker = False
    while True:
        items = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
        ).json()["items"]
        seen_marker = any(
            it.get("kind") == "system_marker"
            and "You have been restarted" in (it.get("payload") or "")
            for it in items
        )
        if seen_marker or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    assert seen_marker, "hosted restart marker never reached the timeline"
    wait_for_status(agent_id, AgentStatus.IDLING.value)

    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        cur.execute(
            "SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'restart_completed' LIMIT 1",
            (agent_id,),
        )
        completed = cur.fetchone()
    assert row is not None and row[0] == AgentStatus.IDLING.value, (
        f"hosted restart must keep the row runnable, got {row[0] if row else None!r}"
    )
    assert row[1] is None, "hosted rows never carry a pid"
    assert completed is None, "hosted restart renders the marker in state; no restarter INSERT"
