"""Guarded registry acceptance survives lost responses without replacing later pages."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import psycopg
import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from base.config import settings
from base.db import create_agent
from gateway.app import app
from gateway.routers import page_acceptance, pages

SECRET = "page-receipt-test-secret"  # noqa: S105 -- isolated credential fixture
HEADERS = {"Idempotency-Key": "intent", "Idempotency-Scope": "principal-v1"}
BODY = {"name": "report", "port": 8801, "host": "127.0.0.1", "ttl_seconds": 60}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    with TestClient(app, headers={"Authorization": f"Bearer {SECRET}"}) as value:
        yield value


def _agent(conn: psycopg.Connection) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status) VALUES (%s, 'idling') ON CONFLICT DO NOTHING",
        (agent,),
    )
    conn.commit()
    return agent


def _path(agent: int) -> str:
    return f"/api/keyed/v1/agents/{agent}/pages"


def _close_path(agent: int) -> str:
    return _path(agent) + "/report/close"


def _key(value: str) -> dict[str, str]:
    return {**HEADERS, "Idempotency-Key": value}


@pytest.mark.parametrize("close", [False, True])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": "x"},
        {"Idempotency-Key": "", "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x" * 129, "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x", "Idempotency-Scope": "other"},
    ],
)
def test_invalid_key_scope_has_no_effect(
    client: TestClient, db_conn: psycopg.Connection, headers: dict[str, str], close: bool
) -> None:
    agent = _agent(db_conn)
    body = {"expected_page_id": 1} if close else BODY
    path = _close_path(agent) if close else _path(agent)
    assert client.post(path, json=body, headers=headers).status_code == 422
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (0,)


def test_verified_principal_required(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(db_conn)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", False)
    assert client.post(_path(agent), json=BODY, headers=HEADERS).status_code == 422
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (0,)


def test_lost_response_concurrency_replays_original_row(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    assert first.status_code == 201, first.text
    deadline = db_conn.execute(
        "SELECT expires_at FROM agent_pages WHERE id=%s", (first.json()["id"],)
    ).fetchone()
    db_conn.commit()
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(client.post, _path(agent), json=BODY, headers=HEADERS) for _ in range(4)
        ]
        responses = [future.result() for future in futures]
    assert all(
        response.status_code == 201 and response.json() == first.json() for response in responses
    )
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (1,)
    assert (
        db_conn.execute(
            "SELECT expires_at FROM agent_pages WHERE id=%s", (first.json()["id"],)
        ).fetchone()
        == deadline
    )
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (1,)


def test_registration_replay_cannot_close_later_page(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    later = client.post(_path(agent), json={**BODY, "port": 8802}, headers=_key("new"))
    assert later.status_code == 201 and later.json()["id"] != first.json()["id"]
    monkeypatch.setattr(settings.daemon, "page_default_ttl_seconds", 1)
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://changed.invalid")

    async def no_old_events(*args: object) -> None:
        raise AssertionError("replaying historical acceptance must not publish obsolete events")

    monkeypatch.setattr(pages, "_publish_page_event", no_old_events)
    replay = client.post(_path(agent), json=BODY, headers=HEADERS)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert db_conn.execute("SELECT id FROM agent_pages WHERE closed_at IS NULL").fetchall() == [
        (later.json()["id"],)
    ]


@pytest.mark.parametrize("mutate", ["terminate", "delete"])
def test_historical_acceptance_precedes_mutable_target(
    client: TestClient, db_conn: psycopg.Connection, mutate: str
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    if mutate == "delete":
        db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent,))
        db_conn.execute("DELETE FROM agent_lifecycle_intervals WHERE agent_id=%s", (agent,))
        db_conn.execute("DELETE FROM agents WHERE id=%s", (agent,))
    else:
        db_conn.execute(
            "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
            (agent,),
        )
    db_conn.commit()
    replay = client.post(_path(agent), json=BODY, headers=HEADERS)
    assert replay.status_code == 201 and replay.json() == first.json()
    assert client.post(_path(agent), json=BODY, headers=_key("fresh")).status_code == (
        404 if mutate == "delete" else 409
    )


def test_changed_body_conflicts_without_replacement(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    assert (
        client.post(_path(agent), json={**BODY, "title": "changed"}, headers=HEADERS).status_code
        == 409
    )
    assert db_conn.execute("SELECT id FROM agent_pages WHERE closed_at IS NULL").fetchall() == [
        (first.json()["id"],)
    ]


def test_close_receipt_and_stale_observation_preserve_new_page(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS).json()
    close_body = {"expected_page_id": first["id"]}
    accepted = client.post(_close_path(agent), json=close_body, headers=_key("close"))
    assert accepted.status_code == 200
    later = client.post(_path(agent), json=BODY, headers=_key("later")).json()
    replay = client.post(_close_path(agent), json=close_body, headers=_key("close"))
    assert replay.json() == accepted.json() and replay.status_code == 200
    assert (
        client.post(_close_path(agent), json=close_body, headers=_key("stale")).status_code == 409
    )
    assert (
        client.post(
            _close_path(agent), json={"expected_page_id": later["id"]}, headers=_key("close")
        ).status_code
        == 409
    )
    assert db_conn.execute("SELECT id FROM agent_pages WHERE closed_at IS NULL").fetchall() == [
        (later["id"],)
    ]


def test_fresh_already_closed_observation_is_accepted(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS).json()
    body = {"expected_page_id": first["id"]}
    accepted = client.post(_close_path(agent), json=body, headers=_key("close"))

    async def forbid_old_hint(*args: object) -> None:
        raise AssertionError("already-closed acceptance must not publish obsolete name hints")

    monkeypatch.setattr(pages, "_publish_page_event", forbid_old_hint)
    no_effect = client.post(_close_path(agent), json=body, headers=_key("already-closed"))
    assert no_effect.status_code == 200 and no_effect.json() == accepted.json()


@pytest.mark.parametrize("value", [0, -1, True, "1"])
def test_observed_id_is_strict(
    client: TestClient, db_conn: psycopg.Connection, value: object
) -> None:
    agent = _agent(db_conn)
    assert (
        client.post(
            _close_path(agent), json={"expected_page_id": value}, headers=HEADERS
        ).status_code
        == 422
    )
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (0,)


def test_failure_before_receipt_rolls_back_replacement(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS)
    original_finish = page_acceptance._finish

    def fail(*args: object) -> None:
        raise HTTPException(status_code=503, detail="fault before page receipt commit")

    monkeypatch.setattr(page_acceptance, "_finish", fail)
    assert (
        client.post(_path(agent), json={**BODY, "port": 8802}, headers=_key("pending")).status_code
        == 503
    )
    assert db_conn.execute("SELECT id FROM agent_pages WHERE closed_at IS NULL").fetchall() == [
        (first.json()["id"],)
    ]
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (1,)
    db_conn.commit()
    monkeypatch.setattr(page_acceptance, "_finish", original_finish)
    recovered = client.post(_path(agent), json={**BODY, "port": 8802}, headers=_key("pending"))
    assert recovered.status_code == 201


def test_revoked_auth_cannot_replay(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(db_conn)
    assert client.post(_path(agent), json=BODY, headers=HEADERS).status_code == 201
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "different-secret")
    assert client.post(_path(agent), json=BODY, headers=HEADERS).status_code == 401


def test_older_routing_has_no_guarded_effect(db_conn: psycopg.Connection) -> None:
    old = FastAPI()
    legacy = APIRouter()
    legacy.routes.extend(
        route
        for route in pages.router.routes
        if isinstance(route, APIRoute) and "/api/keyed/v1/" not in route.path
    )
    old.include_router(legacy)
    agent = _agent(db_conn)
    with TestClient(old) as client:
        assert client.post(_path(agent), json=BODY, headers=HEADERS).status_code == 404
        assert (
            client.post(
                _close_path(agent), json={"expected_page_id": 1}, headers=HEADERS
            ).status_code
            == 404
        )
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (0,)


def test_simultaneous_fresh_duplicates_publish_one_row(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(client.post, _path(agent), json=BODY, headers=HEADERS) for _ in range(4)
        ]
        results = [future.result() for future in futures]
    assert all(
        result.status_code == 201 and result.json() == results[0].json() for result in results
    )
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (1,)


def test_two_fresh_intents_serialize_replacement(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(client.post, _path(agent), json=BODY, headers=HEADERS)
        two = executor.submit(
            client.post,
            _path(agent),
            json={**BODY, "name": "later", "port": 8802},
            headers=_key("new"),
        )
        results = [one.result(), two.result()]
    assert all(result.status_code == 201 for result in results)
    assert len({result.json()["id"] for result in results}) == 2
    assert db_conn.execute(
        "SELECT count(*) FROM agent_pages WHERE closed_at IS NULL"
    ).fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (2,)


def test_commit_survives_response_tail_failure(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(db_conn)

    async def fail_tail(*args: object) -> None:
        raise HTTPException(
            status_code=503, detail="acceptance committed; response tail unavailable"
        )

    monkeypatch.setattr(pages, "_publish_page_event", fail_tail)
    assert client.post(_path(agent), json=BODY, headers=HEADERS).status_code == 503
    row = db_conn.execute("SELECT id FROM agent_pages WHERE agent_id=%s", (agent,)).fetchone()
    replay = client.post(_path(agent), json=BODY, headers=HEADERS)
    assert row is not None and replay.status_code == 201 and replay.json()["id"] == row[0]
    assert db_conn.execute("SELECT count(*) FROM agent_pages").fetchone() == (1,)


def test_raced_unique_conflict_rolls_back_old_close(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, two = _agent(db_conn), _agent(db_conn)
    original = client.post(_path(one), json=BODY, headers=HEADERS).json()
    assert client.post(_path(two), json={**BODY, "port": 8802}, headers=HEADERS).status_code == 201

    # Model a port owner becoming visible only after the advisory pre-check.
    def raced_precheck(*_args: object) -> None:
        pass

    monkeypatch.setattr(page_acceptance, "assert_port_free", raced_precheck)
    rejected = client.post(_path(one), json={**BODY, "port": 8802}, headers=_key("raced"))
    assert rejected.status_code == 409
    assert db_conn.execute(
        "SELECT id FROM agent_pages WHERE agent_id=%s AND closed_at IS NULL", (one,)
    ).fetchone() == (original["id"],)
    assert db_conn.execute("SELECT count(*) FROM page_operation_receipts").fetchone() == (2,)


def test_close_replay_survives_target_cleanup(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _agent(db_conn)
    record = client.post(_path(agent), json=BODY, headers=HEADERS).json()
    body = {"expected_page_id": record["id"]}
    closed = client.post(_close_path(agent), json=body, headers=HEADERS)
    db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent,))
    db_conn.execute("DELETE FROM agent_lifecycle_intervals WHERE agent_id=%s", (agent,))
    db_conn.execute("DELETE FROM agents WHERE id=%s", (agent,))
    db_conn.commit()
    replay = client.post(_close_path(agent), json=body, headers=HEADERS)
    assert replay.status_code == 200 and replay.json() == closed.json()
    assert client.post(_close_path(agent), json=body, headers=_key("fresh")).status_code == 404


def test_host_validation_reuses_single_borrowed_connection(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from psycopg_pool import ConnectionPool

    agent = _agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET machine='page-home' WHERE id=%s", (agent,))
    db_conn.commit()
    # A guarded receipt lookup already owns the only backend. Validation must
    # share it rather than exhaust a pool with a nested connection request.
    with (
        ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=1, timeout=0.5) as pool,
        monkeypatch.context() as patch,
    ):
        patch.setattr(app.state, "db_pool", pool)
        result = client.post(_path(agent), json={**BODY, "host": "page-home"}, headers=HEADERS)
    assert result.status_code == 201, result.text


def test_placement_is_locked_before_host_validation(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from psycopg_pool import ConnectionPool

    agent = _agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET machine='page-home' WHERE id=%s", (agent,))
    db_conn.commit()
    entered, proceed = Event(), Event()
    original = pages._validate_page_dial_target

    def paused_validation(
        pool: ConnectionPool,
        cache: pages.PageHostCache,
        agent_id: int,
        host: str,
        port: int,
        *,
        connection: psycopg.Connection | None = None,
    ) -> None:
        entered.set()
        assert proceed.wait(5), "test did not release target validation"
        original(pool, cache, agent_id, host, port, connection=connection)

    monkeypatch.setattr(pages, "_validate_page_dial_target", paused_validation)
    with ThreadPoolExecutor(max_workers=1) as executor:
        accepted = executor.submit(
            client.post, _path(agent), json={**BODY, "host": "page-home"}, headers=HEADERS
        )
        try:
            assert entered.wait(5), "guarded request did not reach host validation"
            with psycopg.connect(settings.data_plane.db_url) as other:
                other.execute("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    other.execute(
                        "UPDATE agents_meta SET machine='moved-home' WHERE id=%s", (agent,)
                    )
                other.rollback()
        finally:
            proceed.set()
        assert accepted.result().status_code == 201


@pytest.mark.parametrize("legacy", [False, True])
def test_expired_name_reregistration_has_a_new_observed_identity(
    client: TestClient, db_conn: psycopg.Connection, legacy: bool
) -> None:
    agent = _agent(db_conn)
    first = client.post(_path(agent), json=BODY, headers=HEADERS).json()
    db_conn.execute("UPDATE agent_pages SET expired_at=now() WHERE id=%s", (first["id"],))
    db_conn.commit()
    target = f"/api/agents/{agent}/pages" if legacy else _path(agent)
    later = client.post(target, json={**BODY, "port": 8802}, headers=_key("later"))
    assert later.status_code == 201 and later.json()["id"] != first["id"]
    stale = client.post(
        _close_path(agent), json={"expected_page_id": first["id"]}, headers=_key("old-close")
    )
    assert stale.status_code == 409
    assert db_conn.execute("SELECT id FROM agent_pages WHERE closed_at IS NULL").fetchone() == (
        later.json()["id"],
    )
