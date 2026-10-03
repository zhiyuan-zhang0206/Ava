"""The metrics window reduced in Postgres — `fetch_aggregate`, the gateway + CLI entry point.

The digest's units reduce the window's telemetry and log rows to a handful of aggregates (counts,
sums, distributions, per-key counters, position buckets). Each aggregate is computed by a few
statements over `telemetry_events` in one connection; nothing materializes the rows. Every
statement shares one scope: category telemetry or log (the table holds nothing else), the window
`[now - days, now]`, an optional single agent, and, with `since_compact`, each agent's rows
narrowed to those at or after its latest compact halt (service rows, which have no agent, are
always kept).

`EventAggregate` (in `aggregate.py`) carries the result to the assemblers, which reuse the same
helper functions (`pctiles`, `third_of`, `_fix_kinds`, `cost_usd`) as before so the math cannot
drift.

Failure events are the exec outcome spellings the registry names (`EXEC_FAILURE_EVENTS`); the
envelope and boot events are not outcomes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString, cast

import psycopg

from base.events.contract import (
    AGENT_SPAWNED_KEYS,
    CODE_KEYS,
    EXEC_FAILED_KEYS,
    EXEC_KEYS,
    HALT_KEYS,
    LLM_USAGE_KEYS,
    PLUGIN_ACTIVATION_KEYS,
    SDK_CALL_KEYS,
    SYNTAX_FIX_KEYS,
    TURN_END_KEYS,
)
from base.telemetry.event_sql import numeric

EXEC_FAILURE_EVENTS = [
    "exec_failed",
    "exec(failed)",
    "exec_timeout",
    "exec(timeout)",
    "exec_cancelled",
    "exec(cancelled)",
    "exec_node_timeout",
]
LIFECYCLE_EVENTS = {
    "agent_spawned": "spawned",
    "agent_terminated": "terminated",
    "agent_restarted": "restarted",
    "agent_resurrected": "resurrected",
}
IDLE_HALT_BODY = "no tool_call (idle)"


def _n(expression: str, cast_to: str = "numeric") -> str:
    return numeric(expression, cast_to)


class _Scope:
    """The shared FROM/WHERE of every statement of one fetch, with its named parameters."""

    def __init__(
        self,
        start: datetime,
        end: datetime,
        agent_id: int | None,
        cutoffs: dict[int, datetime],
    ) -> None:
        self.params: dict[str, Any] = {
            "start": start,
            "end": end,
            "agent": agent_id,
            "cut_agents": list(cutoffs),
            "cut_times": list(cutoffs.values()),
            "fail": EXEC_FAILURE_EVENTS,
        }
        self.from_sql = "telemetry_events t"
        self.where_sql = "t.ts >= %(start)s AND t.ts <= %(end)s"
        if agent_id is not None:
            self.where_sql += " AND t.agent_id = %(agent)s"
        if cutoffs:
            self.from_sql += (
                " LEFT JOIN unnest(%(cut_agents)s::bigint[], %(cut_times)s::timestamptz[])"
                " AS c(agent_id, cutoff) ON c.agent_id = t.agent_id"
            )
            self.where_sql += " AND (t.agent_id IS NULL OR t.ts >= COALESCE(c.cutoff, '-infinity'))"

    def run(
        self, conn: psycopg.Connection[Any], select: str, extra: str = "", tail: str = ""
    ) -> Any:
        """`SELECT <select> FROM <scope> WHERE <scope> [AND extra] <tail>` as a cursor."""
        where = f"{self.where_sql} AND ({extra})" if extra else self.where_sql
        query = f"SELECT {select} FROM {self.from_sql} WHERE {where} {tail}"  # noqa: S608
        return conn.execute(cast(LiteralString, query), self.params)


def _cutoffs(
    conn: psycopg.Connection[Any], start: datetime, end: datetime, agent_id: int | None
) -> dict[int, datetime]:
    """Each agent's latest compact halt in the window."""
    scope = _Scope(start, end, agent_id, {})
    rows = scope.run(
        conn,
        "t.agent_id, max(t.ts)",
        f"t.event_name = 'halt' AND t.agent_id IS NOT NULL AND {HALT_KEYS['body']} LIKE '%%compact%%'",
        "GROUP BY t.agent_id",
    ).fetchall()
    return {int(agent): ts for agent, ts in rows}


