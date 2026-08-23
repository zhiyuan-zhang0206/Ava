"""PG archive/ledger reads and bounded Loki-tail helpers for agent inspect.

The ``events`` table is a frozen archive, while the daily ledgers are the
durable aggregate source.  Inspect combines either with a small retained Loki
tail; none of the helpers infer data that was never retained at the cutover.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from psycopg import Connection, sql

_LOKI_RETENTION = timedelta(hours=168)
_LOKI_SHARD = timedelta(hours=3)
_LOKI_SHARD_WORKERS = 4


def freeze_point(cur: Any) -> datetime | None:
    """Return the exclusive upper bound of the frozen event archive."""
    cur.execute("SELECT max(ts) FROM events")
    row = cur.fetchone()
    return row[0] if row is not None else None


def _archive_where(
    cur: Any,
    *,
    agent_id: int,
    event_names: list[str],
    categories: list[str] | None,
    attribute_filters: dict[str, str] | None,
    from_: datetime | None,
    to: datetime | None,
) -> tuple[sql.Composable, list[Any]]:
    """Build the events predicate matching the inspect Loki filters."""
    freeze = freeze_point(cur)
    if freeze is None:
        return sql.SQL("FALSE"), []

    clauses: list[sql.Composable] = [
        sql.SQL("agent_id = %s"),
        sql.SQL("event_name ~ ANY(%s::text[])"),
        sql.SQL("ts < COALESCE(%s, now())"),
        sql.SQL("ts < %s"),
    ]
    params: list[Any] = [agent_id, event_names, to, freeze]
    if categories is not None:
        clauses.append(sql.SQL("category = ANY(%s::text[])"))
        params.append(categories)
    if from_ is not None:
        clauses.append(sql.SQL("ts >= %s"))
        params.append(from_)
    for key, value in (attribute_filters or {}).items():
        if value.startswith("!="):
            clauses.append(sql.SQL("COALESCE(attributes ->> %s, '') <> %s"))
            params.extend((key, value[2:]))
        else:
            clauses.append(sql.SQL("attributes ->> %s = %s"))
            params.extend((key, value))
    return sql.SQL(" AND ").join(clauses), params


def archive_count(
    conn: Connection[Any],
    *,
    agent_id: int,
    event_names: list[str],
    categories: list[str] | None,
    attribute_filters: dict[str, str] | None,
    from_: datetime | None,
    to: datetime | None,
) -> int:
    """Count archive events with the inspect path's Loki-equivalent filters."""
    with conn.cursor() as cur:
        where, params = _archive_where(
            cur,
            agent_id=agent_id,
            event_names=event_names,
            categories=categories,
            attribute_filters=attribute_filters,
            from_=from_,
            to=to,
        )
        cur.execute(sql.SQL("SELECT count(*) FROM events WHERE {}").format(where), params)
        row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def archive_aggregate(
    conn: Connection[Any],
    *,
    field: str,
    agg: str,
    agent_id: int,
    event_names: list[str],
    categories: list[str] | None,
    attribute_filters: dict[str, str] | None,
    from_: datetime | None,
    to: datetime | None,
) -> float:
    """Compute one numeric archive aggregate with Loki-equivalent filters."""
    aggregations = {"sum": sql.SQL("sum"), "min": sql.SQL("min"), "max": sql.SQL("max")}
    if agg not in aggregations:
        raise ValueError(f"archive_aggregate: unknown agg {agg!r}")
    with conn.cursor() as cur:
        where, params = _archive_where(
            cur,
            agent_id=agent_id,
            event_names=event_names,
            categories=categories,
            attribute_filters=attribute_filters,
            from_=from_,
            to=to,
        )
        cur.execute(
            sql.SQL("SELECT {}((attributes ->> %s)::float8) FROM events WHERE {}").format(
                aggregations[agg], where
            ),
            [field, *params],
        )
        row = cur.fetchone()
    return float(row[0]) if row is not None and row[0] is not None else 0.0


def archive_distribution(
    conn: Connection[Any],
    *,
    field: str,
    agent_id: int,
    event_names: list[str],
    categories: list[str] | None,
    attribute_filters: dict[str, str] | None,
    from_: datetime | None,
    to: datetime | None,
) -> list[tuple[float, int]]:
    """Return the archive value/count distribution for a numeric attribute."""
    with conn.cursor() as cur:
        where, params = _archive_where(
            cur,
            agent_id=agent_id,
            event_names=event_names,
            categories=categories,
            attribute_filters=attribute_filters,
            from_=from_,
            to=to,
        )
        cur.execute(
            sql.SQL(
                "SELECT (attributes ->> %s)::float8 AS value, count(*) "
                "FROM events WHERE {} GROUP BY value ORDER BY value"
            ).format(where),
            [field, *params],
        )
        rows = cur.fetchall()
    return [(float(value), int(count)) for value, count in rows]


