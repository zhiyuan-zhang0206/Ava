"""AtLeastOnceWithKey dedup middleware: HTTP-level retry/reconcile behavior against the real app + test DB; split from gateway/tests/test_idempotency.py (task #4922)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.tests.test_idempotency import _count_inbounds
from gateway.tests.test_idempotency import agent_id as agent_id
from gateway.tests.test_idempotency import client as client


def test_authenticated_admin_legacy_retry_and_scoped_reconcile(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.config import settings

    secret = "principal-scope-test-secret"  # noqa: S105 — isolated test credential
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    url = f"/api/agents/{agent_id}/messages"
    body = {"content": "admin principal retry", "source": "user"}
    legacy_headers = {"Authorization": f"Bearer {secret}", "Idempotency-Key": "legacy-flight"}
    first = client.post(url, json=body, headers=legacy_headers)
    repeated = client.post(url, json=body, headers=legacy_headers)
    assert first.status_code == repeated.status_code == 201
    assert first.json()["inbound_id"] == repeated.json()["inbound_id"]
    headers = legacy_headers | {
        "Idempotency-Scope": "principal-v1",
        "Idempotency-Key": "new-logical-message",
    }
    scoped = client.post(url, json=body, headers=headers)
    assert scoped.status_code == 201, scoped.text
    reconciled = client.post(f"{url}/reconcile", json=body, headers=headers)
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["inbound_id"] == scoped.json()["inbound_id"]
    conflict = client.post(url, json=body | {"content": "changed"}, headers=headers)
    assert conflict.status_code == 409
    assert _count_inbounds(db_conn, agent_id, "admin principal retry") == 2


def test_browser_rotation_and_bearer_share_admin_retry_namespace(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster.auth import cookie_name
    from base.config import settings

    secret = "principal-rotation-test-secret"  # noqa: S105 — isolated test credential
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(settings.gateway, "session_cookie_secure", False)
    url = f"/api/agents/{agent_id}/messages"
    body = {"content": "same administrator across sessions", "source": "user"}
    headers = {"Idempotency-Key": "session-rotation", "Idempotency-Scope": "principal-v1"}
    login = client.post("/api/auth/login", json={"password": secret})
    assert login.status_code == 200, login.text
    old_cookie = login.cookies[cookie_name()]
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 201, first.text
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    revoked = client.post(
        url, json=body, headers=headers | {"Cookie": f"{cookie_name()}={old_cookie}"}
    )
    assert revoked.status_code == 401
    rotated = client.post("/api/auth/login", json={"password": secret})
    assert rotated.status_code == 200, rotated.text
    assert rotated.cookies[cookie_name()] != old_cookie
    repeated = client.post(url, json=body, headers=headers)
    assert repeated.status_code == 201, repeated.text
    client.cookies.clear()
    bearer = client.post(url, json=body, headers=headers | {"Authorization": f"Bearer {secret}"})
    assert bearer.status_code == 201, bearer.text
    assert (
        first.json()["inbound_id"] == repeated.json()["inbound_id"] == bearer.json()["inbound_id"]
    )
    assert _count_inbounds(db_conn, agent_id, body["content"]) == 1


def test_no_auth_mode_cannot_claim_verified_principal_namespace(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", False)
    response = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "must not insert scoped", "source": "user"},
        headers={"Idempotency-Key": "no-auth-key", "Idempotency-Scope": "principal-v1"},
    )
    assert response.status_code == 422
    assert _count_inbounds(db_conn, agent_id, "must not insert scoped") == 0


def test_same_key_retry_lands_once(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """Two requests with the same key → 201 both times, one inbound row."""
    resp1 = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "hello once", "source": "user"},
        headers={"Idempotency-Key": "key-1"},
    )
    assert resp1.status_code == 201, resp1.text
    resp2 = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "hello once", "source": "user"},
        headers={"Idempotency-Key": "key-1"},
    )
    assert resp2.status_code == 201, resp2.text
    assert resp1.json()["inbound_id"] == resp2.json()["inbound_id"]
    assert _count_inbounds(db_conn, agent_id, "hello once") == 1


def test_same_key_different_body_fails_closed(client: TestClient, agent_id: int) -> None:
    """A key identifies one immutable logical message, not merely one slot."""
    first = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "first body", "source": "user"},
        headers={"Idempotency-Key": "key-body-conflict"},
    )
    second = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "different body", "source": "user"},
        headers={"Idempotency-Key": "key-body-conflict"},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text


def test_same_key_different_agent_fails_closed(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """Client message ids are cluster-wide; cross-agent reuse cannot twin a message."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('idem-other') RETURNING id")
        other_row = cur.fetchone()
        assert other_row is not None
        other_id = int(other_row[0])
        cur.execute(
            "INSERT INTO agents_meta (id, status) VALUES (%s, 'running')",
            (other_id,),
        )
    db_conn.commit()

    first = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "same body", "source": "user"},
        headers={"Idempotency-Key": "key-agent-conflict"},
    )
    second = client.post(
        f"/api/agents/{other_id}/messages",
        json={"content": "same body", "source": "user"},
        headers={"Idempotency-Key": "key-agent-conflict"},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text


def test_concurrent_same_key_requests_land_once(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """Two tabs racing the same logical submit converge on one durable inbound."""

    def _send() -> tuple[int, dict[str, object]]:
        response = client.post(
            f"/api/agents/{agent_id}/messages",
            json={"content": "from two tabs", "source": "user"},
            headers={"Idempotency-Key": "key-two-tabs"},
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_send) for _index in range(2)]
        responses = [future.result() for future in futures]

    assert [status for status, _body in responses] == [201, 201]
    inbound_ids: set[int] = set()
    for _status, body in responses:
        inbound_id = body["inbound_id"]
        assert isinstance(inbound_id, int)
        inbound_ids.add(inbound_id)
    assert len(inbound_ids) == 1
    assert _count_inbounds(db_conn, agent_id, "from two tabs") == 1


def test_different_keys_land_twice(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    resp1 = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "twice", "source": "user"},
        headers={"Idempotency-Key": "key-a"},
    )
    resp2 = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "twice", "source": "user"},
        headers={"Idempotency-Key": "key-b"},
    )
    assert resp1.status_code == 201 and resp2.status_code == 201
    assert _count_inbounds(db_conn, agent_id, "twice") == 2


