"""PG ledger reads, the frozen-archive rollup read, and Loki archive helpers.

The daily ledgers (``agent_metrics_daily`` / ``agent_model_tokens_daily``) are
the durable aggregate source for full days; the frozen pre-cutover archive
lives in the Loki archive stream (task #1281), with ``agent_archive_stats``
materializing whole-life inspector values per agent. Inspect combines these
with a small retained Loki tail; none of the helpers infer data that was never
retained at the cutover.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, NamedTuple, cast

from psycopg import Connection, errors, sql

from gateway import loki_events
from shared.log import logger
from shared.loki_index_labels import ARCHIVE_FLOOR_AT, ARCHIVE_FREEZE_AT, retention_floor

_LOKI_SHARD = timedelta(hours=3)
_LOKI_SHARD_WORKERS = 4


class ArchiveStats(NamedTuple):
    """One agent's materialized frozen-archive inspector values."""

    turn_distribution: list[tuple[float, int]]
    active_seconds: float
    exec_seconds: float
    lifecycle: list[tuple[datetime, str]]


# The exclusive upper bound of the frozen event archive (the LGTM cutover,
# task #1197): rows before it live in the Loki archive stream (task #1281),
# rows at/after it in the live event stream.
FREEZE_AT = ARCHIVE_FREEZE_AT


def archive_stats(conn: Connection[Any], *, agent_id: int) -> ArchiveStats | None:
    """Return the materialized frozen-archive rollup row, or None when absent.

    The events archive is frozen, so one computed row serves every whole-life
    read. An agent with no archive rows has no rollup row; callers fall back
    to the raw archive reads (which return empty for it anyway).
    """
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT turn_distribution, active_seconds, exec_seconds, lifecycle "
                "FROM agent_archive_stats WHERE agent_id = %s",
                (agent_id,),
            )
            row = cur.fetchone()
    except errors.UndefinedTable:
        return None
    if row is None:
        return None
    return ArchiveStats(
        turn_distribution=[(float(value), int(count)) for value, count in row[0]],
        active_seconds=float(row[1]),
        exec_seconds=float(row[2]),
        lifecycle=[(datetime.fromisoformat(ts), str(event_name)) for ts, event_name in row[3]],
    )


_ARCHIVE_QUERY_LIMIT = 50_000


class ArchiveProjection(NamedTuple):
    """One agent's raw archive read, reduced client-side (mirrors the live
    inspect pass): exact turn-duration distribution, active/exec seconds, and
    the lifecycle row list."""

    turn_distribution: list[tuple[float, int]]
    active_seconds: float
    exec_seconds: float
    lifecycle: list[tuple[datetime, str]]


def archive_projected_values(
    *,
    agent_id: int,
    from_: datetime | None,
    to: datetime | None,
    timeout_s: float | None = None,
) -> ArchiveProjection:
    """Raw pre-cutover rows for one agent, reduced to the inspect shapes.

    The fallback to `agent_archive_stats`: whole-life reads use the
    materialized rollup; windowed reads (or agents without a rollup row)
    fetch the raw archive rows from the Loki archive stream in ONE pass and
    compute the exact distribution / active / exec / lifecycle client-side —
    the same reduction the live path applies to its raw lines. The archive
    query is bounded to the archive's own span and capped at
    `_ARCHIVE_QUERY_LIMIT` rows (a truncated pass logs a warning and returns
    the rows it got; the rollup covers the whole-life case).
    """
    rows, has_more = loki_events.query_events(
        agent_id=agent_id,
        event_names=[
            "^turn_end$",
            "^node_exit$",
            "^agent_spawned$",
            "^agent_resurrected$",
            "^agent_terminated$",
        ],
        # The archive stream only holds pre-cutover rows; an unbounded read
        # must span the whole archive, not the live default (now-24h).
        from_=from_ if from_ is not None else ARCHIVE_FLOOR_AT,
        to=to if to is not None else FREEZE_AT,
        limit=_ARCHIVE_QUERY_LIMIT,
        direction="forward",
        archive=True,
        timeout_s=timeout_s,
    )
    if has_more:
        logger.warning(
            "inspect archive read for agent %s exceeded the %d-row cap — values truncated",
            agent_id,
            _ARCHIVE_QUERY_LIMIT,
        )

    distribution: dict[float, int] = {}
    active_seconds = 0.0
    exec_seconds = 0.0
    lifecycle: list[tuple[datetime, str]] = []
    for row in rows:
        event_name = row["event_name"]
        attributes = row["attributes"]
        if event_name == "turn_end" and row["category"] in {"telemetry", "log"}:
            duration = _attribute_number(attributes, "duration_seconds")
            if duration is not None:
                distribution[duration] = distribution.get(duration, 0) + 1
        elif event_name == "node_exit":
            # The legacy per-node shape carries duration_seconds at the top
            # level; the aggregated shape nests it inside attributes.nodes.
            # Passing the top-level value lets _node_exit_durations pick the
            # right branch (same contract as the live path).
            duration = _attribute_number(attributes, "duration_seconds")
            active, exec_ = _node_exit_durations(attributes, duration)
            active_seconds += active
            exec_seconds += exec_
        elif event_name in {"agent_spawned", "agent_resurrected", "agent_terminated"}:
            lifecycle.append((row["ts"], event_name))
    return ArchiveProjection(
        turn_distribution=sorted(distribution.items()),
        active_seconds=active_seconds,
        exec_seconds=exec_seconds,
        lifecycle=lifecycle,
    )


