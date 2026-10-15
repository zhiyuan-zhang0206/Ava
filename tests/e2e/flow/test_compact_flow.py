from __future__ import annotations

import re
from typing import Any

import httpx
import psycopg
import pytest
from playwright.sync_api import ConsoleMessage, Page

from base.api_contracts.agent_contract import AgentStatus
from base.config import settings
from base.db import Database
from base.sessions.tests.poll_until import poll_until
from tests.e2e._db import enqueue_compact_history_fixture, wait_for_status
from tests.e2e._settings import pin_expand_runs_all
from tests.e2e.fakes.scenarios.compact_flow import (
    FIRST_REPLY,
    POST_COMPACT_NARRATION,
    POST_COMPACT_REPLY,
    SUMMARY_TEXT,
)
from tests.e2e.fixture_environment import E2EEnv

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


def _collect_unrecognized_console_warnings(page: Page) -> list[str]:
    warnings: list[str] = []

    def collect_if_unrecognized(message: ConsoleMessage) -> None:
        if "unrecognized system_marker" in message.text.lower():
            warnings.append(message.text)

    page.on("console", collect_if_unrecognized)
    return warnings


def _assert_compact_request_finalized(agent_id: int) -> None:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        rows = cur.fetchall()
    assert rows[-1] == ("compact_request", "done"), rows


def _assert_no_unrecognized_alarm_after_compact(
    page: Page, unrecognized_warnings: list[str]
) -> None:
    assert page.get_by_test_id("marker-unrecognized").count() == 0, (
        "marker-unrecognized alarm rendered"
    )
    assert page.get_by_text(_UNRECOGNIZED_RE).count() == 0, (
        f"unrecognized system_marker alarm rendered after compact — {unrecognized_warnings}"
    )
    page.evaluate("() => 1")
    assert unrecognized_warnings == [], (
        f"[timeline] unrecognized console warnings fired after compact: {unrecognized_warnings}"
    )


@pytest.mark.scenario("tests.e2e.fakes.scenarios.compact_flow:build")
def test_force_compact_renders_envelope_without_unrecognized_marker(
    e2e_env: E2EEnv, database: Database
) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    unrecognized_warnings = _collect_unrecognized_console_warnings(page)

    pin_expand_runs_all(e2e_env.gateway_url)

    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    page.fill('[data-testid="composer-input"]', "\u4f60\u597d")
    page.click('[data-testid="composer-send"]')
    page.wait_for_selector(f"text={FIRST_REPLY}", timeout=30_000)
    wait_for_status(agent_id, AgentStatus.IDLING.value)

    enqueue_compact_history_fixture(agent_id, database=database)

    items = _wait_kinds(e2e_env.gateway_url, agent_id, ["inbound_compact_request"])
    envelope = next(it for it in items if it["kind"] == "inbound_compact_request")
    assert envelope["kind"] == "inbound_compact_request", envelope
    assert SUMMARY_TEXT in envelope["payload"], envelope
    assert "Compact request" in envelope["payload"], envelope

    _assert_compact_request_finalized(agent_id)

    page.wait_for_selector("text=Compact request", timeout=15_000)
    _assert_no_unrecognized_alarm_after_compact(page, unrecognized_warnings)

    page.wait_for_selector(f"text={POST_COMPACT_NARRATION}", timeout=15_000)

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
