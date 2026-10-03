"""Reads of the telemetry and log record — `telemetry_events` (Postgres) in the event row shape.

`category=telemetry` and `category=log` events are recorded in Postgres by the emitter
(`base/telemetry/event_store.py`); Loki holds only an observation copy that expires after 84 hours.
Every reader that needs their history reads here: `/api/events`, the per-agent events query, the
run timeline and the inspector metrics. Filters mean what they meant on the Loki reader, and rows
come back in the same dict shape `audit_rows.query_events` returns, so one merge serves both.

Row `id` is the event's stream id (the unsigned form of `telemetry_events.event_uid`), the same
value the Loki row of that event carries. `line_sha256` is the digest of the recorded row, not of a
Loki line: the table does not keep the line bytes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, LiteralString

import psycopg
from psycopg import sql

from base.events.contract import TIER_BY_EVENT, EventTier

_LEVELS = ("debug", "info", "warning", "error", "critical")
_ANOMALY_LEVELS = ("warning", "error", "critical")

_COLUMNS: LiteralString = (
    "event_uid, ts, trace_id, span_id, agent_id, machine, cluster, process, category, event_name, "
    "level, source, target_agent_id, attributes"
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
        cluster,
        process,
        category,
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
            cluster,
            process,
            category,
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
        "category": category,
        "event_name": event_name,
        "level": level,
        "source": source,
        "target_agent_id": target_agent_id,
        "attributes": attributes,
    }


def _names(tier: EventTier) -> list[str]:
    return sorted(name for name, declared in TIER_BY_EVENT.items() if declared == tier)


def _tier_clause(tiers: list[EventTier]) -> tuple[sql.Composable, list[Any]] | None:
    """The predicate for a union of tiers, mirroring `base.events.contract.tier_for`.

    Every row here is non-audit, so a row is an anomaly from warning up or when its name is
    declared anomaly; otherwise its declared tier (an undeclared name is an observation). The
    `business` tier is the audit category's and matches nothing here.
    """
    clauses: list[sql.Composable] = []
    params: list[Any] = []
    levels = list(_ANOMALY_LEVELS)
    for tier in tiers:
        if tier == "business":
            continue
        if tier == "anomaly":
            clauses.append(sql.SQL("(level = ANY(%s) OR event_name = ANY(%s))"))
            params += [levels, _names("anomaly")]
        elif tier == "noise":
            clauses.append(sql.SQL("(NOT (level = ANY(%s)) AND event_name = ANY(%s))"))
            params += [levels, _names("noise")]
        else:
            clauses.append(sql.SQL("(NOT (level = ANY(%s)) AND NOT (event_name = ANY(%s)))"))
            params += [
                levels,
                sorted(
                    name for name, declared in TIER_BY_EVENT.items() if declared != "observation"
                ),
            ]
    if not clauses:
        return None
    return sql.SQL("(") + sql.SQL(" OR ").join(clauses) + sql.SQL(")"), params


_Clause = tuple[LiteralString, tuple[Any, ...]]


def _agent_clause(
    agent_id: int | None, exclude_agent_ids: list[int] | None, *, service_only: bool
) -> list[_Clause]:
    if agent_id is not None:
        return [("agent_id = %s", (agent_id,))]
    if exclude_agent_ids:
        return [("(agent_id IS NULL OR NOT (agent_id = ANY(%s)))", (exclude_agent_ids,))]
    return [("agent_id IS NULL", ())] if service_only else []


def _attribute_clauses(attribute_filters: dict[str, str] | None) -> list[_Clause]:
    """Values compare as text, so a JSON number or boolean matches its string form; a `!=` prefix
    negates, and a missing key reads as the empty string (the Loki reader's semantics)."""
    clauses: list[_Clause] = []
    for key, value in (attribute_filters or {}).items():
        if value.startswith("!="):
            clauses.append(("COALESCE(attributes ->> %s, '') <> %s", (key, value[2:])))
        else:
            clauses.append(("COALESCE(attributes ->> %s, '') = %s", (key, value)))
    return clauses


def _where(
    *,
    agent_id: int | None,
    exclude_agent_ids: list[int] | None,
    service_only: bool,
    event_names: list[str] | None,
    tiers: list[EventTier] | None,
    level_min: str | None,
    level: str | None,
    grep: str | None,
    categories: list[str] | None,
    cluster: str | None,
    machine: str | None,
    trace_id: str | None,
    attribute_filters: dict[str, str] | None,
    from_: datetime | None,
    to: datetime | None,
) -> tuple[sql.Composable, list[Any]] | None:
    """The WHERE clause and parameters, or None when the filters exclude every row."""
    clauses: list[sql.Composable] = []
    params: list[Any] = []
    if tiers is not None:
        tier = _tier_clause(tiers)
        if tier is None:
            return None
        clauses.append(tier[0])
        params.extend(tier[1])
    optional: list[tuple[bool, _Clause]] = [
        (bool(event_names), ("event_name = ANY(%s)", (event_names,))),
        (
            level_min is not None,
            ("level = ANY(%s)", (list(_LEVELS[_LEVELS.index(level_min) :]) if level_min else [],)),
        ),
        (level is not None, ("level = %s", (level.lower() if level else "",))),
        (
            bool(grep),
            (
                "position(lower(%s) IN lower(event_name || ' ' || source || ' ' || attributes::text)) > 0",
                (grep,),
            ),
        ),
        (bool(categories), ("category = ANY(%s)", (categories,))),
        (cluster is not None, ("(cluster = %s OR cluster = '')", (cluster,))),
        (machine is not None, ("machine = %s", (machine,))),
        (trace_id is not None, ("trace_id = %s", (trace_id,))),
        (from_ is not None, ("ts >= %s", (from_,))),
        (to is not None, ("ts <= %s", (to,))),
    ]
    chosen = [
        *_agent_clause(agent_id, exclude_agent_ids, service_only=service_only),
        *(clause for wanted, clause in optional if wanted),
        *_attribute_clauses(attribute_filters),
    ]
    for text, values in chosen:
        clauses.append(sql.SQL(text))
        params.extend(values)
    where = sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("TRUE")
    return where, params


def query_events(
    conn: psycopg.Connection,
    *,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    direction: str = "backward",
) -> tuple[list[dict[str, Any]], bool]:
    """Slice of the telemetry and log record, newest first (`backward`) or oldest first (`forward`).

    Returns `(rows, has_more)`; `has_more` comes from a one-row lookahead, as in the Loki
    reader. Ties on `ts` are ordered by the table's identity, stable across pages.
    """
    where = _where(
        agent_id=agent_id,
        exclude_agent_ids=exclude_agent_ids,
        service_only=service_only,
        event_names=event_names,
        tiers=tiers,
        level_min=level_min,
        level=level,
        grep=grep,
        categories=categories,
        cluster=cluster,
        machine=machine,
        trace_id=trace_id,
        attribute_filters=attribute_filters,
        from_=from_,
        to=to,
    )
    if where is None:
        return [], False
    clause, params = where
    order = sql.SQL("DESC") if direction == "backward" else sql.SQL("ASC")
    query = sql.SQL(
        "SELECT {columns} FROM telemetry_events WHERE {where} "
        "ORDER BY ts {order}, id {order} LIMIT %s OFFSET %s"
    ).format(columns=sql.SQL(_COLUMNS), where=clause, order=order)
    records = conn.execute(query, [*params, limit + 1, offset]).fetchall()
    return [_row(record) for record in records[:limit]], len(records) > limit


def count_events(
    conn: psycopg.Connection,
    *,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> int:
    """Exact count of the rows `query_events` would page through."""
    where = _where(
        agent_id=agent_id,
        exclude_agent_ids=exclude_agent_ids,
        service_only=service_only,
        event_names=event_names,
        tiers=tiers,
        level_min=level_min,
        level=level,
        grep=grep,
        categories=categories,
        cluster=cluster,
        machine=machine,
        trace_id=trace_id,
        attribute_filters=attribute_filters,
        from_=from_,
        to=to,
    )
    if where is None:
        return 0
    clause, params = where
    query = sql.SQL("SELECT count(*) FROM telemetry_events WHERE {where}").format(where=clause)
    row = conn.execute(query, params).fetchone()
    assert row is not None  # noqa: S101 — an aggregate always returns one row
    return int(row[0])
