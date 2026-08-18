"""`GET /api/computer/traces?task_id=N` — one task's desktop-action trail.

Loki-backed via the `FakeLoki` stand-in (monkeypatched onto
`gateway.loki_events`), same filter/window semantics as the real module.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from tests.gateway.loki_fake import FakeLoki


@pytest.fixture
def loki_fake(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    fake = FakeLoki()
    monkeypatch.setattr("gateway.loki_events.query_events", fake.query_events)
    return fake


def _insert_event(
    fake: FakeLoki,
    *,
    agent_id: int,
    event: str,
    attributes: dict[str, object],
    ts_offset_seconds: float = 0.0,
) -> None:
    fake.add(
        event=event,
        agent_id=agent_id,
        payload=attributes,
        ts_offset_hours=-ts_offset_seconds / 3600,
    )


def _trace(task_id: int) -> dict[str, Any]:
    resp = TestClient(app).get(f"/api/computer/traces?task_id={task_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestComputerTrace:
    def test_empty_task_404(self, loki_fake: FakeLoki) -> None:
        resp = TestClient(app).get("/api/computer/traces?task_id=999")
        assert resp.status_code == 404

    def test_assembles_trace_chronologically(self, loki_fake: FakeLoki) -> None:
        aid = 1
        _insert_event(
            loki_fake,
            agent_id=aid,
            event="computer_session_start",
            attributes={
                "task_id": 42,
                "first_tool": "snapshot",
                "first_action_at": "2026-08-10T12:00:00+00:00",
            },
            ts_offset_seconds=-30,
        )
        _insert_event(
            loki_fake,
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
            ts_offset_seconds=-20,
        )
        _insert_event(
            loki_fake,
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
            ts_offset_seconds=-10,
        )
        _insert_event(
            loki_fake,
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
            loki_fake,
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
        # chronological: ts ascending (Loki ids are stable hashes, not monotonic)
        assert actions[0]["ts"] < actions[1]["ts"]

    def test_open_session_has_null_end(self, loki_fake: FakeLoki) -> None:
        aid = 1
        _insert_event(
            loki_fake,
            agent_id=aid,
            event="computer_session_start",
            attributes={
                "task_id": 7,
                "first_tool": "click",
                "first_action_at": "2026-08-10T12:00:00+00:00",
            },
        )
        _insert_event(
            loki_fake,
            agent_id=aid,
            event="computer_action",
            attributes={"task_id": 7, "action": "click", "outcome": "ok"},
        )
        trace = _trace(7)
        assert trace["start"] is not None
        assert trace["end"] is None
        assert len(trace["actions"]) == 1
