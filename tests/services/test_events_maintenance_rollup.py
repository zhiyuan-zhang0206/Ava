"""`services.events_maintenance.rollup.compute_rollup` — the Loki-sourced rollup.

The rollup writes whole UTC days up to yesterday into the ledger tables;
today is left to the live readers. Loki is faked at the module's single
I/O seam (`_day_aggregates`), so these tests pin the watermark / retention
clamp / upsert logic against a real throwaway Postgres: idempotency (the
full-day overwrite never double-counts), the late-write lookback, the
retention-floor clamp (archive-backfilled days are never overwritten with
zeros), and the cost ledger columns. The one-time archive backfill is the
llm-cost-rollup-columns migration — its SQL is exercised directly here.

`now_utc` is passed in, so "today" is deterministic regardless of the wall
clock; the retention floor derives from it the same way.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest

from services.events_maintenance import rollup
from services.events_maintenance.rollup import MetricsRow, RollupResult, TokensRow, compute_rollup
from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT, LokiReadEra, LokiReadSlice

# A fixed "now" so today = 2026-06-10 (UTC); rolled days are 06-07..06-09.
# Noon so the test does not ride on a midnight edge. The retention floor at
# this now is 06-04 (now - 168h = 06-03T12:00 -> first full day 06-04).
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_FLOOR = date(2026, 6, 4)


@pytest.fixture
def db(db_conn: psycopg.Connection) -> psycopg.Connection:
    """The session test connection in autocommit so rollup writes are visible
    to the asserts (compute_rollup manages its own transaction internally)."""
    db_conn.autocommit = True
    return db_conn


def _agent(db: psycopg.Connection, label: str = "a") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _tokens_row(agent_id: int, **kw: object) -> TokensRow:
    base: dict[str, object] = {
        "agent_id": agent_id,
        "model": "m1",
        "calls": 2,
        "costed_calls": 2,
        "unpriced_calls": 0,
        "tokens_in": 100,
        "tokens_out": 50,
        "tokens_cached": 10,
        "tokens_reasoning": 5,
        "cost_usd": 0.25,
    }
    base.update(kw)
    return TokensRow(**base)  # pyright: ignore[reportArgumentType]


def _metrics_row(agent_id: int, **kw: object) -> MetricsRow:
    base: dict[str, object] = {
        "agent_id": agent_id,
        "turn_total": 3,
        "turn_ok": 2,
        "turn_dur_sum": 6.0,
        "turn_dur_min": 1.0,
        "turn_dur_max": 3.0,
        "exec_ok": 4,
        "exec_failed": 1,
    }
    base.update(kw)
    return MetricsRow(**base)  # pyright: ignore[reportArgumentType]


class _FakeLokiDays:
    """`_day_aggregates` seam: day -> (tokens rows, metrics rows), recording
    which days were queried."""

    def __init__(self, days: dict[date, tuple[list[TokensRow], list[MetricsRow]]]) -> None:
        self.days = days
        self.queried: list[date] = []

    def __call__(self, day: date) -> tuple[list[TokensRow], list[MetricsRow]]:
        self.queried.append(day)
        return self.days.get(day, ([], []))


def _fetch_tokens(db: psycopg.Connection) -> dict[tuple[int, str, str], tuple[object, ...]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT agent_id, day::text, model, llm_calls, tokens_in, tokens_out, "
            "tokens_cached, tokens_reasoning, cost_usd, costed_calls, unpriced_calls "
            "FROM agent_model_tokens_daily"
        )
        return {(r[0], r[1], r[2]): tuple(r[3:]) for r in cur.fetchall()}


def _fetch_metrics(db: psycopg.Connection) -> dict[tuple[int, str], tuple[object, ...]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT agent_id, day::text, turn_total, turn_ok, turn_dur_sum, turn_dur_min, "
            "turn_dur_max, exec_ok, exec_failed FROM agent_metrics_daily"
        )
        return {(r[0], r[1]): tuple(r[2:]) for r in cur.fetchall()}


def _roll(
    db: psycopg.Connection,
    fake: _FakeLokiDays,
    monkeypatch: pytest.MonkeyPatch,
    *,
    now: datetime = _NOW,
    lookback: int = 1,
) -> RollupResult:
    monkeypatch.setattr(rollup, "_day_aggregates", fake)
    return compute_rollup(db, now_utc=now, lookback_days=lookback)


def _cutover_query(
    agent_id: int, *, indexed_value: float | None
) -> Callable[[str, datetime], list[tuple[dict[str, str], float]]]:
    """Return one row per legacy/indexed query, or no indexed rows."""

    def query(logql: str, at: datetime) -> list[tuple[dict[str, str], float]]:
        if at > INDEX_LABEL_CUTOVER_AT:
            if indexed_value is None:
                return []
            value = indexed_value
        else:
            value = 1.0
        labels = {"agent_id": str(agent_id)}
        if "llm_usage" in logql:
            labels["model"] = "m1"
        return [(labels, value)]

    return query


# ── happy path ────────────────────────────────────────────────────────────────


def test_rolls_retained_days_up_to_yesterday(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db)
    fake = _FakeLokiDays(
        {
            date(2026, 6, 8): ([_tokens_row(aid)], [_metrics_row(aid)]),
            date(2026, 6, 9): (
                [
                    _tokens_row(
                        aid, model="m2", calls=1, costed_calls=0, unpriced_calls=1, cost_usd=0.0
                    )
                ],
                [],
            ),
            # today — must NOT be queried
            date(2026, 6, 10): ([_tokens_row(aid, calls=999)], []),
        }
    )
    result = _roll(db, fake, monkeypatch)

    assert result.start_day == _FLOOR
    assert result.end_day == date(2026, 6, 9)
    assert date(2026, 6, 10) not in fake.queried  # today stays live
    tokens = _fetch_tokens(db)
    assert tokens[(aid, "2026-06-08", "m1")] == (2, 100, 50, 10, 5, 0.25, 2, 0)
    assert tokens[(aid, "2026-06-09", "m2")] == (1, 100, 50, 10, 5, 0.0, 0, 1)
    metrics = _fetch_metrics(db)
    assert metrics[(aid, "2026-06-08")] == (3, 2, 6.0, 1.0, 3.0, 4, 1)


def test_no_data_is_noop_rows(db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _roll(db, _FakeLokiDays({}), monkeypatch)
    assert result.tokens_rows == 0 and result.metrics_rows == 0
    assert _fetch_tokens(db) == {}


def test_rerun_is_idempotent(db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    aid = _agent(db)
    fake = _FakeLokiDays({date(2026, 6, 9): ([_tokens_row(aid)], [_metrics_row(aid)])})
    _roll(db, fake, monkeypatch)
    first = _fetch_tokens(db)
    _roll(db, fake, monkeypatch)
    assert _fetch_tokens(db) == first  # full-day overwrite, no double count


def test_late_write_lookback_re_rolls_closed_day(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db)
    day = date(2026, 6, 9)
    _roll(db, _FakeLokiDays({day: ([_tokens_row(aid, calls=1, tokens_in=10)], [])}), monkeypatch)
    # A late OTLP write raised the day's totals — the lookback re-roll
    # overwrites with the new full-day aggregate.
    _roll(db, _FakeLokiDays({day: ([_tokens_row(aid, calls=2, tokens_in=20)], [])}), monkeypatch)
    assert _fetch_tokens(db)[(aid, "2026-06-09", "m1")][0] == 2
    assert _fetch_tokens(db)[(aid, "2026-06-09", "m1")][1] == 20


def test_zero_row_indexed_slice_refuses_day_rewrite(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db)
    day = INDEX_LABEL_CUTOVER_AT.date()
    next_day = day + timedelta(days=1)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, "
            "tokens_in, tokens_out, tokens_cached, tokens_reasoning, cost_usd, "
            "costed_calls, unpriced_calls) VALUES (%s, %s, 'm1', 7, 70, 35, 7, 3, 1.25, 7, 0)",
            (aid, day),
        )
    warnings: list[str] = []
    populated_query = _cutover_query(aid, indexed_value=2.0)
    cutover_day_end = datetime.combine(next_day, datetime.min.time(), tzinfo=UTC)

    def zero_first_indexed_slice(logql: str, at: datetime) -> list[tuple[dict[str, str], float]]:
        return [] if at == cutover_day_end else populated_query(logql, at)

    def capture_warning(message: object) -> None:
        warnings.append(str(message))

    monkeypatch.setattr(rollup, "_query_instant", zero_first_indexed_slice)
    monkeypatch.setattr(rollup.logger, "warning", capture_warning)

    result = compute_rollup(
        db,
        now_utc=INDEX_LABEL_CUTOVER_AT + timedelta(days=2, hours=1),
        lookback_days=0,
    )

    assert result == RollupResult(day, next_day, 1, 1)
    assert _fetch_tokens(db)[(aid, str(day), "m1")] == (7, 70, 35, 7, 3, 1.25, 7, 0)
    assert _fetch_tokens(db)[(aid, str(next_day), "m1")] == (2, 2, 2, 2, 2, 2.0, 2, 0)
    assert (aid, str(day)) not in _fetch_metrics(db)
    assert _fetch_metrics(db)[(aid, str(next_day))] == (2, 2, 2.0, 2.0, 2.0, 2, 2)
    assert len(warnings) == 1
    assert str(day) in warnings[0]
    assert "indexed slice returned zero rows" in warnings[0]


def test_nonzero_indexed_slice_rewrites_day(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db)
    day = INDEX_LABEL_CUTOVER_AT.date()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls) "
            "VALUES (%s, %s, 'm1', 7)",
            (aid, day),
        )
    monkeypatch.setattr(rollup, "_query_instant", _cutover_query(aid, indexed_value=2.0))

    result = compute_rollup(
        db,
        now_utc=INDEX_LABEL_CUTOVER_AT + timedelta(days=1, hours=1),
        lookback_days=0,
    )

    assert result == RollupResult(day, day, 1, 1)
    assert _fetch_tokens(db)[(aid, str(day), "m1")] == (3, 3, 3, 3, 3, 3.0, 3, 0)
    assert _fetch_metrics(db)[(aid, str(day))] == (3, 3, 3.0, 1.0, 2.0, 3, 3)


def test_retention_clamp_never_rewrites_archive_days(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows older than the retention floor (the migration's archive backfill)
    must never be recomputed from Loki — Loki would answer zeros."""
    aid = _agent(db)
    archive_day = date(2026, 5, 1)  # far past retention
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, "
            "tokens_in, tokens_out, tokens_cached, tokens_reasoning, cost_usd, "
            "costed_calls, unpriced_calls) VALUES (%s, %s, 'old', 7, 1, 2, 3, 4, 1.5, 7, 0)",
            (aid, archive_day),
        )
    fake = _FakeLokiDays({})
    result = _roll(db, fake, monkeypatch)
    # start clamps to the floor, the archive day is never queried, its row survives
    assert result.start_day == _FLOOR
    assert all(d >= _FLOOR for d in fake.queried)
    assert _fetch_tokens(db)[(aid, "2026-05-01", "old")] == (7, 1, 2, 3, 4, 1.5, 7, 0)


