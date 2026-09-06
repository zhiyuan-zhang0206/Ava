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

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import psutil
import psycopg
import pytest
from playwright.sync_api import Page

from ops.cluster_rpc import dispatch_to_machine
from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.e2e.conftest import _HOSTED
from tests.shared.poll_until import poll_until


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_resurrect:build_waiting_for_chat")
def test_resurrect_brings_back_terminated_agent(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    if _HOSTED:
        _assert_hosted_resurrect(e2e_env, page, agent_id)
        return

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
    last_resurrect = False
    last_status: str | None = None
    last_pid: int | None = None

    def resurrect_cycle_completed() -> tuple[bool, object]:
        nonlocal last_resurrect, last_status, last_pid
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
        reached_successor = (
            last_resurrect
            and last_status == AgentStatus.IDLING.value
            and last_pid is not None
            and last_pid != first_pid
        )
        state: dict[str, object] = {
            "first_pid": first_pid,
            "resurrect": last_resurrect,
            "status": last_status,
            "pid": last_pid,
        }
        if not reached_successor:
            return False, state

        with psycopg.connect(settings.data_plane.db_url) as conn:
            wake = conn.execute(
                "SELECT status,payload->'resurrection_retry'->>'attempts' "
                "FROM inbound_messages WHERE agent_id=%s AND kind='chat' AND content='wake up'",
                (agent_id,),
            ).fetchone()
        state["wake"] = wake
        if wake is None:
            return False, state
        assert wake[0] in ("claimed", "done")
        assert int(wake[1]) >= 2, "the old process must have remained alive through a retry"

        items = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=30
        ).json()["items"]
        reply_seen = any(
            item["kind"] == "agent_chat"
            and "I processed the wake after resurrection." in item["payload"]
            for item in items
        )
        state["timeline_kinds"] = [item["kind"] for item in items]
        state["reply_seen"] = reply_seen
        return reply_seen, state

    poll_until(
        resurrect_cycle_completed,
        timeout=90.0,
        interval=0.5,
        what=f"agent {agent_id} completes its resurrect cycle",
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_resurrect:build")
def test_explicit_resurrection_after_observed_process_exit(
    e2e_env: E2EEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Separate supported lifecycle RPC, not the chat auto-resurrection path."""
    if _HOSTED:
        pytest.skip("process exit identity belongs to the process-mode contract")
    agent_id = e2e_env.agent_id
    with psycopg.connect(settings.data_plane.db_url) as conn:
        row = conn.execute(
            "SELECT pid,machine FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
    assert row is not None and row[0] is not None
    original = psutil.Process(row[0])
    e2e_env.page.goto(e2e_env.agent_url)
    e2e_env.page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)
    e2e_env.page.fill('[data-testid="composer-input"]', "terminate before explicit resurrection")
    e2e_env.page.click('[data-testid="composer-send"]')
    wait_for_status(agent_id, AgentStatus.TERMINATED.value)

    def original_process_exited() -> tuple[bool, object]:
        try:
            running = original.is_running()
            status = original.status() if running else None
        except psutil.NoSuchProcess:
            return True, {"pid": original.pid, "running": False, "status": "missing"}
        exited = not running or status == psutil.STATUS_ZOMBIE
        return exited, {"pid": original.pid, "running": running, "status": status}

    poll_until(
        original_process_exited,
        timeout=90,
        interval=0.05,
        what=f"original agent process {original.pid} exits",
    )
    # Playwright's sync bridge keeps an event loop on this thread. The real
    # lifecycle RPC therefore runs on a separate, bounded test-owned thread.
    # Root truncated_db disables auth only in this pytest process, while the
    # real ops subprocess retains the root fixture's public test credential.
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-cluster-secret")
    with ThreadPoolExecutor(max_workers=1) as executor:
        response = executor.submit(
            asyncio.run,
            dispatch_to_machine(
                target_machine=row[1],
                kind="lifecycle",
                payload={
                    "path": f"/api/agents/{agent_id}/resurrect-explicit-v2",
                    "body": {"resurrected_by": "user", "prompt": "explicit wake"},
                },
            ),
        ).result(timeout=120)
    assert response["status"] == "spawned"
    wait_for_status(agent_id, AgentStatus.IDLING.value)
    with psycopg.connect(settings.data_plane.db_url) as conn:
        after = conn.execute(
            "SELECT pid,lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        commands = conn.execute(
            "SELECT status,observed_at IS NOT NULL FROM inbound_messages "
            "WHERE agent_id=%s AND kind='terminate'",
            (agent_id,),
        ).fetchall()
    assert (
        after is not None and after[0] is not None and after[0] != original.pid and after[1] is None
    )
    assert commands == [("done", True)]


def _assert_hosted_resurrect(e2e_env: E2EEnv, page: Page, agent_id: int) -> None:
    """Hosted auto-resurrect: no process to respawn and no pid to compare —
    the row becomes runnable with the resurrect inbound inside the op, and the
    dispatcher's turn task claims the marker. The host settles its running
    status after that marker is published, so the test waits for idling before
    asserting the resurrected row is runnable with a NULL pid."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None, "spawned row missing"
    assert row[0] == AgentStatus.IDLING.value, f"expected idling before terminate, got {row[0]!r}"
    assert row[1] is None, "hosted rows never carry a pid"

    # turn 1: agent self-terminate
    page.fill('[data-testid="composer-input"]', "\u518d\u89c1")
    page.click('[data-testid="composer-send"]')
    wait_for_status(agent_id, AgentStatus.TERMINATED.value)

    # a chat to a dead agent auto-resurrects it (deliver_chat_inbound ->
    # resurrect_if_terminated); hosted: the op flips the row and wakes, the
    # dispatcher materializes the turn.
    resp = httpx.post(
        f"{e2e_env.gateway_url}/api/agents/{agent_id}/messages",
        json={"content": "wake up", "source": "user"},
        timeout=90.0,
    )
    resp.raise_for_status()
    assert "status" in resp.json()

    def resurrect_marker_reached_timeline() -> tuple[bool, object]:
        items = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
        ).json()["items"]
        seen_marker = any(
            it.get("kind") == "system_marker"
            and "You have been resurrected by" in (it.get("payload") or "")
            for it in items
        )
        return seen_marker, {"timeline_kinds": [it.get("kind") for it in items]}

    poll_until(
        resurrect_marker_reached_timeline,
        timeout=90.0,
        interval=0.5,
        what=f"hosted resurrect marker reaches agent {agent_id} timeline",
    )
    wait_for_status(agent_id, AgentStatus.IDLING.value)

    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        cur.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id = %s AND kind = 'resurrect' LIMIT 1",
            (agent_id,),
        )
        resurrect = cur.fetchone()
    assert row is not None and row[0] == AgentStatus.IDLING.value, (
        f"hosted resurrect must leave the row runnable, got {row[0] if row else None!r}"
    )
    assert row[1] is None, "hosted rows never carry a pid"
    assert resurrect is not None, "the auto-resurrect inbound row is missing"
