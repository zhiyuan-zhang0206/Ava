"""GET /api/events — unified event stream query (LGTM read side, task #1197).

The route is an adapter over `loki_events.query_events` / `count_events`
(the PG `events` read was replaced by Loki). This file locks the endpoint
contract: filter composition (category / event_name — `kind` kept as a
legacy alias — / agent_id / trace_id / machine / level), the time window
(`from`/`to` and `hours`), offset paging with the `meta` envelope (opt-in
exact `total` from the Loki count path / window / lookahead has_more), the
unified row shape, and the 422s for illegal parameters. The Loki queries are mocked;
`tests/gateway/test_loki_events.py` covers the query building.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway import loki_events
from gateway.app import app

_EVENT_KEYS = {
    "id",
    "ts",
    "trace_id",
    "span_id",
    "agent_id",
    "machine",
    "process",
    "category",
    "event_name",
    "level",
    "source",
    "target_agent_id",
    "attributes",
}


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "ts": datetime(2026, 8, 12, tzinfo=UTC),
        "trace_id": None,
        "span_id": None,
        "agent_id": 7,
        "machine": "gateway-host",
        "process": "agent-kernel",
        "category": "telemetry",
        "event_name": "turn_end",
        "level": "info",
        "source": "agent",
        "target_agent_id": None,
        "attributes": {"msg": "hi"},
    }
    base.update(over)
    return base


class _FakeEvents:
    """Recorded loki_events calls + canned rows/total/has_more."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.totals: list[int] = []
        self.has_more = False


