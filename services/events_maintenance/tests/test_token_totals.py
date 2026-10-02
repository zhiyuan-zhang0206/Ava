"""`services.events_maintenance.token_totals` — the whole-life token sum folded from the ledger.

Days older than the recompute horizon are folded once into `agent_model_tokens_total` and the watermark
moves up to them; a fold is idempotent, a rebuild refolds from scratch after old days changed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from services.events_maintenance import rollup, token_totals

_TODAY = date(2026, 6, 30)


@pytest.fixture
def db(db_conn: psycopg.Connection) -> psycopg.Connection:
    db_conn.autocommit = True
    db_conn.execute("DELETE FROM agent_model_tokens_total")
    db_conn.execute("DELETE FROM agent_model_tokens_total_through")
    return db_conn


def _agent(db: psycopg.Connection) -> int:
    row = db.execute("INSERT INTO agents (label) VALUES ('a') RETURNING id").fetchone()
    assert row is not None
    return int(row[0])


def _ledger(db: psycopg.Connection, agent: int, day: date, tokens_in: int, tokens_out: int) -> None:
    db.execute(
        "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, tokens_in, "
        "tokens_out) VALUES (%s, %s, 'm', 1, %s, %s) ON CONFLICT (agent_id, day, model) "
        "DO UPDATE SET tokens_in = EXCLUDED.tokens_in, tokens_out = EXCLUDED.tokens_out",
        (agent, day, tokens_in, tokens_out),
    )


def _total(db: psycopg.Connection, agent: int) -> tuple[int, int] | None:
    row = db.execute(
        "SELECT sum(tokens_in), sum(tokens_out), count(*) FROM agent_model_tokens_total "
        "WHERE agent_id = %s",
        (agent,),
    ).fetchone()
    assert row is not None
    return None if row[2] == 0 else (int(row[0]), int(row[1]))


def test_only_days_past_the_recompute_horizon_are_folded(db: psycopg.Connection) -> None:
    agent = _agent(db)
    horizon = _TODAY - timedelta(days=token_totals.FOLD_AFTER_DAYS)
    _ledger(db, agent, horizon - timedelta(days=5), 100, 10)
    _ledger(db, agent, horizon, 200, 20)
    _ledger(db, agent, horizon + timedelta(days=1), 400, 40)  # still open to recomputation

    assert token_totals.folded_through(db) == token_totals.NOTHING_FOLDED
    token_totals.fold_totals(db, today=_TODAY)

    assert token_totals.folded_through(db) == horizon
    assert _total(db, agent) == (300, 30)


def test_a_fold_is_idempotent_and_adds_only_newly_settled_days(db: psycopg.Connection) -> None:
    agent = _agent(db)
    horizon = _TODAY - timedelta(days=token_totals.FOLD_AFTER_DAYS)
    _ledger(db, agent, horizon, 200, 20)
    token_totals.fold_totals(db, today=_TODAY)
    assert token_totals.fold_totals(db, today=_TODAY) == 0  # nothing settled since
    assert _total(db, agent) == (200, 20)

    _ledger(db, agent, horizon + timedelta(days=1), 5, 1)
    token_totals.fold_totals(db, today=_TODAY + timedelta(days=1))
    assert token_totals.folded_through(db) == horizon + timedelta(days=1)
    assert _total(db, agent) == (205, 21)


def test_rebuild_refolds_the_whole_ledger_after_a_folded_day_changed(
    db: psycopg.Connection,
) -> None:
    agent = _agent(db)
    old = _TODAY - timedelta(days=40)
    _ledger(db, agent, old, 100, 10)
    token_totals.fold_totals(db, today=_TODAY)
    assert _total(db, agent) == (100, 10)

    _ledger(db, agent, old, 150, 15)  # a backfill of an old day, behind the watermark
    token_totals.fold_totals(db, today=_TODAY)
    assert _total(db, agent) == (100, 10)  # the fold never revisits a folded day

    token_totals.rebuild_totals(db, today=_TODAY)
    assert _total(db, agent) == (150, 15)


def test_every_ledger_column_is_folded_per_model(db: psycopg.Connection) -> None:
    agent = _agent(db)
    old = _TODAY - timedelta(days=40)
    for day, model, cost in (
        (old, "a", 0.5),
        (old + timedelta(days=1), "a", 0.25),
        (old, "b", 2.0),
    ):
        db.execute(
            "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, tokens_in, "
            "tokens_out, tokens_cached, tokens_reasoning, cost_usd, costed_calls, unpriced_calls) "
            "VALUES (%s, %s, %s, 2, 10, 5, 3, 1, %s, 1, 1)",
            (agent, day, model, cost),
        )
    token_totals.fold_totals(db, today=_TODAY)

    rows = db.execute(
        "SELECT model, llm_calls, tokens_in, tokens_out, tokens_cached, tokens_reasoning, cost_usd, "
        "costed_calls, unpriced_calls FROM agent_model_tokens_total WHERE agent_id = %s ORDER BY model",
        (agent,),
    ).fetchall()
    assert rows == [("a", 4, 20, 10, 6, 2, 0.75, 2, 2), ("b", 2, 10, 5, 3, 1, 2.0, 1, 1)]


def _raw_usage(db: psycopg.Connection, agent: int, day: date, tokens_in: int) -> None:
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'm', 'c', 'p', "
        "'telemetry', 'llm_usage', 'info', 'test', %s::jsonb)",
        (
            uuid.uuid4().int % (1 << 62),
            datetime(day.year, day.month, day.day, 10, tzinfo=UTC),
            agent,
            json.dumps({"in_total": tokens_in, "out_total": 1, "model": "m"}),
        ),
    )


def test_the_command_line_rebuilds_the_totals_only_when_its_range_reaches_the_watermark(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = _agent(db)
    today = datetime.now(UTC).date()
    old = today - timedelta(days=40)
    _ledger(db, agent, old, 100, 10)
    token_totals.fold_totals(db, today=today)
    assert _total(db, agent) == (100, 10)

    @contextmanager
    def connect() -> Iterator[psycopg.Connection]:
        yield db

    monkeypatch.setattr("base.db.connect", connect)
    _raw_usage(db, agent, old, 150)  # the backfilled history of a folded day

    recent = (today - timedelta(days=1)).strftime("%Y%m%d")
    rollup.main(["--from", recent, "--to", recent])
    assert "rebuilt" not in capsys.readouterr().out
    assert _total(db, agent) == (100, 10)

    day = old.strftime("%Y%m%d")
    rollup.main(["--from", day, "--to", day])
    assert "rebuilt agent_model_tokens_total" in capsys.readouterr().out
    assert _total(db, agent) == (150, 1)
