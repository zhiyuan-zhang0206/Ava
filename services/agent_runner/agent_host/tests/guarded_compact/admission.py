"""Actual first hosted work and verified guarded HTTP acceptance for fault tests."""

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import _agent
from base.agents.incarnation.resources import ResourceBirth
from base.config import settings
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.guarded_compact.helpers import make_host


@dataclass
class AcceptedHost:
    agent: int
    host: AgentHost
    saver: AsyncPostgresSaver
    config: RunnableConfig
    ordinary: list[object]
    path: str
    headers: dict[str, str]
    target: dict[str, Any]
    acceptance: dict[str, Any]

    def status(self, client: TestClient) -> dict[str, Any]:
        response = client.get(
            self.path + "/compact-commands/" + self.acceptance["command_id"], headers=self.headers
        )
        assert response.status_code == 200, response.text
        return response.json()


async def admit(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    interval: int = 100,
) -> AcceptedHost:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    agent = _agent(conn)
    conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s,config_overlay=%s WHERE id=%s",
        (
            Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")),
            Jsonb({"llm_model": "gpt-6.1-sol"}),
            agent,
        ),
    )
    conn.commit()
    ordinary: list[object] = []
    host, saver, config = await make_host(pool, agent, interval, ordinary, monkeypatch)
    await host.run_turn(agent)
    assert len(ordinary) == 1
    secret = "guarded-compact-test-secret"  # noqa: S105 -- isolated credential
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    headers = {
        "Authorization": f"Bearer {secret}",
        "Idempotency-Scope": "principal-v1",
        "Idempotency-Key": str(uuid4()),
    }
    path = f"/api/keyed/v1/agents/{agent}"
    observed = client.get(path + "/compact-target", headers=headers)
    assert observed.status_code == 200, observed.text
    accepted = client.post(path + "/compact-history", json=observed.json(), headers=headers)
    assert accepted.status_code == 202, accepted.text
    return AcceptedHost(
        agent, host, saver, config, ordinary, path, headers, observed.json(), accepted.json()
    )
