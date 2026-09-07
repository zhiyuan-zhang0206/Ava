"""Real hosted self-termination and automatic/explicit resurrection on the same ID."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest

from ops.cluster_rpc import dispatch_to_machine
from shared.config import settings
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.shared.poll_until import poll_until


@pytest.mark.scenario("tests.e2e.fakes.scenarios.lifecycle_resurrect:build")
@pytest.mark.parametrize("explicit", [False, True])
def test_resurrect_brings_back_terminated_agent(
    e2e_env: E2EEnv, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    page, agent_id = e2e_env.page, e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)
    page.fill('[data-testid="composer-input"]', "\u518d\u89c1")
    page.click('[data-testid="composer-send"]')
    wait_for_status(agent_id, "terminated")
    with psycopg.connect(settings.data_plane.db_url) as conn:
        original = conn.execute(
            "SELECT runtime_generation,machine FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
        command = conn.execute(
            "SELECT status,applied_at IS NOT NULL,observed_at IS NOT NULL "
            "FROM inbound_messages WHERE agent_id=%s AND kind='terminate'",
            (agent_id,),
        ).fetchall()
    assert original is not None and original[0] is not None
    assert command == [("done", True, True)]
    if explicit:
        # Keep Playwright's event loop separate from the async ops RPC.
        monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-cluster-secret")
        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(
                asyncio.run,
                dispatch_to_machine(
                    target_machine=original[1],
                    kind="lifecycle",
                    payload={
                        "path": f"/api/agents/{agent_id}/resurrect-explicit-v2",
                        "body": {"resurrected_by": "user", "prompt": "wake up"},
                    },
                ),
            ).result(timeout=120)
        assert response["status"] == "spawned"
    else:
        response = httpx.post(
            f"{e2e_env.gateway_url}/api/agents/{agent_id}/messages",
            json={"content": "wake up", "source": "user"},
            timeout=90,
        )
        response.raise_for_status()

    def successor_processed_wake() -> tuple[bool, object]:
        with psycopg.connect(settings.data_plane.db_url) as conn:
            row = conn.execute(
                "SELECT status,runtime_generation,lifecycle_command_id FROM agents_meta WHERE id=%s",
                (agent_id,),
            ).fetchone()
            chats = conn.execute(
                "SELECT status,claimed_at IS NOT NULL FROM inbound_messages WHERE agent_id=%s "
                "AND kind='chat' AND content='wake up'",
                (agent_id,),
            ).fetchall()
            resurrect = conn.execute(
                "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect'",
                (agent_id,),
            ).fetchone()
        if (
            row
            and row[0] == "idling"
            and row[1] is not None
            and row[1] != original[0]
            and row[2] is None
            and resurrect == (1,)
            and chats in ([("claimed", True)], [("done", True)])
        ):
            response = httpx.get(
                f"{e2e_env.gateway_url}/api/agents/{agent_id}/timeline?limit=1000",
                timeout=30,
            )
            response.raise_for_status()
            if any(
                item["kind"] == "agent_chat"
                and "I processed the wake after resurrection." in item["payload"]
                for item in response.json()["items"]
            ):
                return True, {"agent": row, "chats": chats, "resurrect": resurrect}
        return False, {"agent": row, "chats": chats, "resurrect": resurrect}

    poll_until(
        successor_processed_wake,
        timeout=90,
        interval=0.2,
        what=f"agent {agent_id} resumes after {'explicit' if explicit else 'automatic'} resurrection",
    )
