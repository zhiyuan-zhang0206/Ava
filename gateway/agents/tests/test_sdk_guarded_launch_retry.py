"""A lost guarded SDK retry response recovers one real committed launch attempt."""

import json

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from ava import agents
from ava.gateway_client import transport
from base.agents import GatewayUnavailable
from gateway.agents.tests.test_keyed_launch_retry import birth
from gateway.agents.tests.test_keyed_launch_retry import client as client


def test_sdk_observation_lost_response_and_historical_replay(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent_id, prior = birth(client)
    posts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            result = client.get(request.url.path)
        else:
            posts.append(request)
            result = client.post(
                request.url.path, json=json.loads(request.content), headers=dict(request.headers)
            )
            if len(posts) == 1:
                assert result.status_code == 200, result.text
                raise httpx.ReadTimeout("committed response lost", request=request)
        return httpx.Response(
            result.status_code, content=result.content, headers=dict(result.headers)
        )

    with (
        httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(handle)) as http,
        transport.use_client(http),
    ):
        observed = agents.get_launch_attempt(agent_id)
        assert str(observed) == prior
        with pytest.raises(GatewayUnavailable):
            agents.retry_launch(
                agent_id,
                require_idempotency=True,
                idempotency_key="intent",
                expected_prior_attempt_id=observed,
            )
        assert len(posts) == 1
        replacement = agents.get_launch_attempt(agent_id)
        assert replacement != observed
        for _ in range(2):
            assert (
                agents.retry_launch(
                    agent_id,
                    require_idempotency=True,
                    idempotency_key="intent",
                    expected_prior_attempt_id=observed,
                )
                == agent_id
            )
            assert agents.get_launch_attempt(agent_id) == replacement
        with pytest.raises(httpx.HTTPStatusError) as conflict:
            agents.retry_launch(
                agent_id,
                require_idempotency=True,
                idempotency_key="intent",
                expected_prior_attempt_id=replacement,
            )
        assert conflict.value.response.status_code == 409
        db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent_id,))
        db_conn.commit()
        assert (
            agents.retry_launch(
                agent_id,
                require_idempotency=True,
                idempotency_key="intent",
                expected_prior_attempt_id=observed,
            )
            == agent_id
        )
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (1,)
