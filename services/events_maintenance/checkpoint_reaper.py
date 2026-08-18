"""Checkpoint retention — the events-maintenance daemon's checkpoint slice.

LangGraph's PostgresSaver keeps every checkpoint of a thread forever (one per
super-step under the default durability): over weeks of operation the
checkpoints / checkpoint_writes / checkpoint_blobs tables grow without bound
(the 2026-08-08 disk crisis: 21GB of blobs from ~12h of terminated-agent
accumulation). The agent process never deletes checkpoints (retention is a
gateway-side concern); this daemon slice is the single owner of checkpoint
retention, in two rules (Task #1125, user ruling):

- Rule B — stale threads (`reap_stale_checkpoints`, hourly): agents whose
  status is 'terminated', or live agents inactive for `_INACTIVE_HOURS`, are
  trimmed to keep=1. A terminated agent cannot resume; an inactive one is
  either dead-weight or (on waking) fully restorable from the single newest
  checkpoint. The status is re-checked immediately before each trim so a
  resurrect racing the pass is skipped, not reaped.
- Rule A — overgrown live threads (`trim_overgrown_threads`, fast loop):
  active threads past `_LIVE_TRIM_THRESHOLD` checkpoints are trimmed to
  keep=`_LIVE_KEEP`, replacing the removed agent-side idle trim (which only
  fired when an agent actually idled — continuously working agents grew
  without bound; the service bounds every thread regardless of liveness).

Both rules share the trim's safety invariant (every channel stores a full
snapshot; `messages` is a plain add_messages accumulator), so the surviving
latest checkpoint restores the full conversation; and both always keep
compaction-boundary checkpoints (metadata `compact_boundary: true`), so each
past compaction segment stays recoverable as one full snapshot.

The reaper is a slow, defensive pass: it logs totals and per-thread trims, and
a pass that finds nothing logs nothing (same convention as the rollup).
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from shared.checkpoint_cleanup import trim_checkpoints_sync
from shared.log import logger

# Rule B: stale threads keep the latest 1 checkpoint — the minimum that still
# lets a resurrect restore the full conversation.
_KEEP = 1
# Rule B: a live agent is "inactive" (a retention candidate) after this many
# hours without a completed turn (last_active_at is updated on every LLM turn,
# never by heartbeats, so idling-but-alive agents are not misclassified).
_INACTIVE_HOURS = 24
# Rule A: an active thread is trimmed once it holds more than this many
# checkpoints, collapsed back to the keep count.
_LIVE_TRIM_THRESHOLD = 20
_LIVE_KEEP = 5


@dataclass(frozen=True)
class ReapCounts:
    """One reaper pass's totals."""

    agents: int
    checkpoints: int
    writes: int
    blobs: int


def _thread_counts(pool: ConnectionPool) -> dict[str, int]:
    """thread_id -> checkpoint row count, for every thread with a row."""
    with pool.connection() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT thread_id, count(*) AS n
                FROM checkpoints
                WHERE NOT COALESCE((metadata ->> 'compact_boundary')::boolean, false)
                GROUP BY thread_id
                """
            )
            return {row[0]: row[1] for row in cur.fetchall()}
        except psycopg.errors.UndefinedTable:
            # A fresh cluster before the PostgresSaver has created its tables,
            # or a throwaway test DB without them: a defensive pass — nothing
            # to reap reads as an empty pass, not a crash. `agents_meta` is
            # baseline schema, so only the saver's table is guarded.
            logger.info("[checkpoint-reaper] checkpoints table not present; skipping pass")
            return {}


def _stale_agents(pool: ConnectionPool, counts: dict[str, int]) -> list[int]:
    """Rule B candidates: terminated, or inactive for `_INACTIVE_HOURS`, and
    still holding more than `_KEEP` checkpoints.

    The `> _KEEP` filter makes the pass idempotent under a per-pass candidate
    cap: an already-trimmed thread (at keep=1) is not a candidate, so a capped
    pass that could not reach it this round does not re-trim it next round
    instead of the ones still waiting.
    """
    if not counts:
        return []
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM agents_meta
            WHERE id::text = ANY(%s)
              AND (status = 'terminated'
                   OR last_active_at < now() - make_interval(hours => %s))
              AND (SELECT count(*) FROM checkpoints c
                   WHERE c.thread_id = agents_meta.id::text
                     AND NOT COALESCE((c.metadata ->> 'compact_boundary')::boolean, false)
                  ) > %s
            ORDER BY id
            """,
            (list(counts.keys()), _INACTIVE_HOURS, _KEEP),
        )
        return [row[0] for row in cur.fetchall()]


