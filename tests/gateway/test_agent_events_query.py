"""`GET /api/agents/{agent_id}/events` — historical REST slice over Loki
(the LGTM read side; task #1197 replaced the PG `events` read).

The route is a thin adapter over `loki_events.query_events`: these tests
monkeypatch the query and lock the parameter mapping (agent scope,
telemetry/log-only categories, exact level, window, paging) and the
wire shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway import loki_events
from gateway.app import app


def _row(
    *,
    ts: str = "2026-08-12T00:00:00Z",
    agent_id: int = 7,
    level: str = "info",
    event: str = "llm_usage",
    msg: str = "hi",
) -> dict[str, Any]:
    return {
        "id": 1,
        "ts": datetime.fromisoformat(ts),
        "agent_id": agent_id,
        "machine": "machine-1",
        "process": "gateway",
        "category": "telemetry",
        "event_name": event,
        "level": level,
        "source": "test",
        "target_agent_id": None,
        "attributes": {"msg": msg},
    }


@pytest.fixture
def fake_query(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Patch loki_events.query_events; record kwargs, return canned rows."""

    calls: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    def _query(**kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        calls.append(kwargs)
        return rows, False

    monkeypatch.setattr(loki_events, "query_events", _query)
    return {"calls": calls, "rows": rows}


class TestAgentEventsQuery:
    def test_returns_rows_newest_first(self, fake_query: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        fake_query["rows"].extend(
            [
                _row(msg="oldest", ts="2026-08-12T00:00:00Z"),
                _row(msg="newest", ts="2026-08-12T00:01:00Z"),
            ]
        )
        with TestClient(app) as client:
            r = client.get("/api/agents/7/events")
        assert r.status_code == 200
        items = r.json()
        assert [i["payload"]["msg"] for i in items] == [
            "oldest",
            "newest",
        ]  # query_events order is preserved
        # wire shape: id / ts / agent_id / level / event / payload
        assert set(items[0]) == {"id", "ts", "agent_id", "level", "event", "payload"}

    def test_scopes_to_agent(self, fake_query: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/agents/42/events")
        assert fake_query["calls"][0]["agent_id"] == 42

    def test_telemetry_and_log_categories_only(
        self, fake_query: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        """The old PG contract was category IN (telemetry, log) — audit rows
        (spawn/send_message/...) must not leak into the feed. The route passes
        the category set to Loki."""
        with TestClient(app) as client:
            client.get("/api/agents/7/events")
        assert fake_query["calls"][0]["categories"] == ["telemetry", "log"]

    def test_filter_event_maps_to_event_names(
        self, fake_query: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/agents/7/events", params={"event": "llm_usage"})
        assert fake_query["calls"][0]["event_names"] == ["llm_usage"]

    def test_filter_level_is_exact(self, fake_query: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        # exact level (case-insensitive) — NOT a minimum threshold
        with TestClient(app) as client:
            client.get("/api/agents/7/events", params={"level": "WARNING"})
        assert fake_query["calls"][0]["level"] == "WARNING"
        assert "level_min" not in fake_query["calls"][0]

    def test_from_to_window_passed_through(
        self, fake_query: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get(
                "/api/agents/7/events",
                params={"from": "2026-08-01T00:00:00Z", "to": "2026-08-02T00:00:00Z"},
            )
        kw = fake_query["calls"][0]
        assert kw["from_"] == datetime(2026, 8, 1, tzinfo=UTC)
        assert kw["to"] == datetime(2026, 8, 2, tzinfo=UTC)

    def test_no_from_means_default_window(
        self, fake_query: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        # the 24h default lower bound lives in query_events
        with TestClient(app) as client:
            client.get("/api/agents/7/events")
        assert fake_query["calls"][0]["from_"] is None

    def test_limit_and_offset_passed_through(
        self, fake_query: dict[str, list[dict[str, Any]]]
    ) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            client.get("/api/agents/7/events", params={"limit": 5, "offset": 10})
        kw = fake_query["calls"][0]
        assert kw["limit"] == 5
        assert kw["offset"] == 10

    def test_unknown_agent_returns_empty(self, fake_query: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/agents/999999/events")
        assert r.status_code == 200
        assert r.json() == []

    def test_limit_over_max_422(self, fake_query: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/agents/7/events", params={"limit": 1001})
        assert r.status_code == 422

    def test_invalid_from_timestamp_422(self, fake_query: dict[str, list[dict[str, Any]]]) -> None:  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            r = client.get("/api/agents/7/events", params={"from": "not-a-date"})
        assert r.status_code == 422