def _ledger_day_where(
    *, agent_id: int, day_from: date | None, day_to: date | None
) -> tuple[sql.Composable, list[Any]]:
    clauses: list[sql.Composable] = [sql.SQL("agent_id = %s")]
    params: list[Any] = [agent_id]
    if day_from is not None:
        clauses.append(sql.SQL("day >= %s"))
        params.append(day_from)
    if day_to is not None:
        clauses.append(sql.SQL("day <= %s"))
        params.append(day_to)
    return sql.SQL(" AND ").join(clauses), params


def _watermark(max_day: date | None) -> datetime | None:
    if max_day is None:
        return None
    return datetime.combine(max_day + timedelta(days=1), time.min, tzinfo=UTC)


def newest_ledger_day(
    conn: Connection[Any], *, agent_id: int, day_from: date | None, day_to: date | None
) -> date | None:
    """Return the newest metrics-ledger day in the requested UTC-day range."""
    where, params = _ledger_day_where(agent_id=agent_id, day_from=day_from, day_to=day_to)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT max(day) FROM agent_metrics_daily WHERE {}").format(where), params
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def ledger_stats(
    conn: Connection[Any], *, agent_id: int, day_from: date | None, day_to: date | None
) -> tuple[int, int, float, float | None, float | None, int, int, datetime | None]:
    """Aggregate full UTC days from ``agent_metrics_daily`` plus its watermark."""
    where, params = _ledger_day_where(agent_id=agent_id, day_from=day_from, day_to=day_to)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT COALESCE(sum(turn_total), 0), COALESCE(sum(turn_ok), 0), "
                "COALESCE(sum(turn_dur_sum), 0), min(turn_dur_min), max(turn_dur_max), "
                "COALESCE(sum(exec_ok), 0), COALESCE(sum(exec_failed), 0), max(day) "
                "FROM agent_metrics_daily WHERE {}"
            ).format(where),
            params,
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("ledger_stats aggregate returned no row")
    return (
        int(row[0]),
        int(row[1]),
        float(row[2]),
        float(row[3]) if row[3] is not None else None,
        float(row[4]) if row[4] is not None else None,
        int(row[5]),
        int(row[6]),
        _watermark(row[7]),
    )


def ledger_tokens(
    conn: Connection[Any], *, agent_id: int, day_from: date | None, day_to: date | None
) -> tuple[int, datetime | None]:
    """Sum full UTC-day output tokens and return the newest day's watermark."""
    where, params = _ledger_day_where(agent_id=agent_id, day_from=day_from, day_to=day_to)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT COALESCE(sum(tokens_out), 0), max(day) FROM agent_model_tokens_daily WHERE {}"
            ).format(where),
            params,
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("ledger_tokens aggregate returned no row")
    return int(row[0]), _watermark(row[1])


def archive_lifecycle(
    conn: Connection[Any],
    *,
    agent_id: int,
    from_: datetime | None,
    to: datetime | None,
) -> list[tuple[datetime, str]]:
    """Return archive lifecycle rows in chronological order for replay."""
    with conn.cursor() as cur:
        where, params = _archive_where(
            cur,
            agent_id=agent_id,
            event_names=["^agent_spawned$", "^agent_resurrected$", "^agent_terminated$"],
            categories=None,
            attribute_filters=None,
            from_=from_,
            to=to,
        )
        cur.execute(
            sql.SQL("SELECT ts, event_name FROM events WHERE {} ORDER BY ts").format(where),
            params,
        )
        rows = cur.fetchall()
    return [(ts, event_name) for ts, event_name in rows]


def retained_live_window(
    *, from_: datetime | None, to: datetime | None, freeze: datetime | None
) -> tuple[datetime, datetime] | None:
    """Clip an inspect Loki read to its retained, post-archive live side."""
    now = datetime.now(tz=UTC)
    end = min(to or now, now)
    start = max(from_ or datetime.min.replace(tzinfo=UTC), now - _LOKI_RETENTION)
    if freeze is not None:
        start = max(start, freeze)
    return (start, end) if start < end else None


def split_loki_window(from_: datetime, to: datetime) -> list[tuple[datetime, datetime]]:
    """Split a retained live read into contiguous, clock-aligned <=3h spans."""
    if from_ >= to:
        return []
    spans: list[tuple[datetime, datetime]] = []
    start = from_
    shard_s = int(_LOKI_SHARD.total_seconds())
    while start < to:
        next_boundary_s = ((int(start.timestamp()) // shard_s) + 1) * shard_s
        end = min(datetime.fromtimestamp(next_boundary_s, tz=UTC), to)
        spans.append((start, end))
        start = end
    return spans


def query_loki_shards[T](
    from_: datetime, to: datetime, query: Callable[[datetime, datetime], T]
) -> list[T]:
    """Run bounded Loki spans concurrently; each query acquires Loki's global slot."""
    spans = split_loki_window(from_, to)
    if len(spans) == 1:
        start, end = spans[0]
        return [query(start, end)]
    with ThreadPoolExecutor(max_workers=min(_LOKI_SHARD_WORKERS, len(spans))) as executor:
        futures = [executor.submit(query, start, end) for start, end in spans]
        return [future.result() for future in futures]
