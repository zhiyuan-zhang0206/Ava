"""Reads of the audit record — `audit_events` (Postgres) in the event stream's row shape.

`category=audit` events are recorded in Postgres (decisions/2026-10-02-audit-events-in-postgres.md);
Loki holds only a projection that expires. Every reader that needs audit history reads here:
`/api/events`, the run timeline, the fleet graph and the neighbors walk. Rows come back in the
same dict shape `gateway.lgtm.loki_events.query_events` returns, so one merge serves both.

Row `id` is the event's stream id (the unsigned form of `audit_events.event_uid`), the same value
the Loki row of that event carries. `line_sha256` is the digest of the recorded row, not of a
Loki line: the table does not keep the line bytes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, LiteralString

import psycopg
from psycopg import sql

from base.events.contract import EventTier

# Audit event names that form edges of the fleet graph and the neighbors walk. Lineage
# (spawn/fork/resurrect) is permanent and all-time; messages (send_message) decay with recency.
LINEAGE_EVENT_NAMES = ("spawn", "fork", "resurrect")
EDGE_EVENT_NAMES = ("send_message", *LINEAGE_EVENT_NAMES)

# A tier is derived from level and category (`base.events.contract.tier_for`): warning and above
# is an anomaly, any other audit row is a business fact.
_ANOMALY_LEVELS = ("warning", "error", "critical")

_COLUMNS: LiteralString = (
    "event_uid, ts, trace_id, span_id, agent_id, machine, process, event_name, level, source, "
    "target_agent_id, attributes"
)


def _stream_id(event_uid: int) -> int:
    """The unsigned stream id the event carries in Loki and the JSONL mirror."""
    return event_uid + (1 << 64) if event_uid < 0 else event_uid


def _row(record: tuple[Any, ...]) -> dict[str, Any]:
    (
        event_uid,
        ts,
        trace_id,
        span_id,
        agent_id,
        machine,
        process,
        event_name,
        level,
        source,
        target_agent_id,
        attributes,
    ) = record
    ts = ts.astimezone(UTC)
    digest = json.dumps(
        [
            ts.isoformat(),
            trace_id,
            span_id,
            agent_id,
            machine,
            process,
            event_name,
            level,
            source,
            target_agent_id,
            attributes,
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return {
        "id": _stream_id(event_uid),
        "line_sha256": sha256(digest.encode()).hexdigest(),
        "ts": ts,
        "trace_id": trace_id,
        "span_id": span_id,
        "agent_id": agent_id,
        "machine": machine,
        "process": process,
        "category": "audit",
        "event_name": event_name,
        "level": level,
        "source": source,
        "target_agent_id": target_agent_id,
        "attributes": attributes,
    }


def _tier_clause(tiers: list[EventTier]) -> tuple[LiteralString | None, bool]:
    """The level predicate for a tier filter, and whether any audit row can match.

    Audit rows are `business` or, from warning up, `anomaly`; asking for both (or
    for neither of them) needs no predicate.
    """
    wants_business, wants_anomaly = "business" in tiers, "anomaly" in tiers
    if not (wants_business or wants_anomaly):
        return None, False
    if wants_business == wants_anomaly:
        return None, True
    return ("level = ANY(%s)" if wants_anomaly else "NOT (level = ANY(%s))"), True


def _where(
    *,
    agent_id: int | None,
    event_names: list[str] | None,
    tiers: list[EventTier] | None,
    trace_id: str | None,
    machine: str | None,
    level: str | None,
    attribute_filters: dict[str, str] | None,
    from_: datetime | None,
    to: datetime | None,
) -> tuple[sql.Composable, list[Any]] | None:
    """The WHERE clause and parameters, or None when the filters exclude every audit row."""
    clauses: list[sql.Composable] = []
    params: list[Any] = []

    def add(clause: LiteralString, value: Any) -> None:
        clauses.append(sql.SQL(clause))
        params.append(value)

    if tiers is not None:
        tier_clause, possible = _tier_clause(tiers)
        if not possible:
            return None
        if tier_clause is not None:
            add(tier_clause, list(_ANOMALY_LEVELS))
    optional: tuple[tuple[LiteralString, Any], ...] = (
        ("agent_id = %s", agent_id),
        ("event_name = ANY(%s)", event_names),
        ("trace_id = %s", trace_id),
        ("machine = %s", machine),
        ("level = %s", level.lower() if level is not None else None),
        ("attributes @> %s::jsonb", json.dumps(attribute_filters) if attribute_filters else None),
        ("ts >= %s", from_),
        ("ts <= %s", to),
    )
    for clause, value in optional:
        if value is not None:
            add(clause, value)
    where = sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("TRUE")
    return where, params


def query_events(
    conn: psycopg.Connection,
    *,
    agent_id: int | None = None,
    event_names: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    trace_id: str | None = None,
    machine: str | None = None,
    level: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    direction: str = "backward",
) -> tuple[list[dict[str, Any]], bool]:
    """Slice of the audit record, newest first (`backward`) or oldest first (`forward`).

    Returns `(rows, has_more)`; `has_more` comes from a one-row lookahead, as in the Loki
    reader. Ties on `ts` are ordered by the table's identity, stable across pages.
    """
    where = _where(
        agent_id=agent_id,
        event_names=event_names,
        tiers=tiers,
        trace_id=trace_id,
        machine=machine,
        level=level,
        attribute_filters=attribute_filters,
        from_=from_,
        to=to,
    )
    if where is None:
        return [], False
    clause, params = where
    order = sql.SQL("DESC") if direction == "backward" else sql.SQL("ASC")
    query = sql.SQL(
        "SELECT {columns} FROM audit_events WHERE {where} "
        "ORDER BY ts {order}, id {order} LIMIT %s OFFSET %s"
    ).format(columns=sql.SQL(_COLUMNS), where=clause, order=order)
    records = conn.execute(query, [*params, limit + 1, offset]).fetchall()
    return [_row(record) for record in records[:limit]], len(records) > limit


def count_events(
    conn: psycopg.Connection,
    *,
    agent_id: int | None = None,
    event_names: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    trace_id: str | None = None,
    machine: str | None = None,
    level: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> int:
    """Exact count of the audit rows `query_events` would page through."""
    where = _where(
        agent_id=agent_id,
        event_names=event_names,
        tiers=tiers,
        trace_id=trace_id,
        machine=machine,
        level=level,
        attribute_filters=attribute_filters,
        from_=from_,
        to=to,
    )
    if where is None:
        return 0
    clause, params = where
    query = sql.SQL("SELECT count(*) FROM audit_events WHERE {where}").format(where=clause)
    row = conn.execute(query, params).fetchone()
    assert row is not None  # noqa: S101 — an aggregate always returns one row
    return int(row[0])


def edge_weights(
    conn: psycopg.Connection,
    *,
    live_ids: set[int] | None,
    win_start: datetime | None,
    now: datetime,
    decay_lambda: float,
) -> list[tuple[int, int, str, float, int, datetime]]:
    """Fleet-graph edge aggregates: `(target, agent, event_name, weight, count, last_seen)`.

    One row per `(target_agent_id, agent_id, event_name)` over the edge events. Lineage rows weigh
    2.0 each, all-time; a message weighs `exp(-lambda * days_ago)` and rows older than `win_start`
    are left out. When `live_ids` is given, both endpoints must be in it.
    """
    query: LiteralString = """
        SELECT target_agent_id, agent_id, event_name,
               sum(CASE WHEN event_name = 'send_message'
                        THEN exp(-%(lam)s * extract(epoch FROM (%(now)s - ts)) / 86400.0)
                        ELSE 2.0 END)::float8,
               count(*), max(ts)
        FROM audit_events
        WHERE event_name = ANY(%(names)s)
          AND agent_id IS NOT NULL AND target_agent_id IS NOT NULL
          AND (event_name <> 'send_message' OR %(win)s::timestamptz IS NULL OR ts >= %(win)s)
          AND (%(live)s::bigint[] IS NULL
               OR (agent_id = ANY(%(live)s) AND target_agent_id = ANY(%(live)s)))
        GROUP BY target_agent_id, agent_id, event_name
    """
    records = conn.execute(
        query,
        {
            "lam": decay_lambda,
            "now": now,
            "names": list(EDGE_EVENT_NAMES),
            "win": win_start,
            "live": sorted(live_ids) if live_ids is not None else None,
        },
    ).fetchall()
    return [(int(t), int(a), str(n), float(w), int(c), last) for t, a, n, w, c, last in records]


def edge_counts(conn: psycopg.Connection) -> list[tuple[int, int, str, int, datetime]]:
    """Neighbors-walk tie inputs: `(agent, target, event_name, count, last_seen)` per edge."""
    records = conn.execute(
        "SELECT agent_id, target_agent_id, event_name, count(*), max(ts) FROM audit_events "
        "WHERE event_name = ANY(%s) AND agent_id IS NOT NULL AND target_agent_id IS NOT NULL "
        "GROUP BY agent_id, target_agent_id, event_name",
        [list(EDGE_EVENT_NAMES)],
    ).fetchall()
    return [(int(a), int(t), str(n), int(c), last) for a, t, n, c, last in records]
