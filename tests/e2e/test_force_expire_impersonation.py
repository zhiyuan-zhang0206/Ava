"""The sidebar closes a live external session and the native agent resumes."""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from shared.config import settings
from shared.db import publish_inbound_wake
from shared.live_announce import publish_agent_updated_sync, publish_impersonation_changed_sync
from tests.e2e._env import E2EEnv
from tests.e2e.fakes.scenarios.force_expire import FIRST_REPLY, RESUMED_REPLY
from tests.shared.poll_until import poll_until


def _seed_active_lease(agent_id: int) -> tuple[UUID, int]:
    """Insert one already-active lease without publishing intermediate consent wakes."""
    lease_id = uuid4()
    with psycopg.connect(settings.data_plane.db_url) as conn:
        row = conn.execute(
            "SELECT runtime_generation,runtime_owner,machine FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
        assert row is not None and row[0] is not None and row[1] is not None
        session_row = conn.execute(
            "INSERT INTO agent_impersonations(id,agent_id,session_id,source,machine,reason,"
            "status,ttl_seconds,expires_at,accepted_generation,accepted_owner,"
            "activated_at,start_message,relay_provider,relay_thread_id,relay_heartbeat_at) "
            "VALUES(%s,%s,0,'external_agent:codex:e2e',%s,'External work','active',3600,"
            "clock_timestamp()+interval '1 hour',%s,%s,clock_timestamp(),"
            "'External work brief','codex',%s,clock_timestamp()) RETURNING session_id",
            (lease_id, agent_id, row[2], row[0], row[1], str(uuid4())),
        ).fetchone()
        assert session_row is not None
        session_id = session_row[0]
    publish_inbound_wake(agent_id, "impersonation")
    publish_impersonation_changed_sync(agent_id)
    publish_agent_updated_sync(agent_id)
    return lease_id, session_id


@pytest.mark.scenario("tests.e2e.fakes.scenarios.force_expire:build")
def test_sidebar_force_expires_takeover_and_agent_resumes(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)
    page.fill('[data-testid="composer-input"]', "Begin work")
    page.click('[data-testid="composer-send"]')
    page.get_by_text(FIRST_REPLY).wait_for(timeout=30_000)

    # A visible streaming reply can precede its durable checkpoint.
    def first_turn_committed() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            inbound = conn.execute(
                "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='chat' "
                "ORDER BY id DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        timeline = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=10.0
        ).json()["items"]
        replied = any(
            item["kind"] == "agent_chat" and FIRST_REPLY in str(item["payload"])
            for item in timeline
        )
        return bool(inbound == ("done",) and replied), {"inbound": inbound, "replied": replied}

    poll_until(first_turn_committed, timeout=30.0, interval=0.5, what="first turn committed")

    lease_id, session_id = _seed_active_lease(agent_id)

    timeline = httpx.get(
        f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=10.0
    ).json()["items"]
    assert not any(RESUMED_REPLY in str(item["payload"]) for item in timeline)

    roster = httpx.get(f"{e2e_env.gateway_url}/api/agents/roster", timeout=10.0).json()
    card = next(card for card in roster["agents"] if card["agent_id"] == agent_id)
    assert card["open_impersonation_session_id"] == session_id

    row = page.locator("li.group.relative").filter(has_text=f"#{agent_id}").first
    action = page.get_by_text("End external takeover session")

    def menu_ready() -> tuple[bool, object]:
        row.click(button="right")
        visible = action.is_visible()
        if not visible:
            page.keyboard.press("Escape")
        return visible, {
            "row": row.inner_text(),
            "menu": page.locator('[role="menu"]').all_inner_texts(),
        }

    poll_until(menu_ready, timeout=15.0, interval=0.5, what="open session appears in sidebar menu")
    page.once("dialog", lambda dialog: dialog.accept())
    action.click()
    page.get_by_text("Takeover session ended").wait_for(timeout=10_000)

    def menu_cleared() -> tuple[bool, object]:
        row.click(button="right")
        visible = action.is_visible()
        page.keyboard.press("Escape")
        return not visible, {"end_action_visible": visible}

    poll_until(menu_cleared, timeout=10.0, interval=0.5, what="closed session leaves sidebar menu")

    def resumed() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            result = conn.execute(
                "SELECT l.status,l.ended_at,l.rejection_reason,i.status "
                "FROM agent_impersonations l LEFT JOIN inbound_messages i "
                "ON i.id=l.summary_inbound_id WHERE l.id=%s",
                (lease_id,),
            ).fetchone()
        timeline = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=10.0
        ).json()["items"]
        replied = any(RESUMED_REPLY in str(item["payload"]) for item in timeline)
        return bool(
            result and result[0] == "expired" and result[1] and result[3] == "done" and replied
        ), result

    poll_until(resumed, timeout=60.0, interval=0.5, what="native agent resumes after force-expire")
    page.get_by_text(RESUMED_REPLY).wait_for(timeout=10_000)
