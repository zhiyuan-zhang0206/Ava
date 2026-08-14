"""`services.events_maintenance.rollup.compute_rollup` — the Since-Birth rollup.

The rollup must reproduce the live reader aggregations exactly (so PR3 can swap
the since-birth readers onto it without changing a single number), and it must
split the timeline at UTC midnight: whole days up to yesterday are rolled, today
is left to the live readers. These tests pin both, plus idempotency (the full-day
overwrite upsert never double-counts on a re-run), the late-write lookback, and
the exec_ok/exec_failed + empty-model edge cases the field extraction depends on.

`now_utc` is passed in, so "today" is deterministic regardless of the wall clock:
every test fixes it and lays its events on days strictly before it.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import psycopg
import pytest

from services.events_maintenance.rollup import compute_rollup

# A fixed "now" so today = 2026-06-10 (UTC); events land on 06-07..06-09 (rolled)
# and 06-10 (today, live). Noon so the test does not ride on a midnight edge.
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(db_conn: psycopg.Connection) -> psycopg.Connection:
    """The session test connection in autocommit so seeds are visible to
    compute_rollup's own transaction and rollup writes are visible to the asserts
    (compute_rollup manages its own transaction internally)."""
    db_conn.autocommit = True
    return db_conn


def _agent(db: psycopg.Connection, label: str = "a") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _event(
    db: psycopg.Connection,
    agent_id: int | None,
    ts: datetime,
    event: str,
    payload: dict[str, object],
    *,
    level: str = "INFO",
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, level, event_name, attributes, machine, process, category, source) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, 'test', 'test', 'telemetry', 'test')",
            (ts, agent_id, level.lower(), event, json.dumps(payload)),
        )


def _llm(
    db: psycopg.Connection,
    agent_id: int | None,
    ts: datetime,
    *,
    model: str | None,
    in_total: int = 0,
    out_total: int = 0,
    cache_read: int = 0,
    reasoning: int = 0,
) -> None:
    payload: dict[str, object] = {
        "in_total": in_total,
        "out_total": out_total,
        "cache_read": cache_read,
        "reasoning": reasoning,
    }
    if model is not None:
        payload["model"] = model
    _event(db, agent_id, ts, "llm_usage", payload)


def _turn(db: psycopg.Connection, agent_id: int, ts: datetime, *, ok: bool, dur: float) -> None:
    _event(db, agent_id, ts, "turn_end", {"ok": ok, "duration_seconds": dur})


def _fetch_tokens(db: psycopg.Connection) -> dict[tuple[int, str, str], tuple[int, ...]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT agent_id, day::text, model, llm_calls, tokens_in, tokens_out, "
            "tokens_cached, tokens_reasoning FROM agent_model_tokens_daily"
        )
        return {(r[0], r[1], r[2]): tuple(r[3:]) for r in cur.fetchall()}


def _fetch_metrics(db: psycopg.Connection) -> dict[tuple[int, str], tuple]:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    with db.cursor() as cur:
        cur.execute(
            "SELECT agent_id, day::text, turn_total, turn_ok, turn_dur_sum, turn_dur_min, "
            "turn_dur_max, exec_ok, exec_failed FROM agent_metrics_daily"
        )
        return {(r[0], r[1]): tuple(r[2:]) for r in cur.fetchall()}  # pyright: ignore[reportUnknownVariableType]


# ── backfill correctness, cross-checked against the live reader SQL ───────────


def test_backfill_matches_live_reader_aggregations(db: psycopg.Connection) -> None:
    """Rollup token/cost aggregates must equal the LIVE reader — the same
    function the inspector panel calls (`agent_inspect._agent_cost`) — not a
    hand-copied SQL twin (appendix scenario 10: a self-comparison against
    re-typed SQL is circular and drifts silently). Per-agent totals must match
    exactly; per-(agent, model) rows must sum to the same totals."""
    a1 = _agent(db, "one")
    a2 = _agent(db, "two")
    d8 = datetime(2026, 6, 8, 10, 0, tzinfo=UTC)
    d9 = datetime(2026, 6, 9, 10, 0, tzinfo=UTC)
    # a1: two models across two days.
    _llm(db, a1, d8, model="deepseek-v4-pro", in_total=100, out_total=20, cache_read=5, reasoning=1)
    _llm(db, a1, d8, model="deepseek-v4-pro", in_total=200, out_total=40, cache_read=7, reasoning=2)
    _llm(db, a1, d9, model="gpt-5", in_total=10, out_total=3, cache_read=0, reasoning=0)
    # a2: one model, one day.
    _llm(db, a2, d9, model="deepseek-v4-pro", in_total=50, out_total=8, cache_read=1, reasoning=0)

    result = compute_rollup(db, now_utc=_NOW, lookback_days=3)
    assert (result.start_day, result.end_day) == (d8.date(), date(2026, 6, 9))

    # The reference: the events-table totals the rollup must reproduce. This
    # used to call the live inspector reader `agent_inspect._agent_cost(cur,
    # aid, ...)`; that reader now aggregates from Loki (task #1197), so the
    # expectation is expressed directly over the events table — the same SQL
    # shape the old reader ran (COUNT + SUM over the llm_usage payload keys).
    with db.cursor() as cur:
        cur.execute(
            "SELECT agent_id, COUNT(*), "
            "  COALESCE(SUM((attributes->>'in_total')::bigint), 0), "
            "  COALESCE(SUM((attributes->>'out_total')::bigint), 0), "
            "  COALESCE(SUM((attributes->>'cache_read')::bigint), 0), "
            "  COALESCE(SUM((attributes->>'reasoning')::bigint), 0) "
            "FROM events WHERE event_name = 'llm_usage' "
            "  AND category IN ('telemetry', 'log') "
            "GROUP BY agent_id"
        )
        live = {r[0]: tuple(int(x) for x in r[1:]) for r in cur.fetchall()}

    # Rollup totals (across models/days) per agent.
    with db.cursor() as cur:
        cur.execute(
            "SELECT agent_id, SUM(llm_calls), SUM(tokens_in), SUM(tokens_out), "
            "  SUM(tokens_cached), SUM(tokens_reasoning) "
            "FROM agent_model_tokens_daily GROUP BY agent_id"
        )
        rollup_by_agent = {r[0]: tuple(int(x) for x in r[1:]) for r in cur.fetchall()}
        # fleet_graph total_tokens = SUM(in)+SUM(out) per agent, no time filter —
        # rollup must reproduce it exactly.
        cur.execute(
            "SELECT agent_id, SUM(tokens_in + tokens_out) FROM agent_model_tokens_daily "
            "GROUP BY agent_id"
        )
        rollup_total = {r[0]: int(r[1]) for r in cur.fetchall()}

    for aid in (a1, a2):
        cost = live[aid]  # (llm_calls, tokens_in, tokens_out, tokens_cached, tokens_reasoning)
        assert rollup_by_agent[aid] == cost, (
            f"rollup diverged from the events-table totals for agent {aid}"
        )
        assert rollup_total[aid] == cost[1] + cost[2]


def test_backfill_day_grain_token_rows(db: psycopg.Connection) -> None:
    """Same model on two days stays two rows; two models on one day stays two rows."""
    a1 = _agent(db)
    d8 = datetime(2026, 6, 8, 1, 0, tzinfo=UTC)
    d9 = datetime(2026, 6, 9, 1, 0, tzinfo=UTC)
    _llm(db, a1, d8, model="m", in_total=1, out_total=1)
    _llm(db, a1, d9, model="m", in_total=2, out_total=2)
    _llm(db, a1, d9, model="n", in_total=4, out_total=4)

    compute_rollup(db, now_utc=_NOW, lookback_days=3)
    tokens = _fetch_tokens(db)
    assert tokens[(a1, "2026-06-08", "m")] == (1, 1, 1, 0, 0)
    assert tokens[(a1, "2026-06-09", "m")] == (1, 2, 2, 0, 0)
    assert tokens[(a1, "2026-06-09", "n")] == (1, 4, 4, 0, 0)


def test_turn_and_exec_metrics(db: psycopg.Connection) -> None:
    """turn_total/ok, duration sum/min/max, and the exec_ok vs exec_failed split."""
    a1 = _agent(db)
    d9 = datetime(2026, 6, 9, 8, 0, tzinfo=UTC)
    _turn(db, a1, d9, ok=True, dur=2.0)
    _turn(db, a1, d9, ok=True, dur=5.0)
    _turn(db, a1, d9, ok=False, dur=1.0)
    # exec family: one ok, three failures (each a distinct failure event shape).
    _event(db, a1, d9, "exec", {})
    _event(db, a1, d9, "exec_failed", {})
    _event(db, a1, d9, "exec_thread_stuck", {})
    _event(db, a1, d9, "exec_timeout", {})

    compute_rollup(db, now_utc=_NOW, lookback_days=3)
    metrics = _fetch_metrics(db)  # pyright: ignore[reportUnknownVariableType]
    turn_total, turn_ok, dur_sum, dur_min, dur_max, exec_ok, exec_failed = metrics[  # pyright: ignore[reportUnknownVariableType]
        (a1, "2026-06-09")
    ]
    assert (turn_total, turn_ok) == (3, 2)
    assert (dur_sum, dur_min, dur_max) == (8.0, 1.0, 5.0)
    assert (exec_ok, exec_failed) == (1, 3)


def test_missing_model_folds_to_empty_string(db: psycopg.Connection) -> None:
    """An llm_usage row with no `model` field rolls under model = '' (not NULL)."""
    a1 = _agent(db)
    d9 = datetime(2026, 6, 9, 3, 0, tzinfo=UTC)
    _llm(db, a1, d9, model=None, in_total=7, out_total=2)
    compute_rollup(db, now_utc=_NOW, lookback_days=3)
    tokens = _fetch_tokens(db)
    assert tokens[(a1, "2026-06-09", "")] == (1, 7, 2, 0, 0)


def test_null_agent_id_excluded(db: psycopg.Connection) -> None:
    """Gateway/system llm_usage with no agent_id is not rolled (FK is NOT NULL)."""
    a1 = _agent(db)
    d9 = datetime(2026, 6, 9, 3, 0, tzinfo=UTC)
    _llm(db, None, d9, model="m", in_total=99, out_total=99)
    _llm(db, a1, d9, model="m", in_total=1, out_total=1)
    compute_rollup(db, now_utc=_NOW, lookback_days=3)
    tokens = _fetch_tokens(db)
    assert set(tokens) == {(a1, "2026-06-09", "m")}
    assert tokens[(a1, "2026-06-09", "m")] == (1, 1, 1, 0, 0)


# ── the UTC-midnight split: today is live, not rolled ─────────────────────────


def test_today_is_not_rolled(db: psycopg.Connection) -> None:
    """Rows on today (>= today 00:00 UTC) stay out of the rollup; yesterday's are
    in — the exact stitch boundary PR3's rollup(day<today)+live(today) relies on."""
    a1 = _agent(db)
    # yesterday 23:59:59 UTC → rolled; today 00:00:01 UTC → live (excluded).
    y_late = datetime(2026, 6, 9, 23, 59, 59, tzinfo=UTC)
    t_early = datetime(2026, 6, 10, 0, 0, 1, tzinfo=UTC)
    _llm(db, a1, y_late, model="m", in_total=10, out_total=1)
    _llm(db, a1, t_early, model="m", in_total=500, out_total=500)

    result = compute_rollup(db, now_utc=_NOW, lookback_days=3)
    assert result.end_day == date(2026, 6, 9)
    tokens = _fetch_tokens(db)
    assert set(tokens) == {(a1, "2026-06-09", "m")}
    assert tokens[(a1, "2026-06-09", "m")] == (1, 10, 1, 0, 0)  # today's 500/500 absent


