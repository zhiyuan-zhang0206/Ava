"""Gateway force-expire request contract and in-place lease closure."""

from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app
from shared import impersonation
from shared.caller_identity import CallerIdentity
from shared.db import create_agent
from shared.machine import machine_name
from tests.impersonation_support import recorded_tree


def test_force_expire_endpoint_requires_observed_session_and_returns_distinct_status(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), uuid4(), uuid4()),
    )
    db_conn.commit()
    lease = impersonation.request(
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        reason="Work",
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    path = f"/api/agents/{agent_id}/impersonation/force-expire"
    with TestClient(app) as client:
        assert client.post(path, json={}).status_code == 422
        assert (
            client.post(
                "/api/agents/999999/impersonation/force-expire",
                json={"session_id": lease["session_id"]},
            ).status_code
            == 404
        )
        wrong = client.post(path, json={"session_id": lease["session_id"] + 1})
        assert wrong.status_code == 200
        assert wrong.json() == {"session_id": lease["session_id"] + 1, "status": "not_open"}
        first = client.post(path, json={"session_id": lease["session_id"]})
        assert first.status_code == 200
        assert first.json() == {"session_id": lease["session_id"], "status": "expired"}
        again = client.post(path, json={"session_id": lease["session_id"]})
        assert again.json() == {"session_id": lease["session_id"], "status": "not_open"}
    entry = db_conn.execute(
        "SELECT payload->>'source' FROM agent_impersonation_entries "
        "WHERE lease_id=%s AND kind='lifecycle' ORDER BY seq DESC LIMIT 1",
        (lease["id"],),
    ).fetchone()
    assert entry == ("local_operator",)
