"""The sidebar closes a live external session and the native agent resumes."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from base.agents.impersonation._store import token_hash
from base.agents.impersonation.delivery import reserve_delivery
from base.agents.impersonation.relay import relay_get, relay_inbox
from base.cluster.machine import set_identity
from base.config import settings
from base.db import Database, publish_inbound_wake
from base.events.live.announce import (
    publish_agent_updated_sync,
    publish_impersonation_changed_sync,
)
from base.events.live.bus import EventBus
from tests.components.base.poll_until import poll_until
from tests.e2e._env import E2EEnv
from tests.e2e.fakes._recording import model_inputs, reset_record
from tests.e2e.fakes.scenarios.force_expire import FIRST_REPLY, RESUMED_REPLY


def _seed_active_lease(agent_id: int, relay_token: str) -> tuple[UUID, int]:
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
            "activated_at,start_message,relay_provider,relay_thread_id,relay_heartbeat_at,"
            "relay_token_hash,ack_window_seconds,max_delivery_attempts) "
            "VALUES(%s,%s,0,'external_agent:codex:e2e',%s,'External work','active',3600,"
            "clock_timestamp()+interval '1 hour',%s,%s,clock_timestamp(),"
            "'External work brief','codex',%s,clock_timestamp(),%s,2,2) RETURNING session_id",
            (lease_id, agent_id, row[2], row[0], row[1], str(uuid4()), token_hash(relay_token)),
        ).fetchone()
        assert session_row is not None
        session_id = session_row[0]
    publish_inbound_wake(
        Database.from_settings(), EventBus.from_settings(), agent_id, "impersonation"
    )
    bus = EventBus.from_settings()
    publish_impersonation_changed_sync(bus, agent_id)
    publish_agent_updated_sync(bus, agent_id)
    return lease_id, session_id


def _exhaust_delivery_budget(env: E2EEnv, lease_id: UUID, relay_token: str) -> str:
    """Drive real relay reservations; leave the unanswered UI request for native recovery."""
    request = "Please keep this unanswered request when the external session ends."
    env.page.fill('[data-testid="composer-input"]', request)
    env.page.click('[data-testid="composer-send"]')
    db, bus = Database.from_settings(), EventBus.from_settings()
    handle = str(lease_id)
    pending: list[int] = []

    def captured() -> tuple[bool, object]:
        messages = relay_inbox(db, handle, relay_token)
        pending[:] = [message["id"] for message in messages if message["content"] == request]
        return len(pending) == 1, messages

    poll_until(captured, timeout=10.0, what="UI request reaches the relay inbox")
    before = relay_get(db, bus, handle, relay_token)
    assert reserve_delivery(db, bus, handle, relay_token, pending) == frozenset(pending)
    # A stale inbox snapshot must not spend a second attempt in the same window.
    assert not reserve_delivery(db, bus, handle, relay_token, pending)

    def retry_due() -> tuple[bool, object]:
        reserved = reserve_delivery(db, bus, handle, relay_token, pending)
        return reserved == frozenset(pending), reserved

    poll_until(retry_due, timeout=10.0, what="second delivery becomes due")

    def budget_exhausted() -> tuple[bool, object]:
        # The real expiry check detects the elapsed final ACK window, without
        # rewriting delivery timestamps or marking the input received.
        session = relay_get(db, bus, handle, relay_token)
        with psycopg.connect(settings.data_plane.db_url) as conn:
            state = conn.execute(
                "SELECT l.relay_degraded_reason,m.delivery_attempts,m.acknowledged_at,i.status "
                "FROM agent_impersonations l JOIN agent_impersonation_messages m "
                "ON m.lease_id=l.id JOIN inbound_messages i ON i.id=m.inbound_id "
                "WHERE l.id=%s AND i.id=%s",
                (lease_id, pending[0]),
            ).fetchone()
        assert session["status"] == "active"
        assert session["expires_at"] == before["expires_at"]
        assert state is not None and state[1:] == (2, None, "pending")
        assert len(model_inputs(env.agent_id)) == 1  # Native execution remains parked.
        return bool(state[0] and "exhausted its delivery budget" in state[0]), state

    poll_until(budget_exhausted, timeout=10.0, what="final ACK window expires")
    assert not reserve_delivery(db, bus, handle, relay_token, pending)
    assert pending[0] in {message["id"] for message in relay_inbox(db, handle, relay_token)}
    return request


@pytest.mark.parametrize("exhaust_ack_budget", [False, True], ids=["ordinary", "ack-exhausted"])
@pytest.mark.scenario("tests.e2e.fakes.scenarios.force_expire:build")
def test_sidebar_force_expires_takeover_and_agent_resumes(
    e2e_env: E2EEnv, exhaust_ack_budget: bool
) -> None:
    # The test-process relay driver uses the same machine as the real e2e host.
    set_identity(name=os.environ["AVA_MACHINE_NAME"])
    reset_record()
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

    relay_token = str(uuid4())
    lease_id, session_id = _seed_active_lease(agent_id, relay_token)
    request = (
        _exhaust_delivery_budget(e2e_env, lease_id, relay_token) if exhaust_ack_budget else None
    )

    timeline = httpx.get(
        f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=10.0
    ).json()["items"]
    assert not any(RESUMED_REPLY in str(item["payload"]) for item in timeline)

    roster = httpx.get(f"{e2e_env.gateway_url}/api/agents/roster", timeout=10.0).json()
    card = next(card for card in roster["agents"] if card["agent_id"] == agent_id)
    assert card["open_impersonation_session_id"] == session_id
    assert card["open_impersonation_status"] == "active"

    # The sidebar shows no dedicated takeover label or on-row button (the only
    # visible difference for an impersonated agent is its status) — ending a
    # takeover is reachable only from the row's right-click context menu.
    # Radix keeps the menu's content live while it stays open, so a single
    # right-click followed by a generous wait still lets the frontend's own
    # SSE-driven roster refetch land the conditional menu item.
    row = page.locator("li.group.relative").filter(has_text=f"#{agent_id}").first
    row.click(button="right")
    menu_item = page.get_by_role("menuitem", name="End external takeover session")
    menu_item.wait_for(state="visible", timeout=15_000)
    page.once("dialog", lambda dialog: dialog.accept())
    menu_item.click()
    page.get_by_text("Takeover session ended").wait_for(timeout=10_000)

    # Re-open the menu until the item is gone — proves the frontend, not just
    # the backend, has picked up the closed session (mirrors the pre-removal
    # test's poll on the on-row button's disappearance).
    def end_item_absent() -> tuple[bool, object]:
        row.click(button="right")
        present = page.get_by_role("menuitem", name="End external takeover session").is_visible()
        page.keyboard.press("Escape")
        return not present, {"end_item_present": present}

    poll_until(
        end_item_absent, timeout=10.0, interval=0.5, what="end-session item leaves the context menu"
    )

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

    if request is not None:
        calls = model_inputs(agent_id)
        assert len(calls) == 2
        assert any(request in message["text"] for message in calls[-1])
        with psycopg.connect(settings.data_plane.db_url) as conn:
            received = conn.execute(
                "SELECT i.status,m.delivery_attempts,m.acknowledged_at FROM inbound_messages i "
                "JOIN agent_impersonation_messages m ON m.inbound_id=i.id "
                "WHERE m.lease_id=%s AND i.content=%s",
                (lease_id, request),
            ).fetchone()
        assert received == ("done", 2, None)  # Native consumed it; no external ACK was invented.
