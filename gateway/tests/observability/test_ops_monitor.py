"""GET /api/ops/monitor HTTP integration tests — the Postgres read path.

The window's events are real `telemetry_events` rows and the `agents` table is real SQL for the
restarts-breakdown labels. Locks the endpoint contract: envelope shape, window → bucket sizing,
zero-filling, payload field-name wiring from the emit sites (kind / latency_ms / name), exact
percentiles, and the fixed-grid alignment against meta.bucket_starts.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.cluster import ops_monitor
from gateway.cluster.ops_series import _GRID_ORIGIN, _bucket_starts


def _insert_agent(db: psycopg.Connection, *, label: str) -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _add(
    db: psycopg.Connection,
    *,
    event: str,
    agent_id: int | None = None,
    payload: dict[str, object] | None = None,
    ts_offset_hours: float = 0,
    ts: datetime | None = None,
) -> None:
    when = ts if ts is not None else datetime.now(UTC) - timedelta(hours=ts_offset_hours)
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'test', 'c', 'test', "
        "'telemetry', %s, 'info', 'test', %s::jsonb)",
        (uuid.uuid4().int % (1 << 62), when, agent_id, event, json.dumps(payload or {})),
    )


def _grid_index(ts: datetime, anchor: datetime, window_s: int, bucket_s: int) -> int:
    """Position of `ts`'s bucket within the API's bucket_starts grid."""
    elapsed = int((ts - _GRID_ORIGIN).total_seconds())
    bucket = _GRID_ORIGIN + timedelta(seconds=elapsed - (elapsed % bucket_s))
    starts = _bucket_starts(anchor, window_s, bucket_s)
    return starts.index(bucket)


def _now_ts() -> datetime:
    return datetime.now(UTC)


def _assert_zero_filled_series(body: dict[str, Any]) -> None:
    """Every group renders the window's fixed point count of zeroed buckets."""
    assert body["meta"]["window"] == "24h"
    assert body["meta"]["bucket_seconds"] == 1800
    assert len(body["meta"]["bucket_starts"]) == 48
    assert len(body["sse"]["series"]) == 48
    assert len(body["llm"]["series"]) == 48
    assert len(body["restarts"]["series"]) == 48


def _assert_zero_totals(body: dict[str, Any]) -> None:
    assert body["sse"]["totals"] == {"queue_full": 0, "publish_error": 0, "event_log_drop": 0}
    assert body["llm"]["totals"]["calls"] == 0
    assert body["llm"]["totals"]["latency_p50_ms"] is None
    assert body["restarts"]["totals"] == {"agent_restarts": 0, "service_starts": 0}
    assert body["restarts"]["services"] == []
    assert body["restarts"]["agents"] == []
    # every llm bucket with no data: percentiles/max None, counts 0
    assert body["llm"]["series"][0] == {
        "bucket": 0,
        "calls": 0,
        "latency_p50_ms": None,
        "latency_p95_ms": None,
        "latency_max_ms": None,
        "tokens_in": 0,
        "tokens_out": 0,
        "tps": None,
        "errors": 0,
    }


def test_ops_monitor_empty_envelope_and_zero_fill(db_conn: psycopg.Connection) -> None:
    """Empty backends -> all groups present, series fully zero-filled at the
    window's fixed point count (24h -> 48 buckets), totals zero."""
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/ops/monitor")
    assert resp.status_code == 200
    body = resp.json()
    _assert_zero_filled_series(body)
    _assert_zero_totals(body)


def test_ops_monitor_window_param_sets_bucket_count(db_conn: psycopg.Connection) -> None:
    """1h -> 60 buckets of 60s; 7d -> 168 buckets of 1h."""
    db_conn.commit()
    with TestClient(app) as client:
        for window, n, bucket_s in (("1h", 60, 60), ("7d", 168, 3600)):
            resp = client.get(f"/api/ops/monitor?window={window}")
            assert resp.status_code == 200, window
            body = resp.json()
            assert body["meta"]["bucket_seconds"] == bucket_s
            assert len(body["meta"]["bucket_starts"]) == n
            assert len(body["sse"]["series"]) == n
            assert len(body["llm"]["series"]) == n
            assert len(body["restarts"]["series"]) == n


