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
from playwright.sync_api import ConsoleMessage, Page

from base.agents import AgentStatus
from base.config import settings
from tests.components.base.poll_until import poll_until
from tests.e2e._db import wait_for_status
from tests.e2e.fakes.scenarios.message_flow import REPLY_TEXT
from tests.e2e.fixture_environment import E2EEnv

# The unrecognized-marker red alarm copy, en + zh. The #1017 user-visible
# warning; its ABSENCE is the semantic assertion of every marker case.
_UNRECOGNIZED_RE = re.compile(
    "Unrecognized system_marker|\u65e0\u6cd5\u8bc6\u522b\u7684 system_marker"
)


def _collect_unrecognized_console_warnings(page: Page) -> list[str]:
    """Return a live list that collects `unrecognized system_marker` console warnings."""
    warnings: list[str] = []

    def collect_if_unrecognized(message: ConsoleMessage) -> None:
        if "unrecognized system_marker" in message.text.lower():
            warnings.append(message.text)

    page.on("console", collect_if_unrecognized)
    return warnings


def _wait_for_scripted_reply_in_timeline(gateway_url: str, agent_id: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def scripted_reply_reached_timeline() -> tuple[bool, object]:
        nonlocal items
        items = httpx.get(
            f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
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
    return items


def _assert_timeline_fan_out_kinds(items: list[dict[str, Any]]) -> None:
    """REST timeline: the turn's fan-out in item_id order."""
    # The agent's boot inserts a few one-time guidance system_markers before
    # the first turn (agent_id / sdk_hint notes); they are filtered here —
    # their alarm-freedom is asserted via the DOM check in the test body.
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


def _assert_timeline_fan_out_payloads(items: list[dict[str, Any]]) -> None:
    reasoning = next(it for it in items if it["kind"] == "agent_reasoning")
    assert "\u5199\u4ee3\u7801\u7b97" in reasoning["payload"], reasoning
    code = next(it for it in items if it["kind"] == "agent_code")
    assert "print(1 + 2)" in code["payload"], code
    output = next(it for it in items if it["kind"] == "code_output")
    assert "3" in output["payload"], output  # real exec output
    assert output.get("exec_ms") is not None, "code_output must carry exec_ms"
    replies = [it["payload"] for it in items if it["kind"] == "agent_chat"]
    assert REPLY_TEXT in replies, replies


def _assert_no_unrecognized_alarm(page: Page, unrecognized_warnings: list[str]) -> None:
    """#1017 class: no unrecognized-marker alarm (testid + text), no console warning."""
    assert page.get_by_test_id("marker-unrecognized").count() == 0, (
        "marker-unrecognized alarm rendered"
    )
    assert page.get_by_text(_UNRECOGNIZED_RE).count() == 0, (
        "unrecognized system_marker alarm rendered for a known marker — "
        f"warnings={unrecognized_warnings}"
    )
    # Pump the playwright loop once before reading the collected list: the
    # page.on("console") deliveries only run while a call is in flight, so a
    # warning emitted right before this assert could still be in flight
    # (task #3927 class).
    page.evaluate("() => 1")
    assert unrecognized_warnings == [], (
        f"[timeline] unrecognized console warnings fired: {unrecognized_warnings}"
    )


def _wait_for_chat_inbound_settled(agent_id: int) -> None:
    """DB: the finished turn disposed its claim — the chat row is 'done'."""

    # Message states (docs/conventions/agents/agent-impersonation.md): a native chat row
    # moves pending -> claimed -> done, confirmed at settlement points —
    # every finished turn (#3999, services/agent_runner/agent_host/settlement.py
    # reconcile_inbounds_after_turn), a boot/recovery, or an abort. 'claimed'
    # is the mid-turn state only. Poll: the settle pass runs with the turn's
    # close, and a regression that leaves the row claimed must read red.
    def inbound_settled() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, status FROM inbound_messages WHERE agent_id = %s ORDER BY id",
                (agent_id,),
            )
            rows = cur.fetchall()
        return bool(rows) and rows[-1] == ("chat", "done"), rows

    poll_until(
        inbound_settled,
        timeout=30.0,
        interval=0.25,
        what=f"agent {agent_id} chat inbound to settle to 'done' after the finished turn",
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.message_flow:build")
def test_message_flow_renders_full_turn_without_unrecognized_marker(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    unrecognized_warnings = _collect_unrecognized_console_warnings(page)

    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    page.fill('[data-testid="composer-input"]', "1+2 \u7b49\u4e8e\u51e0\uff1f")
    page.click('[data-testid="composer-send"]')

    # Turn committed: status flips IDLING at claim entry; the checkpoint write
    # can still be in flight — poll the timeline (same pattern as fork test).
    wait_for_status(agent_id, AgentStatus.IDLING.value)

    items = _wait_for_scripted_reply_in_timeline(e2e_env.gateway_url, agent_id)
    _assert_timeline_fan_out_kinds(items)
    _assert_timeline_fan_out_payloads(items)

    # ── Browser DOM: the reply rendered via SSE (no reload happened) ──
    page.wait_for_selector(f"text={REPLY_TEXT}", timeout=15_000)

    _assert_no_unrecognized_alarm(page, unrecognized_warnings)
    _wait_for_chat_inbound_settled(agent_id)
