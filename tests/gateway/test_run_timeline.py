"""Run-timeline aggregation + endpoint contract tests.

Covers the pure assembly (`_build_rows`: llm_usage↔turn_end span_id join,
exec trace_id attach, anomaly marking, boundary/route computation, paging,
truncation) and the endpoint (default session route, explicit window, 404 for
unknown agents, tz-less 422). The Loki queries are mocked via FakeLoki.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg_pool
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events
from gateway.app import app
from gateway.routers.run_timeline import _build_rows
from tests.gateway.loki_fake import FakeLoki


def _event(
    event_name: str,
    ts: datetime,
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    attributes: dict[str, object] | None = None,
    category: str = "telemetry",
) -> dict[str, object]:
    return {
        "id": 1,
        "ts": ts,
        "trace_id": trace_id,
        "span_id": span_id,
        "agent_id": 405,
        "machine": "test",
        "process": "test",
        "category": category,
        "event_name": event_name,
        "level": "info",
        "source": "test",
        "target_agent_id": None,
        "attributes": attributes or {},
    }


def _usage(
    ts: datetime,
    *,
    span: str,
    trace: str,
    in_total: int = 100,
    out_total: int = 10,
    cost: float = 0.01,
) -> dict[str, object]:
    return _event(
        "llm_usage",
        ts,
        trace_id=trace,
        span_id=span,
        attributes={
            "calls": 1,
            "in_total": in_total,
            "cache_read": in_total - 1,
            "out_total": out_total,
            "reasoning": 2,
            "latency_ms": 500,
            "cost_usd": cost,
            "model": "deepseek-v4-flash",
        },
    )


def _turn_end(
    ts: datetime, *, span: str, trace: str, dur_s: float = 2.0, ok: bool = True
) -> dict[str, object]:
    return _event(
        "turn_end",
        ts,
        trace_id=trace,
        span_id=span,
        attributes={"duration_seconds": dur_s, "ok": ok},
    )


def _exec(ts: datetime, *, trace: str | None, ok: bool = True) -> dict[str, object]:
    return _event(
        "exec" if ok else "exec_failed",
        ts,
        trace_id=trace,
        attributes={
            "duration_seconds": 1.0,
            "ok": ok,
            "exc_type": "ValueError" if not ok else None,
        },
    )


def test_build_rows_joins_usage_to_turn_end_by_span_id() -> None:
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    usage = [
        _usage(start + timedelta(seconds=1), span="s1", trace="t1", in_total=120, out_total=12)
    ]
    ends = [_turn_end(start + timedelta(seconds=3), span="s1", trace="t1", dur_s=2.0)]
    rows, boundaries, meta = _build_rows(usage, ends, [], [], [], limit=500, offset=0)

    assert len(rows) == 1
    row = rows[0]
    assert row.start == start + timedelta(seconds=1)  # end - duration
    assert row.end == start + timedelta(seconds=3)
    assert row.active_s == 2.0
    assert row.ok is True
    assert row.llm is not None
    assert row.llm.in_total == 120
    assert row.llm.out_total == 12
    assert row.llm.model == "deepseek-v4-flash"
    assert row.llm.cost_usd == 0.01
    assert boundaries.initialize_at == start + timedelta(seconds=1)
    assert meta.n_turns == 1
    assert meta.tokens_in == 120


def test_build_rows_marks_exec_failed_as_anomaly_and_counts_meta() -> None:
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    usage = [_usage(start, span="s1", trace="t1", in_total=200, out_total=20)]
    ends = [_turn_end(start + timedelta(seconds=2), span="s1", trace="t1")]
    execs = [
        _exec(start + timedelta(milliseconds=100), trace="t1", ok=True),
        _exec(start + timedelta(milliseconds=300), trace="t1", ok=False),
    ]
    rows, _b, meta = _build_rows(usage, ends, execs, [], [], limit=500, offset=0)

    assert len(rows) == 1
    assert rows[0].anomalies == ["exec_failed@08:00:00 ValueError"]
    assert len(rows[0].execs) == 2
    assert rows[0].execs[0].ok is True
    assert rows[0].execs[1].ok is False
    assert meta.n_exec_failed == 1
    assert meta.tokens_in == 200


def test_build_rows_tags_compact_and_restart_boundaries() -> None:
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    usage = [_usage(start, span="s1", trace="t1")]
    ends = [_turn_end(start + timedelta(seconds=2), span="s1", trace="t1")]
    compacts = [_event("compact", start + timedelta(seconds=1), category="audit")]
    inits = [_event("restart_completed", start - timedelta(seconds=10))]
    rows, boundaries, meta = _build_rows(usage, ends, [], compacts, inits, limit=500, offset=0)

    assert rows[0].tags == ["compact@08:00", "restart"]
    assert boundaries.compact_at == start + timedelta(seconds=1)
    assert meta.n_compact == 1
    assert meta.n_restart == 1


def test_build_rows_pages_and_reports_truncation() -> None:
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    usage: list[dict[str, object]] = []
    ends: list[dict[str, object]] = []
    for i in range(5):
        ts = start + timedelta(minutes=i)
        usage.append(_usage(ts, span=f"s{i}", trace=f"t{i}"))
        ends.append(_turn_end(ts + timedelta(seconds=1), span=f"s{i}", trace=f"t{i}"))

    rows, _b, meta = _build_rows(usage, ends, [], [], [], limit=2, offset=2)
    assert [r.turn for r in rows] == [3, 4]
    assert meta.n_turns == 5
    assert meta.truncated is True

    rows2, _b2, _m2 = _build_rows(usage, ends, [], [], [], limit=2, offset=4)
    assert [r.turn for r in rows2] == [5]
    assert _m2.truncated is False


def test_build_rows_keeps_orphan_usage_without_turn_end() -> None:
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    usage = [_usage(start, span="s1", trace="t1")]
    rows, _b, meta = _build_rows(usage, [], [], [], [], limit=500, offset=0)
    assert len(rows) == 1
    assert rows[0].end == start  # open turn
    assert rows[0].active_s == 0.0
    assert meta.n_turns == 1


# ── endpoint ───────────────────────────────────────────────────────────────


@pytest.fixture()
def fake_loki(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    loki = FakeLoki()
    monkeypatch.setattr(loki_events, "query_events", loki.query_events)
    return loki


def _seed_agent(client: TestClient, agent_id: int = 405) -> None:
    """Create the agent row so the endpoint's existence check passes."""
    from typing import cast

    from shared.db import create_agent

    pool = cast(psycopg_pool.ConnectionPool, client.app.state.db_pool)  # type: ignore[attr-defined]
    with pool.connection() as conn:
        created = create_agent(conn)
        if created != agent_id:
            # The test DB sequence may not align with 405 — patch the row.
            with conn.cursor() as cur:
                cur.execute("UPDATE agents SET id = %s WHERE id = %s", (agent_id, created))
                cur.execute("UPDATE agents_meta SET id = %s WHERE id = %s", (agent_id, created))
            conn.commit()