def _attribute_number(attributes: dict[str, Any], key: str) -> float | None:
    """Return a numeric payload value (Loki's former unwrap would retain it)."""
    try:
        return float(attributes[key])
    except (KeyError, TypeError, ValueError):
        return None


def _node_exit_durations(
    attributes: dict[str, Any], legacy_duration: float | None
) -> tuple[float, float]:
    """Return active/exec seconds from aggregate or retained legacy rows."""
    nodes = attributes.get("nodes")
    entries: list[dict[str, Any]]
    if isinstance(nodes, list):
        entries = [cast(dict[str, Any], entry) for entry in nodes if isinstance(entry, dict)]
    elif legacy_duration is not None:
        entries = [attributes]
    else:
        return 0.0, 0.0

    active_seconds = exec_seconds = 0.0
    for entry in entries:
        duration = _attribute_number(entry, "duration_seconds")
        if duration is None:
            continue
        node = _attribute_text(entry, "node")
        if node != "claim":
            active_seconds += duration
        if node == "exec":
            exec_seconds += duration
    return active_seconds, exec_seconds


def _attribute_text(attributes: dict[str, Any], key: str) -> str:
    """Return a payload value as its JSON text (matching Loki label coercion)."""
    value = attributes.get(key)
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


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


def newest_token_day(
    conn: Connection[Any], *, agent_id: int, day_from: date | None, day_to: date | None
) -> date | None:
    """Return the newest token-ledger day in the requested UTC-day range."""
    where, params = _ledger_day_where(agent_id=agent_id, day_from=day_from, day_to=day_to)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT max(day) FROM agent_model_tokens_daily WHERE {}").format(where), params
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


def ledger_distribution(
    conn: Connection[Any], *, agent_id: int, day_from: date | None, day_to: date | None
) -> tuple[list[tuple[float, int]], bool]:
    """Merge daily duration buckets and report positive-turn histogram coverage.

    Missing ledger days are historical gaps, not coverage failures. A present
    ledger row with turns but an empty histogram is incomplete and makes
    callers retain the raw full-window fallback until maintenance repairs it.
    A zero-turn row is complete without a histogram.
    """
    where, params = _ledger_day_where(agent_id=agent_id, day_from=day_from, day_to=day_to)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT day, turn_total, turn_dur_hist FROM agent_metrics_daily WHERE {}"
            ).format(where),
            params,
        )
        rows = cur.fetchall()

    merged: dict[int, int] = {}
    complete = True
    for _day, turn_total, histogram in rows:
        if turn_total > 0 and not histogram:
            complete = False
        if not histogram:
            continue
        for bucket, count in histogram.items():
            bucket_int = int(bucket)
            merged[bucket_int] = merged.get(bucket_int, 0) + int(count)
    return [(float(bucket), count) for bucket, count in sorted(merged.items())], complete


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


def retained_live_window(
    *, from_: datetime | None, to: datetime | None, freeze: datetime | None
) -> tuple[datetime, datetime] | None:
    """Clip an inspect Loki read to its retained, post-archive live side."""
    now = datetime.now(tz=UTC)
    end = min(to or now, now)
    start = max(from_ or datetime.min.replace(tzinfo=UTC), retention_floor(now))
    if freeze is not None:
        start = max(start, freeze)
    return (start, end) if start < end else None


def split_loki_window(
    from_: datetime,
    to: datetime,
    *,
    shard_width: timedelta = _LOKI_SHARD,
) -> list[tuple[datetime, datetime]]:
    """Split a retained live read into contiguous, clock-aligned bounded spans."""
    if from_ >= to:
        return []
    if shard_width <= timedelta():
        raise ValueError("shard_width must be positive")
    spans: list[tuple[datetime, datetime]] = []
    start = from_
    shard_s = int(shard_width.total_seconds())
    while start < to:
        next_boundary_s = ((int(start.timestamp()) // shard_s) + 1) * shard_s
        end = min(datetime.fromtimestamp(next_boundary_s, tz=UTC), to)
        spans.append((start, end))
        start = end
    return spans


def query_loki_shards[T](
    from_: datetime,
    to: datetime,
    query: Callable[[datetime, datetime], T],
    *,
    shard_width: timedelta = _LOKI_SHARD,
) -> list[T]:
    """Run bounded Loki spans concurrently; each query acquires Loki's global slot."""
    spans = split_loki_window(from_, to, shard_width=shard_width)
    if len(spans) == 1:
        start, end = spans[0]
        return [query(start, end)]
    with ThreadPoolExecutor(max_workers=min(_LOKI_SHARD_WORKERS, len(spans))) as executor:
        futures = [executor.submit(query, start, end) for start, end in spans]
        return [future.result() for future in futures]
