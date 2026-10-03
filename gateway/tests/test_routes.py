"""Integration tests for GET /api/stats/dashboard HTTP.

Locks the query contract of the sidebar stats card — pyright/tsc cannot catch
drift between hard-coded `payload->>'X'` keys in SQL and emit site field names,
these tests are the only defense.

Runs on ava_test DB (real SQL): the window's telemetry rows are INSERTed into
`telemetry_events` (audit rows into `audit_events`), and the `agents` table's
live_count is populated via INSERT of real rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.db import Database
from base.packages.plugins import stats
from base.telemetry.observability import cluster_label
from gateway.app import app
from gateway.cluster import status
from gateway.schemas.stats import StatsWindowHours, window_delta


class _EventRows:
    """Writes the events a test names into the tables the dashboard reads."""

    def __init__(self, db: psycopg.Connection) -> None:
        self._db = db

    def add(
        self,
        *,
        event: str,
        agent_id: int | None = None,
        level: str = "info",
        payload: dict[str, Any] | None = None,
        ts_offset_hours: float = 0,
        ts: datetime | None = None,
        category: str = "telemetry",
        cluster: str | None = None,
    ) -> None:
        when = ts if ts is not None else datetime.now(UTC) - timedelta(hours=ts_offset_hours)
        uid = uuid.uuid4().int % (1 << 62)
        attributes = json.dumps(payload or {})
        if category == "audit":
            self._db.execute(
                "INSERT INTO audit_events (event_uid, ts, agent_id, machine, process, event_name, "
                "level, source, attributes) VALUES (%s, %s, %s, 'test', 'test', %s, %s, 'test', "
                "%s::jsonb)",
                (uid, when, agent_id, event, level, attributes),
            )
            return
        self._db.execute(
            "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
            "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'test', %s, "
            "'test', %s, %s, %s, 'test', %s::jsonb)",
            (uid, when, agent_id, cluster or cluster_label(), category, event, level, attributes),
        )


@pytest.fixture
def event_rows(db_conn: psycopg.Connection) -> _EventRows:
    """The window's event rows: each test adds its own."""
    return _EventRows(db_conn)


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
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """critical-level events count in the sidebar's error gauge — they used to
    be an observability blind spot (audit 2026-08-08: daemon schema-drift
    exits, restarter failures etc. never appeared in any warning/error
    count)."""
    event_rows.add(event="turn_end", level="warning")
    event_rows.add(event="turn_end", level="error")
    event_rows.add(event="turn_end", level="critical")
    event_rows.add(event="turn_end", level="info")
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
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """The dashboard carries total / dismissed / net per level; dismissed
    classes are cancelled from net exactly like the resolution daemon's
    gauges (task #1935)."""
    _insert_dismissal(db_conn, level="warning", event_name="dismissed_warning")
    _insert_dismissal(db_conn, level="error", event_name="dismissed_error")
    event_rows.add(event="dismissed_warning", level="warning")
    event_rows.add(event="dismissed_warning", level="warning")
    event_rows.add(event="remaining_warning", level="warning")
    event_rows.add(event="dismissed_error", level="error")
    event_rows.add(event="remaining_critical", level="critical")
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
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """Every in-window warning class dismissed -> warnings_net 0 (the
    frontend's all-clear state); error side stays untouched."""
    _insert_dismissal(db_conn, level="warning", event_name="only_warning")
    event_rows.add(event="only_warning", level="warning")
    event_rows.add(event="only_warning", level="warning")
    event_rows.add(event="only_error", level="error")
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
    db_conn: psycopg.Connection, event_rows: _EventRows
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
    event_rows.add(event="burst_warning", level="warning")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 1
    assert body["warnings_dismissed"] == 0
    assert body["warnings_net"] == 1


def test_dashboard_per_agent_dismissal_has_no_arithmetic_effect(
    db_conn: psycopg.Connection, event_rows: _EventRows
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
    event_rows.add(event="agent_warning", level="warning")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 1
    assert body["warnings_dismissed"] == 0
    assert body["warnings_net"] == 1


def test_dashboard_live_count_excludes_terminated(db_conn: psycopg.Connection) -> None:
    """live_count = all non-terminated agents, running and idling."""
    _insert_agent(db_conn, status="running")
    _insert_agent(db_conn, status="idling")
    _insert_agent(db_conn, status="terminated")
    _insert_agent(db_conn, status="idling")
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/stats/dashboard")
    assert resp.json()["live_count"] == 3


def test_dashboard_aggregates_llm_usage_payload(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """Windowed LLM token fields sum the corresponding Loki payload values."""
    event_rows.add(
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
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """cache_hit_pct = cache_read / input * 100 with two decimal places (no longer integer truncation).
    1000 / 3000 = 33.333... → 33.33."""
    event_rows.add(
        event="llm_usage",
        payload={"in_total": 3000, "out_total": 100, "cache_read": 1000, "cost_usd": 1.2},
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["cache_hit_pct"] == 33.33


def test_dashboard_cost_uses_usage_time_snapshots(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """Cost sums telemetry snapshots, regardless of model registry state."""
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "claude-opus-4-8",
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "cost_usd": 30.0,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "retired-model",
            "in_total": 999,
            "out_total": 999,
            "cache_read": 0,
            "cost_usd": 7.25,
        },
    )
    event_rows.add(
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


def test_dashboard_aggregates_turn_end_filters_ok(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """`event='turn_end'` AVG only includes ok=true (exclude cancelled / abnormal turns).
    Locks the ok field contract from _llm.py:llm_node finally."""
    aid = _insert_agent(db_conn, status="running")
    event_rows.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 2.0, "ok": True})
    event_rows.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 4.0, "ok": True})
    # ok=False abnormal turn of 100s must not enter AVG
    event_rows.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 100.0, "ok": False})
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    # AVG(2, 4) = 3.0
    assert body["avg_turn_seconds"] == 3.0


