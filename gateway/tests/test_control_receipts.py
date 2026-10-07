"""HTTP control retries replay committed acceptance without issuing another command."""

from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from base.agents.messages.control_delivery import accept_control
from base.agents.messages.inbound import InboundKind
from gateway.agents import lifecycle
from gateway.app import app
from gateway.auth.request_principal import AuthPrincipal, principal_key
from gateway.tests.test_compact_endpoint import _seed_agent
from ops import lifecycle as ops_lifecycle


@pytest.mark.parametrize("command", ["cancel", "compact"])
def test_lost_response_replays_original_identity(db_conn: psycopg.Connection, command: str) -> None:
    agent = _seed_agent(db_conn)
    path = "/api/cancel" if command == "cancel" else f"/api/agents/{agent}/compact"
    body = {"agent_id": agent} if command == "cancel" else None
    with TestClient(app) as client:
        # Ignore the first response, as a client disconnected after commit would.
        client.post(path, json=body, headers={"Idempotency-Key": "lost-response"})
        row = db_conn.execute(
            "SELECT id FROM inbound_messages WHERE agent_id=%s", (agent,)
        ).fetchone()
        assert row is not None
        replay = client.post(path, json=body, headers={"Idempotency-Key": "lost-response"})
        later = client.post(path, json=body, headers={"Idempotency-Key": "new-intent"})
    assert replay.status_code == later.status_code == 200
    assert replay.json()["inbound_id"] == row[0]
    assert later.json()["inbound_id"] != row[0]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (2,)


def test_same_cancel_scope_changed_body_conflicts(db_conn: psycopg.Connection) -> None:
    one, two = _seed_agent(db_conn), _seed_agent(db_conn)
    with TestClient(app) as client:
        first = client.post(
            "/api/cancel", json={"agent_id": one}, headers={"Idempotency-Key": "same"}
        )
        changed = client.post(
            "/api/cancel", json={"agent_id": two}, headers={"Idempotency-Key": "same"}
        )
    assert first.status_code == 200 and changed.status_code == 409
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (two,)
    ).fetchone() == (0,)


@pytest.mark.parametrize("remove", [False, True])
def test_original_compact_no_longer_pending_cannot_resurrect(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    remove: bool,
) -> None:
    agent = _seed_agent(db_conn)
    path = f"/api/agents/{agent}/compact"
    with TestClient(app) as client:
        first = client.post(path, headers={"Idempotency-Key": "once"})
        iid = first.json()["inbound_id"]
        if remove:
            db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (iid,))
        else:
            db_conn.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (iid,))
        db_conn.execute(
            "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
            (agent,),
        )
        db_conn.commit()

        async def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("consumed original compact cannot trigger resurrection")

        def no_wake(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("consumed original compact cannot be woken")

        monkeypatch.setattr(ops_lifecycle, "resurrect_if_terminated", forbidden)
        monkeypatch.setattr(lifecycle, "publish_inbound_wake", no_wake)
        replay = client.post(path, headers={"Idempotency-Key": "once"})
    assert replay.status_code == 200 and replay.json() == first.json()
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "terminated",
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"Idempotency-Key": ""},
        {"Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "one", "Idempotency-Scope": "unknown"},
    ],
)
def test_invalid_operation_identity_does_not_commit(
    db_conn: psycopg.Connection,
    headers: dict[str, str],
) -> None:
    agent = _seed_agent(db_conn)
    with TestClient(app) as client:
        response = client.post("/api/cancel", json={"agent_id": agent}, headers=headers)
    assert response.status_code == 422
    assert db_conn.execute("SELECT count(*) FROM agent_control_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


def test_principal_and_actual_path_scopes_are_independent(db_conn: psycopg.Connection) -> None:
    one, two = _seed_agent(db_conn), _seed_agent(db_conn)
    paths = [f"/api/agents/{agent}/compact" for agent in (one, two)]
    with TestClient(app):
        pool = cast(ConnectionPool, app.state.db_pool)
        for principal in (AuthPrincipal("mcp_client", "one"), AuthPrincipal("mcp_client", "two")):
            for agent, path in zip((one, two), paths, strict=True):
                key = principal_key(principal, "POST", path, "same-raw-key")
                receipt = accept_control(
                    pool, agent, InboundKind.COMPACT_REQUEST, path=path, key=key
                )
                assert receipt.inserted
                assert (
                    accept_control(
                        pool, agent, InboundKind.COMPACT_REQUEST, path=path, key=key
                    ).inbound_id
                    == receipt.inbound_id
                )
    assert db_conn.execute("SELECT count(*) FROM agent_control_receipts").fetchone() == (4,)


def test_compact_tail_failure_replays_committed_acceptance(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _seed_agent(db_conn)
    path = f"/api/agents/{agent}/compact"
    with TestClient(app, raise_server_exceptions=False) as client:
        with monkeypatch.context() as failed_tail:

            def unavailable(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("connection lost after acceptance")

            failed_tail.setattr(lifecycle, "publish_inbound_wake", unavailable)
            assert client.post(path, headers={"Idempotency-Key": "lost-tail"}).status_code == 500
        original = db_conn.execute(
            "SELECT inbound_id FROM agent_control_receipts WHERE agent_id=%s", (agent,)
        ).fetchone()
        assert original is not None
        response = client.post(path, headers={"Idempotency-Key": "lost-tail"})
    assert response.status_code == 200 and response.json()["inbound_id"] == original[0]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)