def _overgrown_active(pool: ConnectionPool, counts: dict[str, int]) -> list[int]:
    """Rule A candidates: active threads holding more than `_LIVE_TRIM_THRESHOLD`
    checkpoints. `_stale_agents` is consulted first (by the caller) so a stale
    thread is handled by Rule B's tighter keep, never by Rule A."""
    if not counts:
        return []
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM agents_meta
            WHERE id::text = ANY(%s)
              AND status <> 'terminated'
              AND last_active_at >= now() - make_interval(hours => %s)
            ORDER BY id
            """,
            (list(counts.keys()), _INACTIVE_HOURS),
        )
        live = [row[0] for row in cur.fetchall()]
    return [aid for aid in live if counts.get(str(aid), 0) > _LIVE_TRIM_THRESHOLD]


def _still_stale(pool: ConnectionPool, agent_id: int) -> bool:
    """Re-check the candidate right before the trim: a resurrect racing the
    pass (status flipped, or last_active_at refreshed after the candidate
    scan) must not have its fresh checkpoints reaped."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT (status = 'terminated'"
            "        OR last_active_at < now() - make_interval(hours => %s))"
            " FROM agents_meta WHERE id = %s",
            (_INACTIVE_HOURS, agent_id),
        )
        row = cur.fetchone()
    return bool(row) and row[0] is True


def _still_overgrown(pool: ConnectionPool, agent_id: int, threshold: int) -> bool:
    """Re-check a Rule A candidate right before the trim: the thread must still
    hold more than `threshold` checkpoints (the agent may have been trimmed by
    another pass, or compacted in between)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s"
            " AND NOT COALESCE((metadata ->> 'compact_boundary')::boolean, false)",
            (str(agent_id),),
        )
        row = cur.fetchone()
    assert row is not None, "count(*) always returns one row"  # noqa: S101
    return row[0] > threshold


_MAX_AGENTS_PER_PASS = 8
"""Cap on candidate threads trimmed in one pass.

The daemon runs the reaper on its hourly loop; a burst of terminated agents
(an incident, or the first pass after a long downtime) can present hundreds of
candidates at once. Trimming them all in one pass is a long busy stretch of
short transactions — fine for the DB (each trim is its own transaction) but it
monopolizes the maintenance daemon's single worker thread for minutes. Capping
the pass spreads the same idempotent work across the following hours; Rule A
(overgrown active threads) still bounds live agents within one interval."""


def _reap(
    pool: ConnectionPool,
    agent_ids: list[int],
    keep: int,
    label: str,
    *,
    max_agents: int = _MAX_AGENTS_PER_PASS,
) -> ReapCounts:
    """Trim up to `max_agents` PRODUCTIVE threads to `keep`, re-checking
    eligibility per thread. The cap counts only trims that actually deleted
    checkpoints: a no-op candidate (already at keep, or only compaction
    boundaries left) does not consume a slot, so a burst of no-op threads can
    never starve the ones still holding doomed rows. The rest stay candidates
    for the next pass (idempotent)."""
    total = ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    productive = 0
    for agent_id in agent_ids:
        if productive >= max_agents:
            break
        counts = trim_checkpoints_sync(pool, str(agent_id), keep=keep)
        if counts.checkpoints:
            productive += 1
            total = ReapCounts(
                agents=total.agents + 1,
                checkpoints=total.checkpoints + counts.checkpoints,
                writes=total.writes + counts.writes,
                blobs=total.blobs + counts.blobs,
            )
    if total.agents:
        logger.info(
            "[checkpoint-reaper] {} reaped {} agent(s): checkpoints={} writes={} blobs={}",
            label,
            total.agents,
            total.checkpoints,
            total.writes,
            total.blobs,
        )
    return total


def reap_stale_checkpoints(pool: ConnectionPool) -> ReapCounts:
    """One Rule B pass: trim every stale thread (terminated, or inactive for
    `_INACTIVE_HOURS`) to `_KEEP` checkpoints.

    Returns the aggregate counts (0/0/0/0 when nothing to reap). Idempotent —
    a thread already at keep=1 is not a candidate, so re-running is a no-op.
    """
    counts = _thread_counts(pool)
    candidates = _stale_agents(pool, counts)
    eligible = [aid for aid in candidates if _still_stale(pool, aid)]
    return _reap(pool, eligible, _KEEP, "stale")


def trim_overgrown_threads(pool: ConnectionPool) -> ReapCounts:
    """One Rule A pass: trim active threads past `_LIVE_TRIM_THRESHOLD`
    checkpoints down to `_LIVE_KEEP` (compaction boundaries always kept).

    Replaces the removed agent-side idle trim: bounds every thread regardless
    of whether the agent ever idles. Runs on the daemon's fast loop.
    """
    counts = _thread_counts(pool)
    stale = set(_stale_agents(pool, counts))
    candidates = [
        aid
        for aid in _overgrown_active(pool, counts)
        if aid not in stale and _still_overgrown(pool, aid, _LIVE_TRIM_THRESHOLD)
    ]
    return _reap(pool, candidates, _LIVE_KEEP, "overgrown")
