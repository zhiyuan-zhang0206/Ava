"""Panoramic Case 3 — LLM error path + recovery (#1018).

The scripted fake raises FatalProviderError on the first LLM call — a class
excluded from the llm node's retry policy (agent/graph/_build.py
`_should_retry`). The agent loop aborts the turn, emits one SSE `error` event
and halts idling; the frontend renders it as the ephemeral `[error] ...`
marker. The next user message gets a normal reply — recovery without a
restart.

Asserts the fail-loud contract in the real browser:
- the error is VISIBLE (error marker, not silent swallow)
- it is the *error* path, NOT the unrecognized-marker red alarm — the two
  must never be confused (#1017 class)
- the agent recovers: next message → reply rendered, timeline has agent_chat
"""

from __future__ import annotations

import re
import time

import httpx
import pytest

from shared.agents import AgentStatus
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.e2e.fakes.scenarios.error_recovery import ERROR_MSG, RECOVERY_REPLY

_UNRECOGNIZED_RE = re.compile(
    "Unrecognized system_marker|\u65e0\u6cd5\u8bc6\u522b\u7684 system_marker"
)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.error_recovery:build")
def test_llm_error_renders_error_marker_and_agent_recovers(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id

    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    # ── turn 1: LLM rejects permanently → SSE error event → [error] marker ──
    page.fill('[data-testid="composer-input"]', "\u7b2c\u4e00\u8f6e\u4f1a\u5931\u8d25")
    page.click('[data-testid="composer-send"]')
    wait_for_status(agent_id, AgentStatus.IDLING.value)
    # The error marker body carries the blocked copy + the exception message.
    page.wait_for_selector(f"text={ERROR_MSG}", timeout=15_000)
    page.wait_for_selector("text=The agent is blocked", timeout=15_000)
    page.wait_for_selector("text=heartbeat check-ins will not re-run this request", timeout=15_000)

    # Error is VISIBLE — and it is the error path, not the unrecognized alarm.
    assert page.get_by_test_id("marker-error").count() > 0, "error marker must render"
    assert page.get_by_test_id("marker-unrecognized").count() == 0, (
        "LLM error rendered as unrecognized-marker alarm instead of the error marker"
    )
    assert page.get_by_text(_UNRECOGNIZED_RE).count() == 0, (
        "LLM error rendered as unrecognized-marker alarm instead of the error marker"
    )

    # REST: the aborted turn left NO agent_chat (no final message committed) —
    # poll, the inbound commit can lag the SSE error by a beat.
    deadline = time.monotonic() + 60.0
    while True:
        items = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
        ).json()["items"]
        kinds = [it["kind"] for it in items if it["kind"] not in ("system_marker", "system_prompt")]
        if kinds == ["inbound_chat"] or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    assert kinds == ["inbound_chat"], f"aborted turn must not commit an agent_chat: {kinds}"

    # ── turn 2: next message → normal reply (recovery without restart) ──
    page.fill('[data-testid="composer-input"]', "\u518d\u6765\u4e00\u6b21")
    page.click('[data-testid="composer-send"]')
    page.wait_for_selector(f"text={RECOVERY_REPLY}", timeout=30_000)

    deadline = time.monotonic() + 60.0
    while True:
        items = httpx.get(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
        ).json()["items"]
        if any(it["kind"] == "agent_chat" for it in items) or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    replies = [it["payload"] for it in items if it["kind"] == "agent_chat"]
    assert RECOVERY_REPLY in replies, f"recovery reply missing: {replies}"
    assert page.get_by_text(_UNRECOGNIZED_RE).count() == 0
