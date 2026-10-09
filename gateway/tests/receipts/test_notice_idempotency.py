"""Notice receipts keep resolution, replies and superseding creation atomic."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.tests.events.test_notices_endpoint import _insert_notice, _seed_agent


def test_concurrent_resolution_replays_one_reply(db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "question", require_response=True)
    path = f"/api/agents/{agent}/notices/{notice}/resolve"
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=4) as executor:

        def resolve() -> dict[str, object]:
            response = client.post(
                path,
                json={"action": "answer", "reply": "yes"},
                headers={"Idempotency-Key": "answer-once"},
            )
            assert response.status_code == 201
            return response.json()

        results = [future.result() for future in [executor.submit(resolve) for _ in range(4)]]
        assert len({result["inbound_id"] for result in results}) == 1
        changed = client.post(
            path,
            json={"action": "answer", "reply": "no"},
            headers={"Idempotency-Key": "answer-once"},
        )
        assert changed.status_code == 409
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent,))
        assert cur.fetchone() == (1,)


def test_distinct_later_read_replies_remain_distinct(db_conn: psycopg.Connection) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "FYI")
    path = f"/api/agents/{agent}/notices/{notice}/resolve"
    with TestClient(app) as client:
        body = {"action": "read", "reply": "thanks"}
        first = client.post(path, json=body, headers={"Idempotency-Key": "read-1"})
        replay = client.post(path, json=body, headers={"Idempotency-Key": "read-1"})
        later = client.post(path, json=body, headers={"Idempotency-Key": "read-2"})
    assert first.status_code == replay.status_code == later.status_code == 201
    assert first.json()["inbound_id"] == replay.json()["inbound_id"]
    assert later.json()["inbound_id"] != first.json()["inbound_id"]


def test_creation_replay_preserves_original_result_after_superseding(
    db_conn: psycopg.Connection,
) -> None:
    agent = _seed_agent(db_conn)
    path = f"/api/agents/{agent}/notices"
    with TestClient(app) as client:
        body = {"title": "original"}
        first = client.post(path, json=body, headers={"Idempotency-Key": "create-1"})
        later = client.post(path, json={"title": "later"}, headers={"Idempotency-Key": "create-2"})
        replay = client.post(path, json=body, headers={"Idempotency-Key": "create-1"})
        conflict = client.post(
            path, json={"title": "different"}, headers={"Idempotency-Key": "create-1"}
        )
    assert first.status_code == later.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT title, resolution FROM agent_notices WHERE agent_id=%s ORDER BY id", (agent,)
        )
        assert cur.fetchall() == [("original", "superseded"), ("later", None)]


def test_resolution_recovers_after_committed_wake_failure(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.agents import notices

    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "question", require_response=True)
    path = f"/api/agents/{agent}/notices/{notice}/resolve"
    original = notices.publish_inbound_wake

    def unavailable(*args: object) -> bool:
        raise RuntimeError("process lost after commit")

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(notices, "publish_inbound_wake", unavailable)
        first = client.post(
            path,
            json={"action": "answer", "reply": "yes"},
            headers={"Idempotency-Key": "lost-tail"},
        )
        assert first.status_code == 500
        monkeypatch.setattr(notices, "publish_inbound_wake", original)
        second = client.post(
            path,
            json={"action": "answer", "reply": "yes"},
            headers={"Idempotency-Key": "lost-tail"},
        )
        assert second.status_code == 201
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM inbound_messages WHERE agent_id=%s", (agent,))
        assert cur.fetchall() == [(second.json()["inbound_id"],)]


def test_create_receipt_survives_expiration(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.agents.notice_operations import receipts as notice_receipts

    agent = _seed_agent(db_conn)
    path = f"/api/agents/{agent}/notices"
    deadline = datetime.now(UTC) + timedelta(hours=1)
    with TestClient(app) as client:
        body = {"title": "expiring", "expire_at": deadline.isoformat()}
        first = client.post(path, json=body, headers={"Idempotency-Key": "expires"})

        # If mutable preconditions run on receipt replay, the expired timestamp
        # would invalidate an already committed operation.
        def reject_fresh(*args: object) -> None:
            raise RuntimeError("fresh operation validation should not run")

        monkeypatch.setattr(notice_receipts, "validate_creation_state", reject_fresh)
        monkeypatch.setattr("gateway.agents.notices.validate_creation_state", reject_fresh)
        replay = client.post(path, json=body, headers={"Idempotency-Key": "expires"})
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()


@pytest.mark.parametrize("resolve", [False, True])
def test_missing_key_cannot_mutate_notice(db_conn: psycopg.Connection, resolve: bool) -> None:
    agent = _seed_agent(db_conn)
    notice = _insert_notice(db_conn, agent, "original", require_response=True)
    path = f"/api/agents/{agent}/notices"
    body = {"title": "replacement"}
    if resolve:
        path += f"/{notice}/resolve"
        body = {"action": "answer", "reply": "yes"}
    with TestClient(app) as client:
        assert client.post(path, json=body).status_code == 422
    assert db_conn.execute(
        "SELECT title, resolved_at FROM agent_notices WHERE agent_id=%s", (agent,)
    ).fetchall() == [("original", None)]
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)