def test_client_message_unique_index_allows_nulls_but_rejects_duplicate_keys(
    db_conn: psycopg.Connection, agent_id: int
) -> None:
    """The partial unique index preserves legacy key-less callers."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, 'null one', 'chat', 'user'), "
            "(%s, 'null two', 'chat', 'user')",
            (agent_id, agent_id),
        )
        cur.execute(
            "INSERT INTO inbound_messages "
            "(agent_id, content, kind, source, client_message_id) "
            "VALUES (%s, 'keyed', 'chat', 'user', 'db-unique-key')",
            (agent_id,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation), db_conn.transaction():
            cur.execute(
                "INSERT INTO inbound_messages "
                "(agent_id, content, kind, source, client_message_id) "
                "VALUES (%s, 'duplicate', 'chat', 'user', 'db-unique-key')",
                (agent_id,),
            )
    db_conn.commit()


def test_no_key_passes_through(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """A legacy key-less caller behaves exactly as before — no dedup."""
    resp = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "legacy", "source": "user"},
    )
    assert resp.status_code == 201, resp.text
    assert _count_inbounds(db_conn, agent_id, "legacy") == 1


def test_non_alwk_route_ignores_key(client: TestClient) -> None:
    """Routes not declaring AT_LEAST_ONCE_WITH_KEY ignore the header."""
    resp = client.get("/api/agents", headers={"Idempotency-Key": "ignored"})
    assert resp.status_code == 200, resp.text


def test_replay_preserves_status_and_body(client: TestClient, agent_id: int) -> None:
    """The replayed response carries the stored status + body."""
    resp1 = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "replay", "source": "user"},
        headers={"Idempotency-Key": "key-replay"},
    )
    body1 = resp1.json()
    resp2 = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "replay", "source": "user"},
        headers={"Idempotency-Key": "key-replay"},
    )
    assert resp2.status_code == resp1.status_code
    assert resp2.json() == body1
