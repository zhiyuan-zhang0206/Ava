"""AtLeastOnceWithKey dedup middleware (R3 door ① server side).

The endpoint POST /api/agents/{id}/messages declares
Idempotency.AT_LEAST_ONCE_WITH_KEY: the caller gives one logical message a
stable key. The inbound INSERT owns that key in its transaction, and a same-key
retry resolves the durable row instead of inserting again — even when the
gateway died after COMMIT and before returning its response.

Tests run against the real app + test DB (TestClient + lifespan), plus
direct unit coverage of the middleware's DB helpers for the failure paths
(non-2xx release, replay shape).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi import Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from gateway import _idempotency
from gateway.app import app
from ops.rpc_schemas import ContentBlock
from shared.agents import AgentStatus
from shared.contracts import Idempotency


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def agent_id(db_conn: psycopg.Connection) -> int:
    """A minimal agents row (agents + agents_meta) for message delivery."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('idem-test') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        aid = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, status) VALUES (%s, 'running')",
            (aid,),
        )
    db_conn.commit()
    return int(aid)


def _count_inbounds(conn: psycopg.Connection, agent_id: int, content: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND content = %s",
            (agent_id, content),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def test_authenticated_admin_legacy_retry_and_scoped_reconcile(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.config import settings

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
    from shared.cluster_auth import cookie_name
    from shared.config import settings

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
    from shared.config import settings

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


# ── DB-helper unit coverage (failure paths without HTTP) ───────────────


def test_non_2xx_releases_the_key(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """A non-2xx outcome deletes the row, so a retry executes afresh."""
    # Simulate an owner that failed: claim the key, store nothing, release.
    pool = app.state.db_pool
    assert _idempotency._claim(pool, "key-fail", "POST", f"/api/agents/{agent_id}/messages")
    assert (
        _idempotency._fetch(pool, "key-fail", "POST", f"/api/agents/{agent_id}/messages") is None
    )  # still executing
    _idempotency._release(pool, "key-fail")
    assert _idempotency._claim(pool, "key-fail", "POST", f"/api/agents/{agent_id}/messages"), (
        "released key must be claimable again"
    )
    # Clear the placeholder again — the claim above is only the unit
    # assertion; a real request must find the key free and execute.
    _idempotency._release(pool, "key-fail")
    # And a real request with that key now executes (201 + row).
    resp = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "after release", "source": "user"},
        headers={"Idempotency-Key": "key-fail"},
    )
    assert resp.status_code == 201, resp.text
    assert _count_inbounds(db_conn, agent_id, "after release") == 1


def test_store_and_replay_roundtrip(client: TestClient, agent_id: int) -> None:
    """_store + _fetch round-trip: a completed row replays as a response."""
    pool = app.state.db_pool
    key = "key-roundtrip"
    assert _idempotency._claim(pool, key, "POST", f"/api/agents/{agent_id}/messages")
    _idempotency._store(pool, key, 201, {"detail": "stored"}, {"content-type": "application/json"})
    done = _idempotency._fetch(pool, key, "POST", f"/api/agents/{agent_id}/messages")
    assert done is not None
    status, body, _headers = done
    assert status == 201
    assert body == {"detail": "stored"}
    resp = _idempotency._replay(done)
    assert resp.status_code == 201
    assert json.loads(bytes(resp.body)) == {"detail": "stored"}


# ── audit round-2 regressions: key scoping, dead-owner recovery ─────────


def _age_placeholder(conn: psycopg.Connection, key: str, days: int) -> None:
    """Backdate a placeholder row's created_at so it reads as dead."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_idempotency SET created_at = now() - make_interval(days => %s) "
            "WHERE key = %s",
            (days, key),
        )
    conn.commit()


def test_stale_placeholder_never_bricks_key(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """An owner that died mid-execution (placeholder older than the retention
    window) must not keep the key bricked: the next claim steals the dead
    placeholder and the request executes afresh."""
    pool = app.state.db_pool
    key = "key-stale"
    path = f"/api/agents/{agent_id}/messages"
    assert _idempotency._claim(pool, key, "POST", path)
    _age_placeholder(db_conn, key, _idempotency._RETENTION_DAYS + 1)
    # A retry re-claims the dead owner's placeholder...
    assert _idempotency._claim(pool, key, "POST", path), (
        "a placeholder past the retention window must be stealable"
    )
    _idempotency._release(pool, key)  # clear the unit claim; a real request owns it
    # ...and the HTTP request executes instead of polling into a 503 timeout.
    resp = client.post(
        path,
        json={"content": "after crash", "source": "user"},
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 201, resp.text
    assert _count_inbounds(db_conn, agent_id, "after crash") == 1


def test_fresh_response_cache_placeholder_cannot_hide_committed_inbound(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash after inbound commit but before response-cache store reconciles immediately.

    This is the production ambiguity window: the durable message exists while
    the old generic idempotency cache still contains a fresh executing
    placeholder. The retry must reach the inbound transaction instead of
    polling that placeholder for 15 seconds (and potentially for seven days).
    """
    key = "key-commit-before-response"
    path = f"/api/agents/{agent_id}/messages"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages "
            "(agent_id, content, kind, source, client_message_id) "
            "VALUES (%s, %s, 'chat', 'user', %s) RETURNING id",
            (agent_id, "already committed", key),
        )
        committed = cur.fetchone()
    db_conn.commit()
    assert committed is not None
    assert _idempotency._claim(app.state.db_pool, key, "POST", path)
    # Keep a regression from taking the middleware's full 15-second wait.
    monkeypatch.setattr(_idempotency, "_MAX_WAIT_SECONDS", 0.01)

    response = client.post(
        path,
        json={"content": "already committed", "source": "user"},
        headers={"Idempotency-Key": key},
    )

    assert response.status_code == 201, response.text
    assert response.json()["inbound_id"] == committed[0]
    assert _count_inbounds(db_conn, agent_id, "already committed") == 1


