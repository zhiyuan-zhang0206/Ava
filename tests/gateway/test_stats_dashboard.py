"""Integration tests for GET /api/stats/dashboard HTTP.

Locks the query contract of the sidebar stats card — pyright/tsc cannot catch
drift between hard-coded `payload->>'X'` keys in SQL and emit site field names,
these tests are the only defense.

Runs on ava_test DB (real SQL) for the Postgres metadata read. The Loki-backed
event aggregates use `FakeLoki`; the `agents` table's live_count is similarly
populated via INSERT of real rows.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events, loki_query_budget
from gateway.app import app
from gateway.routers import status
from gateway.schemas import StatsWindowHours
from tests.gateway.loki_fake import FakeLoki


@pytest.fixture(autouse=True)
def fake_loki(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    """Route all loki_events calls through an in-memory fake; each test gets
    an empty store and adds its own rows."""
    fake = FakeLoki()
    monkeypatch.setattr(loki_events, "query_events", fake.query_events)
    monkeypatch.setattr(loki_events, "count_events", fake.count_events)
    monkeypatch.setattr(loki_events, "attribute_aggregate", fake.attribute_aggregate)
    return fake


def _insert_agent_row(db: psycopg.Connection, label: str = "t") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None, "INSERT ... RETURNING must return a row"
    return row[0]


def _insert_agent(db: psycopg.Connection, *, status: str = "running", spawner: str = "user") -> int:
    tid = _insert_agent_row(db, f"agent-{spawner}")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, %s, %s)",
            (tid, spawner, status),
        )
    return tid


def _insert_event(
    db: psycopg.Connection,
    *,
    event: str,
    level: str = "INFO",
    agent_id: int | None = None,
    payload: dict | None = None,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ts_offset_hours: float = 0,
    category: str = "telemetry",
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, level, event_name, attributes, machine, process, category, source) "
            "VALUES (now() - %s * interval '1 hour', %s, %s, %s, %s::jsonb, "
            "'test', 'test', %s, 'test')",
            (
                ts_offset_hours,
                agent_id,
                level.lower(),
                event,
                json.dumps(payload or {}),
                category,
            ),
        )


def test_dashboard_does_not_hold_db_connection_during_loki_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting/running under the global Loki budget must not consume a DB slot."""

    class FakeCursor:
        def __init__(self) -> None:
            self.rows = [(2,), (10,)]

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, query: str) -> None:
            return None

        def fetchone(self) -> tuple[int]:
            return self.rows.pop(0)

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_value = FakeCursor()

        def __enter__(self) -> FakeConnection:
            pool.active = True
            return self

        def __exit__(self, *args: object) -> None:
            pool.active = False

        def cursor(self) -> FakeCursor:
            return self.cursor_value

    class FakePool:
        active = False

        def connection(self) -> FakeConnection:
            return FakeConnection()

    pool = FakePool()

    def assert_db_released(*args: Any, **kwargs: Any) -> int:
        assert pool.active is False
        return 0

    monkeypatch.setattr(loki_events, "attribute_aggregate", assert_db_released)
    monkeypatch.setattr(loki_events, "count_events", assert_db_released)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    result = status.get_stats_dashboard(request, StatsWindowHours.H24)  # type: ignore[arg-type]
    assert result.live_count == 2
    assert result.total_events == 10


@pytest.mark.parametrize("reason", ["queue_full", "acquire_timeout"])
def test_dashboard_local_loki_budget_rejection_is_503(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    reason: Literal["queue_full", "acquire_timeout"],
) -> None:
    """Dashboard saturation uses the same retriable 503 wire contract."""

    def reject(*args: Any, **kwargs: Any) -> float:
        raise loki_query_budget.LokiQueryBudgetError(reason)

    monkeypatch.setattr(loki_events, "attribute_aggregate", reject)
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get("/api/stats/dashboard")
    assert response.status_code == 503
    assert response.json()["detail"] == f"Loki query budget unavailable ({reason}); retry"
    assert response.headers["retry-after"] == "1"