def test_only_today_data_is_noop(db: psycopg.Connection) -> None:
    """A brand-new cluster with only today's rows has no whole day to roll."""
    a1 = _agent(db)
    _llm(db, a1, datetime(2026, 6, 10, 6, 0, tzinfo=UTC), model="m", in_total=5, out_total=5)
    result = compute_rollup(db, now_utc=_NOW, lookback_days=3)
    assert (result.start_day, result.end_day, result.tokens_rows) == (None, None, 0)
    assert _fetch_tokens(db) == {}


def test_no_events_is_noop(db: psycopg.Connection) -> None:
    result = compute_rollup(db, now_utc=_NOW, lookback_days=3)
    assert (result.start_day, result.metrics_rows, result.tokens_rows) == (None, 0, 0)
    assert _fetch_tokens(db) == {} and _fetch_metrics(db) == {}


# ── idempotency + the late-write lookback ─────────────────────────────────────


def test_rerun_is_idempotent(db: psycopg.Connection) -> None:
    """The full-day overwrite upsert never double-counts on a re-run."""
    a1 = _agent(db)
    d9 = datetime(2026, 6, 9, 4, 0, tzinfo=UTC)
    _llm(db, a1, d9, model="m", in_total=3, out_total=1)
    _turn(db, a1, d9, ok=True, dur=2.0)

    compute_rollup(db, now_utc=_NOW, lookback_days=3)
    first_tokens, first_metrics = _fetch_tokens(db), _fetch_metrics(db)  # pyright: ignore[reportUnknownVariableType]
    compute_rollup(db, now_utc=_NOW, lookback_days=3)
    assert _fetch_tokens(db) == first_tokens
    assert _fetch_metrics(db) == first_metrics
    assert first_tokens[(a1, "2026-06-09", "m")] == (1, 3, 1, 0, 0)