def test_ops_monitor_sse_wires_kind_and_event_log_drop(db_conn: psycopg.Connection) -> None:
    """sse_drop payload kind maps to queue_full / publish_error (a row
    without kind counts toward neither); event_log_drop is its own series."""
    now = _now_ts()
    _add(db_conn, event="sse_drop", payload={"kind": "queue_full", "n": 1}, ts_offset_hours=2)
    _add(db_conn, event="sse_drop", payload={"kind": "publish_error", "n": 1}, ts_offset_hours=2)
    _add(db_conn, event="sse_drop", payload={"kind": "publish_error", "n": 1}, ts_offset_hours=5)
    _add(db_conn, event="sse_drop", payload={"n": 1}, ts_offset_hours=3)  # no kind
    _add(db_conn, event="event_log_drop", payload={"n": 3}, ts_offset_hours=1)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/ops/monitor").json()
    sse = body["sse"]
    assert sse["totals"] == {"queue_full": 1, "publish_error": 2, "event_log_drop": 1}
    i_qf = _grid_index(now - timedelta(hours=2), now, 86400, 1800)
    i_pe5 = _grid_index(now - timedelta(hours=5), now, 86400, 1800)
    i_el = _grid_index(now - timedelta(hours=1), now, 86400, 1800)
    assert sse["series"][i_qf]["queue_full"] == 1
    assert sse["series"][i_qf]["publish_error"] == 1
    assert sse["series"][i_pe5]["publish_error"] == 1
    assert sse["series"][i_el]["event_log_drop"] == 1


def _seed_llm_bucket(db: psycopg.Connection) -> int:
    """Four llm_usage rows with known latency and a row with none, plus two LLM errors, all in
    one bucket; returns that bucket's index."""
    i = 40
    inside = _bucket_starts(_now_ts(), 86400, 1800)[i] + timedelta(seconds=60)
    for latency, tokens_in in ((100.0, 600), (300.0, 300), (900.0, 100), (2000.0, 0)):
        payload: dict[str, object] = {
            "latency_ms": latency,
            "in_total": tokens_in,
            "out_total": 50,
            "reasoning": 0,
        }
        _add(db, event="llm_usage", payload=payload, ts=inside)
    # a row with no numeric latency counts as a call but not toward the latency figures
    _add(db, event="llm_usage", payload={"in_total": 1}, ts=inside)
    _add(db, event="llm_provider_error", ts=inside)
    _add(db, event="stream_stalled_retry", ts=inside)
    db.commit()
    return i


def _llm_report(db: psycopg.Connection) -> tuple[dict[str, Any], int]:
    i = _seed_llm_bucket(db)
    with TestClient(app) as client:
        return client.get("/api/ops/monitor").json()["llm"], i


def test_ops_monitor_llm_counts_tokens_and_errors_from_the_rows(
    db_conn: psycopg.Connection,
) -> None:
    llm, i = _llm_report(db_conn)
    b = llm["series"][i]
    assert (b["calls"], b["tokens_in"], b["tokens_out"], b["errors"]) == (5, 1001, 200, 2)
    assert llm["totals"]["calls"] == 5
    assert llm["totals"]["tokens_in"] == 1001
    assert llm["totals"]["errors"] == 2
    assert llm["series"][0]["calls"] == 0


def test_ops_monitor_llm_percentiles_are_exact_over_the_bucket_rows(
    db_conn: psycopg.Connection,
) -> None:
    llm, i = _llm_report(db_conn)
    b = llm["series"][i]
    assert b["latency_p50_ms"] == 600.0  # between 300 and 900
    assert b["latency_p95_ms"] == pytest.approx(1835.0, abs=0.1)
    assert b["latency_max_ms"] == 2000.0
    assert llm["totals"]["latency_p50_ms"] == 600.0
    assert llm["totals"]["latency_max_ms"] == 2000.0
    assert llm["series"][0]["latency_p50_ms"] is None


