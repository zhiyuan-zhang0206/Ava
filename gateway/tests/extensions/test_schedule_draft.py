"""POST /api/keyed/v1/schedules/draft — the schedule-writer entry."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from gateway.app import app
from gateway.schedules import router
from gateway.tests.lifecycle.creation.test_guarded_creation import HEADERS
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client


def test_draft_spawns_writer_agent(
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
        assert catalog is app.state.catalog
        assert authority is app.state.config_authority
        calls["label"] = body.label
        calls["prompt"] = body.prompt
        calls["target"] = target
        return SpawnedAgent(id=4242)

    monkeypatch.setattr(router, "create_and_launch_agent", _fake_create_launch)
    r = client.post(
        "/api/keyed/v1/schedules/draft",
        json={"nl": "consolidate memory nightly"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["agent_id"] == 4242
    assert calls["label"] == "ava-schedule-writer"
    prompt = str(calls["prompt"])
    assert "consolidate memory nightly" in prompt
    assert "ava.skills.ava_guide.schedules" in prompt
