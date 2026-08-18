"""POST /api/guide/draft — the Ava Guide entry.

Spawns an ava-guide agent for a natural-language ops request. The spawn is
stubbed so the test asserts only the wiring: the fixed prompt points at the ROOT
`ava.skills.ava_guide` skill, carries the user's request, labels the agent
`ava-guide`, and the spawned id is returned as agent_id.
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app


def test_draft_spawns_ava_guide_agent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ops.rpc_schemas import SpawnAgentRequest, SpawnedAgent

    calls: dict[str, object] = {}

    async def _fake_create_launch(
        body: SpawnAgentRequest, target: str, pool: object
    ) -> SpawnedAgent:
        calls["label"] = body.label
        calls["prompt"] = body.prompt
        calls["target"] = target
        return SpawnedAgent(id=777)

    monkeypatch.setattr("gateway.routers.guide.create_and_launch_agent", _fake_create_launch)
    with TestClient(app) as client:
        r = client.post("/api/guide/draft", json={"nl": "install the linear MCP server"})
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == 777
    assert calls["label"] == "ava-guide"
    prompt = str(calls["prompt"])
    assert "install the linear MCP server" in prompt
    assert "ava.skills.ava_guide" in prompt
