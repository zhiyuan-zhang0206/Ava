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


def test_aggregate_turn_timeline_assigns_each_exec_to_its_time_window() -> None:
    """Session-level trace ids must not fan one exec out to every turn."""
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    timeline = aggregate_turn_timeline(
        [
            _event(
                "turn_end",
                start + timedelta(seconds=4),
                trace_id="session-trace",
                span_id="turn-one",
                attributes={"duration_seconds": 4},
            ),
            _event("exec", start + timedelta(seconds=3), trace_id="session-trace"),
            _event(
                "turn_end",
                start + timedelta(seconds=9),
                trace_id="session-trace",
                span_id="turn-two",
                attributes={"duration_seconds": 4},
            ),
        ],
        start,
        start + timedelta(seconds=10),
    )

    assert [len(row.execs) for row in timeline.rows] == [1, 0]
    assert timeline.rows[1].active_s == 0


def test_aggregate_turn_timeline_keeps_a_trailing_exec_on_the_last_completed_turn() -> None:
    """A just-emitted exec must not vanish while its next turn is unfinished."""
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    timeline = aggregate_turn_timeline(
        [
            _event(
                "turn_end",
                start + timedelta(seconds=4),
                trace_id="session-trace",
                attributes={"duration_seconds": 4},
            ),
            _event("exec", start + timedelta(seconds=5), trace_id="session-trace"),
        ],
        start,
        start + timedelta(seconds=6),
    )

    assert [len(row.execs) for row in timeline.rows] == [1]


def test_aggregate_turn_timeline_falls_back_to_time_for_spanless_usage() -> None:
    """A span-less event pair still reports its token usage exactly once."""
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    timeline = aggregate_turn_timeline(
        [
            _event(
                "llm_usage",
                start + timedelta(seconds=1),
                attributes={"in_total": 120, "out_total": 12, "latency_ms": 500},
            ),
            _event(
                "turn_end",
                start + timedelta(seconds=4),
                attributes={"duration_seconds": 4},
            ),
            _event(
                "llm_usage",
                start + timedelta(seconds=6),
                attributes={"in_total": 240, "out_total": 24, "latency_ms": 700},
            ),
            _event(
                "turn_end",
                start + timedelta(seconds=8),
                attributes={"duration_seconds": 4},
            ),
        ],
        start,
        start + timedelta(seconds=9),
    )

    assert [row.llm.in_total for row in timeline.rows] == [120, 240]
    assert timeline.meta.tokens_in == 360
    assert timeline.meta.fallback_turns == 2
    assert timeline.meta.unmatched_turns == 0


def test_aggregate_turn_timeline_counts_one_audit_event_per_restart_family() -> None:
    """Telemetry twins must not double the user-visible restart count."""
    start = datetime(2026, 8, 29, 8, tzinfo=UTC)
    timeline = aggregate_turn_timeline(
        [
            _event("resurrect", start),
            _event("agent_resurrected", start + timedelta(milliseconds=20)),
            _event("agent_restarted", start + timedelta(seconds=1)),
            _event("restart_completed", start + timedelta(seconds=1, milliseconds=20)),
        ],
        start,
        start + timedelta(seconds=2),
    )

    assert timeline.meta.n_restart == 2
