"""Real UI self-restart through durable application and successor admission.

Process mode requires a new PID, one completion marker, the original command's
applied/observed timestamps and a subsequent answered message on that same PID.
Hosted mode verifies its distinct acceptance marker and runnable pidless state;
it must not borrow process completion language or manufacture an exit event.
"""

from __future__ import annotations

import httpx
import psycopg
import pytest
from playwright.sync_api import Page

from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.e2e.conftest import _HOSTED
from tests.shared.poll_until import poll_until


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
    last_completed = False
    last_status: str | None = None
    last_pid: int | None = None
    successor_pid: int | None = None

    def restart_cycle_completed() -> tuple[bool, object]:
        nonlocal last_completed, last_status, last_pid, successor_pid
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
        # New PID alone cannot discharge the durable command or prove completion.
        reached = (
            last_completed
            and last_status == AgentStatus.IDLING.value
            and last_pid is not None
            and last_pid != first_pid
        )
        state: dict[str, object] = {
            "first_pid": first_pid,
            "completed": last_completed,
            "status": last_status,
            "pid": last_pid,
        }
        if reached:
            successor_pid = last_pid
        else:
            state["evidence"] = _restart_evidence(agent_id)
        return reached, state

    poll_until(
        restart_cycle_completed,
        timeout=90.0,
        interval=0.5,
        what=f"agent {agent_id} completes its restart cycle with a new process",
    )
    assert successor_pid is not None
    _assert_successor_consumes_next_message(e2e_env, successor_pid)


def _assert_successor_consumes_next_message(env: E2EEnv, successor_pid: int) -> None:
    """Real UI -> queue -> admitted successor, with no second self restart."""
    env.page.fill('[data-testid="composer-input"]', "continue after verified restart")
    env.page.click('[data-testid="composer-send"]')

    def successor_processed_follow_up() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            row = conn.execute(
                "SELECT status,pid,lifecycle_command_id FROM agents_meta WHERE id=%s",
                (env.agent_id,),
            ).fetchone()
            chats = conn.execute(
                "SELECT status,claimed_at IS NOT NULL FROM inbound_messages "
                "WHERE agent_id=%s AND kind='chat' "
                "AND content='continue after verified restart'",
                (env.agent_id,),
            ).fetchall()
            commands = conn.execute(
                "SELECT status,applied_at IS NOT NULL,observed_at IS NOT NULL "
                "FROM inbound_messages WHERE agent_id=%s AND kind='restart' AND source='self'",
                (env.agent_id,),
            ).fetchall()
            completions = conn.execute(
                "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart_completed'",
                (env.agent_id,),
            ).fetchone()
        assert commands == [("done", True, True)], commands
        assert completions == (1,), completions
        # Ordinary chat is checkpoint-backed: done is reconciled on restart or
        # compaction, not at each idle transition. Claim alone is insufficient;
        # require the persisted answer below while the same successor is idle.
        reply_seen = False
        timeline_kinds: list[str] = []
        if row == ("idling", successor_pid, None) and chats in (
            [("claimed", True)],
            [("done", True)],
        ):
            items = httpx.get(
                f"{env.gateway_url}/api/agents/{env.agent_id}/timeline?limit=1000", timeout=30
            ).json()["items"]
            timeline_kinds = [item["kind"] for item in items]
            reply_seen = any(
                item["kind"] == "agent_chat"
                and "Follow-up processed by successor." in item["payload"]
                for item in items
            )
        return reply_seen, {
            "agent": row,
            "chats": chats,
            "commands": commands,
            "completions": completions,
            "timeline_kinds": timeline_kinds,
            "reply_seen": reply_seen,
        }

    poll_until(
        successor_processed_follow_up,
        timeout=90,
        interval=0.5,
        what=f"successor process {successor_pid} answers the follow-up message",
    )


def _restart_evidence(agent_id: int) -> dict[str, object]:
    """Retain exact IDs/times on failure without logging message bodies or secrets."""
    with psycopg.connect(settings.data_plane.db_url) as conn:
        inbounds = conn.execute(
            "SELECT id,kind,source,status,created_at,claimed_at FROM inbound_messages "
            "WHERE agent_id=%s ORDER BY id",
            (agent_id,),
        ).fetchall()
        checkpoints = conn.execute(
            "SELECT checkpoint_id,parent_checkpoint_id,metadata->>'step' "
            "FROM checkpoints WHERE thread_id=%s ORDER BY checkpoint_id DESC LIMIT 8",
            (str(agent_id),),
        ).fetchall()
        lifecycle = conn.execute(
            "SELECT status,pid,termination_source FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
    return {"inbounds": inbounds, "checkpoints": checkpoints, "lifecycle": lifecycle}


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

    def restart_marker_reached_timeline() -> tuple[bool, object]:
        items = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
        ).json()["items"]
        seen_marker = any(
            it.get("kind") == "system_marker"
            and "Restart was accepted" in (it.get("payload") or "")
            for it in items
        )
        return seen_marker, {"timeline_kinds": [it.get("kind") for it in items]}

    poll_until(
        restart_marker_reached_timeline,
        timeout=90.0,
        interval=0.5,
        what=f"hosted restart marker reaches agent {agent_id} timeline",
    )
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
