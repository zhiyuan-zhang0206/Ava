"""Idle restart applies without a model call and preserves the plugin checkpoint."""

from __future__ import annotations

import time
from typing import Any

import httpx
import psycopg
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres import PostgresSaver

from shared.agents import AgentStatus
from shared.config import settings
from tests.e2e._ports import GATEWAY_URL


def _checkpoint_values(agent_id: int) -> dict[str, Any]:
    """Read agent's latest checkpoint channel_values (returns {} if no checkpoint)."""
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        tup = saver.get_tuple({"configurable": {"thread_id": str(agent_id)}})
    if tup is None:
        return {}
    return tup.checkpoint.get("channel_values", {})  # pyright: ignore[reportUnknownMemberType]


def _ai_count(values: dict[str, Any]) -> int:
    return sum(1 for m in values.get("messages", []) if isinstance(m, AIMessage))


@pytest.mark.scenario("tests.e2e.fakes.scenarios.idle_restart_silent:build")
def test_external_restart_of_idle_agent_is_silent(spawned_agent: int) -> None:
    agent_id = spawned_agent

    # ── pre-restart turn: let agent write non-default cwd into plugin state then back to idle ──
    resp = httpx.post(
        f"{GATEWAY_URL}/api/agents/{agent_id}/messages",
        json={"content": "\u8bf7\u8bbe\u7f6e\u5de5\u4f5c\u76ee\u5f55", "source": "user"},
        timeout=10.0,
    )
    resp.raise_for_status()

    # turn complete = two AIMessages written to checkpoint and status back to idling
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        values = _checkpoint_values(agent_id)
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        if row and row[0] == AgentStatus.IDLING.value and _ai_count(values) == 2:
            break
        time.sleep(0.3)
    else:
        raise RuntimeError(f"agent {agent_id} did not complete pre-restart turn within 90s")

    assert str(values["ava_code__cwd"]).endswith("e2e-idle-restart-cwd"), (
        f"pre-restart cwd not written to plugin state: {values.get('ava_code__cwd')!r}"
    )

    # ── external restart (default source='user') ──
    httpx.post(f"{GATEWAY_URL}/api/agents/{agent_id}/restart", timeout=10.0).raise_for_status()

    # respawn complete + marker committed to checkpoint: completed row / idling / new pid / marker
    deadline = time.monotonic() + 90.0
    last: tuple = ()
    while time.monotonic() < deadline:
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT applied_at IS NOT NULL FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'restart' LIMIT 1",
                (agent_id,),
            )
            completed = cur.fetchone() == (True,)
            cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        values = _checkpoint_values(agent_id)
        marker_committed = any(
            "Restart was accepted from user" in str(m.content) for m in values.get("messages", [])
        )
        last = (completed, row, marker_committed)
        if (
            completed
            and row is not None
            and row[0] == AgentStatus.IDLING.value
            and marker_committed
        ):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(
            f"agent {agent_id} did not complete silent restart cycle within 90s: "
            f"(completed, (status, pid), marker_committed)={last!r}"
        )

    # ── Silent + liveness assertions ──
    # Zero LLM call: AIMessages still the pre-restart two (post-restart script is empty,
    # any wakeup would kill process — this count is belt-and-suspenders)
    assert _ai_count(values) == 2, f"extra LLM turn after respawn: {_ai_count(values)} AIMessages"
    # After marker, agent stopped waiting — halted=True still in checkpoint as is
    assert values["halted"] is True
    # plugin state survives respawn (empty state update no longer overwrites with default)
    assert str(values["ava_code__cwd"]).endswith("e2e-idle-restart-cwd"), (
        f"plugin state cwd did not survive respawn: {values.get('ava_code__cwd')!r}"
    )

    # Short observation window: no delayed wakeup (if any, AIMessage increases / empty script kills process)
    time.sleep(2.0)
    values = _checkpoint_values(agent_id)
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None and row[0] == AgentStatus.IDLING.value
    assert _ai_count(values) == 2
