"""Integration tests for GET /api/stats/dashboard HTTP.

Locks the query contract of the sidebar stats card — pyright/tsc cannot catch
drift between hard-coded `payload->>'X'` keys in SQL and emit site field names,
these tests are the only defense.

Runs on ava_test DB (real SQL) for the Postgres metadata read. The Loki-backed
event aggregates use `FakeLoki`; the `agents` table's live_count is similarly
populated via INSERT of real rows.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events, loki_query_budget
from gateway.app import app
from gateway.routers import _stats_dashboard, status
from gateway.schemas import StatsWindowHours, window_delta
from shared.cluster import home_label
from shared.loki_index_labels import EVENT_STREAM_RETENTION, retention_floor
from shared.paths import ava_home
from tests.gateway.loki_fake import FakeLoki


@pytest.fixture(autouse=True)
def fake_loki(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    """Route all loki_events calls through an in-memory fake; each test gets
    an empty store and adds its own rows."""
    fake = FakeLoki()
    monkeypatch.setattr(loki_events, "query_events", fake.query_events)
    monkeypatch.setattr(loki_events, "count_events", fake.count_events)
    monkeypatch.setattr(loki_events, "count_grouped", fake.count_grouped)
    monkeypatch.setattr(loki_events, "count_event_classes", fake.count_event_classes)
    monkeypatch.setattr(loki_events, "attribute_aggregate", fake.attribute_aggregate)
    return fake


@pytest.fixture(autouse=True)
def clear_llm_usage_sums_cache() -> None:
    """Keep every FakeLoki test isolated from the route's 60-second cache."""
    _stats_dashboard.cache_clear()


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


