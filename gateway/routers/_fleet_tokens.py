"""Per-agent token sums for the fleet graph, read without scanning history.

Three parts cover any window, and none grows with the age of the cluster:

- the newest two UTC days (today and yesterday) from the raw `llm_usage` rows of `telemetry_events`,
  which the ledger may not hold yet;
- whole UTC days before those from the day-grain ledger `agent_model_tokens_daily` (a window that
  starts mid-day also reads that first partial day from the raw rows);
- for the all-time window, the folded whole-life sums `agent_model_tokens_total` up to the fold watermark,
  then the ledger days after it (`services.events_maintenance.token_totals`).

A row is counted in exactly one part: the ledger days end before the raw tail starts, and a partial
first day ends where the ledger days begin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, LiteralString, NamedTuple, cast

from base.events.contract import LLM_USAGE_KEYS
from base.telemetry.event_sql import numeric
from services.events_maintenance.token_totals import folded_through

_RETAINED_WINDOW = timedelta(days=7)


class AgentTokens(NamedTuple):
    """One agent's llm_usage token sums: retained window (7d) and selected window."""

    in_retained: float
    out_retained: float
    in_window: float
    out_window: float


@dataclass(frozen=True)
class _Plan:
    """How one window start splits into raw rows and ledger days."""

    raw_from: datetime  # raw rows at or after this instant
    partial: tuple[datetime, datetime]  # raw rows in [from, to): the first, partial day
    ledger_from: date  # ledger days in [ledger_from, tail_day)


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _plan(start: datetime, *, tail_day: date) -> _Plan:
    tail_start = _midnight(tail_day)
    if start >= tail_start:
        return _Plan(start, (start, start), tail_day)
    first_full = start.date() if start == _midnight(start.date()) else start.date() + timedelta(1)
    return _Plan(tail_start, (start, _midnight(first_full)), first_full)


def agent_tokens(conn: Any, *, now: datetime, win_start: datetime | None) -> dict[int, AgentTokens]:
    """Per-agent token sums over the retained window and `win_start` (None = all time)."""
    tail_day = now.astimezone(UTC).date() - timedelta(days=1)
    retained = _plan(now - _RETAINED_WINDOW, tail_day=tail_day)
    if win_start is not None:
        window = _plan(win_start, tail_day=tail_day)
    else:
        tail_start = _midnight(tail_day)
        window = _Plan(tail_start, (tail_start, tail_start), folded_through(conn) + timedelta(1))
    sums: dict[int, list[float]] = {}

    def add(agent: Any, values: tuple[Any, ...]) -> None:
        row = sums.setdefault(int(agent), [0.0, 0.0, 0.0, 0.0])
        for slot, value in enumerate(values):
            row[slot] += float(value)

    in_total = numeric(LLM_USAGE_KEYS["in_total"])
    out_total = numeric(LLM_USAGE_KEYS["out_total"])

    def in_part(prefix: str) -> str:
        return f"(ts >= %({prefix}_raw)s OR (ts >= %({prefix}_pf)s AND ts < %({prefix}_pt)s))"

    raw_query = f"""
        SELECT agent_id,
               COALESCE(sum({in_total}) FILTER (WHERE {in_part("r")}), 0),
               COALESCE(sum({out_total}) FILTER (WHERE {in_part("r")}), 0),
               COALESCE(sum({in_total}) FILTER (WHERE {in_part("w")}), 0),
               COALESCE(sum({out_total}) FILTER (WHERE {in_part("w")}), 0)
        FROM telemetry_events
        WHERE event_name = 'llm_usage' AND category = 'telemetry' AND agent_id IS NOT NULL
          AND ts <= %(now)s
          AND ({in_part("r")} OR {in_part("w")})
        GROUP BY agent_id
        """  # noqa: S608 — keys come from the registered payload constants
    raw = conn.execute(
        cast(LiteralString, raw_query),
        {
            "now": now,
            "r_raw": retained.raw_from,
            "r_pf": retained.partial[0],
            "r_pt": retained.partial[1],
            "w_raw": window.raw_from,
            "w_pf": window.partial[0],
            "w_pt": window.partial[1],
        },
    )
    for agent, *values in raw.fetchall():
        add(agent, tuple(values))

    ledger = conn.execute(
        """
        SELECT agent_id,
               COALESCE(sum(tokens_in) FILTER (WHERE day >= %(r_from)s), 0),
               COALESCE(sum(tokens_out) FILTER (WHERE day >= %(r_from)s), 0),
               COALESCE(sum(tokens_in) FILTER (WHERE day >= %(w_from)s), 0),
               COALESCE(sum(tokens_out) FILTER (WHERE day >= %(w_from)s), 0)
        FROM agent_model_tokens_daily
        WHERE day >= LEAST(%(r_from)s, %(w_from)s) AND day < %(tail)s
        GROUP BY agent_id
        """,
        {"r_from": retained.ledger_from, "w_from": window.ledger_from, "tail": tail_day},
    )
    for agent, *values in ledger.fetchall():
        add(agent, tuple(values))

    if win_start is None:
        for agent, tokens_in, tokens_out in conn.execute(
            "SELECT agent_id, sum(tokens_in), sum(tokens_out) FROM agent_model_tokens_total "
            "GROUP BY agent_id"
        ).fetchall():
            add(agent, (0, 0, tokens_in, tokens_out))
    return {agent: AgentTokens(*row) for agent, row in sums.items()}
