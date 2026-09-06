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

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from pydantic import ValidationError

from services.events_maintenance import rollup
from services.events_maintenance.rollup import MetricsRow, RollupResult, TokensRow, compute_rollup
from shared.config.daemon import DaemonSettings
from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT, LokiReadEra, LokiReadSlice

# A fixed "now" so today = 2026-06-10 (UTC); retained days are 06-08..06-09.
# Noon so the test does not ride on a midnight edge. The retention floor at
# this now is 06-08 (now - 84h = 06-07T00:00 -> first full day 06-08).
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
_FLOOR = date(2026, 6, 8)


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
        "turn_dur_hist": {1: 1, 2: 1, 3: 1},
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

    def __call__(
        self, day: date, *, now: datetime | None = None
    ) -> tuple[list[TokensRow], list[MetricsRow]]:
        del now
        self.queried.append(day)
        return self.days.get(day, ([], []))


class _FakeSourceCounts:
    def __init__(self, counts: Mapping[date, int | None]) -> None:
        self.counts = counts
        self.queried: list[date] = []

    def __call__(self, day: date, *, now: datetime | None = None) -> int | None:
        del now
        self.queried.append(day)
        return self.counts.get(day, 0)


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
            "turn_dur_max, turn_dur_hist, exec_ok, exec_failed FROM agent_metrics_daily"
        )
        return {(r[0], r[1]): tuple(r[2:]) for r in cur.fetchall()}


def _fetch_state(db: psycopg.Connection) -> dict[str, tuple[str, int, str | None]]:
    with db.cursor() as cur:
        cur.execute("SELECT day::text, status, source_count, error FROM rollup_day_state")
        return {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}


def _state(db: psycopg.Connection, day: date, count: int, status: str = "rolled") -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO rollup_day_state (day, status, source_count) VALUES (%s, %s, %s)",
            (day, status, count),
        )


def _roll(
    db: psycopg.Connection,
    fake: _FakeLokiDays,
    monkeypatch: pytest.MonkeyPatch,
    *,
    now: datetime = _NOW,
    lookback: int = 1,
    counts: dict[date, int | None] | None = None,
    pass_deadline_s: float | None = None,
) -> RollupResult:
    monkeypatch.setattr(rollup, "_day_aggregates", fake)
    inferred = {day: int(bool(tokens or metrics)) for day, (tokens, metrics) in fake.days.items()}
    monkeypatch.setattr(
        rollup, "_day_source_count", _FakeSourceCounts(counts or inferred), raising=False
    )
    return compute_rollup(
        db,
        now_utc=now,
        lookback_days=lookback,
        pass_deadline_s=pass_deadline_s,
    )


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
        if 'pattern "<bucket>"' in logql:
            labels["bucket"] = "1"
        return [(labels, value)]

    return query


def test_query_instant_enters_the_daemon_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    assert hasattr(rollup, "_query_budget")
    assert rollup._query_budget._capacity == 1
    transitions: list[str] = []

    class FakeBudget:
        @contextmanager
        def slot(self):
            transitions.append("entered")
            yield
            transitions.append("released")

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            assert transitions == ["entered"]
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"data":{"result":[]}}'

    def open_response(*_args: object, **_kwargs: object) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(rollup, "_query_budget", FakeBudget())
    monkeypatch.setattr(rollup.urllib.request, "urlopen", open_response)

    assert rollup._query_instant('sum(rate({service_name="test"}[1m]))', _NOW) == []
    assert transitions == ["entered", "released"]


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
    assert metrics[(aid, "2026-06-08")] == (
        3,
        2,
        6.0,
        1.0,
        3.0,
        {"1": 1, "2": 1, "3": 1},
        4,
        1,
    )


def test_unknown_agent_rows_are_skipped_without_aborting_rollup(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db)
    unknown_aid = 424242
    fake = _FakeLokiDays(
        {
            date(2026, 6, 8): (
                [_tokens_row(aid)],
                [_metrics_row(aid), _metrics_row(unknown_aid)],
            ),
            date(2026, 6, 9): (
                [_tokens_row(aid), _tokens_row(unknown_aid)],
                [_metrics_row(aid)],
            ),
        }
    )
    warnings: list[str] = []

    def _fake_warning(message: object, *args: object, **kwargs: object) -> None:
        warnings.append(str(message))

    monkeypatch.setattr(rollup.logger, "warning", _fake_warning)

    result = _roll(db, fake, monkeypatch)

    assert result == RollupResult(_FLOOR, date(2026, 6, 9), 2, 2)
    assert set(_fetch_tokens(db)) == {
        (aid, "2026-06-08", "m1"),
        (aid, "2026-06-09", "m1"),
    }
    assert set(_fetch_metrics(db)) == {
        (aid, "2026-06-08"),
        (aid, "2026-06-09"),
    }
    assert len(warnings) == 1
    assert "424242" in warnings[0]
    assert "tokens rows dropped: 1" in warnings[0]
    assert "metrics rows dropped: 1" in warnings[0]


