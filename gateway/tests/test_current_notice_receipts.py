"""Real notice selector receipts preserve identity across retries and replacements."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.config import settings
from gateway.agents.notice_operations import current as current_notice_receipts
from gateway.agents.notice_operations import router as guarded_notices
from gateway.app import app
from gateway.tests.events.test_notices_endpoint import _insert_notice, _seed_agent

SECRET = "guarded-notice-test-secret"  # noqa: S105 -- isolated credential
HEADERS = {"Idempotency-Key": "intent", "Idempotency-Scope": "principal-v1"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", SECRET)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    with TestClient(
        app, headers={"Authorization": f"Bearer {SECRET}"}, raise_server_exceptions=False
    ) as value:
        yield value


def _path(agent: int, dismiss: bool) -> str:
    return f"/api/agents/{agent}/notices/current" + ("/dismiss" if dismiss else "") + "/guarded-v1"


def _send(
    client: TestClient,
    agent: int,
    notice: int,
    dismiss: bool,
    *,
    key: str = "intent",
    **changes: object,
):
    body = {"observed_notice_id": notice, **changes}
    if not dismiss and not changes:
        body["title"] = "edited"
    return client.request(
        "POST" if dismiss else "PATCH",
        _path(agent, dismiss),
        json=body,
        headers={**HEADERS, "Idempotency-Key": key},
    )


@pytest.mark.parametrize("dismiss", [False, True])
def test_original_snapshot_survives_replacement_and_deletion(
    client: TestClient, db_conn: psycopg.Connection, dismiss: bool
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    first = _send(client, agent, notice, dismiss)
    assert first.status_code == 200, first.text
    later = client.post(f"/api/agents/{agent}/notices", json={"title": "B"})
    assert later.status_code == 201
    assert _send(client, agent, notice, dismiss, key="fresh").status_code == 409
    replay = _send(client, agent, notice, dismiss)
    assert replay.json() == first.json()
    assert db_conn.execute(
        "SELECT title FROM agent_notices WHERE agent_id=%s AND resolved_at IS NULL", (agent,)
    ).fetchone() == ("B",)
    db_conn.execute("DELETE FROM agent_notices WHERE agent_id=%s", (agent,))
    db_conn.commit()
    assert _send(client, agent, notice, dismiss).json() == first.json()
    db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent,))
    db_conn.execute("DELETE FROM agent_lifecycle_intervals WHERE agent_id=%s", (agent,))
    db_conn.execute("DELETE FROM agents WHERE id=%s", (agent,))
    db_conn.commit()
    assert _send(client, agent, notice, dismiss).json() == first.json()
    assert _send(client, agent, notice, dismiss, key="new").status_code == 404


@pytest.mark.parametrize("dismiss", [False, True])
def test_concurrent_duplicate_has_one_acceptance(
    client: TestClient, db_conn: psycopg.Connection, dismiss: bool
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_send, client, agent, notice, dismiss) for _ in range(4)]
        replies = [future.result() for future in futures]
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    assert all(r.json() == replies[0].json() for r in replies)
    assert db_conn.execute(
        "SELECT count(*) FROM notice_operation_receipts WHERE path=%s", (_path(agent, dismiss),)
    ).fetchone() == (1,)
    assert _send(client, agent, notice + 1, dismiss).status_code == 409


@pytest.mark.parametrize("dismiss", [False, True])
def test_receipt_failure_rolls_back_mutation(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, dismiss: bool
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    original = current_notice_receipts.save_receipt

    def fail(*args: object) -> None:
        raise RuntimeError("before receipt commit")

    monkeypatch.setattr(current_notice_receipts, "save_receipt", fail)
    assert _send(client, agent, notice, dismiss).status_code == 500
    assert db_conn.execute(
        "SELECT title,resolved_at FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone() == ("A", None)
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)
    monkeypatch.setattr(current_notice_receipts, "save_receipt", original)
    assert _send(client, agent, notice, dismiss).status_code == 200


@pytest.mark.parametrize("dismiss", [False, True])
def test_lost_post_commit_response_replays_without_old_hint(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, dismiss: bool
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    calls: list[tuple[object, ...]] = []

    async def fail(*args: object) -> None:
        calls.append(args)
        raise RuntimeError("response lost after durable commit")

    name = "publish_notice_resolved" if dismiss else "publish_notice_posted"
    monkeypatch.setattr(guarded_notices.lifecycle, name, fail)
    assert _send(client, agent, notice, dismiss).status_code == 500
    replay = _send(client, agent, notice, dismiss)
    assert replay.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": "x"},
        {**HEADERS, "Idempotency-Key": ""},
        {**HEADERS, "Idempotency-Key": "x" * 129},
        {**HEADERS, "Idempotency-Scope": "wrong"},
    ],
)
def test_required_identity_has_no_effect(
    client: TestClient, db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    response = client.patch(
        _path(agent, False), json={"observed_notice_id": notice, "title": "B"}, headers=headers
    )
    assert response.status_code == 422
    assert db_conn.execute("SELECT title FROM agent_notices WHERE id=%s", (notice,)).fetchone() == (
        "A",
    )
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)


@pytest.mark.parametrize(
    "body",
    [
        {"observed_notice_id": 0, "title": "B"},
        {"observed_notice_id": True, "title": "B"},
        {"observed_notice_id": "1", "title": "B"},
        {"observed_notice_id": 1},
        {"observed_notice_id": 1, "priority": None},
        {"observed_notice_id": 1, "blocking": None},
        {"observed_notice_id": 1, "title": " "},
        {"observed_notice_id": 1, "require_response": True},
    ],
)
def test_invalid_input_precedes_receipt(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, object]
) -> None:
    assert client.patch(_path(999, False), json=body, headers=HEADERS).status_code == 422
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)


def test_omitted_content_differs_from_explicit_null(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A", content="keep")
    first = _send(client, agent, notice, False, title="B")
    assert first.status_code == 200
    assert first.json()["content"] == "keep"
    assert _send(client, agent, notice, False, title="B", content=None).status_code == 409
    assert _send(client, agent, notice, False, key="clear", content=None).json()["content"] is None


def test_paths_do_not_share_receipts(client: TestClient, db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    assert _send(client, agent, notice, False).status_code == 200
    assert _send(client, agent, notice, True).status_code == 200
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (2,)


def test_authentication_is_rechecked_on_replay(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    assert _send(client, agent, notice, False).status_code == 200
    response = client.patch(
        _path(agent, False),
        json={"observed_notice_id": notice, "title": "edited"},
        headers={**HEADERS, "Authorization": "Bearer revoked"},
    )
    assert response.status_code == 401


def test_unverified_principal_cannot_create_receipt(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", False)
    assert _send(client, agent, notice, False).status_code == 422
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)


@pytest.mark.parametrize("dismiss", [False, True])
def test_old_routing_cannot_execute_guarded_request(
    db_conn: psycopg.Connection, dismiss: bool
) -> None:
    from fastapi import FastAPI

    from gateway.agents.notices import router

    legacy = FastAPI()
    legacy.include_router(router)
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    with TestClient(legacy) as client:
        response = _send(client, agent, notice, dismiss)
    assert response.status_code in (404, 405)
    assert db_conn.execute(
        "SELECT title,resolved_at FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone() == ("A", None)


def test_reply_concurrent_with_guarded_edit_has_no_lock_cycle(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A", require_response=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        edit = executor.submit(_send, client, agent, notice, False)
        reply = executor.submit(
            client.post,
            f"/api/agents/{agent}/notices/{notice}/resolve",
            json={"action": "answer", "reply": "yes"},
            headers={"Idempotency-Key": "reply"},
        )
        assert edit.result(timeout=10).status_code in (200, 409)
        assert reply.result(timeout=10).status_code == 201
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)


def test_creation_waits_for_guarded_owner_lock(
    client: TestClient, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    locked = Event()
    release = Event()
    original = current_notice_receipts.save_receipt

    def held(
        conn: psycopg.Connection,
        path: str,
        key: str,
        body: dict[str, object],
        receipt: dict[str, object],
    ) -> None:
        original(conn, path, key, body, receipt)
        locked.set()
        assert release.wait(5)

    monkeypatch.setattr(current_notice_receipts, "save_receipt", held)
    with ThreadPoolExecutor(max_workers=2) as executor:
        edit = executor.submit(_send, client, agent, notice, False)
        assert locked.wait(5)
        with pytest.raises(psycopg.errors.LockNotAvailable), db_conn.transaction():
            db_conn.execute("SELECT id FROM agents WHERE id=%s FOR UPDATE NOWAIT", (agent,))
        later = executor.submit(client.post, f"/api/agents/{agent}/notices", json={"title": "B"})
        try:
            assert not later.done()
        finally:
            release.set()
        assert edit.result(timeout=10).status_code == 200
        assert later.result(timeout=10).status_code == 201
    assert db_conn.execute(
        "SELECT title FROM agent_notices WHERE agent_id=%s AND resolved_at IS NULL", (agent,)
    ).fetchone() == ("B",)


def test_receipt_survives_expiration_and_changed_body_conflicts(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    first = _send(client, agent, notice, False, title="accepted")
    assert first.status_code == 200
    db_conn.execute(
        "UPDATE agent_notices SET expire_at=now()-interval '1 day' WHERE id=%s", (notice,)
    )
    db_conn.commit()
    assert _send(client, agent, notice, False, title="accepted").json() == first.json()
    assert _send(client, agent, notice, False, title="different").status_code == 409


def test_fyi_cannot_become_blocking(client: TestClient, db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A")
    assert _send(client, agent, notice, False, blocking=True).status_code == 422
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)


def test_reply_fk_is_compatible_with_guarded_agent_lock(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "A", require_response=True)
    with ThreadPoolExecutor(max_workers=1) as executor, db_conn.transaction():
        db_conn.execute("SELECT id FROM agents WHERE id=%s FOR NO KEY UPDATE", (agent,))
        reply = executor.submit(
            client.post,
            f"/api/agents/{agent}/notices/{notice}/resolve",
            json={"action": "answer", "reply": "yes"},
            headers={"Idempotency-Key": "reply"},
        )
        assert reply.result(timeout=10).status_code == 201
    assert _send(client, agent, notice, False).status_code == 409