@pytest.fixture
def fake_events(monkeypatch: pytest.MonkeyPatch) -> _FakeEvents:
    """Patch loki_events.query_events + count_events; record kwargs."""

    fake = _FakeEvents()

    def _query(**kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        fake.calls.append(kwargs)
        return fake.rows, fake.has_more

    def _count(**kwargs: Any) -> int:
        fake.calls.append(kwargs)
        return fake.totals[-1] if fake.totals else len(fake.rows)

    monkeypatch.setattr(loki_events, "query_events", _query)
    monkeypatch.setattr(loki_events, "count_events", _count)
    return fake


class TestEventsApi:
    def test_empty_returns_empty_envelope(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get("/api/events")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["meta"]["total"] is None  # exact count is opt-in
        assert body["meta"]["has_more"] is False
        assert body["meta"]["limit"] == 100
        assert body["meta"]["offset"] == 0
        assert body["meta"]["window_from"] is not None  # 24h default echoes

    def test_returns_newest_first_with_unified_shape(self, fake_events: _FakeEvents) -> None:
        fake_events.rows.extend([_row(), _row(event_name="llm_usage")])
        with TestClient(app) as client:
            r = client.get("/api/events")
        assert r.status_code == 200
        items = r.json()["items"]
        assert set(items[0]) == _EVENT_KEYS

    def test_filter_category_maps_to_categories(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get("/api/events", params={"category": "audit"})
        assert fake_events.calls[0]["categories"] == ["audit"]

    def test_no_category_passes_none(self, fake_events: _FakeEvents) -> None:
        # unlike the per-agent/admin endpoints, /api/events serves ALL
        # categories (audit included) — no telemetry/log restriction
        with TestClient(app) as client:
            client.get("/api/events")
        assert fake_events.calls[0]["categories"] is None

    def test_filter_event_name(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get("/api/events", params={"event_name": "spawn"})
        assert fake_events.calls[0]["event_names"] == ["spawn"]

    def test_filter_kind_legacy_alias(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get("/api/events", params={"kind": "spawn"})
        assert fake_events.calls[0]["event_names"] == ["spawn"]

    def test_event_name_and_kind_alias_conflict_422(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get("/api/events", params={"event_name": "a", "kind": "b"})
        assert r.status_code == 422
        assert fake_events.calls == []

    def test_event_name_and_kind_alias_agree(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get("/api/events", params={"event_name": "spawn", "kind": "spawn"})
        assert fake_events.calls[0]["event_names"] == ["spawn"]

    def test_filter_agent_id(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get("/api/events", params={"agent_id": 42})
        assert fake_events.calls[0]["agent_id"] == 42

    def test_filter_trace_id_lowercased(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get("/api/events", params={"trace_id": "ABCDEF"})
        assert fake_events.calls[0]["trace_id"] == "abcdef"

    def test_filter_machine(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get("/api/events", params={"machine": "machine-1"})
        assert fake_events.calls[0]["machine"] == "machine-1"

    def test_filter_level_lowercased_exact(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get("/api/events", params={"level": "WARNING"})
        assert fake_events.calls[0]["level"] == "warning"
        assert "level_min" not in fake_events.calls[0]

    def test_from_to_window_passed_through(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get(
                "/api/events",
                params={"from": "2026-08-01T00:00:00Z", "to": "2026-08-02T00:00:00Z"},
            )
        kw = fake_events.calls[0]
        assert kw["from_"] == datetime(2026, 8, 1, tzinfo=UTC)
        assert kw["to"] == datetime(2026, 8, 2, tzinfo=UTC)

    def test_hours_window(self, fake_events: _FakeEvents) -> None:
        before = datetime.now(UTC)
        with TestClient(app) as client:
            client.get("/api/events", params={"hours": 2})
        after = datetime.now(UTC)
        from_ = fake_events.calls[0]["from_"]
        assert from_ is not None
        assert (
            before - timedelta(hours=2, seconds=10)
            <= from_
            <= after - timedelta(hours=2, seconds=-10)
        )

    def test_default_window_is_24h(self, fake_events: _FakeEvents) -> None:
        before = datetime.now(UTC)
        with TestClient(app) as client:
            client.get("/api/events")
        after = datetime.now(UTC)
        from_ = fake_events.calls[0]["from_"]
        assert from_ is not None
        assert before - timedelta(hours=25) <= from_ <= after - timedelta(hours=23, seconds=59)

    def test_total_skipped_by_default(self, fake_events: _FakeEvents) -> None:
        """Without with_total the count path never runs — the only loki call
        is the list fetch, and meta.total is null."""
        fake_events.rows.extend([_row()])
        with TestClient(app) as client:
            r = client.get("/api/events")
        body = r.json()
        assert body["meta"]["total"] is None
        assert len(fake_events.calls) == 1  # query_events only, no count

    def test_meta_total_opt_in_comes_from_count(self, fake_events: _FakeEvents) -> None:
        fake_events.rows.extend([_row()])
        fake_events.totals.append(3)
        fake_events.has_more = True
        with TestClient(app) as client:
            r = client.get("/api/events", params={"limit": 1, "with_total": 1})
        body = r.json()
        assert body["meta"]["total"] == 3
        assert len(fake_events.calls) == 2  # count + query
        assert body["meta"]["has_more"] is True  # the fetch's +1 lookahead

    def test_has_more_from_lookahead(self, fake_events: _FakeEvents) -> None:
        """has_more echoes query_events' +1-lookahead result — no count
        aggregation involved."""
        fake_events.rows.extend([_row()])
        fake_events.has_more = True
        with TestClient(app) as client:
            r = client.get("/api/events")
        assert r.json()["meta"]["has_more"] is True

    def test_limit_offset_paging(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get("/api/events", params={"limit": 5, "offset": 10})
        kw = fake_events.calls[0]  # the query call (no count by default)
        assert kw["limit"] == 5
        assert kw["offset"] == 10

    def test_filters_compose_and(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            client.get(
                "/api/events",
                params={
                    "category": "telemetry",
                    "event_name": "llm_usage",
                    "agent_id": 7,
                    "machine": "machine-1",
                    "level": "info",
                },
            )
        kw = fake_events.calls[0]
        assert kw["categories"] == ["telemetry"]
        assert kw["event_names"] == ["llm_usage"]
        assert kw["agent_id"] == 7
        assert kw["machine"] == "machine-1"
        assert kw["level"] == "info"

    def test_no_match_returns_empty(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get("/api/events", params={"event_name": "does_not_exist"})
        body = r.json()
        assert body["items"] == []
        assert body["meta"]["total"] is None

    def test_invalid_category_422(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get("/api/events", params={"category": "bogus"})
        assert r.status_code == 422

    def test_invalid_level_422(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get("/api/events", params={"level": "fatal"})
        assert r.status_code == 422

    def test_invalid_from_timestamp_422(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get("/api/events", params={"from": "not-a-date"})
        assert r.status_code == 422

    def test_naive_from_timestamp_422(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get("/api/events", params={"from": "2026-08-04T00:00:00"})
        assert r.status_code == 422

    def test_naive_to_timestamp_422(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get("/api/events", params={"to": "2026-08-04T00:00:00"})
        assert r.status_code == 422

    def test_limit_over_max_422(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get("/api/events", params={"limit": 1001})
        assert r.status_code == 422

    def test_from_and_hours_conflict_422(self, fake_events: _FakeEvents) -> None:
        with TestClient(app) as client:
            r = client.get(
                "/api/events",
                params={"from": "2026-08-04T00:00:00Z", "hours": 1},
            )
        assert r.status_code == 422
