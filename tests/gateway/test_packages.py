"""POST /api/packages/draft — the skill / plugin / MCP install entry.

Spawns an ava-package-installer agent for a natural-language "I want a
capability like X" request. The spawn is stubbed so the tests assert only the
wiring: the prompt points at `ava.skills.ava_package_installer`, carries the
user's request verbatim and which kind the entry point was for, labels the agent
`ava-package-installer`, and the spawned id comes back as agent_id. There is no
URL/spec field by design — every install goes through the agent.
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app


@pytest.fixture
def spawn_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub `create_and_launch_agent`, capturing what the router asked for."""
    from ops.rpc_schemas import SpawnAgentRequest, SpawnedAgent

    calls: dict[str, object] = {}

    async def _fake_create_launch(
        body: SpawnAgentRequest, target: str, pool: object
    ) -> SpawnedAgent:
        calls["label"] = body.label
        calls["prompt"] = body.prompt
        calls["target"] = target
        calls["spawner"] = body.spawner
        return SpawnedAgent(id=901)

    monkeypatch.setattr("gateway.routers.packages.create_and_launch_agent", _fake_create_launch)
    return calls


@pytest.mark.parametrize(
    ("kind", "brief_marker"),
    [
        ("skill", "instruction pack"),
        ("plugin", "RUN CODE"),
        ("mcp", "RUNS AS A PROCESS"),
    ],
)
def test_draft_spawns_installer_for_each_kind(
    db_conn: psycopg.Connection,
    spawn_calls: dict[str, object],
    kind: str,
    brief_marker: str,
) -> None:
    """Each kind spawns the same installer, framed with that kind's trust cost —
    the framing is what the skill turns into the confirm gate."""
    with TestClient(app) as client:
        r = client.post(
            "/api/packages/draft",
            json={"kind": kind, "nl": "something that reads my Obsidian vault"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == 901
    assert spawn_calls["label"] == "ava-package-installer"
    prompt = str(spawn_calls["prompt"])
    assert "ava.skills.ava_package_installer" in prompt
    assert "something that reads my Obsidian vault" in prompt
    assert f"Kind: {kind}" in prompt
    assert brief_marker in prompt


def test_draft_prompt_orders_the_whole_lifecycle(
    db_conn: psycopg.Connection, spawn_calls: dict[str, object]
) -> None:
    """Installing is the middle of the job — the prompt must also order the
    verify / judge / report steps, not just 'install it'."""
    with TestClient(app) as client:
        r = client.post("/api/packages/draft", json={"kind": "mcp", "nl": "linear issues"})
    assert r.status_code == 200, r.text
    prompt = str(spawn_calls["prompt"])
    for phrase in ("confirm with the user", "spawn a test agent", "adaptation"):
        assert phrase in prompt, f"missing lifecycle step: {phrase}"


def test_draft_rejects_an_unknown_kind(
    db_conn: psycopg.Connection, spawn_calls: dict[str, object]
) -> None:
    """`kind` is a closed enum — an unknown value is a 422, never a spawn."""
    with TestClient(app) as client:
        r = client.post("/api/packages/draft", json={"kind": "npm", "nl": "left-pad"})
    assert r.status_code == 422, r.text
    assert spawn_calls == {}