def test_reconcile_endpoint_finds_the_durable_inbound(client: TestClient, agent_id: int) -> None:
    """A browser whose POST timed out can resolve the unknown outcome by key."""
    sent = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "reconcile me", "source": "user"},
        headers={"Idempotency-Key": "key-reconcile"},
    )
    assert sent.status_code == 201, sent.text

    receipt = client.post(
        f"/api/agents/{agent_id}/messages/reconcile",
        json={"content": "reconcile me", "source": "user"},
        headers={"Idempotency-Key": "key-reconcile"},
    )

    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["inbound_id"] == sent.json()["inbound_id"]


def test_reconcile_does_not_repeat_mutable_multimodal_validation(
    client: TestClient,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt survives model/upload changes after the original commit."""
    from gateway.routers import agents_state

    body = {
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": f"/api/agents/{agent_id}/uploads/gone.png"},
            }
        ],
        "source": "user",
    }
    original_normalize = agents_state._normalize_message_content

    def _normalize_without_mutable_gates(
        _request: Request,
        _agent_id: int,
        content: str | list[ContentBlock],
    ) -> tuple[str, dict[str, object] | None]:
        return original_normalize(content)

    monkeypatch.setattr(
        agents_state,
        "_prepare_message_content",
        _normalize_without_mutable_gates,
    )
    sent = client.post(
        f"/api/agents/{agent_id}/messages",
        json=body,
        headers={"Idempotency-Key": "key-multimodal-reconcile"},
    )
    assert sent.status_code == 201, sent.text

    def _mutable_gate_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("same-key receipt lookup re-ran current model/upload validation")

    monkeypatch.setattr(agents_state, "_prepare_message_content", _mutable_gate_must_not_run)
    retried = client.post(
        f"/api/agents/{agent_id}/messages",
        json=body,
        headers={"Idempotency-Key": "key-multimodal-reconcile"},
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["inbound_id"] == sent.json()["inbound_id"]

    receipt = client.post(
        f"/api/agents/{agent_id}/messages/reconcile",
        json=body,
        headers={"Idempotency-Key": "key-multimodal-reconcile"},
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["inbound_id"] == sent.json()["inbound_id"]


def test_reconcile_heals_crash_after_commit_before_resurrect(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost first response heals one pending chat and its terminated owner."""
    import gateway.routers._delivery as delivery

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
    db_conn.commit()
    calls = 0

    async def _crash_then_heal(
        aid: int,
        *,
        trigger_inbound_id: int,
        trigger_inbound_kind: str,
    ) -> AgentStatus:
        nonlocal calls
        calls += 1
        assert aid == agent_id
        assert trigger_inbound_kind == "chat"
        if calls == 1:
            raise RuntimeError("gateway died after commit")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'idling' WHERE id = %s AND status = 'terminated'",
                (aid,),
            )
            if cur.rowcount == 1:
                cur.execute(
                    "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                    "VALUES (%s, '', 'resurrect', 'system')",
                    (aid,),
                )
        db_conn.commit()
        return AgentStatus.IDLING

    monkeypatch.setattr(delivery._ops, "resurrect_if_terminated", _crash_then_heal)
    with pytest.raises(RuntimeError, match="gateway died after commit"):
        client.post(
            f"/api/agents/{agent_id}/messages",
            json={"content": "survive crash", "source": "user"},
            headers={"Idempotency-Key": "key-crash-heal"},
        )

    receipt = client.post(
        f"/api/agents/{agent_id}/messages/reconcile",
        json={"content": "survive crash", "source": "user"},
        headers={"Idempotency-Key": "key-crash-heal"},
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["status"] == "idling"
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, count(*) FROM inbound_messages WHERE agent_id = %s "
            "GROUP BY kind ORDER BY kind",
            (agent_id,),
        )
        assert cur.fetchall() == [("chat", 1), ("resurrect", 1)]