def test_first_pass_rolls_the_whole_retained_window_once(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeLokiDays({})
    result = _roll(db, fake, monkeypatch)
    expected = [_FLOOR + timedelta(days=offset) for offset in range(2)]
    assert fake.queried == expected
    assert _fetch_state(db) == {str(day): ("rolled", 0, None) for day in expected}
    assert result.tokens_rows == 0 and result.metrics_rows == 0
    assert _fetch_tokens(db) == {}


def test_matching_source_count_skips_full_reroll(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = date(2026, 6, 9)
    _state(db, day, 7)
    fake = _FakeLokiDays({day: ([], [])})
    result = _roll(db, fake, monkeypatch, lookback=0, counts={day: 7})
    assert result == RollupResult(None, None, 0, 0)
    assert fake.queried == []
    assert _fetch_state(db)[str(day)] == ("rolled", 7, None)


def test_changed_source_count_rerolls_and_advances_watermark(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db)
    day = date(2026, 6, 9)
    _state(db, day, 1)
    fake = _FakeLokiDays({day: ([_tokens_row(aid)], [])})
    result = _roll(db, fake, monkeypatch, lookback=0, counts={day: 2})
    assert result == RollupResult(day, day, 0, 1)
    assert fake.queried == [day]
    assert _fetch_state(db)[str(day)] == ("rolled", 2, None)


def test_failed_day_stays_dirty_until_success(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db)
    failed_day = date(2026, 6, 8)
    yesterday = date(2026, 6, 9)
    _state(db, failed_day, 1, "failed")
    _state(db, yesterday, 1)
    fake = _FakeLokiDays({failed_day: ([], [_metrics_row(aid)])})
    result = _roll(db, fake, monkeypatch, lookback=0, counts={failed_day: 1, yesterday: 1})
    assert result == RollupResult(failed_day, failed_day, 1, 0)
    assert fake.queried == [failed_day]
    assert _fetch_state(db)[str(failed_day)] == ("rolled", 1, None)


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
    reroll = _FakeLokiDays({day: ([_tokens_row(aid, calls=2, tokens_in=20)], [])})
    _roll(db, reroll, monkeypatch)
    assert reroll.queried == [day]
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
    _state(db, day, 1, "failed")
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
    assert _fetch_metrics(db)[(aid, str(next_day))] == (2, 2, 2.0, 2.0, 2.0, {"1": 2}, 2, 2)
    assert len(warnings) == 1
    assert str(day) in warnings[0]
    assert "indexed slice returned zero rows" in warnings[0]
    assert _fetch_state(db)[str(day)][0] == "failed"


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
    _state(db, day, 1, "failed")
    monkeypatch.setattr(rollup, "_query_instant", _cutover_query(aid, indexed_value=2.0))

    result = compute_rollup(
        db,
        now_utc=INDEX_LABEL_CUTOVER_AT + timedelta(days=1, hours=1),
        lookback_days=0,
    )

    assert result == RollupResult(day, day, 1, 1)
    # `now_utc` pins the split clock (index labels threading): 2026-08-24T12:00Z
    # is inside the legacy-read grace, so the cutover day is the legacy 1.0
    # + indexed 2.0 merge — deterministic regardless of the wall clock.
    assert _fetch_tokens(db)[(aid, str(day), "m1")] == (3, 3, 3, 3, 3, 3.0, 3, 0)
    assert _fetch_metrics(db)[(aid, str(day))] == (3, 3, 3.0, 1.0, 2.0, {"1": 3}, 3, 3)


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


def test_missing_state_triggers_catchup_despite_existing_ledger(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ledger maxima are not dirty watermarks after the state migration."""
    aid = _agent(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily (agent_id, day, turn_total, turn_ok, "
            "turn_dur_sum, exec_ok, exec_failed) VALUES (%s, %s, 1, 1, 1.0, 0, 0)",
            (aid, date(2026, 6, 9)),
        )
    fake = _FakeLokiDays({})
    result = _roll(db, fake, monkeypatch, lookback=0)
    assert result == RollupResult(_FLOOR, date(2026, 6, 9), 0, 0)
    assert fake.queried == [_FLOOR + timedelta(days=offset) for offset in range(2)]


def test_pass_deadline_stops_before_remaining_dirty_days(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    days = [_FLOOR + timedelta(days=offset) for offset in range(2)]
    for day in days:
        _state(db, day, 0, "failed")
    clock = [0.0]
    queried: list[date] = []

    def slow_aggregates(
        day: date, *, now: datetime | None = None
    ) -> tuple[list[TokensRow], list[MetricsRow]]:
        del now
        queried.append(day)
        clock[0] = 2.0
        return [], []

    warnings: list[str] = []

    def capture_warning(message: object) -> None:
        warnings.append(str(message))

    monkeypatch.setattr(rollup, "_day_aggregates", slow_aggregates)
    monkeypatch.setattr(rollup, "_day_source_count", _FakeSourceCounts({}))
    monkeypatch.setattr(rollup.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rollup.logger, "warning", capture_warning)

    result = compute_rollup(db, now_utc=_NOW, lookback_days=0, pass_deadline_s=1.0)

    assert result == RollupResult(days[0], days[0], 0, 0)
    assert queried == [days[0]]
    state = _fetch_state(db)
    assert state[str(days[0])][0] == "rolled"
    assert all(state[str(day)][0] == "failed" for day in days[1:])
    assert str(days[1]) in warnings[-1] and str(days[-1]) in warnings[-1]


def test_probe_failure_rerolls_but_keeps_previous_watermark(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db)
    day = date(2026, 6, 9)
    _state(db, day, 5)
    fake = _FakeLokiDays({day: ([_tokens_row(aid)], [])})
    warnings: list[str] = []

    def capture_warning(message: object) -> None:
        warnings.append(str(message))

    monkeypatch.setattr(rollup.logger, "warning", capture_warning)

    result = _roll(db, fake, monkeypatch, lookback=0, counts={day: None})

    assert result == RollupResult(day, day, 0, 1)
    assert fake.queried == [day]
    assert _fetch_state(db)[str(day)] == ("rolled", 5, None)
    assert any("source-count probe failed" in warning for warning in warnings)


# ── the archive backfill migration ───────────────────────────────────────────


def test_rollup_day_state_migration_creates_writable_watermark(
    db: psycopg.Connection,
) -> None:
    migrations = sorted(
        (Path(__file__).resolve().parents[2] / "migrations").glob("*_rollup-day-state.sql")
    )
    assert len(migrations) == 1
    migration_sql = cast(LiteralString, migrations[0].read_text())
    with db.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS rollup_day_state")
        cur.execute(migration_sql)
        cur.execute(migration_sql)  # baseline convergence and repeated apply are safe
        cur.execute(
            "INSERT INTO rollup_day_state (day, source_count) VALUES (%s, %s)",
            (date(2026, 6, 9), 7),
        )
        cur.execute(
            "SELECT status, source_count, error FROM rollup_day_state WHERE day = %s",
            (date(2026, 6, 9),),
        )
        assert cur.fetchone() == ("rolled", 7, None)


def test_rollup_settings_defaults_aliases_and_lookback_bound() -> None:
    fields = DaemonSettings.model_fields
    assert fields["events_rollup_pass_deadline_s"].default == 1200.0
    assert fields["events_rollup_pass_deadline_s"].alias == "AVA_EVENTS_ROLLUP_PASS_DEADLINE_S"
    assert fields["events_rollup_late_write_lookback_days"].default == 1
    assert (
        fields["events_rollup_late_write_lookback_days"].alias
        == "AVA_EVENTS_ROLLUP_LATE_WRITE_LOOKBACK_DAYS"
    )
    with pytest.raises(ValidationError):
        DaemonSettings.model_validate({"AVA_EVENTS_ROLLUP_LATE_WRITE_LOOKBACK_DAYS": 0})


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
    assert q["turn_dur_hist"].startswith("sum by (agent_id, bucket) (count_over_time(")
    assert (
        '| json duration_seconds="attributes.duration_seconds" '
        '| __error__="" | line_format "{{ floor .duration_seconds }}" | pattern "<bucket>"'
    ) in q["turn_dur_hist"]
    assert 'event_name_extracted=~"exec_.+|exec\\\\(.*"' in q["exec_failed"]
    assert 'event_name_extracted=~"exec"' in q["exec_ok"]


def test_source_count_query_uses_the_union_body_truth_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, datetime]] = []

    def query(logql: str, at: datetime) -> list[tuple[dict[str, str], float]]:
        calls.append((logql, at))
        return [({}, 5.0)]

    monkeypatch.setattr(rollup, "_query_instant", query)
    assert rollup._day_source_count(date(2026, 6, 9), now=_NOW) == 5
    assert len(calls) == 1
    logql, _at = calls[0]
    assert logql.startswith("sum(count_over_time((") and logql.endswith(")[86400s]))")
    assert 'event_name_extracted=~"llm_usage|turn_end|exec|exec_.+|exec\\\\(.*"' in logql


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
        if 'pattern "<bucket>"' in logql:
            labels["bucket"] = "1" if at == cutover else "2"
            return [(labels, value), ({**labels, "bucket": "not-an-integer"}, value)]
        return [(labels, value)]

    def _slices(
        _start: datetime, _end: datetime, *, now: datetime | None = None
    ) -> tuple[LokiReadSlice, ...]:
        del now
        return slices

    monkeypatch.setattr(rollup, "split_index_label_window", _slices)
    monkeypatch.setattr(rollup, "_query_instant", _query)

    aggregates = rollup._day_aggregates(day, now=_NOW)
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
            turn_dur_hist={1: 1, 2: 2},
            exec_ok=3,
            exec_failed=3,
        )
    ]
    assert all('event_name=""' not in logql for logql, _at in calls)
    assert any(
        'event_name!=""' in logql and 'event_name="llm_usage"' in logql and _at == day_end
        for logql, _at in calls
    )
