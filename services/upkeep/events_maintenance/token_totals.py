"""The whole-life token sum per agent, folded from the day-grain ledger.

`agent_model_tokens_daily` rows of a day stay open to recomputation for `RECOMPUTE_DAYS` after the
day closes (late writes, a backfill). Once a day is older than that it can no longer change, so each
maintenance pass folds the newly settled days into `agent_model_tokens_total` and moves the watermark
(`agent_model_tokens_total_through`) up to them. A reader of all-time tokens then adds three parts: this
table, the ledger days after the watermark, and the raw rows of the newest two UTC days; its work
does not grow with history.

An operator who re-rolls days at or before the watermark (a backfill of old history) must rebuild
the totals afterwards: `rebuild_totals` refolds the whole ledger. `rollup`'s command line does it.
"""

from __future__ import annotations

from datetime import date, timedelta

import psycopg

from services.upkeep.events_maintenance.rollup import RECOMPUTE_DAYS

# A day is folded once it is this many days behind today: past the recompute horizon, with a
# margin for a pass that straddles midnight.
FOLD_AFTER_DAYS = RECOMPUTE_DAYS + 2
# The watermark of an empty totals table: before any ledger day.
NOTHING_FOLDED = date.min
_LOCK_KEY = 4_730_001  # advisory lock: one fold or rebuild at a time


def folded_through(conn: psycopg.Connection) -> date:
    """The last UTC day folded into `agent_model_tokens_total` (`NOTHING_FOLDED` before any fold)."""
    row = conn.execute("SELECT day FROM agent_model_tokens_total_through").fetchone()
    return row[0] if row is not None else NOTHING_FOLDED


_SUMMED = (
    "llm_calls",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_reasoning",
    "cost_usd",
    "costed_calls",
    "unpriced_calls",
)


def _fold(conn: psycopg.Connection, *, through: date, target: date) -> int:
    columns = ", ".join(_SUMMED)
    sums = ", ".join(f"sum({name})" for name in _SUMMED)
    adds = ", ".join(
        f"{name} = agent_model_tokens_total.{name} + EXCLUDED.{name}" for name in _SUMMED
    )
    rows = conn.execute(
        f"INSERT INTO agent_model_tokens_total (agent_id, model, {columns}) "  # noqa: S608 — fixed columns
        f"SELECT agent_id, model, {sums} FROM agent_model_tokens_daily "
        "WHERE day > %s AND day <= %s GROUP BY agent_id, model "
        f"ON CONFLICT (agent_id, model) DO UPDATE SET {adds}",
        (through, target),
    ).rowcount
    conn.execute(
        "INSERT INTO agent_model_tokens_total_through (singleton, day) VALUES (true, %s) "
        "ON CONFLICT (singleton) DO UPDATE SET day = EXCLUDED.day",
        (target,),
    )
    return max(rows, 0)


def fold_totals(conn: psycopg.Connection, *, today: date) -> int:
    """Fold the ledger days that settled since the last fold; returns the rows updated."""
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
        conn.execute("DELETE FROM agent_model_tokens_total")
        return _fold(conn, through=NOTHING_FOLDED, target=today - timedelta(days=FOLD_AFTER_DAYS))
