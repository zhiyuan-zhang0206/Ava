from __future__ import annotations

import contextlib
import time
from typing import Any

import httpx
import psycopg
import pytest
from playwright.sync_api import Page, expect

from base.api_contracts.agent_contract import AgentStatus
from base.config import settings
from base.db import Database
from base.sessions.tests.poll_until import poll_until
from tests.e2e._db import enqueue_compact_history_fixture, wait_for_status
from tests.e2e._settings import pin_compact_history_off, pin_expand_runs_all
from tests.e2e.fakes.scenarios.parked_compact import (
    POST_COMPACT_NARRATION,
    REPLY_1,
    REPLY_2,
    REPLY_3,
)
from tests.e2e.fixture_environment import E2EEnv
from tests.fixtures.gateway_config import post_spawn


def _timeline(gateway_url: str, agent_id: int) -> list[dict[str, Any]]:
    return httpx.get(
        f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
    ).json()["items"]


def _wait_kind(gateway_url: str, agent_id: int, kind: str, timeout: float = 60.0) -> None:
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
    return page.locator(f'button:has-text("#{agent_id}")').first


@pytest.mark.scenario("tests.e2e.fakes.scenarios.parked_compact:build")
def test_switch_back_after_parked_compact_shows_post_compact_state(
    e2e_env: E2EEnv, database: Database
) -> None:
    page = e2e_env.page
    agent_a = e2e_env.agent_id
    gateway_url = e2e_env.gateway_url

    resp = post_spawn(gateway_url, {"spawner": "user"}, timeout=30.0)
    resp.raise_for_status()
    agent_b = int(resp.json()["id"])
    try:
        _wait_idle_intent(agent_b)

        pin_expand_runs_all(gateway_url)
        pin_compact_history_off(gateway_url)

        page.goto(e2e_env.agent_url)
        page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

        for reply in (REPLY_1, REPLY_2, REPLY_3):
            page.fill('[data-testid="composer-input"]', "\u4f60\u597d")
            page.click('[data-testid="composer-send"]')
            page.wait_for_selector(f"text={reply}", timeout=30_000)
            wait_for_status(agent_a, AgentStatus.IDLING.value)

        page.wait_for_selector(f'button:has-text("#{agent_b}")', timeout=15_000)
        _sidebar_row(page, agent_b).click()
        page.wait_for_function(f"location.href.includes('agent_id={agent_b}')", timeout=15_000)

        enqueue_compact_history_fixture(agent_a, database=database)
        _wait_kind(gateway_url, agent_a, "inbound_compact_request")
        wait_for_status(agent_a, AgentStatus.IDLING.value)

        switch_back_reads: list[str] = []
        page.on(
            "request",
            lambda req: (
                switch_back_reads.append(req.url)
                if (
                    f"/api/agents/{agent_a}/timeline" in req.url
                    or f"/api/agents/{agent_a}/conversation-snapshot" in req.url
                )
                else None
            ),
        )
        page.evaluate(
            """(agentId) => {
                window.__refreshReads = [];
                const orig = window.fetch;
                window.fetch = (input, init) => {
                    const url = String(input);
                    if (
                        url.includes('/api/agents/' + agentId + '/conversation-snapshot') ||
                        url.includes('/api/agents/' + agentId + '/timeline')
                    ) {
                        window.__refreshReads.push(url);
                    }
                    return orig(input, init);
                };
            }""",
            str(agent_a),
        )
        _sidebar_row(page, agent_a).click()
        page.wait_for_function(f"location.href.includes('agent_id={agent_a}')", timeout=15_000)

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if page.evaluate("() => window.__refreshReads.length > 0") or switch_back_reads:
                break
            time.sleep(0.2)
        assert page.evaluate("() => window.__refreshReads.length > 0") or switch_back_reads, (
            "switch-back after a parked compact fired no conversation refresh read "
            "(neither /timeline nor /conversation-snapshot; witnessed in-page and as a request event)"
        )

        page.wait_for_selector("text=Compact request", timeout=15_000)
        page.wait_for_selector(f"text={POST_COMPACT_NARRATION}", timeout=15_000)
        expect(page.get_by_text(REPLY_2)).to_have_count(0, timeout=15_000)
        expect(page.get_by_text(REPLY_3)).to_have_count(0, timeout=15_000)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{gateway_url}/api/agents/{agent_b}/terminate", timeout=5.0)
