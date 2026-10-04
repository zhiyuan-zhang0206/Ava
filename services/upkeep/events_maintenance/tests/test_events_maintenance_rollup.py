"""`services.upkeep.events_maintenance.rollup` — the day-grain rollup of `telemetry_events`.

The rollup writes whole closed UTC days into the ledger tables from the rows of the telemetry
record: token sums and the cost ledger columns per (agent, day, model), turn and exec counters with
the whole-second duration histogram per (agent, day). A recompute is an idempotent full-day
overwrite, and a monotone guard keeps a day that `telemetry_events` holds only in part from lowering
a ledger row that is already larger.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from services.upkeep.events_maintenance import rollup

_DAY = date(2026, 6, 9)
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(db_conn: psycopg.Connection) -> psycopg.Connection:
    """The session test connection in autocommit so rollup writes are visible to the asserts."""
    db_conn.autocommit = True
    return db_conn


def _agent(db: psycopg.Connection, label: str = "a") -> int:
    row = db.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,)).fetchone()
    assert row is not None
    return int(row[0])


def _event(
    db: psycopg.Connection,
    name: str,
    agent_id: int | None,
    *,
    at: datetime | None = None,
    **attributes: Any,
) -> None:
    when = at if at is not None else datetime(2026, 6, 9, 10, 0, tzinfo=UTC)
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'm', 'c', 'p', "
        "'telemetry', %s, 'info', 'test', %s::jsonb)",
        (uuid.uuid4().int % (1 << 62), when, agent_id, name, json.dumps(attributes)),
    )


def _tokens(db: psycopg.Connection, agent_id: int) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT model, llm_calls, tokens_in, tokens_out, tokens_cached, tokens_reasoning, "
        "cost_usd, costed_calls, unpriced_calls FROM agent_model_tokens_daily "
        "WHERE agent_id = %s AND day = %s ORDER BY model",
        (agent_id, _DAY),
    ).fetchall()


def _metrics(db: psycopg.Connection, agent_id: int) -> tuple[Any, ...] | None:
    return db.execute(
        "SELECT turn_total, turn_ok, turn_dur_sum, turn_dur_min, turn_dur_max, turn_dur_hist, "
        "exec_ok, exec_failed FROM agent_metrics_daily WHERE agent_id = %s AND day = %s",
        (agent_id, _DAY),
    ).fetchone()


def test_tokens_are_summed_per_agent_and_model_with_the_cost_ledger_columns(
    db: psycopg.Connection,
) -> None:
    agent = _agent(db)
    usage: dict[str, Any] = {"in_total": 100, "out_total": 50, "cache_read": 10, "reasoning": 5}
    _event(db, "llm_usage", agent, model="m1", cost_usd=0.25, **usage)
    _event(db, "llm_usage", agent, model="m1", cost_usd="0.5", **usage)
    _event(db, "llm_usage", agent, model="m1", **usage)  # no price snapshot: unpriced
    _event(db, "llm_usage", agent, model="m2", in_total="n/a", cost_usd=1.0)

    rollup.roll_day(db, _DAY)

    assert _tokens(db, agent) == [
        ("m1", 3, 300, 150, 30, 15, pytest.approx(0.75), 2, 1),
        ("m2", 1, 0, 0, 0, 0, pytest.approx(1.0), 1, 0),
    ]


def test_a_day_is_the_closed_utc_day_and_unknown_agents_are_skipped(db: psycopg.Connection) -> None:
    agent = _agent(db)
    start = datetime(2026, 6, 9, tzinfo=UTC)
    _event(db, "llm_usage", agent, at=start - timedelta(microseconds=1), model="m", in_total=1)
    _event(db, "llm_usage", agent, at=start, model="m", in_total=10)
    _event(
        db,
        "llm_usage",
        agent,
        at=start + timedelta(days=1) - timedelta(microseconds=1),
        model="m",
        in_total=100,
    )
    _event(db, "llm_usage", agent, at=start + timedelta(days=1), model="m", in_total=1000)
    _event(db, "llm_usage", agent + 9999, at=start, model="m", in_total=5)  # not in agents

    rollup.roll_day(db, _DAY)

    assert [(row[1], row[2]) for row in _tokens(db, agent)] == [(2, 110)]
    assert db.execute("SELECT count(*) FROM agent_model_tokens_daily").fetchone() == (1,)


def test_turn_and_exec_counters_use_the_registry_failure_names(db: psycopg.Connection) -> None:
    agent = _agent(db)
    for ok, duration in ((True, 1.5), (True, 1.9), (False, 3.0), (True, "oops")):
        _event(db, "turn_end", agent, ok=ok, duration_seconds=duration)
    _event(db, "turn_end", agent, ok=True)  # no duration: a turn, but not in the histogram
    _event(db, "exec", agent)
    _event(db, "exec", agent)
    for name in ("exec_failed", "exec_timeout", "exec(failed)"):
        _event(db, name, agent)
    # Prefix lookalikes that are not outcomes: the envelope and boot events, a bare prefix.
    for name in ("exec_envelope", "exec_child_boot", "exec(", "exec_", "execute"):
        _event(db, name, agent)

    rollup.roll_day(db, _DAY)

    row = _metrics(db, agent)
    assert row is not None
    assert row[:5] == (5, 4, pytest.approx(6.4), 1.5, 3.0)
    assert row[5] == {"1": 2, "3": 1}
    assert (row[6], row[7]) == (2, 3)


def test_rows_without_an_agent_do_not_count(db: psycopg.Connection) -> None:
    agent = _agent(db)
    _event(db, "turn_end", None, ok=True, duration_seconds=1.0)
    _event(db, "turn_end", agent, ok=True, duration_seconds=2.0)

    rollup.roll_day(db, _DAY)

    row = _metrics(db, agent)
    assert row is not None and row[0] == 1
    assert db.execute("SELECT count(*) FROM agent_metrics_daily").fetchone() == (1,)


def test_a_recompute_overwrites_with_the_new_rows_and_never_doubles(db: psycopg.Connection) -> None:
    agent = _agent(db)
    _event(db, "llm_usage", agent, model="m", in_total=10, cost_usd=0.1)
    rollup.roll_day(db, _DAY)
    rollup.roll_day(db, _DAY)
    assert [row[1] for row in _tokens(db, agent)] == [1]

    _event(db, "llm_usage", agent, model="m", in_total=10, cost_usd=0.1)  # a late write
    rollup.roll_day(db, _DAY)
    assert [(row[1], row[2]) for row in _tokens(db, agent)] == [(2, 20)]


def test_the_monotone_guard_keeps_a_larger_ledger_row_and_estimated_calls(
    db: psycopg.Connection,
) -> None:
    agent = _agent(db)
    db.execute(
        "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, tokens_in, "
        "estimated_calls) VALUES (%s, %s, 'm', 50, 5000, 7)",
        (agent, _DAY),
    )
    db.execute(
        "INSERT INTO agent_metrics_daily (agent_id, day, turn_total, turn_ok, exec_ok) "
        "VALUES (%s, %s, 40, 30, 12)",
        (agent, _DAY),
    )
    _event(db, "llm_usage", agent, model="m", in_total=10)  # the table holds the day only in part
    _event(db, "turn_end", agent, ok=True, duration_seconds=1.0)

    rollup.roll_day(db, _DAY)

    assert [(row[1], row[2]) for row in _tokens(db, agent)] == [(50, 5000)]
    metrics = _metrics(db, agent)
    assert metrics is not None and metrics[0] == 40

    for _ in range(60):
        _event(db, "llm_usage", agent, model="m", in_total=1)
    rollup.roll_day(db, _DAY)
    assert [(row[1], row[2]) for row in _tokens(db, agent)] == [(61, 70)]
    estimated = db.execute(
        "SELECT estimated_calls FROM agent_model_tokens_daily WHERE agent_id = %s", (agent,)
    ).fetchone()
    assert estimated == (7,)


def test_compute_rollup_recomputes_the_last_closed_days_but_not_today(
    db: psycopg.Connection,
) -> None:
    agent = _agent(db)
    for offset in (0, 1, 7, 8, 9):
        _event(
            db,
            "llm_usage",
            agent,
            at=_NOW - timedelta(days=offset),
            model="m",
            in_total=1,
        )

    result = rollup.compute_rollup(db, now_utc=_NOW)

    assert (result.start_day, result.end_day) == (date(2026, 6, 2), date(2026, 6, 9))
    days = db.execute(
        "SELECT day FROM agent_model_tokens_daily WHERE agent_id = %s ORDER BY day", (agent,)
    ).fetchall()
    # today (offset 0) is left to the live readers; offset 9 is older than the window
    assert [row[0] for row in days] == [date(2026, 6, 2), date(2026, 6, 3), date(2026, 6, 9)]
    assert result.tokens_rows == 3