def test_prune_removes_stale_placeholder(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """The opportunistic prune covers status-NULL rows too — their
    completed_at is NULL, so a completed-only predicate would never delete
    them (the 7-day retention promise must hold for dead owners as well)."""
    pool = app.state.db_pool
    key = "key-prune"
    path = f"/api/agents/{agent_id}/messages"
    assert _idempotency._claim(pool, key, "POST", path)
    _age_placeholder(db_conn, key, _idempotency._RETENTION_DAYS + 1)
    # Any claim triggers the prune sweep.
    assert _idempotency._claim(pool, "key-prune-other", "POST", path)
    assert _idempotency._fetch(pool, key, "POST", path) is None, (
        "a stale placeholder must be pruned like a completed row"
    )
    assert _idempotency._claim(pool, key, "POST", path), "a pruned key must be claimable again"
    _idempotency._release(pool, key)


def test_key_scoped_to_method_path(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """The same key on a different route is a different idempotency unit: no
    replay across endpoints, and a cross-route row never answers this route's
    poll (the two callers cannot replay each other's responses)."""
    pool = app.state.db_pool
    key = "key-cross"
    path = f"/api/agents/{agent_id}/messages"
    other = f"/api/agents/{agent_id}/notices"
    assert _idempotency._claim(pool, key, "POST", path)
    assert not _idempotency._claim(pool, key, "POST", path), (
        "a live placeholder on the same route is owned — poll, don't steal"
    )
    assert _idempotency._claim(pool, key, "POST", other), (
        "a live placeholder on ANOTHER route is not this request's business"
    )
    _idempotency._store(pool, key, 201, {"ok": True}, {"content-type": "application/json"})
    done = _idempotency._fetch(pool, key, "POST", other)
    assert done is not None and done[1] == {"ok": True}
    assert _idempotency._fetch(pool, key, "POST", path) is None, (
        "a completed row must only replay on its own route"
    )
    _idempotency._release(pool, key)


def test_completed_row_not_stolen(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """A completed same-route row must replay, not be re-claimed by a retry."""
    pool = app.state.db_pool
    key = "key-done"
    path = f"/api/agents/{agent_id}/messages"
    assert _idempotency._claim(pool, key, "POST", path)
    _idempotency._store(pool, key, 201, {"ok": True}, {"content-type": "application/json"})
    assert not _idempotency._claim(pool, key, "POST", path), (
        "a completed same-route row is a replay, not a new claim"
    )
    _idempotency._release(pool, key)


class _FakeAlwkContract:
    """A minimal RouteContract stand-in declaring AT_LEAST_ONCE_WITH_KEY."""

    idempotency = Idempotency.AT_LEAST_ONCE_WITH_KEY
    transactional_idempotency = False


class _FakeTransactionalAlwkContract:
    """A keyed effect owned by the handler's business transaction."""

    idempotency = Idempotency.AT_LEAST_ONCE_WITH_KEY
    transactional_idempotency = True


def _fake_transactional_contract_for(_method: str, _path: str) -> _FakeTransactionalAlwkContract:
    return _FakeTransactionalAlwkContract()


def _fake_contract_for(_method: str, _path: str) -> _FakeAlwkContract:
    """contract_for stand-in: every route declares ALWK (the tests drive the
    middleware directly, bypassing the real contract table)."""
    return _FakeAlwkContract()


def _run_middleware_once(
    key: str, path: str, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Drive idempotency_middleware for one request on the real app/DB."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [(b"idempotency-key", key.encode())],
        "scheme": "http",
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }

    async def _run() -> Response:
        return await _idempotency.idempotency_middleware(Request(scope), call_next)

    return asyncio.run(_run())


def test_in_flight_key_timeout_uses_typed_retriable_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live owner timeout remains retriable without exposing a detail-only body."""
    monkeypatch.setattr(_idempotency.contracts, "contract_for", _fake_contract_for)
    monkeypatch.setattr(app.state, "db_pool", object(), raising=False)

    def claim_false(*_args: object) -> bool:
        return False

    def fetch_none(*_args: object) -> None:
        return None

    monkeypatch.setattr(_idempotency, "_claim", claim_false)
    monkeypatch.setattr(_idempotency, "_fetch", fetch_none)
    monotonic_values = iter((0.0, _idempotency._MAX_WAIT_SECONDS))
    monkeypatch.setattr(_idempotency, "_monotonic", lambda: next(monotonic_values))

    async def call_next(_request: Request) -> Response:
        raise AssertionError("an in-flight request must not execute again")

    response = _run_middleware_once("key-in-flight", "/api/test-keyed", call_next)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    body = json.loads(bytes(response.body))
    assert body["code"] == "idempotency_in_flight"
    assert body["retryable"] is True


def test_follower_polling_uses_capped_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy same-key follower backs off instead of issuing 10 polls/second."""
    claims = iter((False, False, False, True))
    sleeps: list[float] = []

    def claim(*_args: object) -> bool:
        return next(claims)

    def fetch(*_args: object) -> None:
        return None

    monkeypatch.setattr(_idempotency.contracts, "contract_for", _fake_contract_for)
    monkeypatch.setattr(app.state, "db_pool", object(), raising=False)
    monkeypatch.setattr(_idempotency, "_claim", claim)
    monkeypatch.setattr(_idempotency, "_fetch", fetch)
    monkeypatch.setattr(_idempotency, "_monotonic", lambda: 0.0)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def drain(*_args: object) -> tuple[bytes, str]:
        return b"{}", "application/json"

    async def call_next(_request: Request) -> Response:
        return Response(content=b"{}", media_type="application/json")

    monkeypatch.setattr(_idempotency.asyncio, "sleep", sleep)
    monkeypatch.setattr(_idempotency, "_drain_and_store", drain)
    _run_middleware_once("key-backoff", "/api/test-keyed", call_next)

    assert sleeps == [0.1, 0.1 * 1.75]


def test_owner_drain_failure_releases_key(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body-drain failure inside the owner's response path releases the row
    (audit P1-3): the key is claimable again instead of bricking for the
    retention window. Before the fix the drain happened outside any
    try/except and left a permanent placeholder."""
    pool = app.state.db_pool
    key = "key-drain"
    path = f"/api/agents/{agent_id}/messages"
    monkeypatch.setattr(_idempotency.contracts, "contract_for", _fake_contract_for)

    async def _boom() -> AsyncGenerator[bytes, None]:
        yield b"partial"
        raise RuntimeError("upstream died mid-body")

    async def call_next(_request: Request) -> Response:
        return StreamingResponse(_boom())

    with pytest.raises(RuntimeError, match="upstream died mid-body"):
        _run_middleware_once(key, path, call_next)
    assert _idempotency._fetch(pool, key, "POST", path) is None, (
        "a drained-body failure must release the row"
    )
    assert _idempotency._claim(pool, key, "POST", path), "the key must be claimable again"
    _idempotency._release(pool, key)


def test_only_transactional_route_bypasses_generic_response_cache(
    client: TestClient,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transactional handlers execute; ordinary ALWK handlers claim/replay."""
    key = "key-strategy-boundary"
    path = f"/api/agents/{agent_id}/messages"
    executions = 0

    async def call_next(_request: Request) -> Response:
        nonlocal executions
        executions += 1

        async def _body() -> AsyncGenerator[bytes, None]:
            yield b'{"ok": true}'

        return StreamingResponse(_body(), media_type="application/json")

    monkeypatch.setattr(
        _idempotency.contracts,
        "contract_for",
        _fake_transactional_contract_for,
    )
    _run_middleware_once(key, path, call_next)
    _run_middleware_once(key, path, call_next)
    assert executions == 2
    assert _idempotency._fetch(app.state.db_pool, key, "POST", path) is None

    monkeypatch.setattr(_idempotency.contracts, "contract_for", _fake_contract_for)
    _run_middleware_once(key, path, call_next)
    _run_middleware_once(key, path, call_next)
    assert executions == 3, "ordinary ALWK second call must replay without executing"
    _idempotency._release(app.state.db_pool, key)


def test_owner_non_streaming_response_releases_key(
    client: TestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TypeError branch (a non-streaming response on an ALWK route — the
    contract lying) also releases the row: same bricking risk as the drain
    failure, same containment."""
    pool = app.state.db_pool
    key = "key-typeerror"
    path = f"/api/agents/{agent_id}/messages"
    monkeypatch.setattr(_idempotency.contracts, "contract_for", _fake_contract_for)

    async def call_next(_request: Request) -> Response:
        return Response(status_code=200, content=b"{}")

    with pytest.raises(TypeError, match="non-streaming"):
        _run_middleware_once(key, path, call_next)
    assert _idempotency._fetch(pool, key, "POST", path) is None, (
        "a non-streaming-response TypeError must release the row"
    )
    assert _idempotency._claim(pool, key, "POST", path), "the key must be claimable again"
    _idempotency._release(pool, key)
