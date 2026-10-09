"""SDK restart preserves original durable runner acceptance and retained progress."""

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
from base.agents.incarnation.native_restart_models import NativeRestartOperation
from base.config import settings
from gateway.agents import lifecycle
from gateway.app import app
from gateway.tests.test_idempotency import client as client
from ops.lifecycle.native_restart import restart_native_work_op
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work


async def test_sdk_recovers_original_restart_after_lost_response_and_source_cleanup(
    client: TestClient,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _incarnation, target = await managed_work(db_conn, aops_pool)
    secret = "sdk-restart-test-secret"  # noqa: S105 -- isolated fixture credential
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    calls: list[str] = []

    async def forward(
        agent_id: int, path: str, packet: dict[str, object], *, idempotency_key: str
    ) -> dict[str, object]:
        calls.append(path)
        assert path == f"/api/agents/{agent_id}/restart-work-v1"
        operation = NativeRestartOperation.model_validate(packet)
        assert operation.operation_key == idempotency_key
        result = await restart_native_work_op(
            app.state.db, app.state.bus, agent_id, operation, app.state.db_pool
        )
        return result.model_dump(mode="json")

    monkeypatch.setattr(lifecycle, "forward_to_home_machine", forward)
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

    overlay: dict[str, object] = {"completion_notice_policy": "hourly"}
    with (
        httpx.Client(
            base_url="http://gateway",
            headers={"Authorization": f"Bearer {secret}"},
            transport=httpx.MockTransport(handle),
        ) as http,
        transport.use_client(http),
    ):
        observed = work.observe(target.agent_id)
        with pytest.raises(GatewayUnavailable):
            work.restart(observed, idempotency_key="intent", config_overlay=overlay)
        assert len(posts) == len(calls) == 1
        original = db_conn.execute(
            "SELECT command_id FROM native_restart_commands WHERE agent_id=%s", (target.agent_id,)
        ).fetchone()
        assert original is not None
        db_conn.execute(
            "UPDATE agents_meta SET lifecycle_command_id=NULL,native_work_id=NULL,runtime_owner=%s,config_overlay='{}' WHERE id=%s",
            (uuid4(), target.agent_id),
        )
        db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (original[0],))
        db_conn.commit()
        recovered = work.restart(observed, idempotency_key="intent", config_overlay=overlay)
        assert recovered.command_id == original[0] and recovered.target == observed
        progress = work.restart_status(recovered)
        assert (
            progress.acceptance == recovered
            and progress.applied_at is None
            and progress.observed_at is None
        )
        assert len(calls) == 1
        with pytest.raises(httpx.HTTPStatusError) as conflict:
            work.restart(
                observed,
                idempotency_key="intent",
                config_overlay={"completion_notice_policy": "never"},
            )
        assert conflict.value.response.status_code == 409 and len(calls) == 1
    assert db_conn.execute(
        "SELECT count(*) FROM native_restart_commands WHERE agent_id=%s", (target.agent_id,)
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT config_overlay FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone() == ({},)
