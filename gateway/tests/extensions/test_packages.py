"""POST /api/keyed/v1/packages/draft — the skill / plugin / MCP install entry.

Spawns an meiva-package-installer agent for a natural-language "I want a
capability like X" request. The spawn is stubbed so the tests assert only the
wiring: the prompt points at `meiva.skills.meiva_guide.packages.install`, carries the
user's request verbatim and which kind the entry point was for, labels the agent
`meiva-package-installer`, and the spawned id comes back as agent_id. There is no
URL/spec field by design — every install goes through the agent.
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from gateway.extensions import packages
from gateway.tests.lifecycle.creation.test_guarded_creation import HEADERS
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client

DRAFT_PATH = "/api/keyed/v1/packages/draft"


@pytest.fixture
def spawn_calls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub `create_and_launch_agent`, capturing what the router asked for."""
    from ops.rpc_schemas import SpawnAgentRequest, SpawnedAgent

    calls: dict[str, object] = {}

    async def _fake_create_launch(
        body: SpawnAgentRequest,
        target: str,
        pool: object,
        db: object,
        bus: object,
        *,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        creation_key: str,
        creation_identity: dict[str, object],
    ) -> SpawnedAgent:
        assert catalog is client.app.state.catalog  # type: ignore[union-attr]
        assert authority is client.app.state.config_authority  # type: ignore[union-attr]
        calls["label"] = body.label
        calls["prompt"] = body.prompt
        calls["target"] = target
        calls["spawner"] = body.spawner
        return SpawnedAgent(id=901)

    monkeypatch.setattr(packages, "create_and_launch_agent", _fake_create_launch)
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
    client: TestClient,
    db_conn: psycopg.Connection,
    spawn_calls: dict[str, object],
    kind: str,
    brief_marker: str,
) -> None:
    """Each kind spawns the same installer, framed with that kind's trust cost —
    the framing is what the skill turns into the confirm gate."""
    r = client.post(
        DRAFT_PATH,
        json={"kind": kind, "nl": "something that reads my Obsidian vault"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == 901
    assert spawn_calls["label"] == "meiva-package-installer"
    prompt = str(spawn_calls["prompt"])
    assert "meiva.skills.meiva_guide.packages.install" in prompt
    assert "something that reads my Obsidian vault" in prompt
    assert f"Kind: {kind}" in prompt
    assert brief_marker in prompt


def test_draft_prompt_orders_the_whole_lifecycle(
    client: TestClient, db_conn: psycopg.Connection, spawn_calls: dict[str, object]
) -> None:
    """Installing is the middle of the job — the prompt must also order the
    verify / judge / report steps, not just 'install it'."""
    r = client.post(DRAFT_PATH, json={"kind": "mcp", "nl": "linear issues"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    prompt = str(spawn_calls["prompt"])
    for phrase in ("confirm with the user", "spawn a test agent", "adaptation"):
        assert phrase in prompt, f"missing lifecycle step: {phrase}"


def test_draft_rejects_an_unknown_kind(
    client: TestClient, db_conn: psycopg.Connection, spawn_calls: dict[str, object]
) -> None:
    """`kind` is a closed enum — an unknown value is a 422, never a spawn."""
    r = client.post(DRAFT_PATH, json={"kind": "npm", "nl": "left-pad"}, headers=HEADERS)
    assert r.status_code == 422, r.text
    assert spawn_calls == {}
