"""Low-traffic-window VACUUM for the checkpoint tables (incremental reclamation).

The checkpoint reaper deletes old rows every hour, but physical space only
returns when a VACUUM reclaims the dead tuples — including the TOAST storage
that carries checkpoint blobs (a blob table can sit at hundreds of MB of
physical size while holding a few thousand live rows: 2026-08-10 measured
790 MB physical / 1.3 MB heap). Autovacuum eventually gets there, but on a
small, append-heavy table its default thresholds fire late, so dead TOAST
tuples accumulate between trims.

This module runs a plain `VACUUM (ANALYZE)` (never FULL — FULL takes an
ACCESS EXCLUSIVE lock and stalls agents, which the user explicitly ruled out)
on the checkpoint tables during the measured agent-lowest window. Each run is
small and lock-free (VACUUM allows concurrent reads and writes), matching the
"little and often, never blocking agents" retention philosophy.

Window: 05:00-08:00 CLUSTER time (`AVA_TIMEZONE`, cluster-pinned). The hours
themselves were measured on one fleet in America/Los_Angeles (turn_end/1h:
150-528 vs 6.8k peak at 11:00, 7-day sample, 2026-08-10) and are now read as
cluster wall clock — the same clock the built-in schedules fire on, so
"off-peak" means one thing across the fleet instead of three. A cluster whose
real trough sits elsewhere retunes the hours below, not the timezone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

import shared.db
from shared.config import settings
from shared.log import logger

_WINDOW_START_HOUR = 5
_WINDOW_END_HOUR = 8  # exclusive

_TABLES = ("checkpoint_blobs", "checkpoints", "checkpoint_writes")


@dataclass(frozen=True)
class VacuumResult:
    """One vacuum run's outcome: whether it ran, and the sizes it saw."""

    ran: bool
    total_bytes: int
    dead_tuples: int

    def summary(self) -> str:
        if not self.ran:
            return "outside low-traffic window — skipped"
        return (
            f"total={_mb(self.total_bytes)}MB dead={self.dead_tuples} "
            "(next window run re-measures reclamation)"
        )


def _mb(b: int) -> float:
    return round(b / 1024 / 1024, 1)


def in_low_traffic_window(now: datetime | None = None) -> bool:
    """True inside 05:00-08:00 cluster time (agent-lowest hours).

    Resolved per call rather than at import so a config write plus a daemon
    restart is enough to move the window; the daemon is long-lived and would
    otherwise hold a timezone captured at boot.
    """
    if now is None:
        now = datetime.now(UTC)
    local = now.astimezone(ZoneInfo(settings.general.timezone))
    return _WINDOW_START_HOUR <= local.hour < _WINDOW_END_HOUR


def _physical_state(cur: Any) -> tuple[int, int]:
    """(pg_total_relation_size(checkpoint_blobs), n_dead_tup) in one query.

    Both references pin the `public` schema explicitly: pg_stat_user_tables
    reports every schema, and a same-named table in another schema (e.g. the
    forensics `recovery` schema) would make the scalar subquery return more
    than one row — a CardinalityViolation that the daemon misreads as schema
    drift and crash-loops on (2026-08-12 incident).
    """
    cur.execute(
        """
        SELECT pg_total_relation_size('public.checkpoint_blobs'::regclass),
               COALESCE((SELECT n_dead_tup FROM pg_stat_user_tables
                         WHERE schemaname = 'public'
                           AND relname = 'checkpoint_blobs'), 0)
        """
    )
    row = cur.fetchone()
    assert row is not None  # noqa: S101 — aggregate over a fixed table always returns one row
    return int(row[0]), int(row[1])


def vacuum_checkpoint_tables(conn: Any) -> VacuumResult:
    """One incremental VACUUM (ANALYZE) pass over the checkpoint tables.

    `conn` must be an AUTONOMOUS autocommit connection (VACUUM cannot run in a
    transaction block and must not ride the pgbouncer transaction pool — same
    posture as the reindex governance pass). Plain VACUUM, never FULL: FULL
    takes ACCESS EXCLUSIVE and stalls agents, which the user explicitly ruled
    out; VACUUM allows concurrent reads and writes. Logs physical size +
    dead-tuple count before and after so the reclamation trend is visible in
    the daemon log (the convergence signal for Task #1130: 790MB -> steady
    state <=150MB).
    """
    with conn.cursor() as cur:
        before = _physical_state(cur)
        for table in _TABLES:
            cur.execute(f"VACUUM (ANALYZE) {table}")  # table names are module constants
        after = _physical_state(cur)
    result = VacuumResult(ran=True, total_bytes=after[0], dead_tuples=after[1])
    logger.info(
        "[events-maintenance] blob vacuum: %s (before=%sMB dead=%s)",
        result.summary(),
        _mb(before[0]),
        before[1],
    )
    return result


def run_blob_vacuum(*, force: bool = False) -> VacuumResult:
    """Daemon entry point: skip outside the low-traffic window (unless
    `force`), then dial a direct autocommit connection and vacuum.

    A fresh cluster before the PostgresSaver has created its tables reads as a
    no-op (logged, not raised) — same defensive posture as the checkpoint
    reaper's `_thread_counts`: a missing table here must not crash the
    maintenance daemon (its ProgrammingError handler exits the daemon), which
    would turn the daily window into a crash loop on greenfield clusters.
    """
    if not force and not in_low_traffic_window():
        return VacuumResult(ran=False, total_bytes=0, dead_tuples=0)
    try:
        with shared.db.connect(direct=True, autocommit=True) as conn:
            return vacuum_checkpoint_tables(conn)
    except psycopg.errors.UndefinedTable:
        logger.info(
            "[events-maintenance] blob vacuum skipped: checkpoint tables not present"
            " (fresh cluster before the PostgresSaver created them)"
        )
        return VacuumResult(ran=False, total_bytes=0, dead_tuples=0)
