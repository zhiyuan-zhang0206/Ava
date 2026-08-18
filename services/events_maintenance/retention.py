"""Event-name-aware retention for the unified `events` table — the daemon's
retention slice.

Each maintenance pass prunes or drops `events` month partitions whose rows have
outlived their retention. The policy is derived from the event registry
(`shared/events/contract.py`), the single source of truth: each event's
`EventSpec.retention_days` override, else its category floor (audit 365d /
telemetry 90d / log 30d), with per-category settings overrides applied on top
by the daemon. This slice only touches the unified `events` table; the legacy
`agent_events` / `event_log` tables are cleaned up separately after the
migration, out of scope here.

## Why plain DELETE on a per-month grid, without category sub-partitions

`events` partitions by month (RANGE on ts), so rows of every category share a
month partition. A naive "drop when the whole partition is expired" rule would
keep 90-day telemetry and 30-day log rows alive for the full 365-day audit
window — the opposite of the per-event contract. The alternative, LIST
sub-partitions by category inside each month, would restructure the table W1
built (a migration on a table whose partitions the daemon keeps rolling) for
little gain. The fact that makes plain DELETE exact: retention is computed from
`ts`, and a partition is a contiguous `ts` range, so **all rows of one event
kind in one partition expire at the same moment**. The per-partition plan:

  1. For each `events_YYYY_MM` partition, group rows by event_name and take
     max(ts) — the expiry moment of each event kind in that partition.
  2. If every event kind present is past its retention -> DROP the whole
     partition (DDL — no bloat, no per-row index churn).
  3. Else, DELETE the rows of the kinds that ARE past their retention;
     the audit rows stay, and the partition is dropped whole months later
     when audit expires too.
  4. Event names absent from the retention map are left untouched and block
     the whole-partition DROP (conservative: never delete data we have no
     policy for). Known kinds beside them are still pruned.

Idempotent: a dropped partition is no longer listed; a pruned category has
nothing left to delete. Observable: the result reports every partition dropped
and every category pruned with row counts; the daemon logs them (see
`services.events_maintenance.daemon`). The pass enforces per-operation
transactions itself: `apply_retention` switches the connection to autocommit on
entry (the daemon's pool connection defaults to autocommit=False — without the
switch every `conn.transaction()` below would degrade to a savepoint and the
first DROP's ACCESS EXCLUSIVE lock on `events` would be held until the whole
pass ended). A failure mid-pass therefore rolls back only the current operation
and the rest of the pass stays committed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg import sql

# Month partitions of the unified events table are named `events_YYYY_MM` by
# `services.events_maintenance.partitions`. Anything else under the parent
# (the DEFAULT catch-all, a legacy wide partition) is not a droppable month.
_PARTITION_RE = re.compile(r"^events_(\d{4})_(\d{2})$")

# Rows deleted per `_prune_event` batch: bounds the per-transaction work (and
# the row-lock hold time) when a first pass after the migration prunes a
# multi-million-row month partition.
_PRUNE_BATCH_SIZE = 5000

# Milliseconds a prune batch waits for the partition's lock before the batch
# fails. The pass is idempotent and self-catching-up, so a lock-contention
# failure just defers the batch to the next hourly pass.
_PRUNE_LOCK_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class DroppedPartition:
    """One month partition dropped whole — every event kind present had expired."""

    partition: str
    month_end: datetime
    events: tuple[str, ...]
    rows: int


@dataclass(frozen=True)
class PrunedEvent:
    """Rows of one expired event kind deleted from a still-live partition."""

    partition: str
    event: str
    cutoff: datetime
    rows: int


@dataclass(frozen=True)
class RetentionResult:
    """What one retention pass did — the daemon logs every entry."""

    dropped: tuple[DroppedPartition, ...] = ()
    pruned: tuple[PrunedEvent, ...] = ()

    def summary(self) -> str:
        """One-line human summary for the daemon log. Empty string when nothing
        was dropped or pruned this pass."""
        parts = [
            f"dropped {d.partition} ({', '.join(d.events) or 'empty'}, {d.rows} rows)"
            for d in self.dropped
        ]
        parts += [f"pruned {p.rows} {p.event} rows from {p.partition}" for p in self.pruned]
        return "; ".join(parts)


def _month_partitions(conn: psycopg.Connection) -> list[tuple[str, datetime]]:
    """(name, month_end) for every `events_YYYY_MM` partition, oldest first.
    Partitions whose names do not match the month shape (DEFAULT, legacy wide
    partitions) are skipped — they are not droppable units."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = 'events'"
        )
        names = [row[0] for row in cur.fetchall()]
    months: list[tuple[str, datetime]] = []
    for name in names:
        m = _PARTITION_RE.match(name)
        if m is None:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        month_end = datetime(
            year + 1 if month == 12 else year,
            1 if month == 12 else month + 1,
            1,
            tzinfo=UTC,
        )
        months.append((name, month_end))
    months.sort(key=lambda pair: pair[1])
    return months