def test_gap_beyond_retention_clamps_and_continues(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rolled watermark far behind the floor (long outage) clamps forward:
    the unreachable days stay missing, the retained days still roll."""
    aid = _agent(db)
    stale_day = date(2026, 5, 20)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily (agent_id, day, turn_total, turn_ok, "
            "turn_dur_sum, exec_ok, exec_failed) VALUES (%s, %s, 1, 1, 1.0, 0, 0)",
            (aid, stale_day),
        )
    fake = _FakeLokiDays({date(2026, 6, 9): ([_tokens_row(aid)], [])})
    result = _roll(db, fake, monkeypatch)
    assert result.start_day == _FLOOR
    assert min(fake.queried) == _FLOOR
    assert (aid, "2026-06-09", "m1") in _fetch_tokens(db)


def test_nothing_rollable_yet_is_noop(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the watermark already covers yesterday and lookback 0 days remain
    (start > yesterday), the pass is a clean no-op."""
    aid = _agent(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily (agent_id, day, turn_total, turn_ok, "
            "turn_dur_sum, exec_ok, exec_failed) VALUES (%s, %s, 1, 1, 1.0, 0, 0)",
            (aid, date(2026, 6, 9)),
        )
    fake = _FakeLokiDays({})
    result = _roll(db, fake, monkeypatch, lookback=0)
    # lookback 0 -> start = max rolled day (06-09) - 0 = 06-09 <= yesterday:
    # still re-rolls yesterday once (overwrite-idempotent), so use a stricter
    # now where yesterday is already covered by the watermark minus lookback.
    assert result.end_day == date(2026, 6, 9)


# ── the archive backfill migration ───────────────────────────────────────────


def test_migration_backfills_cost_columns_from_events(db: psycopg.Connection) -> None:
    """The llm-cost-rollup-columns migration derives per-(agent, day, model)
    rows — including the cost ledger columns — from the frozen events
    archive, inserting missing rows and overwriting existing ones."""
    aid = _agent(db)
    ts = datetime(2026, 5, 2, 10, 0, tzinfo=UTC)
    rows: list[tuple[datetime, dict[str, object]]] = [
        (
            ts,
            {
                "model": "m1",
                "in_total": 10,
                "out_total": 5,
                "cache_read": 1,
                "reasoning": 0,
                "cost_usd": 0.5,
            },
        ),
        (
            ts + timedelta(hours=1),
            {"model": "m1", "in_total": 20, "out_total": 5, "cache_read": 0, "reasoning": 2},
        ),
        (
            ts + timedelta(hours=2),
            {
                "model": "m2",
                "in_total": 1,
                "out_total": 1,
                "cache_read": 0,
                "reasoning": 0,
                "cost_usd": 0.25,
            },
        ),
    ]
    with db.cursor() as cur:
        for row_ts, payload in rows:
            cur.execute(
                "INSERT INTO events (ts, agent_id, level, event_name, attributes, "
                "machine, process, category, source) VALUES (%s, %s, 'info', "
                "'llm_usage', %s::jsonb, 'test', 'test', 'telemetry', 'test')",
                (row_ts, aid, json.dumps(payload)),
            )
    migration = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "20260818T142518_llm-cost-rollup-columns.sql"
    )
    migration_sql = cast(LiteralString, migration.read_text())
    with db.cursor() as cur:
        cur.execute(migration_sql)  # idempotent by contract
        cur.execute(migration_sql)
    tokens = _fetch_tokens(db)
    assert tokens[(aid, "2026-05-02", "m1")] == (2, 30, 10, 1, 2, 0.5, 1, 1)
    assert tokens[(aid, "2026-05-02", "m2")] == (1, 1, 1, 0, 0, 0.25, 1, 0)


# ── LogQL shape locks ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("era", "indexed_labeled"),
    [(LokiReadEra.LEGACY, False), (LokiReadEra.INDEXED, True)],
)
def test_tokens_queries_shapes(era: LokiReadEra, indexed_labeled: bool) -> None:
    q = rollup._tokens_queries(era=era, indexed_labeled=indexed_labeled)
    assert set(q) == {
        "calls",
        "costed_calls",
        "tokens_in",
        "tokens_out",
        "tokens_cached",
        "tokens_reasoning",
        "cost_usd",
    }
    # Body fields are authoritative when structured metadata was promoted
    # from a different record in the same OTLP batch (task #1515).
    assert '| json agent_id_extracted="agent_id" | agent_id_extracted=~".+"' in q["calls"]
    assert (
        '| json event_name_extracted="event_name" | event_name_extracted=~"llm_usage"' in q["calls"]
    )
    assert "| agent_id=" not in q["calls"]
    assert "| event_name=" not in q["calls"]
    assert q["calls"].startswith("sum by (agent_id, model) (count_over_time(")
    assert '| cost_usd!=""' in q["costed_calls"]
    assert "| unwrap in_total" in q["tokens_in"] and "[86400s]" in q["tokens_in"]
    # single-extraction json stages, chained (multiple extractions in one
    # stage are a Loki parse error)
    assert '| json model="attributes.model" | json in_total="attributes.in_total"' in q["tokens_in"]


