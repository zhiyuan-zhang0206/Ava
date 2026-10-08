"""ava.context cross-process e2e — an exec child holds the host's context.

One turn: the scripted model's execute_code reads `ava.context.identity` in the real exec child,
then uses that identity for an SQL read over `ava.context.sql` and gateway reads over
`ava.agents` and `ava.context.gateway`
(see fakes/scenarios/context_identity). The code_output the turn commits must show

- the agent id the host's context carried (the e2e agent's own id), owning its loop, no actor;
- the database row of that agent, read through the child's own connection;
- the agent's status and its gateway record (HTTP 200), read through the gateway the child's own
  client dialed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from base.agents import AgentStatus
from tests.components.base.poll_until import poll_until
from tests.e2e._db import wait_for_status
from tests.e2e._env import E2EEnv
from tests.e2e.fakes.scenarios.context_identity import REPLY_TEXT


def _wait_for_code_output(gateway_url: str, agent_id: int) -> str:
    output = ""

    def reply_reached_timeline() -> tuple[bool, object]:
        nonlocal output
        items: list[dict[str, Any]] = httpx.get(
            f"{gateway_url}/api/agents/{agent_id}/timeline?limit=1000", timeout=90.0
        ).json()["items"]
        output = "\n".join(str(it["payload"]) for it in items if it["kind"] == "code_output")
        reply_seen = any(it["kind"] == "agent_chat" and REPLY_TEXT in it["payload"] for it in items)
        return reply_seen, {"kinds": [it["kind"] for it in items], "code_output": output}

    poll_until(
        reply_reached_timeline,
        timeout=90.0,
        interval=0.5,
        what=f"context turn reaches agent {agent_id} timeline",
    )
    return output


@pytest.mark.scenario("tests.e2e.fakes.scenarios.context_identity:build")
def test_exec_child_holds_the_hosts_context(e2e_env: E2EEnv) -> None:
    page = e2e_env.page
    agent_id = e2e_env.agent_id
    page.goto(e2e_env.agent_url)
    page.wait_for_selector('[data-testid="sse-ready"]', state="attached", timeout=10_000)

    page.fill('[data-testid="composer-input"]', "who are you")
    page.click('[data-testid="composer-send"]')
    wait_for_status(agent_id, AgentStatus.IDLING.value)

    output = _wait_for_code_output(e2e_env.gateway_url, agent_id)
    assert (
        f"CTX agent_id={agent_id} owns_loop=True actor=None db_row={agent_id} status=" in output
    ), output
    assert "gateway=200" in output