def _event_max_ts(conn: psycopg.Connection, partition: str) -> dict[str, datetime]:
    """event_name -> max(ts) within the partition: each event kind's expiry
    moment. Empty for an empty partition."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT event_name, max(ts) FROM {} GROUP BY event_name").format(
                sql.Identifier(partition)
            )
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _scalar(cur: psycopg.Cursor) -> int:
    """First column of the single result row (asserted present — these queries
    always return one row)."""
    row = cur.fetchone()
    assert row is not None  # noqa: S101 — count(*) always yields one row
    return row[0]


def _drop_partition(conn: psycopg.Connection, partition: str) -> int:
    """DROP the month partition (detaches it from `events`). Returns the row
    count read just before the drop, so the log says how much data went."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(partition)))
        rows = _scalar(cur)
        cur.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(partition)))
    return rows


def _prune_event(conn: psycopg.Connection, partition: str, event: str, cutoff: datetime) -> int:
    """Delete the expired rows of one event kind from a still-live partition, in
    bounded batches — one transaction per batch (each batch commits, so a
    multi-million-row first pass never holds its row locks for the whole pass)
    and a per-batch `lock_timeout` so a contended partition fails fast instead
    of blocking the pass. Returns the total deleted row count (0 when already
    pruned — idempotent)."""
    total = 0
    while True:
        with conn.transaction(), conn.cursor() as cur:
            # SET does not accept bind parameters — inline the literal.
            cur.execute(
                sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(_PRUNE_LOCK_TIMEOUT_MS))
            )
            cur.execute(
                sql.SQL(
                    "DELETE FROM {} WHERE ctid IN ("
                    "SELECT ctid FROM {} WHERE event_name = %s AND ts <= %s LIMIT %s)"
                ).format(sql.Identifier(partition), sql.Identifier(partition)),
                (event, cutoff, _PRUNE_BATCH_SIZE),
            )
            deleted = cur.rowcount
        total += deleted
        if deleted < _PRUNE_BATCH_SIZE:
            break
    return total


def apply_retention(
    conn: psycopg.Connection,
    *,
    now_utc: datetime,
    retention_days: Mapping[str, int],
) -> RetentionResult:
    """One retention pass over the `events` table — drop every month partition
    whose event kinds have all expired, prune the expired kinds from the ones
    that still hold live data.

    `retention_days` is the per-event policy (event_name -> days); the daemon
    derives it from the event registry (`shared/events/contract.py`), tests pass
    it directly. An event name not present in the map is never deleted and
    blocks the whole-partition drop of any partition containing it (see module
    docstring). Idempotent: re-running after a successful pass is a no-op.
    `conn` must have no open transaction — the entry point switches it to
    autocommit (raising if a transaction is open), so each partition operation
    commits independently and the pass never holds the `events` parent lock
    across operations regardless of the caller's connection mode; the caller's
    connection mode is restored on exit (the pool's connection stays
    autocommit=False for the next borrower).
    """
    # The daemon's pool connection defaults to autocommit=False: without the
    # switch the SELECT in `_month_partitions` would open an implicit
    # transaction and every `conn.transaction()` below would degrade to a
    # savepoint, holding the first DROP's ACCESS EXCLUSIVE lock on `events`
    # until the pass ends. Switch FIRST — before any statement runs; restore
    # in `finally` so a borrowed pool connection is never left autocommit=True
    # for the next borrower (psycopg_pool does not reset autocommit on return).
    conn.autocommit = True
    try:
        dropped: list[DroppedPartition] = []
        pruned: list[PrunedEvent] = []
        for partition, month_end in _month_partitions(conn):
            if month_end > now_utc:
                # Current / future month — not retention's business yet. The daemon
                # ensures the current + next month partitions every pass; a
                # not-yet-started month is always empty, and dropping it here would
                # delete the partition the daemon just created, every hourly pass,
                # each time taking ACCESS EXCLUSIVE on `events` (and stranding that
                # month's writes in DEFAULT until the next carve). Only months
                # strictly in the past may be dropped or pruned.
                continue
            max_ts_by_event = _event_max_ts(conn, partition)
            if not max_ts_by_event:
                # Empty month partition strictly in the past — nothing to keep.
                rows = _drop_partition(conn, partition)
                dropped.append(DroppedPartition(partition, month_end, (), rows))
                continue
            cutoffs = {
                event: now_utc - timedelta(days=retention_days[event])
                for event in max_ts_by_event
                if event in retention_days
            }
            unknown = [e for e in max_ts_by_event if e not in retention_days]
            expired = {
                event: cutoff
                for event, cutoff in cutoffs.items()
                if max_ts_by_event[event] <= cutoff
            }
            if not unknown and len(expired) == len(max_ts_by_event):
                # Every event kind present has expired — the whole partition goes.
                rows = _drop_partition(conn, partition)
                dropped.append(DroppedPartition(partition, month_end, tuple(max_ts_by_event), rows))
            else:
                # The partition still holds live data (audit, or an unknown
                # event name): prune only the kinds that have expired.
                for event, cutoff in expired.items():
                    rows = _prune_event(conn, partition, event, cutoff)
                    if rows:
                        pruned.append(PrunedEvent(partition, event, cutoff, rows))
        return RetentionResult(dropped=tuple(dropped), pruned=tuple(pruned))
    finally:
        conn.autocommit = False
