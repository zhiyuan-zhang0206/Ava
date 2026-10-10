"""Guarded compact raw input, principal scope and retained immutable receipt boundaries."""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit


@pytest.mark.parametrize(
    "invalid", ["protocol_bool", "extra", "source_id_bool", "model_null", "segment_negative"]
)
async def test_strict_target_rejects_before_creating_another_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    body = dict(accepted.target)
    if invalid == "protocol_bool":
        body["protocol"] = True
    elif invalid == "extra":
        body["unknown"] = "invalid"
    elif invalid == "source_id_bool":
        body["source"] = {**body["source"], "agent_id": True}
    elif invalid == "model_null":
        body["model"] = None
    else:
        body["segment_version"] = -1
    headers = {**accepted.headers, "Idempotency-Key": str(uuid4())}
    response = client.post(accepted.path + "/compact-history", json=body, headers=headers)
    assert response.status_code == 422, response.text
    assert db_conn.execute(
        "SELECT count(*) FROM native_compact_commands WHERE agent_id=%s", (accepted.agent,)
    ).fetchone() == (1,)


async def test_verified_principal_scope_key_and_changed_original_body(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    for omitted in ("Idempotency-Key", "Idempotency-Scope"):
        headers = {k: v for k, v in accepted.headers.items() if k != omitted}
        assert (
            client.post(
                accepted.path + "/compact-history", json=accepted.target, headers=headers
            ).status_code
            == 400
        )
    assert client.post(accepted.path + "/compact-history", json=accepted.target).status_code == 401
    changed = {**accepted.target, "model": "claude-sonnet-5"}
    conflict = client.post(
        accepted.path + "/compact-history", json=changed, headers=accepted.headers
    )
    assert conflict.status_code == 409 and "different source" in conflict.text
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated',runtime_owner=%s WHERE id=%s",
        (uuid4(), accepted.agent),
    )
    db_conn.execute("DELETE FROM native_compact_observations WHERE agent_id=%s", (accepted.agent,))
    db_conn.commit()
    replay = client.post(
        accepted.path + "/compact-history", json=accepted.target, headers=accepted.headers
    )
    assert replay.status_code == 202 and replay.json() == accepted.acceptance
    assert (
        client.get(accepted.path + "/compact-target", headers=accepted.headers).status_code == 409
    )


@pytest.mark.parametrize("agent_id", [0, -1, 2**63])
async def test_path_bounds_before_domain_access(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: int,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    path = f"/api/keyed/v1/agents/{agent_id}"
    assert client.get(path + "/compact-target", headers=accepted.headers).status_code == 422
    assert (
        client.post(
            path + "/compact-history", json=accepted.target, headers=accepted.headers
        ).status_code
        == 422
    )
    assert (
        client.get(
            path + "/compact-commands/" + accepted.acceptance["command_id"],
            headers=accepted.headers,
        ).status_code
        == 422
    )
