"""Bound every checkpoint thread to its newest three versions.

LangGraph's PostgresSaver appends one checkpoint per super-step under Ava's
default durability. The gateway-owned events-maintenance daemon therefore
scans the checkpoint table every minute and prunes every thread above the
fixed keep-three budget, independent of agent status or liveness. This makes
the scan and retained storage O(thread count).

Compaction-boundary checkpoints are exempt from the budget: a row stamped
`compact_boundary: true` is never trimmed, so each past compaction segment
stays recoverable as one full snapshot (user ruling: preserve information,
bound storage). The shared trim protects each candidate against an
in-progress messages write and retains blobs by the channel-version references
of surviving checkpoints.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from shared.checkpoint_cleanup import trim_checkpoints_sync
from shared.log import logger

_KEEP = 3
_MAX_THREADS_PER_PASS = 64


@dataclass(frozen=True)
class ReapCounts:
    """One pruning pass's totals.

    `agents` is retained for the existing telemetry contract; it counts
    productive checkpoint threads, including threads without an agent row.
    """

    agents: int
    checkpoints: int
    writes: int
    blobs: int


def _thread_counts(pool: ConnectionPool) -> dict[str, int]:
    """Return the checkpoint row count for every stored thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT thread_id, count(*) AS n
                FROM checkpoints
                GROUP BY thread_id
                """
            )
            return {row[0]: row[1] for row in cur.fetchall()}
        except psycopg.errors.UndefinedTable:
            # The saver creates its tables lazily. A fresh cluster has no
            # checkpoint retention work yet.
            logger.info("[checkpoint-reaper] checkpoints table not present; skipping pass")
            return {}


def _still_above_keep(pool: ConnectionPool, thread_id: str) -> bool:
    """Re-check eligibility immediately before pruning one candidate."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s",
            (thread_id,),
        )
        row = cur.fetchone()
    assert row is not None, "count(*) always returns one row"  # noqa: S101
    return row[0] > _KEEP


def _rotate_candidates(
    candidates: list[str],
    *,
    max_threads: int = _MAX_THREADS_PER_PASS,
    now_seconds: float | None = None,
) -> list[str]:
    """Rotate the candidate window by the pass size, restart-safe.

    The wall-clock-derived offset advances by `max_threads` each minute. A
    repeatedly over-budget thread at the sorted head therefore cannot starve
    the rest of a candidate set larger than the per-pass cap.
    """
    candidate_count = len(candidates)
    if candidate_count == 0:
        return []
    minute = int(time.time() // 60) if now_seconds is None else int(now_seconds // 60)
    start = (minute * max_threads) % candidate_count
    return candidates[start:] + candidates[:start]


def _reap(
    pool: ConnectionPool,
    thread_ids: list[str],
    *,
    max_threads: int = _MAX_THREADS_PER_PASS,
) -> ReapCounts:
    """Prune up to `max_threads` productive candidates to `_KEEP`.

    Eligibility is re-checked immediately before each trim. A candidate whose
    row count changed, or whose trim is blocked by the in-flight write guard,
    does not consume a productive slot.
    """
    total = ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    productive = 0
    for thread_id in thread_ids:
        if productive >= max_threads:
            break
        if not _still_above_keep(pool, thread_id):
            continue
        counts = trim_checkpoints_sync(pool, thread_id, keep=_KEEP)
        if not counts.checkpoints:
            continue
        productive += 1
        total = ReapCounts(
            agents=total.agents + 1,
            checkpoints=total.checkpoints + counts.checkpoints,
            writes=total.writes + counts.writes,
            blobs=total.blobs + counts.blobs,
        )
    if total.agents:
        logger.info(
            "[checkpoint-reaper] pruned {} thread(s): checkpoints={} writes={} blobs={}",
            total.agents,
            total.checkpoints,
            total.writes,
            total.blobs,
        )
    return total


def prune_threads(pool: ConnectionPool) -> ReapCounts:
    """Prune every thread above three checkpoints on the daemon's fast loop.

    The initial table-wide grouping is O(thread count), candidates are rotated
    for fair coverage, and one pass performs at most 64 productive trims.
    Re-running is idempotent because threads at the keep-three budget are no
    longer candidates.
    """
    counts = _thread_counts(pool)
    candidates = sorted(thread_id for thread_id, count in counts.items() if count > _KEEP)
    rotated = _rotate_candidates(candidates)
    return _reap(pool, rotated)
