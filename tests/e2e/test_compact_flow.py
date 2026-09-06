"""Panoramic Case 2 — compact flow: force compact → summary → timeline (#1018).

The #1017 regression path: a UI-triggered compact produced an untagged
summary message → system_marker source=null → the red "UNRECOGNIZED
SYSTEM_MARKER" alarm in the browser. PR #1796 fixed the tagging; this test
locks the full flow in the real browser:

1. user message → reply (conversation content to compress)
2. POST /api/agents/{id}/compact (the exact call the UI compact button makes)
   → compact_request inbound → claim runs the Compaction LLM (script turn 2)
   → clean wipe → compact summary stamped ava_msg_type=compact_request
3. assertions:
   - REST timeline is [system_prompt, inbound_compact_request] — the chat and
     reply are gone (clean wipe), the compact envelope took their place
   - the envelope card ("Compact request") renders in the browser
   - NO unrecognized-marker alarm + NO `[timeline] unrecognized` console
     warning — the #1017 regression class
   - a following message still gets a reply — the agent works post-compact
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
from tests.e2e.fakes.scenarios.compact_flow import (
    FIRST_REPLY,
    POST_COMPACT_NARRATION,
    POST_COMPACT_REPLY,
    SUMMARY_TEXT,
)
from tests.shared.poll_until import poll_until

_UNRECOGNIZED_RE = re.compile(
    "Unrecognized system_marker|\u65e0\u6cd5\u8bc6\u522b\u7684 system_marker"
)


def _timeline(gateway_url: str, agent_id: int) -> list[dict[str, Any]]:
    return httpx.get(
        f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
    ).json()["items"]


def _wait_kinds(
    gateway_url: str, agent_id: int, expected: list[str], timeout: float = 60.0
) -> list[dict[str, Any]]:
    """Poll until `expected` appears as an in-order subsequence of the
    turn-level kinds (system_markers filtered — boot guidance notes precede
    the first turn). Subsequence (not equality): the post-compact narration
    agent_chat can commit a beat after the compact envelope, and the poll
    must not race it."""
    items: list[dict[str, Any]] = []

    def expected_kinds_reached_timeline() -> tuple[bool, object]:
        nonlocal items
        items = _timeline(gateway_url, agent_id)
        kinds = [it["kind"] for it in items if it["kind"] != "system_marker"]
        kinds_iter = iter(kinds)
        reached = all(any(kind == want for kind in kinds_iter) for want in expected)
        return reached, {"expected": expected, "timeline_kinds": kinds}

    poll_until(
        expected_kinds_reached_timeline,
        timeout=timeout,
        interval=0.5,
        what=f"agent {agent_id} timeline contains {expected!r} in order",
    )
    return items


@pytest.mark.scenario("tests.e2e.fakes.scenarios.compact_flow:build")
def test_force_compact_renders_envelope_without_unrecognized_marker(e2e_env: E2EEnv) -> None:
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

    # ── 1. conversation content ──
    page.fill('[data-testid="composer-input"]', "\u4f60\u597d")
    page.click('[data-testid="composer-send"]')
    page.wait_for_selector(f"text={FIRST_REPLY}", timeout=30_000)
    wait_for_status(agent_id, AgentStatus.IDLING.value)

    # ── 2. force compact (same call as the UI compact button) ──
    resp = httpx.post(f"{e2e_env.gateway_url}/api/agents/{agent_id}/compact", timeout=30.0)
    resp.raise_for_status()
    assert resp.json()["status"] == "enqueued"

    # ── 3. compact ran: clean wipe + compact_request envelope ──
    items = _wait_kinds(e2e_env.gateway_url, agent_id, ["inbound_compact_request"])
    envelope = next(it for it in items if it["kind"] == "inbound_compact_request")
    assert envelope["kind"] == "inbound_compact_request", envelope
    assert SUMMARY_TEXT in envelope["payload"], envelope
    assert "Compact request" in envelope["payload"], envelope

    # DB: the compact_request inbound was claimed and finalized.
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        rows = cur.fetchall()
    assert rows[-1] == ("compact_request", "done"), rows

    # Browser: the envelope card renders, and NO #1017-class alarm.
    page.wait_for_selector("text=Compact request", timeout=15_000)
    assert page.get_by_test_id("marker-unrecognized").count() == 0, (
        "marker-unrecognized alarm rendered"
    )
    assert page.get_by_text(_UNRECOGNIZED_RE).count() == 0, (
        f"unrecognized system_marker alarm rendered after compact — {unrecognized_warnings}"
    )
    assert unrecognized_warnings == [], (
        f"[timeline] unrecognized console warnings fired after compact: {unrecognized_warnings}"
    )

    # The post-compact state: the compact transition resumes at LLM, so the
    # model speaks once after the wipe (narration) before the next message.
    page.wait_for_selector(f"text={POST_COMPACT_NARRATION}", timeout=15_000)

    # ── 4. the agent still works post-compact ──
    page.fill('[data-testid="composer-input"]', "\u8fd8\u5728\u5417")
    page.click('[data-testid="composer-send"]')
    page.wait_for_selector(f"text={POST_COMPACT_REPLY}", timeout=30_000)
    wait_for_status(agent_id, AgentStatus.IDLING.value)
    items = _wait_kinds(
        e2e_env.gateway_url,
        agent_id,
        ["inbound_compact_request", "agent_chat", "inbound_chat", "agent_chat"],
    )
    replies = [it["payload"] for it in items if it["kind"] == "agent_chat"]
    assert POST_COMPACT_REPLY in replies, replies
    assert page.get_by_text(_UNRECOGNIZED_RE).count() == 0
