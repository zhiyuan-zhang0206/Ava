"""Observed facts survive replay without multiplying totals or inventing coverage."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import Mock

import psycopg
import pytest

from shared import observed_metrics as metrics
from shared import telemetry
from shared.config import settings

_AT = datetime(2026, 9, 15, 23, 59, 59, tzinfo=UTC)


def _event(name: str, **attributes: Any) -> telemetry.Event:
    return telemetry.Event(
        ts=_AT,
        trace_id=None,
        span_id=None,
        agent_id=71,
        machine="test",
        cluster="test",
        process="test",
        category="telemetry",
        event_name=name,
        level="info",
        source="system",
        target_agent_id=None,
        attributes=attributes,
    )


def _agent(db: psycopg.Connection) -> None:
    db.execute("INSERT INTO agents (id) VALUES (71)")
    db.commit()


def test_usage_keeps_usage_time_price_without_payload_or_repricing() -> None:
    event = _event(
        "llm_usage",
        model="unknown-today",
        in_total=100,
        out_total=20,
        cache_read=30,
        reasoning=5,
        cost_usd="0.012345678901",
        body="private output",
    )
    fact = metrics.observe_event(event)
    assert fact is not None
    assert fact.cost_usd == Decimal("0.012345678901")
    assert (fact.usage_calls, fact.unpriced_calls) == (1, 0)
    assert (fact.tokens_in, fact.tokens_out, fact.tokens_cached, fact.tokens_reasoning) == (
        100,
        20,
        30,
        5,
    )
    assert not hasattr(fact, "body")
    assert metrics.observe_row(telemetry.event_row(event)) == fact


def test_absent_usage_price_and_optional_fields_stay_unknown() -> None:
    fact = metrics.observe_event(_event("llm_usage", model="known-but-no-price-snapshot"))
    assert fact is not None
    assert (fact.usage_calls, fact.unpriced_calls, fact.cost_usd) == (1, 1, 0)
    assert (fact.tokens_in, fact.tokens_out, fact.tokens_cached) == (0, 0, 0)


def test_unrelated_large_logs_are_not_serialized_again(monkeypatch: pytest.MonkeyPatch) -> None:
    encode = Mock(side_effect=AssertionError("unrelated body must not be serialized"))
    monkeypatch.setattr(metrics, "event_row", encode)
    assert metrics.observe_event(_event("log", body="large diagnostic")) is None
    encode.assert_not_called()


def test_turn_and_node_exact_durations_preserve_missing_optional_fields() -> None:
    turn = metrics.observe_event(_event("turn_end", duration_seconds=1.23456789))
    assert turn is not None
    assert (turn.turn_total, turn.turn_ok, turn.turn_duration_seconds) == (1, 0, 1.23456789)
    legacy = metrics.observe_event(_event("node_exit", duration_seconds=0.125))
    assert legacy is not None and legacy.active_seconds == 0.125
    batched = metrics.observe_event(
        _event(
            "node_exit",
            nodes=[
                {"node": "claim", "duration_seconds": 1000},
                {"node": "llm", "duration_seconds": 1.25},
                {"node": "exec", "duration_seconds": 2.5},
            ],
        )
    )
    assert batched is not None
    assert (batched.active_seconds, batched.exec_seconds) == (3.75, 2.5)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_malformed_duration_cannot_poison_numeric_sums(bad: Any) -> None:
    with pytest.raises(ValueError):
        metrics.observe_event(_event("turn_end", duration_seconds=bad, ok=True))


def test_sequential_and_concurrent_replay_add_each_fact_once(db_conn: psycopg.Connection) -> None:
    _agent(db_conn)
    facts = [
        metrics.MetricObservation(
            event_id=2**64 - 1,
            agent_id=71,
            occurred_at=_AT,
            kind="usage",
            usage_calls=1,
            tokens_in=17,
            cost_usd=Decimal("0.012345678901"),
        ),
        metrics.MetricObservation(
            event_id=18,
            agent_id=71,
            occurred_at=_AT,
            kind="turn",
            turn_total=1,
            turn_ok=1,
            turn_duration_seconds=1.23456789,
        ),
    ]
    barrier = Barrier(2)

    def replay() -> int:
        with psycopg.connect(settings.data_plane.db_url) as connection:
            barrier.wait(timeout=10)
            return metrics.write_observations(facts, db=connection)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(replay) for _ in range(2)]
        assert sorted(future.result(timeout=15) for future in futures) == [0, 2]
    assert metrics.write_observations(facts, db=db_conn) == 0
    row = db_conn.execute(
        "SELECT usage_calls, tokens_in, cost_usd, turn_total, turn_ok, "
        "turn_duration_sum, turn_duration_min, turn_duration_max "
        "FROM agent_metric_days WHERE agent_id=71"
    ).fetchone()
    assert row == (1, 17, Decimal("0.012345678901"), 1, 1, 1.23456789, 1.23456789, 1.23456789)
    assert db_conn.execute("SELECT count(*) FROM agent_metric_observations").fetchone() == (2,)


def test_day_boundaries_are_utc_and_durations_do_not_floor(db_conn: psycopg.Connection) -> None:
    _agent(db_conn)
    facts = [
        metrics.MetricObservation(
            event_id=i,
            agent_id=71,
            occurred_at=_AT + timedelta(seconds=i),
            kind="turn",
            turn_total=1,
            turn_duration_seconds=0.1 + i,
        )
        for i in (0, 1)
    ]
    assert metrics.write_observations(facts, db=db_conn) == 2
    assert db_conn.execute(
        "SELECT day, turn_duration_sum FROM agent_metric_days ORDER BY day"
    ).fetchall() == [(date(2026, 9, 15), 0.1), (date(2026, 9, 16), 1.1)]


def test_outer_rollback_covers_facts_and_sums(db_conn: psycopg.Connection) -> None:
    _agent(db_conn)
    fact = metrics.MetricObservation(
        event_id=1,
        agent_id=71,
        occurred_at=_AT,
        kind="usage",
        usage_calls=1,
    )
    assert metrics.write_observations([fact], db=db_conn) == 1
    db_conn.rollback()
    assert db_conn.execute("SELECT count(*) FROM agent_metric_observations").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM agent_metric_days").fetchone() == (0,)


def test_missing_agent_fails_batch_explicitly_without_partial_commit(
    db_conn: psycopg.Connection,
) -> None:
    _agent(db_conn)
    good = metrics.MetricObservation(
        event_id=1,
        agent_id=71,
        occurred_at=_AT,
        kind="usage",
        usage_calls=1,
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        metrics.write_observations([good, replace(good, event_id=2, agent_id=72)], db=db_conn)
    db_conn.rollback()
    assert db_conn.execute("SELECT count(*) FROM agent_metric_days").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM agent_metric_observations").fetchone() == (0,)


def test_projection_failure_keeps_jsonl_and_otlp_and_never_emits_recursively(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(telemetry, "logs_dir", lambda: tmp_path)
    monkeypatch.setattr(metrics, "_failures", 0)
    monkeypatch.setattr(metrics, "write_observations", Mock(side_effect=RuntimeError("DB down")))
    diagnostic = Mock()
    monkeypatch.setattr(telemetry, "_report_no_pipeline", diagnostic)
    emitted = Mock(side_effect=AssertionError("diagnostics must bypass emitter"))
    monkeypatch.setattr(telemetry, "emit", emitted)
    exported = Mock()
    monkeypatch.setattr(telemetry, "_export_otlp", exported)
    event = _event("turn_end", ok=True, duration_seconds=2.5)
    telemetry._write_batch([event])
    exported.assert_called_once_with([event])
    assert len(list(tmp_path.glob("events-*.jsonl"))) >= 1
    assert (
        str(telemetry.event_row(event)["id"]) in next(tmp_path.glob("events-*.jsonl")).read_text()
    )
    diagnostic.assert_called_once()
    emitted.assert_not_called()


def test_one_malformed_fact_does_not_discard_other_supported_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = Mock(return_value=1)
    report = Mock()
    monkeypatch.setattr(metrics, "write_observations", write)
    monkeypatch.setattr(telemetry, "_report_no_pipeline", report)
    metrics.project_events(
        [
            _event("turn_end", duration_seconds="invalid", ok=True),
            _event("turn_end", duration_seconds=1.25, ok=False),
        ]
    )
    assert len(write.call_args.args[0]) == 1
    assert write.call_args.args[0][0].turn_duration_seconds == 1.25
    report.assert_called_once()


def test_lifecycle_intervals_follow_transactional_status_not_telemetry(
    db_conn: psycopg.Connection,
) -> None:
    _agent(db_conn)
    db_conn.execute("INSERT INTO agents_meta (id,status) VALUES (71,'running')")
    db_conn.commit()
    first = db_conn.execute(
        "SELECT started_at, ended_at FROM agent_lifecycle_intervals WHERE agent_id=71"
    ).fetchone()
    assert first is not None and first[1] is None
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=71")
    db_conn.execute("UPDATE agents_meta SET status='restarting' WHERE id=71")
    assert db_conn.execute(
        "SELECT count(*) FROM agent_lifecycle_intervals WHERE agent_id=71"
    ).fetchone() == (1,)
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=71")
    assert db_conn.execute(
        "SELECT ended_at IS NOT NULL FROM agent_lifecycle_intervals WHERE agent_id=71"
    ).fetchone() == (True,)
    db_conn.rollback()
    assert db_conn.execute(
        "SELECT ended_at FROM agent_lifecycle_intervals WHERE agent_id=71"
    ).fetchone() == (None,)
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=71")
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=71")
    assert db_conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE ended_at IS NULL) "
        "FROM agent_lifecycle_intervals WHERE agent_id=71"
    ).fetchone() == (2, 1)
