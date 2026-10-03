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
import itertools
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from typing import Any, cast

import psycopg
import pytest
from fastapi import Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from base.agents import AgentStatus
from base.api_contracts.contracts import Idempotency
from base.events.live.bus import EventBus
from gateway.app import app
from gateway.middleware import idempotency
from ops.rpc_schemas import ContentBlock


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


_rows = idempotency.IdempotencyStore


# ── DB-helper unit coverage (failure paths without HTTP) ───────────────


def test_non_2xx_releases_the_key(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """A non-2xx outcome deletes the row, so a retry executes afresh."""
    # Simulate an owner that failed: claim the key, store nothing, release.
    pool = app.state.db_pool
    assert _rows(pool).claim("key-fail", "POST", f"/api/agents/{agent_id}/messages")
    assert (
        _rows(pool).fetch("key-fail", "POST", f"/api/agents/{agent_id}/messages") is None
    )  # still executing
    _rows(pool).release("key-fail")
    assert _rows(pool).claim("key-fail", "POST", f"/api/agents/{agent_id}/messages"), (
        "released key must be claimable again"
    )
    # Clear the placeholder again — the claim above is only the unit
    # assertion; a real request must find the key free and execute.
    _rows(pool).release("key-fail")
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
    assert _rows(pool).claim(key, "POST", f"/api/agents/{agent_id}/messages")
    _rows(pool).store(key, 201, {"detail": "stored"}, {"content-type": "application/json"})
    done = _rows(pool).fetch(key, "POST", f"/api/agents/{agent_id}/messages")
    assert done is not None
    status, body, _headers = done
    assert status == 201
    assert body == {"detail": "stored"}
    resp = idempotency._replay(done)
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
    assert _rows(pool).claim(key, "POST", path)
    _age_placeholder(db_conn, key, idempotency._RETENTION_DAYS + 1)
    # A retry re-claims the dead owner's placeholder...
    assert _rows(pool).claim(key, "POST", path), (
        "a placeholder past the retention window must be stealable"
    )
    _rows(pool).release(key)  # clear the unit claim; a real request owns it
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
    assert _rows(app.state.db_pool).claim(key, "POST", path)
    # Keep a regression from taking the middleware's full 15-second wait.
    monkeypatch.setattr(app.state.idempotency, "max_wait_s", 0.01)

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
    from gateway.agents import state

    body = {
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": f"/api/agents/{agent_id}/uploads/gone.png"},
            }
        ],
        "source": "user",
    }
    original_normalize = state._normalize_message_content

    def _normalize_without_mutable_gates(
        _request: Request,
        _agent_id: int,
        content: str | list[ContentBlock],
    ) -> tuple[str, dict[str, object] | None]:
        return original_normalize(content)

    monkeypatch.setattr(
        state,
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

    monkeypatch.setattr(state, "_prepare_message_content", _mutable_gate_must_not_run)
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
    from ops import lifecycle as ops_lifecycle

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
    db_conn.commit()
    calls = 0

    async def _crash_then_heal(_db: object, _bus: EventBus, aid: int, **kw: object) -> AgentStatus:
        nonlocal calls
        calls += 1
        assert aid == agent_id
        assert kw["trigger_inbound_kind"] == "chat"
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

    monkeypatch.setattr(ops_lifecycle, "resurrect_if_terminated", _crash_then_heal)
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
    assert _rows(pool).claim(key, "POST", path)
    _age_placeholder(db_conn, key, idempotency._RETENTION_DAYS + 1)
    # Any claim triggers the prune sweep.
    assert _rows(pool).claim("key-prune-other", "POST", path)
    assert _rows(pool).fetch(key, "POST", path) is None, (
        "a stale placeholder must be pruned like a completed row"
    )
    assert _rows(pool).claim(key, "POST", path), "a pruned key must be claimable again"
    _rows(pool).release(key)


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
    assert _rows(pool).claim(key, "POST", path)
    assert not _rows(pool).claim(key, "POST", path), (
        "a live placeholder on the same route is owned — poll, don't steal"
    )
    assert _rows(pool).claim(key, "POST", other), (
        "a live placeholder on ANOTHER route is not this request's business"
    )
    _rows(pool).store(key, 201, {"ok": True}, {"content-type": "application/json"})
    done = _rows(pool).fetch(key, "POST", other)
    assert done is not None and done[1] == {"ok": True}
    assert _rows(pool).fetch(key, "POST", path) is None, (
        "a completed row must only replay on its own route"
    )
    _rows(pool).release(key)


def test_completed_row_not_stolen(
    client: TestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    """A completed same-route row must replay, not be re-claimed by a retry."""
    pool = app.state.db_pool
    key = "key-done"
    path = f"/api/agents/{agent_id}/messages"
    assert _rows(pool).claim(key, "POST", path)
    _rows(pool).store(key, 201, {"ok": True}, {"content-type": "application/json"})
    assert not _rows(pool).claim(key, "POST", path), (
        "a completed same-route row is a replay, not a new claim"
    )
    _rows(pool).release(key)


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
        return await idempotency.idempotency_middleware(Request(scope), call_next)

    return asyncio.run(_run())


class _ScriptedStore:
    """A store whose `claim` follows a script and whose key never completes."""

    def __init__(self, claims: Iterator[bool]) -> None:
        self._claims = claims

    def claim(self, *_args: object) -> bool:
        return next(self._claims)

    def fetch(self, *_args: object) -> None:
        return None


def test_in_flight_key_timeout_uses_typed_retriable_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live owner timeout remains retriable without exposing a detail-only body."""
    monkeypatch.setattr(idempotency.contracts, "contract_for", _fake_contract_for)

    monotonic_values = iter((0.0, idempotency._MAX_WAIT_SECONDS))
    service = idempotency.IdempotencyService(
        cast("Any", _ScriptedStore(itertools.repeat(False))),
        monotonic=lambda: next(monotonic_values),
    )
    monkeypatch.setattr(app.state, "idempotency", service, raising=False)

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
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    class _Service(idempotency.IdempotencyService):
        async def drain_and_store(self, resp: Response, key: str) -> tuple[bytes, str]:
            return b"{}", "application/json"

    store = _ScriptedStore(iter((False, False, False, True)))  # owned for three polls, then free
    service = _Service(cast("Any", store), monotonic=lambda: 0.0, sleep=sleep)
    monkeypatch.setattr(idempotency.contracts, "contract_for", _fake_contract_for)
    monkeypatch.setattr(app.state, "idempotency", service, raising=False)

    async def call_next(_request: Request) -> Response:
        return Response(content=b"{}", media_type="application/json")

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
    monkeypatch.setattr(idempotency.contracts, "contract_for", _fake_contract_for)

    async def _boom() -> AsyncGenerator[bytes, None]:
        yield b"partial"
        raise RuntimeError("upstream died mid-body")

    async def call_next(_request: Request) -> Response:
        return StreamingResponse(_boom())

    with pytest.raises(RuntimeError, match="upstream died mid-body"):
        _run_middleware_once(key, path, call_next)
    assert _rows(pool).fetch(key, "POST", path) is None, (
        "a drained-body failure must release the row"
    )
    assert _rows(pool).claim(key, "POST", path), "the key must be claimable again"
    _rows(pool).release(key)


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
        idempotency.contracts,
        "contract_for",
        _fake_transactional_contract_for,
    )
    _run_middleware_once(key, path, call_next)
    _run_middleware_once(key, path, call_next)
    assert executions == 2
    assert _rows(app.state.db_pool).fetch(key, "POST", path) is None

    monkeypatch.setattr(idempotency.contracts, "contract_for", _fake_contract_for)
    _run_middleware_once(key, path, call_next)
    _run_middleware_once(key, path, call_next)
    assert executions == 3, "ordinary ALWK second call must replay without executing"
    _rows(app.state.db_pool).release(key)


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
    monkeypatch.setattr(idempotency.contracts, "contract_for", _fake_contract_for)

    async def call_next(_request: Request) -> Response:
        return Response(status_code=200, content=b"{}")

    with pytest.raises(TypeError, match="non-streaming"):
        _run_middleware_once(key, path, call_next)
    assert _rows(pool).fetch(key, "POST", path) is None, (
        "a non-streaming-response TypeError must release the row"
    )
    assert _rows(pool).claim(key, "POST", path), "the key must be claimable again"
    _rows(pool).release(key)
