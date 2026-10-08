"""`GET /api/computer/traces?task_id=N` — one task's desktop-action trail.

Read from `audit_events` (the computer_* events are audit), with no time window.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app


def _insert_event(
    db: psycopg.Connection,
    *,
    agent_id: int,
    event: str,
    attributes: dict[str, object],
    seconds_ago: float = 0.0,
) -> None:
    db.execute(
        "INSERT INTO audit_events (event_uid, ts, agent_id, machine, process, event_name, level, "
        "source, attributes) VALUES (%s, now() - (%s * interval '1 second'), %s, 'm', 'p', %s, "
        "'info', 'test', %s::jsonb)",
        (uuid.uuid4().int % (1 << 62), seconds_ago, agent_id, event, json.dumps(attributes)),
    )
    db.commit()


def _get(task_id: int) -> tuple[int, dict[str, Any]]:
    with TestClient(app) as client:
        response = client.get(f"/api/computer/traces?task_id={task_id}")
    return response.status_code, response.json()


def _trace(task_id: int) -> dict[str, Any]:
    status, body = _get(task_id)
    assert status == 200, body
    return body


class TestComputerTrace:
    def test_empty_task_404(self, db_conn: psycopg.Connection) -> None:
        assert _get(999)[0] == 404

    def test_assembles_trace_chronologically(self, db_conn: psycopg.Connection) -> None:
        aid = 1
        _insert_event(
            db_conn,
            agent_id=aid,
            event="computer_session_start",
            attributes={
                "task_id": 42,
                "first_tool": "snapshot",
                "first_action_at": "2026-08-10T12:00:00+00:00",
            },
            seconds_ago=30,
        )
        _insert_event(
            db_conn,
            agent_id=aid,
            event="computer_action",
            attributes={
                "task_id": 42,
                "action": "snapshot",
                "app": "Finder",
                "outcome": "ok",
                "coords": None,
                "path": "/tmp/snap.png",  # noqa: S108
                "error": None,
            },
            seconds_ago=20,
        )
        _insert_event(
            db_conn,
            agent_id=aid,
            event="computer_action",
            attributes={
                "task_id": 42,
                "action": "click",
                "app": "Finder",
                "outcome": "ok",
                "coords": "100,200",
                "path": None,
                "error": None,
            },
            seconds_ago=10,
        )
        _insert_event(
            db_conn,
            agent_id=aid,
            event="computer_session_end",
            attributes={
                "task_id": 42,
                "action_count": 2,
                "first_action_at": "2026-08-10T12:00:00+00:00",
                "last_action_at": "2026-08-10T12:00:30+00:00",
                "outcome": "idle_timeout",
            },
        )
        # a different task's rows must not leak in
        _insert_event(
            db_conn,
            agent_id=aid,
            event="computer_action",
            attributes={"task_id": 43, "action": "key", "outcome": "ok"},
        )

        trace = _trace(42)
        assert trace["task_id"] == 42
        assert trace["start"]["event"] == "computer_session_start"
        assert trace["start"]["first_tool"] == "snapshot"
        assert trace["end"]["event"] == "computer_session_end"
        assert trace["end"]["outcome"] == "idle_timeout"
        assert trace["end"]["action_count"] == 2
        actions = trace["actions"]
        assert [a["action"] for a in actions] == ["snapshot", "click"]
        assert actions[0]["path"] == "/tmp/snap.png"  # noqa: S108
        assert actions[1]["coords"] == "100,200"
        # chronological: ts ascending
        assert actions[0]["ts"] < actions[1]["ts"]

    def test_open_session_has_null_end(self, db_conn: psycopg.Connection) -> None:
        aid = 1
        _insert_event(
            db_conn,
            agent_id=aid,
            event="computer_session_start",
            attributes={
                "task_id": 7,
                "first_tool": "click",
                "first_action_at": "2026-08-10T12:00:00+00:00",
            },
        )
        _insert_event(
            db_conn,
            agent_id=aid,
            event="computer_action",
            attributes={"task_id": 7, "action": "click", "outcome": "ok"},
        )
        trace = _trace(7)
        assert trace["start"] is not None
        assert trace["end"] is None
        assert len(trace["actions"]) == 1
