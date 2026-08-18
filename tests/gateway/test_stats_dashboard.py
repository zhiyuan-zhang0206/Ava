"""Integration tests for GET /api/stats/dashboard HTTP.

Locks the query contract of the sidebar stats card — pyright/tsc cannot catch
drift between hard-coded `payload->>'X'` keys in SQL and emit site field names,
these tests are the only defense.

Runs on ava_test DB (real SQL); directly INSERT `events` rows to simulate
endpoint aggregation after emit. The `agents` table's live_count is similarly
populated via INSERT of real rows. The token + cost block reads Prometheus
(task #1197) and is faked via prom_metrics (an autouse fixture installs an
empty fake; tests that need token values re-install a richer one over it).
"""

from __future__ import annotations

import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events, prom_metrics
from gateway.app import app
from tests.gateway.loki_fake import FakeLoki

# The OTLP-mapped llm_usage counters the dashboard reads (must match
# shared/telemetry_otlp._record_metrics + gateway/routers/status.py).
_IN_METRIC = "ava_llm_usage_in_total"
_OUT_METRIC = "ava_llm_usage_out_total"
_CACHED_METRIC = "ava_llm_usage_cache_read_total"


@pytest.fixture(autouse=True)
def fake_loki(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    """Route all loki_events calls through an in-memory fake; each test gets
    an empty store and adds its own rows."""
    fake = FakeLoki()
    monkeypatch.setattr(loki_events, "query_events", fake.query_events)
    monkeypatch.setattr(loki_events, "count_events", fake.count_events)
    monkeypatch.setattr(loki_events, "attribute_aggregate", fake.attribute_aggregate)
    return fake


@pytest.fixture(autouse=True)
def _mock_prom(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prometheus is not part of the test DB — every dashboard test fakes
    prom_metrics.sum_by; the default fake has no series (all zeros)."""

    def fake_sum_by(metric: str, by: str, *, window_hours: int | None = None) -> dict[str, float]:
        return {}

    monkeypatch.setattr(prom_metrics, "sum_by", fake_sum_by)


def _install_prom(
    monkeypatch: pytest.MonkeyPatch, *, windowed: dict[str, dict[str, float]]
) -> None:
    """Richer fake: metric -> {model: value}, returned for every window."""

    def fake_sum_by(metric: str, by: str, *, window_hours: int | None = None) -> dict[str, float]:
        return windowed.get(metric, {})

    monkeypatch.setattr(prom_metrics, "sum_by", fake_sum_by)


def _install_prom_per_window(
    monkeypatch: pytest.MonkeyPatch, per_window: dict[int, dict[str, dict[str, float]]]
) -> None:
    """Fake keyed by window_hours — for the `?hours=` window-switch test."""

    def fake_sum_by(metric: str, by: str, *, window_hours: int | None = None) -> dict[str, float]:
        return per_window.get(window_hours or 0, {}).get(metric, {})

    monkeypatch.setattr(prom_metrics, "sum_by", fake_sum_by)


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
    """live_count = all non-terminated agents, including hibernating / allocated / restarting."""
    _insert_agent(db_conn, status="running")
    _insert_agent(db_conn, status="idling")
    _insert_agent(db_conn, status="terminated")
    _insert_agent(db_conn, status="allocated")
    _insert_agent(db_conn, status="restarting")
    _insert_agent(db_conn, status="hibernating")
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/stats/dashboard")
    assert resp.json()["live_count"] == 5


def test_dashboard_aggregates_llm_usage_payload(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windowed per-model sums from the Prometheus counters -> token totals.
    Locks the metric-name contract: ava_llm_usage_{in,out,cache_read}_total
    feed the three token fields. If the OTLP mapper drifts the names, these
    mocks stop matching the route's constants and the test fails."""
    _install_prom(
        monkeypatch,
        windowed={
            _IN_METRIC: {"deepseek-v4-pro": 1500.0},
            _OUT_METRIC: {"deepseek-v4-pro": 300.0},
            _CACHED_METRIC: {"deepseek-v4-pro": 1200.0},
        },
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
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cache_hit_pct = cache_read / input * 100 with two decimal places (no longer integer truncation).
    1000 / 3000 = 33.333... → 33.33."""
    _install_prom(
        monkeypatch,
        windowed={
            _IN_METRIC: {"deepseek-v4-pro": 3000.0},
            _OUT_METRIC: {"deepseek-v4-pro": 100.0},
            _CACHED_METRIC: {"deepseek-v4-pro": 1000.0},
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["cache_hit_pct"] == 33.33


def test_dashboard_cost_aggregates_per_model(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windowed cost grouped by the model label; each group is priced via
    cost_usd (3-tier) and the per-group costs sum. A missing model label
    (pre-model-tracking events) groups under "" and an unpriced model both
    contribute 0 (unknown cost is treated as 0, not free)."""
    # claude-opus-4-8 (5, 5, 25) USD/M: in=1M out=1M cache=0 →
    # (1M*5 + 0 + 1M*25)/1e6 = 30.0
    # mimo-v2.5-pro (0.435, 0.0036, 0.87): in=1M out=1M cache=1M (full hit) →
    # (0 + 1M*0.0036 + 1M*0.87)/1e6 = 0.8736
    _install_prom(
        monkeypatch,
        windowed={
            _IN_METRIC: {
                "claude-opus-4-8": 1_000_000.0,
                "mimo-v2.5-pro": 1_000_000.0,
                "no-such-model": 999.0,
                "": 999.0,
            },
            _OUT_METRIC: {
                "claude-opus-4-8": 1_000_000.0,
                "mimo-v2.5-pro": 1_000_000.0,
                "no-such-model": 999.0,
                "": 999.0,
            },
            _CACHED_METRIC: {
                "claude-opus-4-8": 0.0,
                "mimo-v2.5-pro": 1_000_000.0,
                "no-such-model": 0.0,
                "": 0.0,
            },
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["cost_usd"] == pytest.approx(30.0 + 0.8736)  # pyright: ignore[reportUnknownMemberType]


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
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Prometheus side applies the window (increase over [24h]): the
    mocked windowed counters only carry the recent increment, so tokens read
    10/5 — the old row contributes to total_events only."""
    aid = _insert_agent(db_conn, status="running")
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
    _install_prom(
        monkeypatch,
        windowed={
            _IN_METRIC: {"deepseek-v4-pro": 10.0},
            _OUT_METRIC: {"deepseek-v4-pro": 5.0},
            _CACHED_METRIC: {},
        },
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
    db_conn: psycopg.Connection, fake_loki: FakeLoki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?hours= switches window: the Prometheus mock returns different
    windowed sums per hours, while warnings / avg_turn read the same window
    from Loki — the two sources share the same hours parameter."""
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
    # 3h-old increment is outside hours=1, inside hours=6 — the Prometheus side
    # applies the window; the route just forwards `hours` into the PromQL.
    _install_prom_per_window(
        monkeypatch,
        {
            1: {
                _IN_METRIC: {"deepseek-v4-pro": 10.0},
                _OUT_METRIC: {"deepseek-v4-pro": 5.0},
                _CACHED_METRIC: {},
            },
            6: {
                _IN_METRIC: {"deepseek-v4-pro": 110.0},
                _OUT_METRIC: {"deepseek-v4-pro": 55.0},
                _CACHED_METRIC: {},
            },
        },
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
