"""Parked-compact switch-back — task #1959 regression.

The report: with the inspector open the user switches to another agent; the
original agent compacts while the user is away; switching back shows the
compact-era stale data. The frontend's all-events stream was filtered to the
ACTIVE agent only, so a parked thread never received its compact_done — the
timeline store's compact reset window could never arm, and the switch-back
keep-all merge resurrected the compacted-away history (the stale bubbles
persisted indefinitely). The fix selects the parked threads (plus
compact-marker ids) in the /api/system/all filter, so compact_done and the
first post-compact timeline_snapshot reach the store: the parked bucket is
wholesale-replaced by the post-compact snapshot, and the switch-back refetch
(whose invalidations now also cover inspect / pending / token-usage) merges as
a no-op.

This test locks the full flow in the real browser:

1. agent A gets conversation content (the pre-compact history to compress)
2. switch to agent B in the sidebar (A becomes a parked thread)
3. force-compact A while it is parked (the exact call the UI button makes)
4. wait for the compact to commit (REST timeline shows the envelope)
5. switch back to A and assert, at the network layer, a fresh GET /timeline
   fired for A AFTER the compact, and at the DOM layer the post-compact state
   renders — the compact envelope is present and the pre-compact replies are
   GONE (no resurrection).

Deflake note: the switch-back deliberately does NOT sleep for the SSE frames.
The stream can lose the parked thread's compact events entirely (the
all-events re-key on agent switch drops whatever publishes inside the
reconnect gap), so the test waits on the REAL post-compact condition — the
post-compact-only narration renders — and holds the no-resurrection invariant
with auto-retrying counts. The frontend heals the lost-event case at the
merge (the fetched snapshot's compact envelope is a fingerprint the stale
bucket cannot match → wholesale replace), so the condition always converges
instead of racing a clock.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import httpx
import psycopg
import pytest
from playwright.sync_api import Page, expect

from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.e2e.fakes.scenarios.parked_compact import (
    POST_COMPACT_NARRATION,
    REPLY_1,
    REPLY_2,
    REPLY_3,
)
from tests.shared.poll_until import poll_until


def _timeline(gateway_url: str, agent_id: int) -> list[dict[str, Any]]:
    return httpx.get(
        f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
    ).json()["items"]


def _wait_kind(gateway_url: str, agent_id: int, kind: str, timeout: float = 60.0) -> None:
    """Poll until a timeline item of `kind` exists (the post-compact narration
    can commit a beat after the envelope)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = _timeline(gateway_url, agent_id)
        if any(it["kind"] == kind for it in items):
            return
        time.sleep(0.5)
    items = _timeline(gateway_url, agent_id)
    raise RuntimeError(
        f"kind={kind!r} never appeared for agent {agent_id} within {timeout}s; "
        f"last items: {[it['kind'] for it in items][-8:]}"
    )


def _wait_idle_intent(agent_id: int) -> None:
    """An unmessaged agent may be idle before its first hosted admission."""

    def idle() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, runtime_kind, runtime_owner, runtime_generation "
                "FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
        if row is None:
            return False, row
        status, kind, owner, generation = row
        unadmitted = kind is None and owner is None and generation is None
        hosted = kind == "hosted" and owner is not None and generation is not None
        return status == AgentStatus.IDLING.value and (unadmitted or hosted), row

    poll_until(
        idle,
        timeout=90.0,
        interval=0.3,
        what=f"agent {agent_id} has idle intent and a coherent hosted identity",
    )


def _sidebar_row(page: Page, agent_id: int):
    """The sidebar row button for an agent — its default label renders as
    `#<id>`."""
    return page.locator(f'button:has-text("#{agent_id}")').first


@pytest.mark.scenario("tests.e2e.fakes.scenarios.parked_compact:build")
def test_switch_back_after_parked_compact_shows_post_compact_state(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_a = e2e_env.agent_id
    gateway_url = e2e_env.gateway_url

    # Second agent the user switches to. Same scripted fake LLM — B is never
    # messaged, so its script turns are never consumed.
    resp = httpx.post(f"{gateway_url}/api/agents", json={"spawner": "user"}, timeout=30.0)
    resp.raise_for_status()
    agent_b = int(resp.json()["id"])
    try:
        _wait_idle_intent(agent_b)

        page.goto(e2e_env.agent_url)
        page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

        # ── 1. conversation content on A (the history the compact rewrites) ──
        for reply in (REPLY_1, REPLY_2, REPLY_3):
            page.fill('[data-testid="composer-input"]', "\u4f60\u597d")
            page.click('[data-testid="composer-send"]')
            page.wait_for_selector(f"text={reply}", timeout=30_000)
            wait_for_status(agent_a, AgentStatus.IDLING.value)

        # ── 2. switch to B — A becomes a parked thread ──
        page.wait_for_selector(f'button:has-text("#{agent_b}")', timeout=15_000)
        _sidebar_row(page, agent_b).click()
        page.wait_for_function(f"location.href.includes('agent_id={agent_b}')", timeout=15_000)

        # ── 3. compact A while it is parked (same call as the UI button) ──
        resp = httpx.post(f"{gateway_url}/api/agents/{agent_a}/compact", timeout=30.0)
        resp.raise_for_status()
        assert resp.json()["status"] == "enqueued"
        _wait_kind(gateway_url, agent_a, "inbound_compact_request")
        wait_for_status(agent_a, AgentStatus.IDLING.value)

        # ── 4. switch back: network evidence + post-compact DOM state ──
        timeline_requests: list[str] = []
        page.on(
            "request",
            lambda req: (
                timeline_requests.append(req.url)
                if f"/api/agents/{agent_a}/timeline" in req.url
                else None
            ),
        )
        _sidebar_row(page, agent_a).click()
        page.wait_for_function(f"location.href.includes('agent_id={agent_a}')", timeout=15_000)

        # Network layer: switching back after the parked compact must re-fire
        # the timeline fetch (the stale-while-revalidate reconcile).
        deadline = time.monotonic() + 15.0
        while not timeline_requests and time.monotonic() < deadline:
            time.sleep(0.2)
        assert timeline_requests, "switch-back after a parked compact did not refetch the timeline"

        # DOM layer: the post-compact state renders — the compact envelope is
        # there and the compacted-away replies are GONE (the regression left
        # them resurrected by the keep-all merge).
        page.wait_for_selector("text=Compact request", timeout=15_000)
        # Wait on the REAL post-compact condition, not a clock: the narration
        # is content that exists only in the post-compact history, so it proves
        # the rendered thread has left the pre-compact state behind. The
        # switch-back refetch may land a beat after the (possibly stale)
        # parked bucket seeds, so this is a condition wait, not a sleep.
        page.wait_for_selector(f"text={POST_COMPACT_NARRATION}", timeout=15_000)
        # Pre-compact exchanges sit at item_ids above the post-compact tail
        # (the envelope + narration reuse only the first two wiped slots) — a
        # keep-all merge of the stale parked bucket would resurrect them (the
        # regression). Auto-retrying counts: a late resurrecting merge must
        # flip these red, so the assertion holds the invariant instead of
        # sampling it once.
        expect(page.get_by_text(REPLY_2)).to_have_count(0, timeout=15_000)
        expect(page.get_by_text(REPLY_3)).to_have_count(0, timeout=15_000)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{gateway_url}/api/agents/{agent_b}/terminate", timeout=5.0)