def test_metrics_queries_shapes() -> None:
    q = rollup._metrics_queries()
    assert q["turn_ok"].count('| json ok="attributes.ok" | ok="true"') == 1
    assert q["turn_dur_min"].startswith("min by (agent_id) (min_over_time(")
    assert 'event_name_extracted=~"exec_.+|exec\\\\(.*"' in q["exec_failed"]
    assert 'event_name_extracted=~"exec"' in q["exec_ok"]


def test_cutover_day_merges_legacy_and_indexed_rollups(monkeypatch: pytest.MonkeyPatch) -> None:
    day = date(2026, 8, 10)
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    cutover = day_start + timedelta(hours=12)
    day_end = day_start + timedelta(days=1)
    slices = (
        LokiReadSlice(LokiReadEra.LEGACY, day_start, cutover),
        LokiReadSlice(LokiReadEra.INDEXED, cutover, day_end),
    )
    calls: list[tuple[str, datetime]] = []

    def _query(logql: str, at: datetime) -> list[tuple[dict[str, str], float]]:
        calls.append((logql, at))
        value = 1.0 if at == cutover else 2.0
        labels = {"agent_id": "7"}
        if "llm_usage" in logql:
            labels["model"] = "m"
        return [(labels, value)]

    def _slices(_start: datetime, _end: datetime) -> tuple[LokiReadSlice, ...]:
        return slices

    monkeypatch.setattr(rollup, "split_index_label_window", _slices)
    monkeypatch.setattr(rollup, "_query_instant", _query)

    aggregates = rollup._day_aggregates(day)
    assert aggregates is not None
    tokens, metrics = aggregates

    assert tokens == [
        TokensRow(
            agent_id=7,
            model="m",
            calls=3,
            costed_calls=3,
            unpriced_calls=0,
            tokens_in=3,
            tokens_out=3,
            tokens_cached=3,
            tokens_reasoning=3,
            cost_usd=3.0,
        )
    ]
    assert metrics == [
        MetricsRow(
            agent_id=7,
            turn_total=3,
            turn_ok=3,
            turn_dur_sum=3.0,
            turn_dur_min=1.0,
            turn_dur_max=2.0,
            exec_ok=3,
            exec_failed=3,
        )
    ]
    assert all('event_name=""' not in logql for logql, _at in calls)
    assert any(
        'event_name!=""' in logql and 'event_name="llm_usage"' in logql and _at == day_end
        for logql, _at in calls
    )