def test_dashboard_default_24h_window_filters_old_rows(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """Loki aggregates include only recent usage (the PG events table is gone
    since the #1823 cleanup — the stream lives in Loki)."""
    aid = _insert_agent(db_conn, status="running")
    event_rows.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 999, "out_total": 999, "cache_read": 0, "cost_usd": 99.9},
        ts_offset_hours=25,
    )
    event_rows.add(
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
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """Warning/error gauges count telemetry+log rows by level over the 24h
    window, read from Loki (task #1280 — the PG events read flatlines post-freeze)."""
    aid = _insert_agent(db_conn, status="running")
    event_rows.add(event="some_warning", level="warning", agent_id=aid)
    event_rows.add(event="some_warning", level="warning", agent_id=aid)
    event_rows.add(event="some_error", level="error", agent_id=aid)
    # exec_failed logs at INFO (agent trial-and-error) — must not count
    event_rows.add(event="exec_failed", level="info", agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 2
    assert body["errors"] == 1


def test_dashboard_audit_warning_not_counted(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """An audit-category WARNING row (an agent operation with a warning level —
    e.g. a mislabeled write) must NOT count toward the sidebar warning/error
    gauge: the query filters category IN (telemetry, log) (appendix scenario 4)."""
    aid = _insert_agent(db_conn, status="running")
    event_rows.add(event="some_warning", level="warning", agent_id=aid)
    event_rows.add(event="spawn", level="warning", agent_id=aid, category="audit")
    event_rows.add(event="spawn", level="error", agent_id=aid, category="audit")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["warnings"] == 1
    assert body["errors"] == 0


def test_dashboard_hours_param_selects_window(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """?hours= applies the same Loki window to every event aggregate."""
    aid = _insert_agent(db_conn, status="running")
    event_rows.add(event="some_warning", level="warning", agent_id=aid, ts_offset_hours=3)
    event_rows.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 10.0, "ok": True},
        ts_offset_hours=3,
    )
    event_rows.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 2.0, "ok": True},
        ts_offset_hours=0.5,
    )
    event_rows.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 100, "out_total": 50, "cache_read": 0, "cost_usd": 1.0},
        ts_offset_hours=3,
    )
    event_rows.add(
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


# ── plugin stat values (task #2911) ────────────────────────────────────


def test_dashboard_carries_plugin_stat_values_unwindowed(database: Database) -> None:
    """The runtime half of collected plugin cards rides this response: every
    row, its status, and its freshness metadata. Not windowed — the plugin's
    value is a point-in-time fact and the window selector must not pretend to
    aggregate it. Declarations are joined by the console from
    /api/ui/contributions on (plugin, id)."""
    stats.upsert(
        database,
        plugin="codex_usage",
        id="codex-zhang0206",
        value="6%",
        detail="94% used - weekly",
        status="warn",
        updated_by="macmini",
    )
    stats.upsert(
        database,
        plugin="codex_usage",
        id="codex-wuji",
        value="!",
        detail="token revoked",
        status="error",
        updated_by="macmini",
    )

    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard", params={"hours": 6}).json()

    rows = body["plugin_stats"]
    assert [
        (r["plugin"], r["id"], r["value"], r["detail"], r["status"], r["updated_by"]) for r in rows
    ] == [
        ("codex_usage", "codex-wuji", "!", "token revoked", "error", "macmini"),
        ("codex_usage", "codex-zhang0206", "6%", "94% used - weekly", "warn", "macmini"),
    ]
    assert all(r["updated_at"] for r in rows)


def test_dashboard_plugin_stats_is_empty_without_writers() -> None:
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["plugin_stats"] == []


def test_dashboard_week_window_reads_the_whole_week_without_a_retention_clamp(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    event_rows.add(
        event="llm_usage", payload={"in_total": 10, "out_total": 5}, ts_offset_hours=24 * 6
    )
    event_rows.add(
        event="llm_usage", payload={"in_total": 100, "out_total": 50}, ts_offset_hours=24 * 8
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard", params={"hours": 168}).json()
    assert body["applied_window_hours"] == 168
    assert body["tokens"]["input"] == 10
    assert body["tokens"]["output"] == 5


def test_dashboard_counts_the_home_cluster_and_unlabelled_rows_only(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    event_rows.add(event="boom", level="error")
    event_rows.add(event="boom", level="error", cluster="")
    event_rows.add(event="boom", level="error", cluster="some-other-cluster")
    event_rows.add(event="llm_usage", payload={"in_total": 7}, cluster="some-other-cluster")
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["errors"] == 2
    assert body["tokens"]["input"] == 0


def test_dashboard_leaves_out_values_that_are_not_numbers(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    event_rows.add(event="llm_usage", payload={"in_total": 10, "cost_usd": 0.5})
    event_rows.add(event="llm_usage", payload={"in_total": "n/a", "cost_usd": None})
    event_rows.add(event="llm_usage", payload={})
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 10
    assert body["cost_usd"] == pytest.approx(0.5)