def test_late_write_into_rolled_day_is_picked_up(db: psycopg.Connection) -> None:
    """A row written into an already-rolled day within the lookback window is
    absorbed on the next run (the recompute re-derives the whole day)."""
    a1 = _agent(db)
    d9 = datetime(2026, 6, 9, 4, 0, tzinfo=UTC)
    _llm(db, a1, d9, model="m", in_total=3, out_total=1)
    compute_rollup(db, now_utc=_NOW, lookback_days=3)
    assert _fetch_tokens(db)[(a1, "2026-06-09", "m")] == (1, 3, 1, 0, 0)

    # Late write into 06-09, then re-run at the same "now" (06-09 is within the
    # 3-day lookback of yesterday=06-09).
    _llm(db, a1, datetime(2026, 6, 9, 22, 0, tzinfo=UTC), model="m", in_total=10, out_total=4)
    compute_rollup(db, now_utc=_NOW, lookback_days=3)
    assert _fetch_tokens(db)[(a1, "2026-06-09", "m")] == (2, 13, 5, 0, 0)


def test_empty_family_does_not_retrigger_full_backfill(db: psycopg.Connection) -> None:
    """A tokens-only cluster (agent_metrics_daily stays empty forever) must not
    re-backfill from the earliest day every run — the populated table's watermark
    governs the recompute range."""
    a1 = _agent(db)
    # llm_usage on 06-01 and 06-08; no turn/exec, so agent_metrics_daily is empty.
    _llm(db, a1, datetime(2026, 6, 1, 4, 0, tzinfo=UTC), model="m", in_total=1, out_total=1)
    _llm(db, a1, datetime(2026, 6, 8, 4, 0, tzinfo=UTC), model="m", in_total=2, out_total=2)

    first = compute_rollup(db, now_utc=_NOW, lookback_days=3)
    assert first.start_day == date(2026, 6, 1)  # backfill from earliest

    # Second run: tokens max is 06-08, so start = 06-08 - 3 = 06-05 (the watermark),
    # NOT 06-01 (a full re-backfill), even though the metrics table is still empty.
    second = compute_rollup(db, now_utc=_NOW, lookback_days=3)
    assert second.start_day == date(2026, 6, 5)


def test_downtime_gap_fully_recovered(db: psycopg.Connection) -> None:
    """A multi-day gap beyond the lookback is still fully rolled: start is anchored
    to the last rolled day and end is always yesterday, so the whole gap is in range."""
    a1 = _agent(db)
    # First run rolls only 06-03 (now=06-04). Then the daemon is "down" for days.
    _llm(db, a1, datetime(2026, 6, 3, 4, 0, tzinfo=UTC), model="m", in_total=1, out_total=1)
    compute_rollup(db, now_utc=datetime(2026, 6, 4, 12, 0, tzinfo=UTC), lookback_days=3)
    # Rows accrue on 06-05..06-08 while down; next run is 06-10 (gap = 06-06..06-08
    # are >3 days back from yesterday=06-09, beyond the lookback).
    for d in (6, 7, 8):
        _llm(db, a1, datetime(2026, 6, d, 4, 0, tzinfo=UTC), model="m", in_total=d, out_total=0)
    compute_rollup(db, now_utc=_NOW, lookback_days=3)
    tokens = _fetch_tokens(db)
    for d in (3, 6, 7, 8):
        assert (a1, f"2026-06-0{d}", "m") in tokens, f"day 06-0{d} missing from rollup"
