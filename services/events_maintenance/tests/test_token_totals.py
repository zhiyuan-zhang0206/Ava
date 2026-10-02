"""`services.events_maintenance.token_totals` — the whole-life token sum folded from the ledger.

Days older than the recompute horizon are folded once into `agent_token_totals` and the watermark
moves up to them; a fold is idempotent, a rebuild refolds from scratch after old days changed.
"""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
import pytest

from services.events_maintenance import token_totals

_TODAY = date(2026, 6, 30)


@pytest.fixture
def db(db_conn: psycopg.Connection) -> psycopg.Connection:
    db_conn.autocommit = True
    db_conn.execute("DELETE FROM agent_token_totals")
    db_conn.execute("DELETE FROM agent_token_totals_through")
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
        "SELECT tokens_in, tokens_out FROM agent_token_totals WHERE agent_id = %s", (agent,)
    ).fetchone()
    return None if row is None else (row[0], row[1])


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