# ── QA data-source regressions (2026-08-29) ───────────────────────────────


def test_usage_without_span_pairs_by_time_and_warns() -> None:
    """Post-19:03Z events carry no span/trace — llm_usage must still pair to
    its turn_end (time containment) and surface a warning, never zeroing
    tokens (QA ②)."""
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    usage = [_usage(start + timedelta(seconds=1), span="", trace="", in_total=777, out_total=9)]
    ends = [_turn_end(start + timedelta(seconds=3), span="", trace="", dur_s=2.0)]
    rows, _b, meta = _build_rows(usage, ends, [], [], [], limit=500, offset=0)

    assert len(rows) == 1
    assert rows[0].llm is not None and rows[0].llm.in_total == 777  # tokens NOT zeroed
    assert "unpaired" in rows[0].tags
    assert meta.tokens_in == 777
    assert any("paired by time" in w for w in meta.warnings)


def test_exec_attaches_by_time_containment_in_session_root_shape() -> None:
    """Session-root shape: many turns share one trace_id and exec spans are
    children of the turn root (own span_id ≠ turn span_id) — exec must attach
    by time containment (QA ①), and only to ITS turn."""
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    shared_trace = "f" * 32
    usage = [
        _usage(start, span="s1", trace=shared_trace, in_total=100),
        _usage(start + timedelta(minutes=1), span="s2", trace=shared_trace, in_total=200),
    ]
    ends = [
        _turn_end(start + timedelta(seconds=5), span="s1", trace=shared_trace, dur_s=5.0),
        _turn_end(
            start + timedelta(minutes=1, seconds=5), span="s2", trace=shared_trace, dur_s=5.0
        ),
    ]
    # One exec inside turn 1's window, one inside turn 2's — same trace_id.
    execs = [
        _exec(start + timedelta(seconds=3), trace=shared_trace, ok=True),
        _exec(start + timedelta(minutes=1, seconds=3), trace=shared_trace, ok=True),
    ]
    rows, _b, meta = _build_rows(usage, ends, execs, [], [], limit=500, offset=0)

    assert len(rows) == 2
    assert len(rows[0].execs) == 1  # exec@3s → turn 1
    assert len(rows[1].execs) == 1  # exec@63s → turn 2
    assert meta.warnings == []


