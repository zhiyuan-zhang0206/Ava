"""GET /api/ops/monitor HTTP integration tests — the LGTM read path.

Same posture as test_metrics_router.py: the Loki backend is the in-memory
`FakeLoki`, the Prometheus backend a small `FakePrometheus` (both
monkeypatched onto `gateway.loki_events` / `gateway.prom_metrics`); the
`agents` table stays real SQL for the restarts-breakdown labels. Locks the
endpoint contract: envelope shape, window → bucket sizing, zero-filling,
payload field-name wiring from the emit sites (kind / latency_ms / name),
and the fixed-grid alignment against meta.bucket_starts.

LLM p50/p95 are the Prometheus histogram_quantile values (the fake hands
them back verbatim); latency max is the Loki unwrap max; counts are
zero-filled per bucket.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import prom_metrics
from gateway.app import app
from gateway.ops_series_lgtm import _GRID_ORIGIN, _bucket_starts
from gateway.routers import ops_monitor
from tests.gateway.loki_fake import FakeLoki
from tests.gateway.prom_fake import FakePrometheus


@pytest.fixture
def loki_fake(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    fake = FakeLoki()
    monkeypatch.setattr("gateway.loki_events.count_events", fake.count_events)
    monkeypatch.setattr("gateway.loki_events.count_grouped", fake.count_grouped)
    monkeypatch.setattr("gateway.loki_events.count_events_series", fake.count_events_series)
    monkeypatch.setattr("gateway.loki_events.attribute_max_series", fake.attribute_max_series)
    monkeypatch.setattr("gateway.loki_events.query_events", fake.query_events)
    monkeypatch.setattr("gateway.loki_events.query_projected_lines", fake.query_projected_lines)
    return fake


@pytest.fixture
def prom_fake(monkeypatch: pytest.MonkeyPatch) -> FakePrometheus:
    fake = FakePrometheus()
    monkeypatch.setattr("gateway.prom_metrics.query", fake.query)
    monkeypatch.setattr("gateway.prom_metrics.query_range", fake.query_range)
    return fake


def _insert_agent(db: psycopg.Connection, *, label: str) -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _add(
    fake: FakeLoki,
    *,
    event: str,
    agent_id: int | None = None,
    payload: dict[str, object] | None = None,
    ts_offset_hours: float = 0,
) -> None:
    fake.add(
        event=event,
        agent_id=agent_id,
        payload=payload or {},
        ts_offset_hours=ts_offset_hours,
    )


def _grid_index(ts: datetime, anchor: datetime, window_s: int, bucket_s: int) -> int:
    """Position of `ts`'s bucket within the API's bucket_starts grid."""
    elapsed = int((ts - _GRID_ORIGIN).total_seconds())
    bucket = _GRID_ORIGIN + timedelta(seconds=elapsed - (elapsed % bucket_s))
    starts = _bucket_starts(anchor, window_s, bucket_s)
    return starts.index(bucket)


def _now_ts() -> datetime:
    return datetime.now(UTC)


def test_ops_monitor_empty_envelope_and_zero_fill(
    db_conn: psycopg.Connection, loki_fake: FakeLoki, prom_fake: FakePrometheus
) -> None:
    """Empty backends -> all groups present, series fully zero-filled at the
    window's fixed point count (24h -> 48 buckets), totals zero."""
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/ops/monitor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["window"] == "24h"
    assert body["meta"]["bucket_seconds"] == 1800
    assert len(body["meta"]["bucket_starts"]) == 48
    assert len(body["sse"]["series"]) == 48
    assert len(body["llm"]["series"]) == 48
    assert len(body["restarts"]["series"]) == 48
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


def test_ops_monitor_window_param_sets_bucket_count(
    db_conn: psycopg.Connection, loki_fake: FakeLoki, prom_fake: FakePrometheus
) -> None:
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


def test_ops_monitor_sse_wires_kind_and_event_log_drop(
    db_conn: psycopg.Connection, loki_fake: FakeLoki, prom_fake: FakePrometheus
) -> None:
    """sse_drop payload kind maps to queue_full / publish_error (a row
    without kind counts toward neither); event_log_drop is its own series."""
    now = _now_ts()
    _add(loki_fake, event="sse_drop", payload={"kind": "queue_full", "n": 1}, ts_offset_hours=2)
    _add(loki_fake, event="sse_drop", payload={"kind": "publish_error", "n": 1}, ts_offset_hours=2)
    _add(loki_fake, event="sse_drop", payload={"kind": "publish_error", "n": 1}, ts_offset_hours=5)
    _add(loki_fake, event="sse_drop", payload={"n": 1}, ts_offset_hours=3)  # no kind
    _add(loki_fake, event="event_log_drop", payload={"n": 3}, ts_offset_hours=1)
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