def _insert_token_ledger_row(
    db: psycopg.Connection,
    *,
    agent_id: int,
    days_ago: int,
    model: str,
    tokens_in: int,
    tokens_out: int,
    tokens_cached: int,
    cost_usd: float,
) -> None:
    """Add one per-agent, per-model daily token rollup row."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_model_tokens_daily "
            "(agent_id, day, model, tokens_in, tokens_out, tokens_cached, cost_usd) "
            "VALUES (%s, (now() AT TIME ZONE 'UTC')::date - %s, %s, %s, %s, %s, %s)",
            (agent_id, days_ago, model, tokens_in, tokens_out, tokens_cached, cost_usd),
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

        def execute(self, query: str, params: object | None = None) -> None:
            return None

        def fetchone(self) -> tuple[int]:
            return self.rows.pop(0)

        def fetchall(self) -> list[tuple[int]]:
            # active dismissals read: no dismissal rows in the fake DB.
            return []

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_value = FakeCursor()

        def __enter__(self) -> FakeConnection:
            pool.active = True
            return self

        def __exit__(self, *args: object) -> None:
            pool.active = False

        def cursor(self, *args: object, **kwargs: object) -> FakeCursor:
            return self.cursor_value

    class FakePool:
        active = False

        def connection(self) -> FakeConnection:
            return FakeConnection()

    pool = FakePool()

    clusters: list[str | None] = []

    def assert_db_released(*args: Any, **kwargs: Any) -> int:
        assert pool.active is False
        clusters.append(kwargs.get("cluster"))
        return 0

    def assert_db_released_grouped(*args: Any, **kwargs: Any) -> dict[str, int]:
        assert pool.active is False
        clusters.append(kwargs.get("cluster"))
        return {}

    def assert_db_released_classes(*args: Any, **kwargs: Any) -> dict[object, int]:
        assert pool.active is False
        return {}

    monkeypatch.setattr(loki_events, "attribute_aggregate", assert_db_released)
    monkeypatch.setattr(loki_events, "count_events", assert_db_released)
    monkeypatch.setattr(loki_events, "count_grouped", assert_db_released_grouped)
    monkeypatch.setattr(loki_events, "count_event_classes", assert_db_released_classes)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    result = status.get_stats_dashboard(request, StatsWindowHours.H24)  # type: ignore[arg-type]
    assert result.live_count == 2
    assert result.total_events == status.ARCHIVE_TOTAL_ROWS
    assert result.warnings == 0
    assert result.warnings_dismissed == 0
    assert clusters
    assert set(clusters) == {home_label(ava_home())}


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


def test_observability_read_unavailable_is_clean_503(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = (
        "observability reads unavailable for this cluster; set "
        "AVA_TELEMETRY_LOKI_URL and provide its stack, or accept that this "
        "cluster has no observability"
    )

    def unavailable(*_args: Any, **_kwargs: Any) -> float:
        raise loki_events.ObservabilityReadUnavailable(message)

    monkeypatch.setattr(loki_events, "attribute_aggregate", unavailable)
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get("/api/stats/dashboard")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "observability_read_unavailable"
    assert body["detail"] == message
    assert body["retryable"] is True


def test_dashboard_shards_long_loki_windows_and_merges_aggregates(
    db_conn: psycopg.Connection,
    fake_loki: FakeLoki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turn reads use 12h shards; each W/E shard has one grouped query."""
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

    llm_usage_windows: list[tuple[datetime, datetime]] = []
    sharded_aggregate_spans: list[tuple[datetime, datetime]] = []
    count_spans: list[tuple[datetime, datetime]] = []
    class_spans: list[tuple[datetime, datetime]] = []
    real_aggregate = fake_loki.attribute_aggregate
    real_count = fake_loki.count_events
    real_classes = fake_loki.count_event_classes

    def spy_aggregate(**kwargs: Any) -> float | list[tuple[str, float]]:
        spans = (
            llm_usage_windows
            if kwargs.get("event_names") == ["llm_usage"]
            else sharded_aggregate_spans
        )
        spans.append((kwargs["from_"], kwargs["to"]))
        return real_aggregate(**kwargs)

    def spy_count(**kwargs: Any) -> int:
        count_spans.append((kwargs["from_"], kwargs["to"]))
        return real_count(**kwargs)

    def spy_classes(**kwargs: Any) -> dict[object, int]:
        class_spans.append((kwargs["from_"], kwargs["to"]))
        return real_classes(**kwargs)

    monkeypatch.setattr(loki_events, "attribute_aggregate", spy_aggregate)
    monkeypatch.setattr(loki_events, "count_events", spy_count)
    monkeypatch.setattr(loki_events, "count_event_classes", spy_classes)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard", params={"hours": 24}).json()

    assert body["avg_turn_seconds"] == 6.0
    assert body["warnings"] == 1
    assert body["warnings_dismissed"] == 0
    assert body["warnings_net"] == 1
    assert body["errors"] == 1
    assert body["errors_dismissed"] == 0
    assert body["errors_net"] == 1
    assert body["tokens"] == {"input": 110, "output": 22, "cache_read": 55, "cache_hit_pct": 50}
    assert body["cost_usd"] == 2.0
    assert len(llm_usage_windows) == 4
    assert len(set(llm_usage_windows)) == 1
    assert llm_usage_windows[0][1] - llm_usage_windows[0][0] == timedelta(hours=24)
    aggregate_counts = Counter(sharded_aggregate_spans)
    spans = sorted(aggregate_counts)
    assert len(spans) > 1
    assert sum((end - start for start, end in spans), timedelta()) == timedelta(hours=24)
    assert all(end - start <= timedelta(hours=12) for start, end in spans)
    assert all(end == next_start for (_, end), (next_start, _) in pairwise(spans))
    assert set(aggregate_counts.values()) == {1}
    assert Counter(count_spans) == aggregate_counts
    # The W/E class read shares the same 12-hour shard grid as turn/exec.
    assert Counter(class_spans) == aggregate_counts


