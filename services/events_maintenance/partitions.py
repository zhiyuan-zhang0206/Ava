"""Monthly partition rolling for the event tables — the events-maintenance
daemon's partition-management responsibility.

Each maintenance pass ensures the current and next UTC-month partitions of
the unified `events` table exist, so writes never fall into a DEFAULT
catch-all (which would strand a partial month there and defeat the
per-month retention DROP of `services.events_maintenance.retention`). The daemon (`services.events_maintenance.daemon`)
calls `ensure_month_partitions` on its poll loop; tests call it directly
against a testcontainers DB.

Partition boundaries are explicit UTC month midnights, matching the rollup's
UTC-day model, so the grid is timezone-independent regardless of the server's
timezone.

The create is idempotent and covers three cases, so a month is "ensured"
whether it is new, already partitioned, or currently sitting in DEFAULT:
  - already covered by a partition (by name, or by the wide legacy partition
    the conversion migration left behind) — no-op;
  - uncovered and DEFAULT holds no row in the month — a plain CREATE;
  - uncovered but DEFAULT already holds rows for the month (a fresh DB that
    wrote before the first pass, or a long daemon outage) — carve them out:
    DETACH DEFAULT, create the partition, move the rows in, reattach DEFAULT.

In steady state the daemon stays ahead of the write frontier, so every call
hits the "already covered" no-op path and the carve is never exercised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg import sql

_log = logging.getLogger("services.events_maintenance.partitions")

# (parent table, DEFAULT partition) — the unified event tables share the same
# monthly grid; a new event store registers here and inherits the whole
# maintenance contract. `events` is the unified stream (design §1); the legacy
# `agent_events` mirror is frozen (zero writes since the migration window
# closed) and deliberately NOT registered — its removal is a migration-cleanup
# task, but it no longer pays a per-hour partition tax.
_EVENT_TABLES: tuple[tuple[str, str], ...] = (("events", "events_default"),)


@dataclass(frozen=True)
class _Month:
    """A UTC calendar month as its half-open partition range [start, end)."""

    start: datetime
    end: datetime

    def partition_name(self, parent: str) -> str:
        """Partition name for a parent: `<parent>_YYYY_MM`."""
        return f"{parent}_{self.start:%Y_%m}"


def _month_of(dt: datetime) -> _Month:
    """The UTC month containing `dt`, as a [first-of-month, first-of-next) range."""
    start = datetime(dt.year, dt.month, 1, tzinfo=UTC)
    end = (
        datetime(dt.year + 1, 1, 1, tzinfo=UTC)
        if dt.month == 12
        else datetime(dt.year, dt.month + 1, 1, tzinfo=UTC)
    )
    return _Month(start=start, end=end)


def _partition_exists(cur: psycopg.Cursor, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_class WHERE relname = %s AND relnamespace = 'public'::regnamespace",
        (name,),
    )
    return cur.fetchone() is not None


def _create_partition(cur: psycopg.Cursor, parent: str, month: _Month) -> None:
    """Plain CREATE of the month partition. Raises InvalidObjectDefinition (42P17)
    if the range overlaps an existing partition and CheckViolation (23514) if
    DEFAULT holds rows in the range — both handled by the caller. Partition bounds
    must be literal constants (not bind parameters), so the timestamps are inlined
    via sql.Literal."""
    cur.execute(
        sql.SQL("CREATE TABLE {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})").format(
            sql.Identifier(month.partition_name(parent)),
            sql.Identifier(parent),
            sql.Literal(month.start),
            sql.Literal(month.end),
        )
    )


def _carve_from_default(conn: psycopg.Connection, parent: str, default: str, month: _Month) -> None:
    """Create the month partition when DEFAULT already holds rows for it: detach
    DEFAULT, create the partition, move the in-range rows across, reattach DEFAULT.
    One transaction so a failure leaves the partition set intact. Takes a brief
    ACCESS EXCLUSIVE lock on the parent (the detach/attach), so writes stall
    momentarily — acceptable because this path is only reached off the steady
    state (fresh DB / long outage), where DEFAULT is small."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER TABLE {} DETACH PARTITION {}").format(
                sql.Identifier(parent), sql.Identifier(default)
            )
        )
        _create_partition(cur, parent, month)
        cur.execute(
            sql.SQL("INSERT INTO {} SELECT * FROM {} WHERE ts >= %s AND ts < %s").format(
                sql.Identifier(parent), sql.Identifier(default)
            ),
            (month.start, month.end),
        )
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE ts >= %s AND ts < %s").format(sql.Identifier(default)),
            (month.start, month.end),
        )
        cur.execute(
            sql.SQL("ALTER TABLE {} ATTACH PARTITION {} DEFAULT").format(
                sql.Identifier(parent), sql.Identifier(default)
            )
        )


