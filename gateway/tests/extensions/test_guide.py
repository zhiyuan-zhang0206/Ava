"""POST /api/keyed/v1/guide/draft — the Meiva Guide entry.

Spawns an meiva-guide agent for a natural-language ops request. The spawn is
stubbed so the test asserts only the wiring: the fixed prompt points at the ROOT
`meiva.skills.meiva_guide` skill, carries the user's request, labels the agent
`meiva-guide`, and the spawned id is returned as agent_id.
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from gateway.routers import guide
from gateway.tests.lifecycle.creation.test_guarded_creation import HEADERS
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client


def test_draft_spawns_meiva_guide_agent(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        return SpawnedAgent(id=777)

    monkeypatch.setattr(guide, "create_and_launch_agent", _fake_create_launch)
    r = client.post(
        "/api/keyed/v1/guide/draft",
        json={"nl": "install the linear MCP server"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == 777
    assert calls["label"] == "meiva-guide"
    prompt = str(calls["prompt"])
    assert "install the linear MCP server" in prompt
    assert "meiva.skills.meiva_guide" in prompt