def test_unattached_execs_surface_warning() -> None:
    """An exec with no trace and no span, outside every turn window, must be
    counted and warned about — not silently dropped (QA ①)."""
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    usage = [_usage(start, span="s1", trace="t1")]
    ends = [_turn_end(start + timedelta(seconds=5), span="s1", trace="t1", dur_s=5.0)]
    # No span_id, no trace_id, far outside the turn window.
    execs = [_exec(start + timedelta(minutes=30), trace=None, ok=True)]
    rows, _b, meta = _build_rows(usage, ends, execs, [], [], limit=500, offset=0)

    assert len(rows) == 1
    assert rows[0].execs == []
    assert any("unattached exec" in w for w in meta.warnings)


def test_lifecycle_marker_dedupe() -> None:
    """One restart emits the audit+telemetry pair ~1-2s apart — the boundary
    and the restart tally must not double-count (QA ③)."""
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    usage = [_usage(start, span="s1", trace="t1")]
    ends = [_turn_end(start + timedelta(seconds=2), span="s1", trace="t1")]
    # Two initialize markers 1s apart (audit + telemetry pair)
    inits = [
        _event("restart_completed", start - timedelta(seconds=10)),
        _event("restart_completed", start - timedelta(seconds=9)),
    ]
    rows, _b, meta = _build_rows(usage, ends, [], [], inits, limit=500, offset=0)

    assert meta.n_restart == 1
    assert rows[0].tags.count("restart") == 1


def test_failed_turn_without_usage_forms_a_row() -> None:
    """A failed turn emits turn_end(ok=False) but no llm_usage (usage is
    logged only on success) — the turn_end skeleton must still show it
    (QA W1: the red failed-turn encoding must not be dead code)."""
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    usage = [_usage(start, span="s1", trace="t1", in_total=100)]
    ends = [
        _turn_end(start + timedelta(seconds=3), span="s1", trace="t1", dur_s=3.0, ok=True),
        _turn_end(start + timedelta(minutes=1), span="s2", trace="t2", dur_s=2.0, ok=False),
    ]
    rows, _b, meta = _build_rows(usage, ends, [], [], [], limit=500, offset=0)

    assert len(rows) == 2
    assert rows[0].ok is True
    assert rows[0].llm is not None
    assert rows[1].ok is False  # failed turn — no llm row
    assert rows[1].llm is None
    assert meta.n_turns == 2