def test_dashboard_168h_reads_tokens_from_ledger(
    db_conn: psycopg.Connection,
    fake_loki: FakeLoki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 168h request reports and serves the shorter Loki retention horizon."""
    first_agent = _insert_agent(db_conn)
    second_agent = _insert_agent(db_conn)
    for days_ago, model, agent_id, tokens_in, tokens_out, tokens_cached, cost_usd in (
        (2, "model-a", first_agent, 100, 10, 50, 1.0),
        (2, "model-b", second_agent, 200, 20, 25, 2.0),
        (2, "model-c", first_agent, 300, 30, 100, 3.0),
    ):
        _insert_token_ledger_row(
            db_conn,
            agent_id=agent_id,
            days_ago=days_ago,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=tokens_cached,
            cost_usd=cost_usd,
        )
    # Zero newest-day row establishes the retained live reread seam.
    _insert_token_ledger_row(
        db_conn,
        agent_id=first_agent,
        days_ago=1,
        model="model-a",
        tokens_in=0,
        tokens_out=0,
        tokens_cached=0,
        cost_usd=0.0,
    )
    fake_loki.add(
        event="llm_usage",
        payload={"in_total": 20, "out_total": 2, "cache_read": 5, "cost_usd": 0.5},
        ts_offset_hours=1,
    )
    fake_loki.add(
        event="turn_end",
        payload={"duration_seconds": 100.0, "ok": True},
        ts_offset_hours=100,
    )
    fake_loki.add(event="old_warning", level="warning", ts_offset_hours=100)
    llm_usage_spans: list[tuple[datetime, datetime]] = []
    loki_froms: list[datetime] = []
    real_aggregate = fake_loki.attribute_aggregate
    real_count = fake_loki.count_events
    real_classes = fake_loki.count_event_classes

    def spy_aggregate(**kwargs: Any) -> float | list[tuple[str, float]]:
        loki_froms.append(kwargs["from_"])
        if kwargs.get("event_names") == ["llm_usage"]:
            llm_usage_spans.append((kwargs["from_"], kwargs["to"]))
        return real_aggregate(**kwargs)

    def spy_count(**kwargs: Any) -> int:
        loki_froms.append(kwargs["from_"])
        return real_count(**kwargs)

    def spy_classes(**kwargs: Any) -> dict[object, int]:
        loki_froms.append(kwargs["from_"])
        return real_classes(**kwargs)

    monkeypatch.setattr(loki_events, "attribute_aggregate", spy_aggregate)
    monkeypatch.setattr(loki_events, "count_events", spy_count)
    monkeypatch.setattr(loki_events, "count_event_classes", spy_classes)
    db_conn.commit()
    floor_before = retention_floor()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard", params={"hours": 168}).json()
    floor_after = retention_floor()

    expected_hours = int(EVENT_STREAM_RETENTION.total_seconds() // 3600)
    assert body["window_hours"] == 168
    assert body["applied_window_hours"] == expected_hours
    assert body["tokens"] == {"input": 620, "output": 62, "cache_read": 180, "cache_hit_pct": 29.03}
    assert body["cost_usd"] == pytest.approx(6.5)  # pyright: ignore[reportUnknownMemberType]
    assert body["avg_turn_seconds"] is None
    assert body["warnings"] == 0
    assert floor_before <= min(loki_froms) <= floor_after
    assert len(llm_usage_spans) == 8
    assert len(set(llm_usage_spans)) == 2
    assert all(end - start < EVENT_STREAM_RETENTION for start, end in llm_usage_spans)


def test_dashboard_72h_uses_ledger_and_tail_seam(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """The newest retained ledger day is reread live, not double counted."""
    aid = _insert_agent(db_conn)
    _insert_token_ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=2,
        model="model-a",
        tokens_in=100,
        tokens_out=10,
        tokens_cached=40,
        cost_usd=1.0,
    )
    _insert_token_ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=1,
        model="model-a",
        tokens_in=500,
        tokens_out=50,
        tokens_cached=200,
        cost_usd=5.0,
    )
    # A live event inside the newest retained ledger day's tail span
    # ([yesterday 00:00Z, now]). Any offset in (0, 24h] stays inside that
    # span at every wall-clock time; 30h crossed the UTC-day boundary
    # during 00:00-06:00Z and the event fell out of the tail (time-flaky).
    fake_loki.add(
        event="llm_usage",
        payload={"in_total": 150, "out_total": 15, "cache_read": 60, "cost_usd": 1.5},
        ts_offset_hours=12,
    )
    db_conn.commit()

    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard", params={"hours": 72}).json()

    assert body["applied_window_hours"] == 72
    assert body["tokens"] == {"input": 250, "output": 25, "cache_read": 100, "cache_hit_pct": 40}
    assert body["cost_usd"] == pytest.approx(2.5)  # pyright: ignore[reportUnknownMemberType]


def test_dashboard_24h_stays_pure_loki(
    db_conn: psycopg.Connection,
    fake_loki: FakeLoki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-day window has no complete UTC day and performs no ledger sum."""
    fake_loki.add(
        event="llm_usage",
        payload={"in_total": 10, "out_total": 5, "cache_read": 4, "cost_usd": 0.25},
        ts_offset_hours=1,
    )

    def unexpected_ledger_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("24h dashboard windows must not read the token ledger")

    monkeypatch.setattr(_stats_dashboard, "ledger_token_sums", unexpected_ledger_read)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard", params={"hours": 24}).json()

    assert body["applied_window_hours"] == 24
    assert body["tokens"] == {"input": 10, "output": 5, "cache_read": 4, "cache_hit_pct": 40}
    assert body["cost_usd"] == pytest.approx(0.25)  # pyright: ignore[reportUnknownMemberType]


def test_token_window_plan_24h_at_midnight_stays_pure_loki() -> None:
    """A UTC-aligned 24-hour window has no ledger-safe settled day."""
    now = datetime(2026, 8, 25, tzinfo=UTC)

    assert _stats_dashboard.token_window_plan(now - timedelta(hours=24), now) == (
        None,
        None,
        [(now - timedelta(hours=24), now)],
    )


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
    # total_events is the frozen archive's historical constant, not a live count.
    assert body["total_events"] == status.ARCHIVE_TOTAL_ROWS


def test_dashboard_warn_error_folds_critical(
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


def _insert_dismissal(
    db: psycopg.Connection,
    *,
    level: str,
    event_name: str,
    category: str = "telemetry",
    source: str = "test",
) -> None:
    """Insert one active class-wide dismissal (the event_dismissals row the
    resolution daemon and the dashboard both subtract)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO event_dismissals (category, level, event_name, source, dismissed_by) "
            "VALUES (%s, %s, %s, %s, 0)",
            (category, level, event_name, source),
        )
    db.commit()


def test_dashboard_three_way_resolution_split(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """The dashboard carries total / dismissed / net per level; dismissed
    classes are cancelled from net exactly like the resolution daemon's
    gauges (task #1935)."""
    _insert_dismissal(db_conn, level="warning", event_name="dismissed_warning")
    _insert_dismissal(db_conn, level="error", event_name="dismissed_error")
    fake_loki.add(event="dismissed_warning", level="warning")
    fake_loki.add(event="dismissed_warning", level="warning")
    fake_loki.add(event="remaining_warning", level="warning")
    fake_loki.add(event="dismissed_error", level="error")
    fake_loki.add(event="remaining_critical", level="critical")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 3
    assert body["warnings_dismissed"] == 2
    assert body["warnings_net"] == 1
    # critical folds into error for both the total and the split.
    assert body["errors"] == 2
    assert body["errors_dismissed"] == 1
    assert body["errors_net"] == 1
    # The three-way split sums back to the raw totals by construction.
    assert body["warnings_dismissed"] + body["warnings_net"] == body["warnings"]
    assert body["errors_dismissed"] + body["errors_net"] == body["errors"]


def test_dashboard_all_dismissed_level_reports_zero_net(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """Every in-window warning class dismissed -> warnings_net 0 (the
    frontend's all-clear state); error side stays untouched."""
    _insert_dismissal(db_conn, level="warning", event_name="only_warning")
    fake_loki.add(event="only_warning", level="warning")
    fake_loki.add(event="only_warning", level="warning")
    fake_loki.add(event="only_error", level="error")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 2
    assert body["warnings_dismissed"] == 2
    assert body["warnings_net"] == 0
    assert body["errors"] == 1
    assert body["errors_dismissed"] == 0
    assert body["errors_net"] == 1


def test_dashboard_reopened_dismissal_counts_as_net(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """A dismissal flipped to reopened (burst safety valve) no longer
    cancels its class — same active-set semantics as the daemon."""
    _insert_dismissal(db_conn, level="warning", event_name="burst_warning")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE event_dismissals SET status = 'reopened', reopened_at = now() "
            "WHERE event_name = 'burst_warning'"
        )
    db_conn.commit()
    fake_loki.add(event="burst_warning", level="warning")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 1
    assert body["warnings_dismissed"] == 0
    assert body["warnings_net"] == 1


def test_dashboard_per_agent_dismissal_has_no_arithmetic_effect(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """v1 rejects per-agent dismissals; a manually inserted agent-scoped row
    must not subtract from the class-wide aggregate (resolution daemon
    contract)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO event_dismissals "
            "(category, level, event_name, source, agent_id, dismissed_by) "
            "VALUES ('telemetry', 'warning', 'agent_warning', 'test', 7, 0)"
        )
    db_conn.commit()
    fake_loki.add(event="agent_warning", level="warning")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 1
    assert body["warnings_dismissed"] == 0
    assert body["warnings_net"] == 1


def test_dashboard_live_count_excludes_terminated(db_conn: psycopg.Connection) -> None:
    """live_count = all non-terminated agents, including idling / restarting."""
    _insert_agent(db_conn, status="running")
    _insert_agent(db_conn, status="idling")
    _insert_agent(db_conn, status="terminated")
    _insert_agent(db_conn, status="idling")
    _insert_agent(db_conn, status="restarting")
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/stats/dashboard")
    assert resp.json()["live_count"] == 4


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
    """Cost sums telemetry snapshots, regardless of model registry state."""
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
    # The matching log event is excluded: llm_usage is telemetry-only in both
    # the status card and Grafana panels.
    assert body["cost_usd"] == pytest.approx(37.25)  # pyright: ignore[reportUnknownMemberType]


def test_dashboard_caches_llm_usage_sums_per_window(
    db_conn: psycopg.Connection, fake_loki: FakeLoki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 30s sidebar poll reuses only its matching window's four Loki sums."""
    assert _stats_dashboard._CACHE_TTL_S == 60.0
    fake_loki.add(
        event="llm_usage",
        payload={"in_total": 10, "out_total": 5, "cache_read": 0, "cost_usd": 1.0},
    )
    aggregate_calls = 0
    real_aggregate = fake_loki.attribute_aggregate

    def spy_aggregate(**kwargs: Any) -> float | list[tuple[str, float]]:
        nonlocal aggregate_calls
        if kwargs.get("event_names") == ["llm_usage"]:
            aggregate_calls += 1
        return real_aggregate(**kwargs)

    monkeypatch.setattr(loki_events, "attribute_aggregate", spy_aggregate)
    db_conn.commit()
    with TestClient(app) as client:
        assert client.get("/api/stats/dashboard").json()["cost_usd"] == 1.0
        initial_calls = aggregate_calls
        fake_loki.add(
            event="llm_usage",
            payload={"in_total": 20, "out_total": 10, "cache_read": 0, "cost_usd": 2.0},
        )
        assert client.get("/api/stats/dashboard").json()["cost_usd"] == 1.0
        assert aggregate_calls == initial_calls
        assert client.get("/api/stats/dashboard", params={"hours": 6}).json()["cost_usd"] == 3.0
        window_calls = aggregate_calls
        assert window_calls > initial_calls
        _stats_dashboard.cache_clear()
        assert client.get("/api/stats/dashboard").json()["cost_usd"] == 3.0
    assert aggregate_calls > window_calls


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
    """Loki aggregates include only recent usage (the PG events table is gone
    since the #1823 cleanup — the stream lives in Loki)."""
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
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 10
    assert body["tokens"]["output"] == 5
    # total_events is the frozen archive's historical constant, not a live count.
    assert body["total_events"] == status.ARCHIVE_TOTAL_ROWS


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


def test_five_minute_window_delta() -> None:
    assert window_delta(StatsWindowHours.M5) == timedelta(minutes=5)


def test_dashboard_all_whitelisted_hours_accepted(db_conn: psycopg.Connection) -> None:
    """All 6 whitelist values (0/1/6/24/72/168) return 200, window_hours echoed as-is."""
    db_conn.commit()
    with TestClient(app) as client:
        for hours in (0, 1, 6, 24, 72, 168):
            resp = client.get("/api/stats/dashboard", params={"hours": hours})
            assert resp.status_code == 200
            assert resp.json()["window_hours"] == hours


@pytest.mark.parametrize("bad", ["5", "-1", "25", "169", "abc", "24.5"])
def test_dashboard_invalid_hours_422(db_conn: psycopg.Connection, bad: str) -> None:
    """hours outside {0,1,6,24,72,168} → 422 (fail-fast), not silently fallback to 24."""
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/stats/dashboard", params={"hours": bad})
    assert resp.status_code == 422
