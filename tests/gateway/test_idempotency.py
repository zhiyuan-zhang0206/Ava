"""AtLeastOnceWithKey dedup middleware (R3 door ① server side).

The endpoint POST /api/agents/{id}/messages declares
Idempotency.AT_LEAST_ONCE_WITH_KEY: the first request with an
Idempotency-Key header executes and stores its response in the shared
api_idempotency table; a same-key retry replays it instead of re-executing
— one logical message lands exactly once no matter how many retries.

Tests run against the real app + test DB (TestClient + lifespan), plus
direct unit coverage of the middleware's DB helpers for the failure paths
(non-2xx release, replay shape).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator

import psycopg
import pytest
from fastapi import Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from gateway import _idempotency
from gateway.app import app
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
    assert _count_inbounds(db_conn, agent_id, "hello once") == 1


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


def _fake_contract_for(_method: str, _path: str) -> _FakeAlwkContract:
    """contract_for stand-in: every route declares ALWK (the tests drive the
    middleware directly, bypassing the real contract table)."""
    return _FakeAlwkContract()


def _run_middleware_once(
    key: str, path: str, call_next: Callable[[Request], Awaitable[Response]]
) -> None:
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

    async def _run() -> None:
        await _idempotency.idempotency_middleware(Request(scope), call_next)

    asyncio.run(_run())


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
