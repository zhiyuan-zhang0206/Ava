"""SDK requests recover one actual hosted-source compaction command."""

import json
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import _agent
from ava.agents import compaction
from ava.gateway_client import transport
from base.agents import GatewayUnavailable
from base.agents.incarnation.resources import ResourceBirth
from base.config import settings
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.helpers import make_host


async def test_lost_sdk_acceptance_replays_original_source_after_owner_change(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    agent = _agent(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s,config_overlay=%s WHERE id=%s",
        (
            Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")),
            Jsonb({"llm_model": "gpt-6.1-sol"}),
            agent,
        ),
    )
    db_conn.commit()
    ordinary: list[object] = []
    host, _saver, _config = await make_host(aops_pool, agent, 100, ordinary, monkeypatch)
    await host.run_turn(agent)
    assert len(ordinary) == 1
    secret = "sdk-compact-test-secret"  # noqa: S105 -- isolated fixture credential
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            result = client.get(request.url.path, headers=dict(request.headers))
        else:
            posts.append(request)
            result = client.post(
                request.url.path, json=json.loads(request.content), headers=dict(request.headers)
            )
            if len(posts) == 1:
                assert result.status_code == 202, result.text
                raise httpx.ReadTimeout("accepted response lost", request=request)
        return httpx.Response(
            result.status_code, content=result.content, headers=dict(result.headers)
        )

    with (
        httpx.Client(
            base_url="http://gateway",
            headers={"Authorization": f"Bearer {secret}"},
            transport=httpx.MockTransport(handle),
        ) as http,
        transport.use_client(http),
    ):
        target = compaction.observe(agent)
        with pytest.raises(GatewayUnavailable):
            compaction.submit(target, idempotency_key="intent")
        assert len(posts) == 1
        db_conn.execute(
            "UPDATE agents_meta SET status='terminated',runtime_owner=%s WHERE id=%s",
            (uuid4(), agent),
        )
        db_conn.execute("DELETE FROM native_compact_observations WHERE agent_id=%s", (agent,))
        db_conn.commit()
        accepted = compaction.submit(target, idempotency_key="intent")
        assert compaction.submit(target, idempotency_key="intent") == accepted
        current = compaction.status(accepted)
        assert current.acceptance == accepted
        assert (
            current.outcome == "accepted"
            and not current.result_available
            and not current.continuation_released
        )
        with pytest.raises(httpx.HTTPStatusError) as conflict:
            compaction.submit(
                target.model_copy(update={"observation_id": uuid4()}), idempotency_key="intent"
            )
        assert conflict.value.response.status_code == 409
        with pytest.raises(httpx.HTTPStatusError):
            compaction.observe(agent)
    assert db_conn.execute(
        "SELECT count(*) FROM native_compact_commands WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)
    assert (
        posts[0].url == posts[1].url
        and posts[0].headers == posts[1].headers
        and posts[0].content == posts[1].content
    )
    assert len(ordinary) == 1  # acceptance/status never invoke another native/model turn