def test_ops_monitor_llm_tps_is_tokens_over_latency_seconds(db_conn: psycopg.Connection) -> None:
    llm, i = _llm_report(db_conn)
    # 1201 tokens over 3.3 seconds of latency
    assert llm["series"][i]["tps"] == pytest.approx(363.9, abs=0.1)
    assert llm["totals"]["tps"] == pytest.approx(363.9, abs=0.1)


def test_ops_monitor_restarts_wires_agent_and_service_breakdown(
    db_conn: psycopg.Connection,
) -> None:
    """agent_restarted / service_started series plus the whole-window
    breakdowns: services by attributes.name (with last_start), agents by the
    stream agent_id label with labels from the real agents table."""
    a100 = _insert_agent(db_conn, label="worker-a")
    a101 = _insert_agent(db_conn, label="worker-b")
    db_conn.commit()
    _add(db_conn, event="agent_restarted", agent_id=a100, ts_offset_hours=2)
    _add(db_conn, event="agent_restarted", agent_id=a100, ts_offset_hours=2)
    _add(db_conn, event="agent_restarted", agent_id=a101, ts_offset_hours=4)
    _add(db_conn, event="agent_restarted", ts_offset_hours=6)  # no agent id: series only
    _add(db_conn, event="service_started", payload={"name": "gateway"}, ts_offset_hours=3)
    _add(db_conn, event="service_started", payload={"name": "gateway"}, ts_offset_hours=1)
    _add(db_conn, event="service_started", payload={"name": "restarter"}, ts_offset_hours=2)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/ops/monitor").json()
    restarts = body["restarts"]
    # 4 rows total: the no-agent-id row counts in the series but not the breakdown
    assert restarts["totals"] == {"agent_restarts": 4, "service_starts": 3}
    assert restarts["services"] == [
        {"name": "gateway", "starts": 2, "last_start": restarts["services"][0]["last_start"]},
        {"name": "restarter", "starts": 1, "last_start": restarts["services"][1]["last_start"]},
    ]
    # last_start is a real ISO timestamp of the most recent service_started
    for s in restarts["services"]:
        assert s["last_start"] is not None
        datetime.fromisoformat(s["last_start"])
    # top agents: a100 (2) first, a101 (1) second, labels from the agents table
    assert restarts["agents"] == [
        {"agent_id": a100, "label": "worker-a", "restarts": 2},
        {"agent_id": a101, "label": "worker-b", "restarts": 1},
    ]


def test_ops_monitor_grid_alignment(db_conn: psycopg.Connection) -> None:
    """An event at an exact minute offset lands in the bucket whose start is
    in meta.bucket_starts (positional alignment of the series arrays)."""
    now = _now_ts()
    _add(db_conn, event="sse_drop", payload={"kind": "queue_full"}, ts_offset_hours=0.5)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/ops/monitor").json()
    meta = body["meta"]
    i = _grid_index(now - timedelta(hours=0.5), now, 86400, 1800)
    assert body["sse"]["series"][i]["queue_full"] == 1
    # the bucket start for index i is the grid-aligned boundary, not `now`
    start = datetime.fromisoformat(meta["bucket_starts"][i])
    assert start.minute % 30 == 0 and start.second == 0


def test_ops_monitor_read_that_exceeds_its_statement_timeout_is_a_retriable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cancelled(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise psycopg.errors.QueryCanceled("statement timeout")

    monkeypatch.setattr(ops_monitor, "fetch_ops_series", cancelled)
    with TestClient(app) as client:
        response = client.get("/api/ops/monitor")
    assert response.status_code == 503
    assert "retry" in response.json()["detail"]