def _per_agent(scope: _Scope, conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    """Counts per agent (and for service rows, agent NULL) from columns the index carries."""
    return scope.run(
        conn,
        "t.agent_id, count(*), "
        "count(*) FILTER (WHERE t.event_name = 'code'), "
        "count(*) FILTER (WHERE t.event_name = 'turn_end'), "
        "count(*) FILTER (WHERE t.event_name = 'exec'), "
        "count(*) FILTER (WHERE t.event_name = ANY(%(fail)s)), "
        + ", ".join(f"count(*) FILTER (WHERE t.event_name = '{name}')" for name in LIFECYCLE_EVENTS)
        + ", min(t.ts), max(t.ts)",
        "",
        "GROUP BY t.agent_id",
    ).fetchall()


def _outcomes(scope: _Scope, conn: psycopg.Connection[Any]) -> dict[int | None, tuple[int, int]]:
    """Per agent: successful turns and idle halts (both read an attribute, so they get their own
    statement over the few rows of those two events)."""
    ok = f"COALESCE({TURN_END_KEYS['ok']}, '') = 'true'"
    idle = f"COALESCE({HALT_KEYS['body']}, '') = '{IDLE_HALT_BODY}'"
    rows = scope.run(
        conn,
        f"t.agent_id, count(*) FILTER (WHERE t.event_name = 'turn_end' AND {ok}), "
        f"count(*) FILTER (WHERE t.event_name = 'halt' AND {idle})",
        "t.event_name IN ('turn_end', 'halt')",
        "GROUP BY t.agent_id",
    ).fetchall()
    return {(int(r[0]) if r[0] is not None else None): (int(r[1]), int(r[2])) for r in rows}


def _failure_types(scope: _Scope, conn: psycopg.Connection[Any]) -> dict[str, int]:
    rows = scope.run(
        conn,
        f"COALESCE(NULLIF({EXEC_FAILED_KEYS['exc_type']}, ''), t.event_name), count(*)",
        "t.event_name = ANY(%(fail)s)",
        "GROUP BY 1 ORDER BY 2 DESC, 1",
    ).fetchall()
    return {str(name): int(count) for name, count in rows}


def _lengths(scope: _Scope, conn: psycopg.Connection[Any]) -> dict[str, list[float]]:
    """The values of the three distributions, which feed `pctiles` (nearest rank in Python)."""
    out: dict[str, list[float]] = {}
    for key, expression, extra in (
        (
            "code_len",
            f"COALESCE(char_length({CODE_KEYS['body']}), 0)::float8",
            "t.event_name = 'code'",
        ),
        (
            "output_len",
            f"COALESCE(char_length({EXEC_KEYS['body']}), 0)::float8",
            "t.event_name = 'exec' OR t.event_name = ANY(%(fail)s)",
        ),
        (
            "turn_dur",
            f"COALESCE({_n(TURN_END_KEYS['duration_seconds'], 'float8')}, 0)",
            "t.event_name = 'turn_end'",
        ),
    ):
        out[key] = [float(r[0]) for r in scope.run(conn, expression, extra).fetchall()]
    return out


def _llm_sums(
    scope: _Scope, conn: psycopg.Connection[Any]
) -> dict[int | None, dict[str, tuple[int, int, int, int, int]]]:
    """Per (agent, model) sums of the `llm_usage` rows: calls, in, out, cached, reasoning."""
    sums = scope.run(
        conn,
        f"t.agent_id, COALESCE({LLM_USAGE_KEYS['model']}, ''), count(*), "
        f"COALESCE(sum({_n(LLM_USAGE_KEYS['in_total'])}), 0)::bigint, "
        f"COALESCE(sum({_n(LLM_USAGE_KEYS['out_total'])}), 0)::bigint, "
        f"COALESCE(sum({_n(LLM_USAGE_KEYS['cache_read'])}), 0)::bigint, "
        f"COALESCE(sum({_n(LLM_USAGE_KEYS['reasoning'])}), 0)::bigint",
        "t.event_name = 'llm_usage'",
        "GROUP BY t.agent_id, 2",
    ).fetchall()
    per_agent: dict[int | None, dict[str, tuple[int, int, int, int, int]]] = {}
    for agent, model, calls, tin, tout, cached, reasoning in sums:
        key = int(agent) if agent is not None else None
        per_agent.setdefault(key, {})[str(model)] = (
            int(calls),
            int(tin),
            int(tout),
            int(cached),
            int(reasoning),
        )
    return per_agent


def _llm_position(scope: _Scope, conn: psycopg.Connection[Any]) -> dict[str, tuple[int, int]]:
    """Cache-read and input sums in the early, mid and late thirds of each agent's `llm_usage`
    rows in time order (the buckets `third_of` defines, as integer arithmetic)."""
    ranked = conn.execute(
        cast(
            LiteralString,
            "SELECT bucket, COALESCE(sum(cached), 0)::bigint, COALESCE(sum(tin), 0)::bigint FROM ("  # noqa: S608 — keys come from the registered payload constants
            "SELECT CASE WHEN 3 * ordinal < total THEN 'early' WHEN 3 * ordinal < 2 * total "
            "THEN 'mid' ELSE 'late' END AS bucket, cached, tin FROM ("
            "SELECT row_number() OVER (PARTITION BY t.agent_id ORDER BY t.ts, t.id) - 1 AS ordinal, "
            "count(*) OVER (PARTITION BY t.agent_id) AS total, "
            f"COALESCE({_n(LLM_USAGE_KEYS['cache_read'])}, 0) AS cached, "
            f"COALESCE({_n(LLM_USAGE_KEYS['in_total'])}, 0) AS tin "
            f"FROM {scope.from_sql} WHERE {scope.where_sql} AND t.event_name = 'llm_usage') r) s "
            "GROUP BY bucket",
        ),
        scope.params,
    ).fetchall()
    return {str(b): (int(c), int(i)) for b, c, i in ranked}


def _fix_events(scope: _Scope, conn: psycopg.Connection[Any]) -> list[tuple[int | None, int, str]]:
    """Each `syntax_fix` event with the number of code blocks its agent had written by then."""
    rows = conn.execute(
        cast(
            LiteralString,
            "SELECT agent_id, blk, fixes FROM ("  # noqa: S608 — keys come from the registered payload constants
            "SELECT t.agent_id, t.ts, t.id, t.event_name, "
            f"CASE WHEN t.event_name = 'syntax_fix' THEN COALESCE({SYNTAX_FIX_KEYS['fixes']}, '') END "
            "AS fixes, "
            "sum((t.event_name = 'code')::int) OVER (PARTITION BY t.agent_id ORDER BY t.ts, t.id) "
            f"AS blk FROM {scope.from_sql} WHERE {scope.where_sql} "
            "AND t.event_name IN ('code', 'syntax_fix')) s "
            "WHERE event_name = 'syntax_fix' ORDER BY agent_id, ts, id",
        ),
        scope.params,
    ).fetchall()
    return [(int(a) if a is not None else None, int(b), str(f)) for a, b, f in rows]


def _per_agent_counters(
    scope: _Scope, conn: psycopg.Connection[Any]
) -> dict[int | None, dict[str, Any]]:
    """The window's counters per agent (key None = service rows), merged from both statements."""
    per_agent: dict[int | None, dict[str, Any]] = {}
    for row in _per_agent(scope, conn):
        key = int(row[0]) if row[0] is not None else None
        per_agent[key] = {
            "events": int(row[1]),
            "code": int(row[2]),
            "turn_total": int(row[3]),
            "exec_ok": int(row[4]),
            "exec_failed": int(row[5]),
            "lifecycle": dict(
                zip(LIFECYCLE_EVENTS.values(), (int(x) for x in row[6:10]), strict=True)
            ),
            "first_ts": row[10],
            "last_ts": row[11],
        }
    for key, (turn_ok, idle) in _outcomes(scope, conn).items():
        per_agent.setdefault(key, {"events": 0})
        per_agent[key]["turn_ok"] = turn_ok
        per_agent[key]["idle_halts"] = idle
    return per_agent


def read_window(
    conn: psycopg.Connection[Any],
    days: int,
    agent_id: int | None,
    *,
    since_compact: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Every aggregate of the metrics window, as plain Python values."""
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    cutoffs = _cutoffs(conn, start, end, agent_id) if since_compact else {}
    scope = _Scope(start, end, agent_id, cutoffs)

    per_agent = _per_agent_counters(scope, conn)
    llm_per_agent = _llm_sums(scope, conn)
    spawners = {
        str(r[0]): int(r[1])
        for r in scope.run(
            conn,
            f"COALESCE(NULLIF({AGENT_SPAWNED_KEYS['spawner']}, ''), '?'), count(*)",
            "t.event_name = 'agent_spawned'",
            "GROUP BY 1",
        ).fetchall()
    }
    weight = _n(SDK_CALL_KEYS["sample_rate"])
    sdk_fns = {
        str(r[0]): int(r[1])
        for r in scope.run(
            conn,
            f"{SDK_CALL_KEYS['fn']}, COALESCE(sum({weight}), 0)::bigint",
            f"t.event_name = 'sdk_call' AND COALESCE({SDK_CALL_KEYS['fn']}, '') <> '' "
            f"AND {weight} IS NOT NULL",
            "GROUP BY 1 ORDER BY 2 DESC, 1",
        ).fetchall()
    }
    plugin_acts = {
        (str(r[0]), str(r[1]), str(r[2]), str(r[3])): int(r[4])
        for r in scope.run(
            conn,
            f"{PLUGIN_ACTIVATION_KEYS['plugin']}, COALESCE({PLUGIN_ACTIVATION_KEYS['surface']}, ''), "
            f"COALESCE({PLUGIN_ACTIVATION_KEYS['identifier']}, ''), "
            f"COALESCE({PLUGIN_ACTIVATION_KEYS['model']}, ''), count(*)",
            f"t.event_name = 'plugin_activation' AND COALESCE({PLUGIN_ACTIVATION_KEYS['plugin']}, '') <> ''",
            "GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC, 1, 2, 3, 4",
        ).fetchall()
    }
    return {
        "start": start,
        "end": end,
        "per_agent": per_agent,
        "failure_types": _failure_types(scope, conn),
        "lengths": _lengths(scope, conn),
        "llm_per_agent": llm_per_agent,
        "llm_position": _llm_position(scope, conn),
        "fix_events": _fix_events(scope, conn),
        "spawners": spawners,
        "sdk_fns": sdk_fns,
        "plugin_acts": plugin_acts,
    }


def read_agent_window(
    conn: psycopg.Connection[Any],
    days: int,
    *,
    since_compact: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The per-agent counters and `llm_usage` sums of the window, for `/api/metrics/agents`."""
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    cutoffs = _cutoffs(conn, start, end, None) if since_compact else {}
    scope = _Scope(start, end, None, cutoffs)
    return {
        "per_agent": _per_agent_counters(scope, conn),
        "llm_per_agent": _llm_sums(scope, conn),
    }
