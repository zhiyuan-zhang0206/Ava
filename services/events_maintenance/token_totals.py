"""The whole-life token sum per agent, folded from the day-grain ledger.

`agent_model_tokens_daily` rows of a day stay open to recomputation for `RECOMPUTE_DAYS` after the
day closes (late writes, a backfill). Once a day is older than that it can no longer change, so each
maintenance pass folds the newly settled days into `agent_token_totals` and moves the watermark
(`agent_token_totals_through`) up to them. A reader of all-time tokens then adds three parts: this
table, the ledger days after the watermark, and the raw rows of the newest two UTC days; its work
does not grow with history.

An operator who re-rolls days at or before the watermark (a backfill of old history) must rebuild
the totals afterwards: `rebuild_totals` refolds the whole ledger. `rollup`'s command line does it.
"""

from __future__ import annotations

from datetime import date, timedelta

import psycopg

from services.events_maintenance.rollup import RECOMPUTE_DAYS

# A day is folded once it is this many days behind today: past the recompute horizon, with a
# margin for a pass that straddles midnight.
FOLD_AFTER_DAYS = RECOMPUTE_DAYS + 2
# The watermark of an empty totals table: before any ledger day.
NOTHING_FOLDED = date.min
_LOCK_KEY = 4_730_001  # advisory lock: one fold or rebuild at a time


def folded_through(conn: psycopg.Connection) -> date:
    """The last UTC day folded into `agent_token_totals` (`NOTHING_FOLDED` before any fold)."""
    row = conn.execute("SELECT day FROM agent_token_totals_through").fetchone()
    return row[0] if row is not None else NOTHING_FOLDED


def _fold(conn: psycopg.Connection, *, through: date, target: date) -> int:
    rows = conn.execute(
        "INSERT INTO agent_token_totals (agent_id, tokens_in, tokens_out) "
        "SELECT agent_id, sum(tokens_in), sum(tokens_out) FROM agent_model_tokens_daily "
        "WHERE day > %s AND day <= %s GROUP BY agent_id "
        "ON CONFLICT (agent_id) DO UPDATE SET "
        "tokens_in = agent_token_totals.tokens_in + EXCLUDED.tokens_in, "
        "tokens_out = agent_token_totals.tokens_out + EXCLUDED.tokens_out",
        (through, target),
    ).rowcount
    conn.execute(
        "INSERT INTO agent_token_totals_through (singleton, day) VALUES (true, %s) "
        "ON CONFLICT (singleton) DO UPDATE SET day = EXCLUDED.day",
        (target,),
    )
    return max(rows, 0)


def fold_totals(conn: psycopg.Connection, *, today: date) -> int:
    """Fold the ledger days that settled since the last fold; returns the agents updated."""
    target = today - timedelta(days=FOLD_AFTER_DAYS)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        through = folded_through(conn)
        if target <= through:
            return 0
        return _fold(conn, through=through, target=target)


def rebuild_totals(conn: psycopg.Connection, *, today: date) -> int:
    """Refold the whole ledger from scratch (after days at or before the watermark changed)."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        conn.execute("DELETE FROM agent_token_totals")
        return _fold(conn, through=NOTHING_FOLDED, target=today - timedelta(days=FOLD_AFTER_DAYS))