@pytest.mark.parametrize(
    ("loki_method", "error"),
    [
        ("attribute_aggregate", httpx.ConnectError("Loki disconnected")),
        ("count_events", httpx.ReadTimeout("Loki timed out")),
    ],
)
def test_dashboard_loki_transport_error_is_retriable_503(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    loki_method: Literal["attribute_aggregate", "count_events"],
    error: httpx.HTTPError,
) -> None:
    """Loki transport failures become the dashboard's typed retry response."""

    def unavailable(*args: Any, **kwargs: Any) -> float:
        raise error

    monkeypatch.setattr(loki_events, loki_method, unavailable)
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get("/api/stats/dashboard")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert type(error).__name__ in response.json()["detail"]


def test_dashboard_shards_long_loki_windows_and_merges_aggregates(
    db_conn: psycopg.Connection,
    fake_loki: FakeLoki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long dashboard windows sum exact, contiguous three-hour Loki shards."""
    aid = _insert_agent(db_conn, status="running")
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 10.0, "ok": True},
        ts_offset_hours=5,
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 2.0, "ok": True},
        ts_offset_hours=0.5,
    )
    fake_loki.add(event="log_warning", level="warning", ts_offset_hours=5)
    fake_loki.add(event="log_error", level="error", ts_offset_hours=0.5)
    fake_loki.add(
        event="llm_usage",
        payload={"in_total": 100, "out_total": 20, "cache_read": 50, "cost_usd": 1.5},
        ts_offset_hours=5,
    )
    fake_loki.add(
        event="llm_usage",
        payload={"in_total": 10, "out_total": 2, "cache_read": 5, "cost_usd": 0.5},
        ts_offset_hours=0.5,
    )

    aggregate_spans: list[tuple[datetime, datetime]] = []
    count_spans: list[tuple[datetime, datetime]] = []
    real_aggregate = fake_loki.attribute_aggregate
    real_count = fake_loki.count_events

    def spy_aggregate(**kwargs: Any) -> float | list[tuple[str, float]]:
        aggregate_spans.append((kwargs["from_"], kwargs["to"]))
        return real_aggregate(**kwargs)

    def spy_count(**kwargs: Any) -> int:
        count_spans.append((kwargs["from_"], kwargs["to"]))
        return real_count(**kwargs)

    monkeypatch.setattr(loki_events, "attribute_aggregate", spy_aggregate)
    monkeypatch.setattr(loki_events, "count_events", spy_count)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard", params={"hours": 6}).json()

    assert body["avg_turn_seconds"] == 6.0
    assert body["warnings"] == 1
    assert body["errors"] == 1
    assert body["tokens"] == {"input": 110, "output": 22, "cache_read": 55, "cache_hit_pct": 50}
    assert body["cost_usd"] == 2.0
    aggregate_counts = Counter(aggregate_spans)
    spans = sorted(aggregate_counts)
    assert len(spans) > 1
    assert sum((end - start for start, end in spans), timedelta()) == timedelta(hours=6)
    assert all(end - start <= timedelta(hours=3) for start, end in spans)
    assert all(end == next_start for (_, end), (next_start, _) in pairwise(spans))
    assert set(aggregate_counts.values()) == {5}
    assert Counter(count_spans) == Counter(dict.fromkeys(spans, 3))


def test_dashboard_empty_db_returns_zeros(db_conn: psycopg.Connection) -> None:
    """Empty events / empty agents_meta table → all counts 0, avg_turn_seconds None,
    cache_hit_pct 0, cost_usd 0 (no input → division-by-zero fallback). Without ?hours=
    window_hours echoes default 24."""
    db_conn.commit()  # let truncate take effect before starting client
    with TestClient(app) as client:
        resp = client.get("/api/stats/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live_count"] == 0
    assert body["window_hours"] == 24
    assert body["tokens"] == {"input": 0, "output": 0, "cache_read": 0, "cache_hit_pct": 0}
    assert body["cost_usd"] == 0
    assert body["avg_turn_seconds"] is None
    assert body["warnings"] == 0
    assert body["errors"] == 0
    assert body["total_events"] == 0


def test_dashboard_folds_critical_into_error_gauge(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """critical-level events count in the sidebar's error gauge — they used to
    be an observability blind spot (audit 2026-08-08: daemon schema-drift
    exits, restarter failures etc. never appeared in any warning/error
    count)."""
    fake_loki.add(event="turn_end", level="warning")
    fake_loki.add(event="turn_end", level="error")
    fake_loki.add(event="turn_end", level="critical")
    fake_loki.add(event="turn_end", level="info")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 1
    assert body["errors"] == 2  # error + critical folded


def test_dashboard_live_count_excludes_terminated(db_conn: psycopg.Connection) -> None:
    """live_count = all non-terminated agents, including hibernating / idling / restarting."""
    _insert_agent(db_conn, status="running")
    _insert_agent(db_conn, status="idling")
    _insert_agent(db_conn, status="terminated")
    _insert_agent(db_conn, status="idling")
    _insert_agent(db_conn, status="restarting")
    _insert_agent(db_conn, status="hibernating")
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/stats/dashboard")
    assert resp.json()["live_count"] == 5


def test_dashboard_aggregates_llm_usage_payload(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """Windowed LLM token fields sum the corresponding Loki payload values."""
    fake_loki.add(
        event="llm_usage",
        payload={"in_total": 1500, "out_total": 300, "cache_read": 1200, "cost_usd": 1.2},
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 1500
    assert body["tokens"]["output"] == 300
    assert body["tokens"]["cache_read"] == 1200
    # 1200 / 1500 = 80%
    assert body["tokens"]["cache_hit_pct"] == 80


def test_dashboard_cache_hit_pct_two_decimals(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """cache_hit_pct = cache_read / input * 100 with two decimal places (no longer integer truncation).
    1000 / 3000 = 33.333... → 33.33."""
    fake_loki.add(
        event="llm_usage",
        payload={"in_total": 3000, "out_total": 100, "cache_read": 1000, "cost_usd": 1.2},
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["cache_hit_pct"] == 33.33


def test_dashboard_cost_uses_usage_time_snapshots(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """Cost sums the event payload snapshots, regardless of model registry state."""
    fake_loki.add(
        event="llm_usage",
        payload={
            "model": "claude-opus-4-8",
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "cost_usd": 30.0,
        },
    )
    fake_loki.add(
        event="llm_usage",
        payload={
            "model": "retired-model",
            "in_total": 999,
            "out_total": 999,
            "cache_read": 0,
            "cost_usd": 7.25,
        },
    )
    fake_loki.add(
        event="llm_usage",
        category="log",
        payload={
            "model": "retired-log-model",
            "in_total": 1,
            "out_total": 1,
            "cache_read": 0,
            "cost_usd": 0.75,
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["cost_usd"] == pytest.approx(38.0)  # pyright: ignore[reportUnknownMemberType]


def test_dashboard_aggregates_turn_end_filters_ok(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """`event='turn_end'` AVG only includes ok=true (exclude cancelled / abnormal turns).
    Locks the ok field contract from _llm.py:llm_node finally."""
    aid = _insert_agent(db_conn, status="running")
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 2.0, "ok": True})
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 4.0, "ok": True})
    # ok=False abnormal turn of 100s must not enter AVG
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 100.0, "ok": False})
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    # AVG(2, 4) = 3.0
    assert body["avg_turn_seconds"] == 3.0


def test_dashboard_default_24h_window_filters_old_rows(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """Loki aggregates include only recent usage; the old DB row is metadata only."""
    aid = _insert_agent(db_conn, status="running")
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 999, "out_total": 999, "cache_read": 0, "cost_usd": 99.9},
        ts_offset_hours=25,
    )
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 10, "out_total": 5, "cache_read": 0, "cost_usd": 0.2},
        ts_offset_hours=1,
    )
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 999, "out_total": 999, "cache_read": 0},
        ts_offset_hours=25,
    )
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 10, "out_total": 5, "cache_read": 0},
        ts_offset_hours=1,
    )
    db_conn.commit()
    # total_events reads pg_class.reltuples (planner estimate), not a windowed
    # COUNT — it only refreshes on ANALYZE/autovacuum, so force ANALYZE to make
    # the estimate deterministic (= 2 on a freshly-truncated 2-row table).
    with db_conn.cursor() as cur:
        cur.execute("ANALYZE events")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 10
    assert body["tokens"]["output"] == 5
    # reltuples estimate counts all rows regardless of the 24h window; both rows
    # contribute, so post-ANALYZE total_events == 2.
    assert body["total_events"] == 2


def test_dashboard_warn_err_counts_by_level(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """Warning/error gauges count telemetry+log rows by level over the 24h
    window, read from Loki (task #1280 — the PG events read flatlines post-freeze)."""
    aid = _insert_agent(db_conn, status="running")
    fake_loki.add(event="some_warning", level="warning", agent_id=aid)
    fake_loki.add(event="some_warning", level="warning", agent_id=aid)
    fake_loki.add(event="some_error", level="error", agent_id=aid)
    # exec_failed logs at INFO (agent trial-and-error) — must not count
    fake_loki.add(event="exec_failed", level="info", agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 2
    assert body["errors"] == 1


def test_dashboard_audit_warning_not_counted(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """An audit-category WARNING row (an agent operation with a warning level —
    e.g. a mislabeled write) must NOT count toward the sidebar warning/error
    gauge: the query filters category IN (telemetry, log) (appendix scenario 4)."""
    aid = _insert_agent(db_conn, status="running")
    fake_loki.add(event="some_warning", level="warning", agent_id=aid)
    fake_loki.add(event="spawn", level="warning", agent_id=aid, category="audit")
    fake_loki.add(event="spawn", level="error", agent_id=aid, category="audit")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 1
    assert body["errors"] == 0


def test_dashboard_hours_param_selects_window(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """?hours= applies the same Loki window to every event aggregate."""
    aid = _insert_agent(db_conn, status="running")
    fake_loki.add(event="some_warning", level="warning", agent_id=aid, ts_offset_hours=3)
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 10.0, "ok": True},
        ts_offset_hours=3,
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 2.0, "ok": True},
        ts_offset_hours=0.5,
    )
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 100, "out_total": 50, "cache_read": 0, "cost_usd": 1.0},
        ts_offset_hours=3,
    )
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 10, "out_total": 5, "cache_read": 0, "cost_usd": 0.1},
        ts_offset_hours=0.5,
    )
    db_conn.commit()
    with TestClient(app) as client:
        narrow = client.get("/api/stats/dashboard", params={"hours": 1}).json()
        wide = client.get("/api/stats/dashboard", params={"hours": 6}).json()
    assert narrow["window_hours"] == 1
    assert narrow["tokens"]["input"] == 10
    assert narrow["tokens"]["output"] == 5
    assert narrow["warnings"] == 0
    assert narrow["avg_turn_seconds"] == 2.0
    assert wide["window_hours"] == 6
    assert wide["tokens"]["input"] == 110
    assert wide["tokens"]["output"] == 55
    assert wide["warnings"] == 1
    # AVG(10, 2) = 6.0
    assert wide["avg_turn_seconds"] == 6.0


def test_dashboard_all_whitelisted_hours_accepted(db_conn: psycopg.Connection) -> None:
    """All 5 whitelist values (1/6/24/72/168) return 200, window_hours echoed as-is."""
    db_conn.commit()
    with TestClient(app) as client:
        for hours in (1, 6, 24, 72, 168):
            resp = client.get("/api/stats/dashboard", params={"hours": hours})
            assert resp.status_code == 200
            assert resp.json()["window_hours"] == hours


@pytest.mark.parametrize("bad", ["0", "5", "-1", "25", "169", "abc", "24.5"])
def test_dashboard_invalid_hours_422(db_conn: psycopg.Connection, bad: str) -> None:
    """hours not in whitelist {1,6,24,72,168} → 422 (fail-fast), not silently fallback to 24."""
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/stats/dashboard", params={"hours": bad})
    assert resp.status_code == 422
