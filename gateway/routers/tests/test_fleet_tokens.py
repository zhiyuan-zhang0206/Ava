"""`gateway.routers._fleet_tokens` — the fleet graph's token sums over every window shape.

One agent has `llm_usage` rows across forty days. The ledger is rolled from those rows and the old
days are folded, as the maintenance daemon would; each window then reads the totals, the ledger days
and the raw tail in parts, and must equal the sum of the raw rows in that window.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from gateway.routers._fleet_tokens import agent_tokens
from services.events_maintenance import rollup, token_totals

_NOW = datetime(2026, 6, 30, 15, 30, tzinfo=UTC)


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


def _usage(
    db: psycopg.Connection, agent: int, at: datetime, tokens_in: int, tokens_out: int
) -> None:
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'm', 'c', 'p', "
        "'telemetry', 'llm_usage', 'info', 'test', %s::jsonb)",
        (
            uuid.uuid4().int % (1 << 62),
            at,
            agent,
            json.dumps({"in_total": tokens_in, "out_total": tokens_out, "model": "m"}),
        ),
    )


def _brute(db: psycopg.Connection, agent: int, start: datetime | None) -> tuple[float, float]:
    row = db.execute(
        "SELECT COALESCE(sum((attributes->>'in_total')::float8), 0), "
        "COALESCE(sum((attributes->>'out_total')::float8), 0) FROM telemetry_events "
        "WHERE agent_id = %s AND event_name = 'llm_usage' AND ts <= %s AND (%s::timestamptz IS NULL "
        "OR ts >= %s)",
        (agent, _NOW, start, start),
    ).fetchone()
    assert row is not None
    return float(row[0]), float(row[1])


@pytest.fixture
def agent(db: psycopg.Connection) -> int:
    agent = _agent(db)
    for age_hours in range(1, 24 * 40, 7):  # a row every seven hours for forty days
        _usage(db, agent, _NOW - timedelta(hours=age_hours), 100 + age_hours, 10 + age_hours % 13)
    _usage(db, agent, _NOW - timedelta(minutes=5), 7, 3)
    yesterday = (_NOW - timedelta(days=1)).date()
    rollup.roll_days(db, yesterday - timedelta(days=45), yesterday)
    token_totals.fold_totals(db, today=_NOW.date())
    assert token_totals.folded_through(db) < yesterday - timedelta(days=5)
    folded = db.execute("SELECT count(*) FROM agent_token_totals WHERE agent_id = %s", (agent,))
    assert folded.fetchone() == (1,)
    return agent


@pytest.mark.parametrize(
    "start",
    [
        _NOW - timedelta(minutes=5),
        _NOW - timedelta(hours=1),
        _NOW - timedelta(hours=24),
        _NOW - timedelta(hours=72),
        _NOW - timedelta(days=7),
        datetime(2026, 6, 27, tzinfo=UTC),  # a window that starts on midnight
        datetime(2026, 6, 25, 3, 17, tzinfo=UTC),  # starts mid-day
        None,  # all time
    ],
)
def test_every_window_equals_the_sum_of_its_raw_rows(
    db: psycopg.Connection, agent: int, start: datetime | None
) -> None:
    tokens = agent_tokens(db, now=_NOW, win_start=start)[agent]

    in_window, out_window = _brute(db, agent, start)
    in_retained, out_retained = _brute(db, agent, _NOW - timedelta(days=7))
    assert (tokens.in_window, tokens.out_window) == (in_window, out_window)
    assert (tokens.in_retained, tokens.out_retained) == (in_retained, out_retained)


def test_an_agent_without_usage_has_no_entry(db: psycopg.Connection, agent: int) -> None:
    assert agent_tokens(db, now=_NOW, win_start=None).get(_agent(db)) is None
