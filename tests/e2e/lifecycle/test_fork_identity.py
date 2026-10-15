from __future__ import annotations

import contextlib
import time

import httpx
import psycopg
import pytest

from base.api_contracts.agent_contract import AgentStatus
from base.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e.fakes.scenarios.fork_identity import FORK_OK, FORK_PROMPT
from tests.e2e.fixture_environment import E2EEnv
from tests.fixtures.gateway_config import post_spawn


@pytest.mark.scenario("tests.e2e.fakes.scenarios.fork_identity:build")
def test_fork_delivers_identity_marker_and_prompt(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    source_id = e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    page.fill('[data-testid="composer-input"]', "\u6e90 agent \u8dd1\u4e00\u8f6e")
    page.click('[data-testid="composer-send"]')
    wait_for_status(source_id, AgentStatus.IDLING.value)

    resp = post_spawn(
        e2e_env.gateway_url,
        {
            "spawner": "user",
            "fork_from": source_id,
            "prompt": FORK_PROMPT,
            "prompt_source": "user",
        },
        timeout=90.0,
    )
    resp.raise_for_status()
    forked_id = int(resp.json()["id"])
    assert forked_id != source_id

    try:
        wait_for_status(forked_id, AgentStatus.IDLING.value)

        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind FROM inbound_messages WHERE agent_id = %s ORDER BY id", (forked_id,)
            )
            kinds = [r[0] for r in cur.fetchall()]
        assert kinds == ["fork", "chat"], kinds

        deadline = time.monotonic() + 90.0
        while True:
            items = httpx.get(
                f"{e2e_env.gateway_url}/api/agents/{forked_id}/timeline?limit=1000", timeout=90.0
            ).json()["items"]
            replies = [it["payload"] for it in items if it["kind"] == "agent_chat"]
            if FORK_OK in replies or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        assert any(it["source"] == "lifecycle_fork" for it in items), (
            f"fork identity marker missing from forked timeline: {items}"
        )
        assert any(it["kind"] == "inbound_chat" and FORK_PROMPT in it["payload"] for it in items), (
            f"prompt missing from forked timeline: {items}"
        )
        assert FORK_OK in replies, (
            f"forked agent did not confirm marker+prompt in its first-turn context; "
            f"replies={replies}"
        )
    finally:
        with contextlib.suppress(Exception):
            httpx.post(f"{e2e_env.gateway_url}/api/agents/{forked_id}/terminate", timeout=5.0)
            wait_for_status(forked_id, AgentStatus.TERMINATED.value, timeout=10.0)
