"""Actual SQL winners, preflight windows and rollback of every compound effect."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.agents.tasks import creation
from base.db import Database
from gateway.agents import router as agent_router
from gateway.agents.task_assignment import router as assignments
from gateway.agents.tests.test_task_assignments import HEADERS, PATH
from gateway.agents.tests.test_task_assignments import body as body
from gateway.agents.tests.test_task_assignments import client as client


def _counts(conn: psycopg.Connection) -> tuple[int, ...]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM agents),(SELECT count(*) FROM agent_tasks),(SELECT count(*) FROM audit_events),(SELECT count(*) FROM inbound_messages),(SELECT count(*) FROM task_assignment_receipts)"
    ).fetchone()
    assert row is not None
    conn.commit()
    return row


def test_fresh_simultaneous_intents_produce_one_pair(
    client: TestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _counts(db_conn)
    boundary = Barrier(3)
    real = agent_router._spawn_preflight_blocking

    def preflight(*args: Any):
        boundary.wait(timeout=10)
        return real(*args)

    monkeypatch.setattr(agent_router, "_spawn_preflight_blocking", preflight)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(client.post, PATH, json=body, headers=HEADERS) for _ in range(3)]
        responses = [future.result(timeout=20) for future in futures]
    assert all(response.status_code == 201 for response in responses), [r.text for r in responses]
    assert len({r.json()["agent_id"] for r in responses}) == 1
    assert len({r.json()["task"]["id"] for r in responses}) == 1
    assert len({r.json()["launch_attempt_id"] for r in responses}) == 1
    after = _counts(db_conn)
    assert after[0] == before[0] + 1
    assert after[1] == before[1] + 1
    assert after[3] == before[3] + 1
    assert after[4] == before[4] + 1


@pytest.mark.parametrize("stage", ["birth", "task", "notification", "receipt"])
def test_exception_after_each_real_writer_rolls_back_all_effects(
    client: TestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    before = _counts(db_conn)
    owner, name = {
        "birth": (assignments, "insert_agent_birth"),
        "task": (creation, "insert_task"),
        "notification": (creation, "queue_creation_notifications"),
        "receipt": (assignments, "save_assignment"),
    }[stage]
    real = getattr(owner, name)

    def fail_after(*args: Any, **kwargs: Any):
        real(*args, **kwargs)
        raise RuntimeError(f"failure after {stage}")

    monkeypatch.setattr(owner, name, fail_after)
    with pytest.raises(RuntimeError, match=f"failure after {stage}"):
        client.post(PATH, json=body, headers=HEADERS)
    assert _counts(db_conn) == before


def test_preflight_failure_returns_winner_committed_during_its_window(
    client: TestClient, body: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = agent_router._spawn_preflight_blocking
    winner: list[dict[str, Any]] = []

    def loses(*args: Any):
        monkeypatch.setattr(agent_router, "_spawn_preflight_blocking", real)
        accepted = client.post(PATH, json=body, headers=HEADERS)
        assert accepted.status_code == 201, accepted.text
        winner.append(accepted.json())
        raise ValueError("preset vanished after concurrent acceptance")

    monkeypatch.setattr(agent_router, "_spawn_preflight_blocking", loses)
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert replay.json()["task"] == winner[0]["task"]
    assert replay.json()["agent_id"] == winner[0]["agent_id"]


def test_second_lookup_replays_winner_after_successful_preflight_window(
    client: TestClient, body: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = agent_router._spawn_preflight_blocking
    winner: list[dict[str, Any]] = []

    def loses(*args: Any):
        prepared = real(*args)
        monkeypatch.setattr(agent_router, "_spawn_preflight_blocking", real)
        accepted = client.post(PATH, json=body, headers=HEADERS)
        assert accepted.status_code == 201, accepted.text
        winner.append(accepted.json())
        return prepared

    monkeypatch.setattr(agent_router, "_spawn_preflight_blocking", loses)
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert replay.json()["task"] == winner[0]["task"]
    assert replay.json()["agent_id"] == winner[0]["agent_id"]


def test_single_connection_pool_accepts_without_nested_borrow(
    client: TestClient, database: Database, body: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.app import app

    with database.pool(min_size=1, max_size=1, timeout=2) as pool, monkeypatch.context() as patcher:
        patcher.setattr(app.state, "db_pool", pool)
        response = client.post(PATH, json=body, headers=HEADERS)
        assert response.status_code == 201, response.text
        again = client.post(PATH, json=body, headers=HEADERS)
        assert again.status_code == 201
        assert again.json()["task"] == response.json()["task"]


def test_title_rejection_rolls_back_a_different_key_birth(
    client: TestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    assert client.post(PATH, json=body, headers=HEADERS).status_code == 201
    before = _counts(db_conn)
    rejected = client.post(
        PATH, json=body, headers={**HEADERS, "Idempotency-Key": "another-operation"}
    )
    assert rejected.status_code == 422, rejected.text
    assert _counts(db_conn) == before


def test_closed_parent_rejection_does_not_leave_agent_or_receipt(
    client: TestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    row = db_conn.execute(
        "INSERT INTO agent_tasks(title,description,status,created_by) VALUES ('Closed parent','','done','system') RETURNING id"
    ).fetchone()
    assert row is not None
    db_conn.commit()
    body["task"]["parent"] = row[0]
    before = _counts(db_conn)
    rejected = client.post(PATH, json=body, headers=HEADERS)
    assert rejected.status_code == 422, rejected.text
    assert _counts(db_conn) == before
