from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page

from base.api_contracts.agent_contract import AgentStatus
from tests.e2e._db import wait_for_status
from tests.e2e.fakes.scenarios.error_recovery import ERROR_MSG, RECOVERY_REPLY
from tests.e2e.fixture_environment import E2EEnv

_UNRECOGNIZED_RE = re.compile(
    "Unrecognized system_marker|\u65e0\u6cd5\u8bc6\u522b\u7684 system_marker"
)


def _poll_timeline_items(
    gateway_url: str,
    agent_id: int,
    is_ready: Callable[[list[dict[str, Any]]], bool],
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        items = httpx.get(
            f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
        ).json()["items"]
        if is_ready(items) or time.monotonic() > deadline:
            return items
        time.sleep(0.5)


def _turn_kinds(items: list[dict[str, Any]]) -> list[str]:
    return [it["kind"] for it in items if it["kind"] not in ("system_marker", "system_prompt")]


def _assert_error_marker_not_unrecognized_alarm(page: Page) -> None:
    assert page.get_by_test_id("marker-error").count() > 0, "error marker must render"
    assert page.get_by_test_id("marker-unrecognized").count() == 0, (
        "LLM error rendered as unrecognized-marker alarm instead of the error marker"
    )
    assert page.get_by_text(_UNRECOGNIZED_RE).count() == 0, (
        "LLM error rendered as unrecognized-marker alarm instead of the error marker"
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.error_recovery:build")
def test_llm_error_renders_error_marker_and_agent_recovers(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id

    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    page.fill('[data-testid="composer-input"]', "\u7b2c\u4e00\u8f6e\u4f1a\u5931\u8d25")
    page.click('[data-testid="composer-send"]')
    wait_for_status(agent_id, AgentStatus.IDLING.value)
    page.wait_for_selector(f"text={ERROR_MSG}", timeout=15_000)
    page.wait_for_selector("text=The agent is blocked", timeout=15_000)
    page.wait_for_selector("text=heartbeat check-ins will not re-run this request", timeout=15_000)

    _assert_error_marker_not_unrecognized_alarm(page)

    items = _poll_timeline_items(
        e2e_env.gateway_url, agent_id, lambda items: _turn_kinds(items) == ["inbound_chat"]
    )
    kinds = _turn_kinds(items)
    assert kinds == ["inbound_chat"], f"aborted turn must not commit an agent_chat: {kinds}"

    page.fill('[data-testid="composer-input"]', "\u518d\u6765\u4e00\u6b21")
    page.click('[data-testid="composer-send"]')
    page.wait_for_selector(f"text={RECOVERY_REPLY}", timeout=30_000)

    items = _poll_timeline_items(
        e2e_env.gateway_url,
        agent_id,
        lambda items: any(it["kind"] == "agent_chat" for it in items),
    )
    replies = [it["payload"] for it in items if it["kind"] == "agent_chat"]
    assert RECOVERY_REPLY in replies, f"recovery reply missing: {replies}"
    assert page.get_by_text(_UNRECOGNIZED_RE).count() == 0
