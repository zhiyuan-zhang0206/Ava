"""Guarded SDK cancel replays one actual native command after lost acceptance."""

import json
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from ava.agents import work
from ava.gateway_client import transport
from base.agents import GatewayUnavailable
from base.config import settings
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work


async def test_sdk_replays_original_command_after_lost_response_and_owner_change(
    client: TestClient,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _incarnation, target = await managed_work(db_conn, aops_pool)
    secret = "sdk-cancel-test-secret"  # noqa: S105 — isolated fixture credential
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
                assert result.status_code == 200, result.text
                raise httpx.ReadTimeout("committed response lost", request=request)
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
        observed = work.observe(target.agent_id)
        assert observed == target
        with pytest.raises(GatewayUnavailable):
            work.cancel(observed, idempotency_key="intent")
        assert len(posts) == 1
        db_conn.execute(
            "UPDATE agents_meta SET native_work_id=NULL,runtime_owner=%s WHERE id=%s",
            (uuid4(), target.agent_id),
        )
        db_conn.execute("DELETE FROM native_graph_work WHERE id=%s", (target.work_id,))
        db_conn.commit()
        recovered = work.cancel(observed, idempotency_key="intent")
        assert recovered.target == observed
        assert work.cancel(observed, idempotency_key="intent") == recovered
        with pytest.raises(httpx.HTTPStatusError) as conflict:
            work.cancel(observed.model_copy(update={"work_id": uuid4()}), idempotency_key="intent")
        assert conflict.value.response.status_code == 409
    assert db_conn.execute(
        "SELECT count(*) FROM native_cancel_commands WHERE agent_id=%s", (target.agent_id,)
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (target.agent_id,)
    ).fetchone() == (0,)
