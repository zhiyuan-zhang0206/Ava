"""Gateway requires actual restart domain acceptance, never an old runner hint."""

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from base.agents.incarnation.native_restart_models import NativeRestartOperation
from base.lm.plugin_providers import build_model_catalog
from gateway.tests.test_idempotency import client as client
from gateway.tests.test_native_cancel import _headers
from ops.lifecycle.native_restart import restart_native_work_op
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work


async def test_committed_acceptance_response_loss_replays_before_routing(
    client: TestClient,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.agents import lifecycle
    from gateway.app import app

    _inc, target = await managed_work(db_conn, aops_pool)
    headers = _headers(monkeypatch)
    body = {
        "target": target.model_dump(mode="json"),
        "config_overlay": {"completion_notice_policy": "hourly"},
    }
    url = f"/api/keyed/v1/agents/{target.agent_id}/restart-work"
    calls = 0
    original: Any = None

    async def forward(agent_id: int, path: str, packet: Any, *, idempotency_key: str) -> Any:
        nonlocal calls, original
        calls += 1
        assert path == f"/api/agents/{agent_id}/restart-work-v1"
        operation = NativeRestartOperation.model_validate(packet)
        assert idempotency_key == operation.operation_key
        original = await restart_native_work_op(
            app.state.db,
            app.state.bus,
            agent_id,
            operation,
            app.state.db_pool,
            catalog=build_model_catalog(),
        )
        if original.status == "refused":
            return original.model_dump(mode="json")
        from fastapi import HTTPException

        raise HTTPException(status_code=502, detail="test lost accepted response")

    monkeypatch.setattr(lifecycle, "forward_to_home_machine", forward)
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 502
    assert original is not None
    db_conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=NULL, native_work_id=NULL,config_overlay='{}' WHERE id=%s",
        (target.agent_id,),
    )
    db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (original.acceptance.command_id,))
    db_conn.commit()
    replay = client.post(url, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == original.acceptance.model_dump(
        mode="json"
    )
    assert calls == 1
    changed = body | {"config_overlay": {"completion_notice_policy": "never"}}
    assert client.post(url, json=changed, headers=headers).status_code == 409
    assert (
        client.post(
            url, json=body, headers=headers | {"Idempotency-Key": "new-operation"}
        ).status_code
        == 409
    )
    assert calls == 2  # fresh key is a new refused invocation, never old receipt recovery


@pytest.mark.parametrize(
    "wire", [{"status": "restarting"}, {"status": "accepted", "acceptance": {}}, {}]
)
async def test_unknown_executor_wire_fails_closed_without_legacy_fallback(
    client: TestClient,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    wire: dict[str, object],
) -> None:
    from gateway.agents import lifecycle

    _inc, target = await managed_work(db_conn, aops_pool)
    calls: list[str] = []

    async def unsupported(_agent: int, path: str, _body: Any, **_kwargs: Any) -> Any:
        calls.append(path)
        return wire

    monkeypatch.setattr(lifecycle, "forward_to_home_machine", unsupported)
    response = client.post(
        f"/api/keyed/v1/agents/{target.agent_id}/restart-work",
        json={"target": target.model_dump(mode="json")},
        headers=_headers(monkeypatch),
    )
    assert response.status_code == 502
    assert calls == [f"/api/agents/{target.agent_id}/restart-work-v1"]
    assert db_conn.execute("SELECT count(*) FROM native_restart_commands").fetchone() == (0,)
