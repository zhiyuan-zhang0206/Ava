"""Panoramic Case 1 — normal message flow across the full stack (#1018).

UI message → gateway POST → agent claim → LLM (scripted fake: thinking block
+ execute_code tool call) → real exec → SSE stream → frontend timeline render
→ committed snapshot → DB. One turn asserts at every layer:

- DB: the inbound committed; the turn's messages reach the checkpoint
- REST timeline: the full item fan-out (reasoning / code / output / chat) in
  item_id order — the backend's rendering contract
- Browser DOM: the reply appears via SSE (page never reloaded), and NO
  "Unrecognized system_marker" red alarm + NO `[timeline] unrecognized`
  console warning — the #1017 regression class, now asserted in the real
  browser against real backend-produced markers.

This is the G1/G2/G5 gap closer: the first e2e scenario that drives a real
message turn and asserts the rendered timeline.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import psycopg
import pytest

from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.e2e.fakes.scenarios.message_flow import REPLY_TEXT
from tests.shared.poll_until import poll_until

# The unrecognized-marker red alarm copy, en + zh. The #1017 user-visible
# warning; its ABSENCE is the semantic assertion of every marker case.
_UNRECOGNIZED_RE = re.compile(
    "Unrecognized system_marker|\u65e0\u6cd5\u8bc6\u522b\u7684 system_marker"
)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.message_flow:build")
def test_message_flow_renders_full_turn_without_unrecognized_marker(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    unrecognized_warnings: list[str] = []
    page.on(
        "console",
        lambda m: (
            unrecognized_warnings.append(m.text)
            if "unrecognized system_marker" in m.text.lower()
            else None
        ),
    )

    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    page.fill('[data-testid="composer-input"]', "1+2 \u7b49\u4e8e\u51e0\uff1f")
    page.click('[data-testid="composer-send"]')

    # Turn committed: status flips IDLING at claim entry; the checkpoint write
    # can still be in flight — poll the timeline (same pattern as fork test).
    wait_for_status(agent_id, AgentStatus.IDLING.value)

    items: list[dict[str, Any]] = []

    def scripted_reply_reached_timeline() -> tuple[bool, object]:
        nonlocal items
        items = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
        ).json()["items"]
        reply_seen = any(it["kind"] == "agent_chat" and REPLY_TEXT in it["payload"] for it in items)
        return reply_seen, {
            "timeline_kinds": [it["kind"] for it in items],
            "agent_chat_payloads": [it["payload"] for it in items if it["kind"] == "agent_chat"],
        }

    poll_until(
        scripted_reply_reached_timeline,
        timeout=90.0,
        interval=0.5,
        what=f"scripted reply reaches agent {agent_id} timeline",
    )

    # ── REST timeline: the turn's fan-out in item_id order ──
    # The agent's boot inserts a few one-time guidance system_markers before
    # the first turn (agent_id / sdk_hint notes); they are filtered here —
    # their alarm-freedom is asserted via the DOM check below.
    turn_kinds = [
        it["kind"] for it in items if it["kind"] not in ("system_marker", "system_prompt")
    ]
    assert turn_kinds == [
        "inbound_chat",
        "agent_reasoning",
        "agent_chat",
        "agent_code",
        "code_output",
        "agent_chat",
    ], f"timeline fan-out wrong: {turn_kinds} (full: {[it['kind'] for it in items]})"

    reasoning = next(it for it in items if it["kind"] == "agent_reasoning")
    assert "\u5199\u4ee3\u7801\u7b97" in reasoning["payload"], reasoning
    code = next(it for it in items if it["kind"] == "agent_code")
    assert "print(1 + 2)" in code["payload"], code
    output = next(it for it in items if it["kind"] == "code_output")
    assert "3" in output["payload"], output  # real exec output
    assert output.get("exec_ms") is not None, "code_output must carry exec_ms"
    replies = [it["payload"] for it in items if it["kind"] == "agent_chat"]
    assert REPLY_TEXT in replies, replies

    # ── Browser DOM: the reply rendered via SSE (no reload happened) ──
    page.wait_for_selector(f"text={REPLY_TEXT}", timeout=15_000)

    # ── #1017 class: no unrecognized-marker alarm (testid + text), no console warning ──
    assert page.get_by_test_id("marker-unrecognized").count() == 0, (
        "marker-unrecognized alarm rendered"
    )
    assert page.get_by_text(_UNRECOGNIZED_RE).count() == 0, (
        "unrecognized system_marker alarm rendered for a known marker — "
        f"warnings={unrecognized_warnings}"
    )
    assert unrecognized_warnings == [], (
        f"[timeline] unrecognized console warnings fired: {unrecognized_warnings}"
    )

    # ── DB: the inbound was claimed (two-phase dispatch). 'claimed' is the
    # steady state for chat inbounds — 'done' only lands at process startup
    # reconcile or compaction (agent/db.py finalize_claimed_inbounds), so a
    # single read after IDLING is deterministic here. ──
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        rows = cur.fetchall()
    assert rows and rows[-1] == ("chat", "claimed"), rows