def test_run_timeline_endpoint_default_session_route(fake_loki: FakeLoki) -> None:
    now = datetime.now(UTC)
    start = now - timedelta(hours=2)
    fake_loki.add(event="restart_completed", agent_id=405, ts_offset_hours=0)
    fake_loki.rows[-1]["ts"] = start - timedelta(minutes=5)
    fake_loki.add(
        event="llm_usage",
        agent_id=405,
        ts_offset_hours=0,
        payload={
            "calls": 1,
            "in_total": 100,
            "cache_read": 99,
            "out_total": 10,
            "reasoning": 2,
            "latency_ms": 500,
            "cost_usd": 0.01,
            "model": "deepseek-v4-flash",
        },
    )
    fake_loki.rows[-1]["ts"] = start + timedelta(seconds=1)
    fake_loki.rows[-1]["span_id"] = "s1"
    fake_loki.rows[-1]["trace_id"] = "t1"
    fake_loki.add(
        event="turn_end",
        agent_id=405,
        ts_offset_hours=0,
        payload={"duration_seconds": 2, "ok": True},
    )
    fake_loki.rows[-1]["ts"] = start + timedelta(seconds=3)
    fake_loki.rows[-1]["span_id"] = "s1"
    fake_loki.rows[-1]["trace_id"] = "t1"

    with TestClient(app) as client:
        _seed_agent(client)
        from urllib.parse import quote

        resp = client.get(
            f"/api/agents/405/run-timeline?from={quote((start - timedelta(hours=1)).isoformat())}"
            f"&to={quote((now + timedelta(minutes=5)).isoformat())}"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_id"] == 405
        assert body["meta"]["n_turns"] == 1
        assert body["rows"][0]["llm"]["in_total"] == 100
        assert body["rows"][0]["ok"] is True
        assert body["boundaries"]["compact_at"] is None


def test_run_timeline_endpoint_unknown_agent_404() -> None:
    with TestClient(app) as client:
        resp = client.get(
            "/api/agents/999999/run-timeline?from=2026-08-29T00:00:00Z&to=2026-08-29T01:00:00Z"
        )
        assert resp.status_code == 404


def test_run_timeline_endpoint_naive_from_422() -> None:
    with TestClient(app) as client:
        _seed_agent(client)
        resp = client.get(
            "/api/agents/405/run-timeline?from=2026-08-29T00:00:00&to=2026-08-29T01:00:00Z"
        )
        assert resp.status_code == 422


def test_run_timeline_endpoint_from_after_to_422() -> None:
    with TestClient(app) as client:
        _seed_agent(client)
        resp = client.get(
            "/api/agents/405/run-timeline?from=2026-08-29T02:00:00Z&to=2026-08-29T01:00:00Z"
        )
        assert resp.status_code == 422


def test_run_timeline_endpoint_reports_compact_at_window_end(fake_loki: FakeLoki) -> None:
    """A compact event sitting exactly at the session-route end must surface
    as the boundary (Loki query_range end is exclusive — the probe resolves
    it, the in-window fetch does not)."""
    now = datetime.now(UTC)
    start = now - timedelta(hours=3)
    fake_loki.add(event="restart_completed", agent_id=405, ts_offset_hours=0)
    fake_loki.rows[-1]["ts"] = start - timedelta(minutes=10)
    fake_loki.add(
        event="llm_usage",
        agent_id=405,
        ts_offset_hours=0,
        payload={
            "calls": 1,
            "in_total": 100,
            "cache_read": 99,
            "out_total": 10,
            "reasoning": 2,
            "latency_ms": 500,
            "cost_usd": 0.01,
            "model": "deepseek-v4-flash",
        },
    )
    fake_loki.rows[-1]["ts"] = start + timedelta(seconds=1)
    fake_loki.rows[-1]["span_id"] = "s1"
    fake_loki.rows[-1]["trace_id"] = "t1"
    fake_loki.add(
        event="turn_end",
        agent_id=405,
        ts_offset_hours=0,
        payload={"duration_seconds": 2, "ok": True},
    )
    fake_loki.rows[-1]["ts"] = start + timedelta(seconds=3)
    fake_loki.rows[-1]["span_id"] = "s1"
    fake_loki.rows[-1]["trace_id"] = "t1"
    compact_ts = start + timedelta(minutes=5)
    fake_loki.add(event="compact", agent_id=405, ts_offset_hours=0, category="audit")
    fake_loki.rows[-1]["ts"] = compact_ts

    with TestClient(app) as client:
        _seed_agent(client)
        resp = client.get("/api/agents/405/run-timeline")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Session route resolves to [initialize, compact]; the compact at the
        # window end must appear as the boundary.
        assert body["boundaries"]["compact_at"] is not None
        assert body["window_to"] == body["boundaries"]["compact_at"]
