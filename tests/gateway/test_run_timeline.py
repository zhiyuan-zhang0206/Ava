"""Run timeline aggregation contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gateway.routers.run_timeline import aggregate_turn_timeline


def _event(
    event_name: str,
    ts: datetime,
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    attributes: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": 1,
        "ts": ts,
        "trace_id": trace_id,
        "span_id": span_id,
        "agent_id": 405,
        "machine": "test",
        "process": "test",
        "category": "telemetry",
        "event_name": event_name,
        "level": "info",
        "source": "test",
        "target_agent_id": None,
        "attributes": attributes or {},
    }


def test_aggregate_turn_timeline_joins_usage_and_marks_compact_boundary() -> None:
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    events = [
        _event("agent_spawned", start),
        _event(
            "llm_usage",
            start + timedelta(seconds=1),
            trace_id="trace-1",
            span_id="span-1",
            attributes={
                "model": "deepseek-v4-flash",
                "in_total": 120,
                "cache_read": 100,
                "out_total": 12,
                "reasoning": 4,
                "latency_ms": 1500,
                "cost_usd": 0.02,
            },
        ),
        _event("exec_failed", start + timedelta(seconds=3), trace_id="trace-1"),
        _event(
            "turn_end",
            start + timedelta(seconds=4),
            trace_id="trace-1",
            span_id="span-1",
            attributes={"duration_seconds": 4, "ok": True},
        ),
        _event("compact", start + timedelta(seconds=6)),
        _event(
            "llm_usage",
            start + timedelta(seconds=10),
            trace_id="trace-2",
            span_id="span-2",
            attributes={"model": "deepseek-v4-flash", "in_total": 240, "out_total": 24},
        ),
        _event(
            "turn_end",
            start + timedelta(seconds=13),
            trace_id="trace-2",
            span_id="span-2",
            attributes={"duration_seconds": 3, "ok": False},
        ),
    ]

    timeline = aggregate_turn_timeline(events, start, start + timedelta(seconds=16))

    assert timeline.meta.n_turns == 2
    assert timeline.meta.tokens_in == 360
    assert timeline.meta.tokens_out == 36
    assert timeline.meta.n_exec_failed == 1
    assert timeline.boundaries.initialize_turn == 1
    assert timeline.boundaries.last_before_compact_turn == 1
    assert timeline.rows[0].start == start
    assert timeline.rows[0].llm.in_total == 120
    assert timeline.rows[0].llm.cache_read == 100
    assert timeline.rows[0].anomalies == ["exec_failed"]
    assert timeline.rows[1].ok is False


def test_aggregate_turn_timeline_buckets_rows_without_changing_token_totals() -> None:
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    events = [
        _event(
            "llm_usage",
            start + timedelta(minutes=1),
            trace_id="trace-1",
            span_id="span-1",
            attributes={"in_total": 10, "out_total": 2},
        ),
        _event(
            "turn_end",
            start + timedelta(minutes=2),
            trace_id="trace-1",
            span_id="span-1",
            attributes={"duration_seconds": 2, "ok": True},
        ),
        _event(
            "llm_usage",
            start + timedelta(minutes=5),
            trace_id="trace-2",
            span_id="span-2",
            attributes={"in_total": 20, "out_total": 3},
        ),
        _event(
            "turn_end",
            start + timedelta(minutes=6),
            trace_id="trace-2",
            span_id="span-2",
            attributes={"duration_seconds": 2, "ok": True},
        ),
    ]

    timeline = aggregate_turn_timeline(
        events,
        start,
        start + timedelta(hours=1),
        bucket_seconds=3600,
    )

    assert len(timeline.rows) == 1
    assert timeline.rows[0].n_turns == 2
    assert timeline.rows[0].llm.in_total == 30
    assert timeline.rows[0].llm.out_total == 5


def test_aggregate_turn_timeline_preserves_legacy_exec_failure_events() -> None:
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    timeline = aggregate_turn_timeline(
        [
            _event("exec(timeout)", start, trace_id="trace-1"),
            _event(
                "turn_end",
                start + timedelta(seconds=2),
                trace_id="trace-1",
                attributes={"duration_seconds": 2, "ok": False},
            ),
        ],
        start,
        start + timedelta(seconds=3),
    )

    assert timeline.meta.n_exec_failed == 1
    assert timeline.rows[0].execs[0].ok is False
    assert "exec(timeout)" in timeline.rows[0].anomalies
