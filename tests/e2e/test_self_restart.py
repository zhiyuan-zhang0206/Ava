"""Real UI self-restart preserves agent identity and admits a fresh runtime."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest

from shared.config import settings
from tests.e2e._env import E2EEnv
from tests.shared.poll_until import poll_until


def _evidence(agent_id: int) -> tuple[tuple[Any, ...] | None, list[tuple[Any, ...]]]:
    with psycopg.connect(settings.data_plane.db_url) as conn:
        row = conn.execute(
            "SELECT status,runtime_generation,lifecycle_command_id FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
        commands = conn.execute(
            "SELECT target_generation,status,applied_at IS NOT NULL,observed_at IS NOT NULL "
            "FROM inbound_messages WHERE agent_id=%s AND kind='restart' AND source='self'",
            (agent_id,),
        ).fetchall()
    return row, commands


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_restart:build")
def test_self_restart_admits_successor_and_answers_on_same_agent(e2e_env: E2EEnv) -> None:
    page, agent_id = e2e_env.page, e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)
    page.fill('[data-testid="composer-input"]', "\u91cd\u542f")
    page.click('[data-testid="composer-send"]')
    original_generation: UUID | None = None

    def restart_applied() -> tuple[bool, object]:
        nonlocal original_generation
        row, commands = _evidence(agent_id)
        reached = bool(row and row[0] == "idling" and len(commands) == 1 and commands[0][2])
        if reached:
            original_generation = commands[0][0]
        return reached, {"agent": row, "commands": commands}

    poll_until(
        restart_applied,
        timeout=90,
        interval=0.2,
        what=f"agent {agent_id} applies its self restart",
    )
    assert original_generation is not None

    # A distinct subsequent UI request proves the successor actually executes.
    page.fill('[data-testid="composer-input"]', "continue after verified restart")
    page.click('[data-testid="composer-send"]')

    def successor_processed_follow_up() -> tuple[bool, object]:
        row, commands = _evidence(agent_id)
        with psycopg.connect(settings.data_plane.db_url) as conn:
            chats = conn.execute(
                "SELECT status,claimed_at IS NOT NULL FROM inbound_messages "
                "WHERE agent_id=%s AND kind='chat' AND content='continue after verified restart'",
                (agent_id,),
            ).fetchall()
        if (
            row
            and row[0] == "idling"
            and row[1] is not None
            and row[1] != original_generation
            and row[2] is None
            and commands == [(original_generation, "done", True, True)]
            and chats in ([("claimed", True)], [("done", True)])
        ):
            response = httpx.get(
                f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000",
                timeout=30,
            )
            response.raise_for_status()
            items = response.json()["items"]
            if any(
                item["kind"] == "agent_chat"
                and "Follow-up processed by successor." in item["payload"]
                for item in items
            ):
                assert any(
                    item["kind"] == "system_marker" and "Restart was accepted" in item["payload"]
                    for item in items
                )
                return True, {"agent": row, "commands": commands, "chats": chats}
        return False, {"agent": row, "commands": commands, "chats": chats}

    poll_until(
        successor_processed_follow_up,
        timeout=90,
        interval=0.2,
        what=f"successor generation answers agent {agent_id}'s follow-up",
    )