def test_ops_monitor_llm_wires_prometheus_and_loki(
    db_conn: psycopg.Connection, loki_fake: FakeLoki, prom_fake: FakePrometheus
) -> None:
    """calls/tokens/latency p50/p95 come from Prometheus, max latency from
    the Loki unwrap, errors from the LLM error family — one bucket wired."""
    now = _now_ts()
    anchor = now
    bucket_s = 1800
    i = 40
    starts = _bucket_starts(anchor, 86400, bucket_s)
    eval_ts = int(starts[i].timestamp()) + bucket_s  # the range query's eval point

    prom_fake.add_llm_bucket(
        calls=10,
        tokens_in=1000,
        tokens_out=200,
        tokens_reasoning=50,
        lat_sum=1250.0,  # 1.25s of LLM latency
        p50=300.0,
        p95=900.0,
        point=(eval_ts, 1.0),
    )
    _add(loki_fake, event="llm_usage", payload={"latency_ms": 4000.0}, ts_offset_hours=0)
    _add(loki_fake, event="llm_provider_error", payload={}, ts_offset_hours=0)
    _add(loki_fake, event="stream_stalled_retry", payload={}, ts_offset_hours=0)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/ops/monitor").json()
    llm = body["llm"]
    b = llm["series"][i]
    assert b["calls"] == 10
    assert b["tokens_in"] == 1000
    assert b["tokens_out"] == 200
    assert b["latency_p50_ms"] == 300.0
    assert b["latency_p95_ms"] == 900.0
    # max latency: the Loki unwrap value (the fake places it in the last bucket)
    last = llm["series"][-1]
    assert last["latency_max_ms"] == 4000.0
    # errors: the two LLM-error-family rows land in the last bucket
    assert llm["series"][-1]["errors"] == 2
    # tps = (1000+200+50) / (1250/1000) = 1000.0
    assert b["tps"] == 1000.0
    # totals derive from the series; p50/p95 totals are the instant queries
    assert llm["totals"]["calls"] == 10
    assert llm["totals"]["tokens_in"] == 1000
    assert llm["totals"]["latency_max_ms"] == 4000.0
    assert llm["totals"]["latency_p50_ms"] == 300.0
    assert llm["totals"]["latency_p95_ms"] == 900.0
    assert llm["totals"]["errors"] == 2
    assert llm["totals"]["tps"] == 1000.0


def test_ops_monitor_restarts_wires_agent_and_service_breakdown(
    db_conn: psycopg.Connection, loki_fake: FakeLoki, prom_fake: FakePrometheus
) -> None:
    """agent_restarted / service_started series plus the whole-window
    breakdowns: services by attributes.name (with last_start), agents by the
    stream agent_id label with labels from the real agents table."""
    a100 = _insert_agent(db_conn, label="worker-a")
    a101 = _insert_agent(db_conn, label="worker-b")
    db_conn.commit()
    _add(loki_fake, event="agent_restarted", agent_id=a100, ts_offset_hours=2)
    _add(loki_fake, event="agent_restarted", agent_id=a100, ts_offset_hours=2)
    _add(loki_fake, event="agent_restarted", agent_id=a101, ts_offset_hours=4)
    _add(loki_fake, event="agent_restarted", ts_offset_hours=6)  # no agent id: series only
    _add(loki_fake, event="service_started", payload={"name": "gateway"}, ts_offset_hours=3)
    _add(loki_fake, event="service_started", payload={"name": "gateway"}, ts_offset_hours=1)
    _add(loki_fake, event="service_started", payload={"name": "restarter"}, ts_offset_hours=2)
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


def test_ops_monitor_grid_alignment(
    db_conn: psycopg.Connection, loki_fake: FakeLoki, prom_fake: FakePrometheus
) -> None:
    """An event at an exact minute offset lands in the bucket whose start is
    in meta.bucket_starts (positional alignment of the series arrays)."""
    now = _now_ts()
    _add(loki_fake, event="sse_drop", payload={"kind": "queue_full"}, ts_offset_hours=0.5)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get("/api/ops/monitor").json()
    meta = body["meta"]
    i = _grid_index(now - timedelta(hours=0.5), now, 86400, 1800)
    assert body["sse"]["series"][i]["queue_full"] == 1
    # the bucket start for index i is the grid-aligned boundary, not `now`
    start = datetime.fromisoformat(meta["bucket_starts"][i])
    assert start.minute % 30 == 0 and start.second == 0


def test_ops_monitor_borrows_db_only_after_lgtm_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label projection cannot occupy a DB slot while network futures run."""
    events: list[str] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: tuple[list[int]]) -> None:
            events.append("label_query")

        def fetchall(self) -> list[tuple[int, str]]:
            return [(7, "worker")]

    class Connection:
        def __enter__(self) -> Connection:
            events.append("db_borrow")
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    class Pool:
        def connection(self) -> Connection:
            return Connection()

    def fetch(
        _window: str,
        *,
        label_lookup: object,
    ) -> dict[str, object]:
        events.append("fanout_done")
        assert callable(label_lookup)
        assert label_lookup([7]) == {7: "worker"}
        return {"ok": True}

    def report(**data: object) -> dict[str, object]:
        return data

    monkeypatch.setattr(ops_monitor, "fetch_ops_series", fetch)
    monkeypatch.setattr(ops_monitor, "OpsMonitorReport", report)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=Pool())))

    assert ops_monitor.get_ops_monitor(request) == {"ok": True}  # type: ignore[arg-type]
    assert events == ["fanout_done", "db_borrow", "label_query"]


def test_ops_monitor_backend_failure_is_retriable_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise httpx.ReadTimeout("observability unavailable")

    monkeypatch.setattr(ops_monitor, "fetch_ops_series", unavailable)
    with TestClient(app) as client:
        response = client.get("/api/ops/monitor")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert "ReadTimeout" in response.json()["detail"]


def test_ops_monitor_prom_budget_failure_uses_global_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def saturated(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise prom_metrics.PromQueryBudgetError("queue_full")

    monkeypatch.setattr(ops_monitor, "fetch_ops_series", saturated)
    with TestClient(app) as client:
        response = client.get("/api/ops/monitor")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"] == "Prometheus query budget unavailable (queue_full); retry"