def _stranded_default_months(conn: psycopg.Connection, default: str) -> list[_Month]:
    """The distinct UTC months whose rows are currently sitting in the DEFAULT
    partition. Non-empty only after a write beat the pass or the daemon was down
    across a month boundary; in steady state DEFAULT is empty and this returns []."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT DISTINCT date_trunc('month', ts AT TIME ZONE 'UTC') FROM {}").format(
                sql.Identifier(default)
            )
        )
        return [_month_of(row[0].replace(tzinfo=UTC)) for row in cur.fetchall()]


def _ensure_one(conn: psycopg.Connection, parent: str, default: str, month: _Month) -> str | None:
    """Ensure the single month partition exists. Returns its name if this call
    created it, None if it was already covered."""
    with conn.cursor() as cur:
        if _partition_exists(cur, month.partition_name(parent)):
            return None
    try:
        with conn.transaction(), conn.cursor() as cur:
            _create_partition(cur, parent, month)
    except psycopg.errors.InvalidObjectDefinition:
        # 42P17: the range overlaps an existing partition — the month is already
        # covered (e.g. by the wide legacy partition the conversion left behind).
        return None
    except psycopg.errors.CheckViolation:
        # 23514: DEFAULT holds rows in this range — carve them out into the new
        # partition instead of a plain create.
        _carve_from_default(conn, parent, default, month)
        return month.partition_name(parent)
    return month.partition_name(parent)


def ensure_month_partitions(conn: psycopg.Connection, *, now_utc: datetime) -> list[str]:
    """Ensure every event table has a real month partition for the current +
    next UTC month AND for every month whose rows are stranded in DEFAULT.

    The current + next months keep partitions ahead of the write frontier. The
    stranded-DEFAULT months drain the catch-all back to empty after a write that
    beat the pass or a multi-month daemon outage — otherwise those intermediate
    months would sit in DEFAULT forever, keeping windowed readers scanning it and
    blocking the retention slice from dropping them as whole partitions.

    Returns the names of the partitions created by this call (an already-covered
    month is a no-op). Idempotent. `conn` must have no open transaction (a fresh
    pool connection — the daemon's contract); each month is ensured in its own
    transaction so one month's carve cannot roll back another's create. The
    entry switches the connection to autocommit for the pass and restores the
    caller's mode in `finally`: on a default (autocommit=False) connection the
    `_partition_exists` SELECT would otherwise open an implicit transaction and
    every `conn.transaction()` below would degrade to a savepoint, holding the
    carve's ACCESS EXCLUSIVE locks until the whole pass ends. The restore also
    keeps a borrowed pool connection clean for the next borrower (psycopg_pool
    does not reset autocommit on return).
    """
    autocommit = conn.autocommit
    conn.autocommit = True
    try:
        created: list[str] = []
        this_month = _month_of(now_utc)
        next_month = _month_of(this_month.end)
        for parent, default in _EVENT_TABLES:
            seen: set[str] = set()
            for month in (this_month, next_month, *_stranded_default_months(conn, default)):
                name = month.partition_name(parent)
                if name in seen:
                    continue
                seen.add(name)
                created_name = _ensure_one(conn, parent, default, month)
                if created_name is not None:
                    created.append(created_name)
        return created
    finally:
        conn.autocommit = autocommit
