"""Guarded explicit-row resolution uses the existing transactional notice owner."""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.config import settings
from gateway.agents import notices
from gateway.app import app
from gateway.tests.events.test_notices_endpoint import _insert_notice, _seed_agent
from gateway.tests.receipts.test_current_notice_receipts import HEADERS
from gateway.tests.receipts.test_current_notice_receipts import client as client


def _path(agent: int, notice: int) -> str:
    return f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": "intent"},
        {"Idempotency-Scope": "principal-v1"},
        {**HEADERS, "Idempotency-Key": ""},
        {**HEADERS, "Idempotency-Key": "x" * 129},
        {**HEADERS, "Idempotency-Scope": "wrong"},
    ],
)
def test_required_identity_rejects_before_effects(
    client: TestClient, db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "FYI")
    result = client.post(
        _path(agent, notice), json={"action": "read", "reply": "yes"}, headers=headers
    )
    assert result.status_code == 422
    assert db_conn.execute(
        "SELECT resolved_at FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone() == (None,)
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


def test_concurrent_read_reply_replays_one_inbound(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "FYI")
    path = _path(agent, notice)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(client.post, path, json={"action": "read", "reply": "yes"}, headers=HEADERS)
            for _ in range(4)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert all(result.status_code == 201 for result in results), [r.text for r in results]
    assert len({r.json()["inbound_id"] for r in results}) == 1
    assert (
        client.post(path, json={"action": "read", "reply": "changed"}, headers=HEADERS).status_code
        == 409
    )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)


def test_lost_tail_replays_after_notice_deletion_without_touching_later_row(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A", require_response=True)
    path = _path(agent, notice)
    body = {"action": "answer", "reply": "yes"}

    def unavailable(*args: object) -> bool:
        raise RuntimeError("response lost after acceptance")

    with monkeypatch.context() as lost:
        lost.setattr(notices, "publish_inbound_wake", unavailable)
        assert client.post(path, json=body, headers=HEADERS).status_code == 500
    original = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone()
    assert original is not None
    later = _insert_notice(db_conn, agent, "B", require_response=True)
    db_conn.execute("DELETE FROM agent_notices WHERE id=%s", (notice,))
    db_conn.commit()
    replay = client.post(path, json=body, headers=HEADERS)
    assert replay.status_code == 201 and replay.json()["inbound_id"] == original[0]
    assert db_conn.execute(
        "SELECT resolved_at FROM agent_notices WHERE id=%s", (later,)
    ).fetchone() == (None,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)


@pytest.mark.parametrize("remove", [False, True])
def test_consumed_original_reply_does_not_resurrect(
    client: TestClient, db_conn: psycopg.Connection, remove: bool
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    path = _path(agent, notice)
    body = {"action": "read", "reply": "yes"}
    first = client.post(path, json=body, headers=HEADERS)
    assert first.status_code == 201
    inbound_id = first.json()["inbound_id"]
    if remove:
        db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (inbound_id,))
    else:
        db_conn.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (inbound_id,))
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s", (agent,)
    )
    db_conn.commit()
    replay = client.post(path, json=body, headers=HEADERS)
    assert replay.status_code == 201 and replay.json()["inbound_id"] == inbound_id
    assert replay.json()["status"] == "terminated"
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "terminated",
    )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0 if remove else 1,)


def test_authentication_and_principal_are_rechecked_on_replay(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "FYI")
    path = _path(agent, notice)
    body = {"action": "read"}
    assert client.post(path, json=body, headers=HEADERS).status_code == 201
    assert (
        client.post(
            path, json=body, headers={**HEADERS, "Authorization": "Bearer revoked"}
        ).status_code
        == 401
    )
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", False)
    assert client.post(path, json=body, headers=HEADERS).status_code == 422


def test_guarded_and_legacy_paths_do_not_share_receipts(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "FYI")
    body = {"action": "read", "reply": "yes"}
    guarded = client.post(_path(agent, notice), json=body, headers=HEADERS)
    legacy = client.post(
        f"/api/agents/{agent}/notices/{notice}/resolve", json=body, headers=HEADERS
    )
    assert guarded.status_code == legacy.status_code == 201
    assert guarded.json()["inbound_id"] != legacy.json()["inbound_id"]


def test_old_routing_cannot_execute_guarded_request(db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "FYI")
    legacy = FastAPI()
    legacy.include_router(notices.router)
    with TestClient(legacy) as http:
        result = http.post(_path(agent, notice), json={"action": "read"}, headers=HEADERS)
    assert result.status_code in (404, 405)
    assert db_conn.execute(
        "SELECT resolved_at FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone() == (None,)


def test_openapi_requires_both_identity_headers() -> None:
    route = app.openapi()["paths"]["/api/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve"][
        "post"
    ]
    headers = {
        param["name"]: param["required"] for param in route["parameters"] if param["in"] == "header"
    }
    assert headers["Idempotency-Key"] and headers["Idempotency-Scope"]
