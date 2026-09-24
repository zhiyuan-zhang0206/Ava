"""Trim accumulated LangGraph checkpoints for one thread to bound storage.

The graph runs on langgraph's default durability ("async" — the agent's
`graph.ainvoke` never passes `durability="exit"`; per-node checkpointing is a
deliberate design, see decisions/2026-05-02-self-cycling-langgraph.md), so
the PostgresSaver writes one checkpoint row per super-step (node boundary; a
turn spans several). Over a long-running agent these accumulate without bound
across `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`. `trim_checkpoints`
deletes a thread's older checkpoints, keeping the latest K, and cascades in one
atomic statement to the writes those checkpoints owned and to any blob no
surviving checkpoint references.

Compaction boundaries are never trimmed: a checkpoint whose metadata carries
`compact_boundary: true` (stamped by `mark_compact_boundary` when a compaction
replaces the pre-compact history) is kept regardless of age, so each past
compaction segment stays recoverable as one full snapshot — the user's
"preserve information, bound storage" retention rule (Task #1125).

Safety invariant — version-4 full snapshots only; other formats are exempt:
    This naive "keep the latest K, drop the rest" trim is correct ONLY because
    legacy Ava state channels store self-contained full values in their blobs.
    The pre-cutover `messages` channel is a plain accumulator, so each legacy
    checkpoint's blob holds the entire message list, not a delta.
    LangGraph's beta `DeltaChannel` instead stores deltas and reconstructs by
    walking the parent chain back to a snapshot ancestor; dropping intermediate
    checkpoints would silently sever that chain (the channel reconstructs as
    empty, with no error raised).

    `_TRIM_SQL` therefore refuses every delta-written thread: the `delta_thread`
    CTE matches either delta replay counters in checkpoint metadata or an
    EXT-7 (`_DeltaSnapshot`) messages blob, and a match parks the whole trim
    (`ready = false`). Detection is shape-based on purpose — blob absence
    alone does NOT identify a delta thread, because at a snapshot step the
    blob exists; that is exactly the hole review #6143 (x2b) found. On any
    doubt the bias is always "park": a false positive costs a no-op, a false
    negative deletes the chain.

    Older checkpoint formats also recover pending sends from their parent's
    writes. They cannot use this full-snapshot pruning path. Surviving v4
    checkpoints retain their own pending writes and reconnect to their nearest
    surviving ancestor, so a later fork still copies the retained segments.

Blob retention is keyed on the (channel, version) pairs a surviving checkpoint's
`channel_versions` still references — NOT on which (deleted) checkpoint wrote the
blob. An unchanged channel keeps the same blob version across many turns, so the
latest checkpoint can reference a blob first written long ago; deleting "old"
blobs by checkpoint age would corrupt it. Deleting only blobs unreferenced by
any survivor is the correct rule.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import NamedTuple

from psycopg_pool import AsyncConnectionPool, ConnectionPool

from shared import telemetry
from shared.config import settings
from shared.db_transaction import async_write_transaction, write_transaction
from shared.log import logger


class TrimCounts(NamedTuple):
    """Rows deleted by one `trim_checkpoints` call, per table."""

    checkpoints: int
    writes: int
    blobs: int


# One statement, one snapshot. PostgreSQL runs every CTE — including the
# data-modifying ones — against the snapshot taken at statement start, and a
# data-modifying CTE's effects are invisible to the other CTEs. So `ranked`
# (and the survivor / blob-retention sets derived from it) read the pre-deletion
# rows regardless of the three DELETEs in the same statement. A single SQL
# statement is its own transaction (the pool runs autocommit), so the deletes
# across checkpoints / writes / blobs commit atomically or not at all.
#
# Ordering: ROW_NUMBER() OVER (ORDER BY checkpoint_id DESC) ranks newest first,
# matching how the PostgresSaver itself selects the latest checkpoint (UUIDv6
# ids sort lexicographically by time). rn <= keep survives; rn > keep is doomed.
#
# Race guard: PostgresSaver may expose a new messages blob before its checkpoint
# row. Trimming in that window could sweep the in-flight blob, or could delete
# old rows while the current newest checkpoint still lacks its referenced blob.
# A trim therefore proceeds only when the newest checkpoint's messages version
# is committed and no higher messages counter is visible. A missing messages
# version means that checkpoint has nothing to await.
_TRIM_SQL = """
WITH RECURSIVE ranked AS (
    SELECT checkpoint_id, parent_checkpoint_id, checkpoint, metadata,
           ROW_NUMBER() OVER (ORDER BY checkpoint_id DESC) AS rn
    FROM checkpoints
    WHERE thread_id = %(thread_id)s AND checkpoint_ns = %(ns)s
),
newest_messages AS (
    SELECT checkpoint -> 'channel_versions' ->> 'messages' AS version
    FROM ranked
    WHERE rn = 1
),
delta_thread AS (
    -- Explicit delta-written-thread exemption (never-delete ruling, task
    -- #3180; review #6143 I1/x2b). Shape-based on purpose: at a snapshot step
    -- the newest messages version DOES have a blob (an EXT-7 _DeltaSnapshot),
    -- so blob absence alone cannot carry the exemption. Both markers below
    -- are written only by delta-capable runtimes; on any doubt park the trim.
    SELECT (
        EXISTS (
            SELECT 1 FROM ranked r
            WHERE r.metadata ? 'counters_since_delta_snapshot'
        )
        OR EXISTS (
            SELECT 1 FROM checkpoint_blobs b
            WHERE b.thread_id = %(thread_id)s
              AND b.checkpoint_ns = %(ns)s
              AND b.channel = 'messages'
              AND (
                  -- msgpack EXT-7 framing: fixext1..16 start with the type
                  -- byte right after the marker (xx 07); ext8/ext16/ext32
                  -- carry it after 1/2/4 length bytes (c7/c8/c9).
                  substring(b.blob FROM 1 FOR 2) IN (
                      '\\xd407'::bytea, '\\xd507'::bytea, '\\xd607'::bytea,
                      '\\xd707'::bytea, '\\xd807'::bytea
                  )
                  OR (substring(b.blob FROM 1 FOR 1) = '\\xc7'::bytea
                      AND substring(b.blob FROM 3 FOR 1) = '\\x07'::bytea)
                  OR (substring(b.blob FROM 1 FOR 1) = '\\xc8'::bytea
                      AND substring(b.blob FROM 4 FOR 1) = '\\x07'::bytea)
                  OR (substring(b.blob FROM 1 FOR 1) = '\\xc9'::bytea
                      AND substring(b.blob FROM 6 FOR 1) = '\\x07'::bytea)
              )
        )
    ) AS is_delta
),
trim_guard AS (
    SELECT (
        NOT (SELECT is_delta FROM delta_thread)
        AND NOT EXISTS (
            SELECT 1 FROM ranked
            WHERE checkpoint ->> 'v' IS DISTINCT FROM '4'
               OR parent_checkpoint_id >= checkpoint_id
        )
        AND (
            newest.version IS NULL
            OR (
                EXISTS (
                    SELECT 1
                    FROM checkpoint_blobs b
                    WHERE b.thread_id = %(thread_id)s
                      AND b.checkpoint_ns = %(ns)s
                      AND b.channel = 'messages'
                      AND b.version = newest.version
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM checkpoint_blobs b
                    WHERE b.thread_id = %(thread_id)s
                      AND b.checkpoint_ns = %(ns)s
                      AND b.channel = 'messages'
                      AND split_part(b.version, '.', 1)::bigint
                          > split_part(newest.version, '.', 1)::bigint
                )
            )
        )
    ) AS ready
    FROM newest_messages newest
),
doomed AS (
    -- Oldest-first, capped at %(batch)s per statement: a thread can hold
    -- thousands of doomed checkpoints, and one unbounded DELETE is a long
    -- transaction that blocks nothing (row locks only) but delays VACUUM
    -- bloat reclamation and holds the reaper's connection. The caller loops
    -- the statement until a short batch comes back (see trim_checkpoints).
    SELECT checkpoint_id, parent_checkpoint_id FROM ranked
    WHERE rn > %(keep)s
      AND COALESCE((SELECT ready FROM trim_guard), false)
      AND NOT COALESCE((metadata ->> 'compact_boundary')::boolean, false)
      AND checkpoint_id <> ALL(%(preserve)s)
    ORDER BY checkpoint_id ASC
    LIMIT %(batch)s
),
survivors AS (
    -- Retain this batch's survivors, including rows a LATER batch may delete.
    -- A crash between batches must not leave checkpoints without their blobs.
    SELECT r.* FROM ranked r
    WHERE NOT EXISTS (SELECT 1 FROM doomed d WHERE d.checkpoint_id = r.checkpoint_id)
),
kept_blob_refs AS (
    SELECT DISTINCT cv.key AS channel, cv.value AS version
    FROM survivors, jsonb_each_text(survivors.checkpoint -> 'channel_versions') AS cv
),
bypassed_parents AS (
    SELECT s.checkpoint_id, s.parent_checkpoint_id FROM survivors s
    WHERE EXISTS (SELECT 1 FROM doomed d WHERE d.checkpoint_id = s.parent_checkpoint_id)
    UNION ALL
    SELECT p.checkpoint_id, d.parent_checkpoint_id
    FROM bypassed_parents p JOIN doomed d ON d.checkpoint_id = p.parent_checkpoint_id
),
relinked AS (
    UPDATE checkpoints c SET parent_checkpoint_id = p.parent_checkpoint_id
    FROM bypassed_parents p
    WHERE c.thread_id = %(thread_id)s AND c.checkpoint_ns = %(ns)s
      AND c.checkpoint_id = p.checkpoint_id
      AND NOT EXISTS (
          SELECT 1 FROM doomed d WHERE d.checkpoint_id = p.parent_checkpoint_id
      )
    RETURNING 1
),
del_checkpoints AS (
    DELETE FROM checkpoints
    WHERE thread_id = %(thread_id)s AND checkpoint_ns = %(ns)s
      AND checkpoint_id IN (SELECT checkpoint_id FROM doomed)
    RETURNING 1
),
del_writes AS (
    DELETE FROM checkpoint_writes
    WHERE thread_id = %(thread_id)s AND checkpoint_ns = %(ns)s
      AND checkpoint_id IN (SELECT checkpoint_id FROM doomed)
    RETURNING 1
),
del_blobs AS (
    DELETE FROM checkpoint_blobs b
    WHERE b.thread_id = %(thread_id)s AND b.checkpoint_ns = %(ns)s
      AND COALESCE((SELECT ready FROM trim_guard), false)
      AND NOT EXISTS (
          SELECT 1 FROM kept_blob_refs k
          WHERE k.channel = b.channel AND k.version = b.version
      )
    RETURNING 1
)
SELECT
    (SELECT count(*) FROM del_checkpoints),
    (SELECT count(*) FROM del_writes),
    (SELECT count(*) FROM del_blobs)
"""


# Stamp the thread's newest checkpoint as a compaction boundary; the RETURNING
# names exactly the stamped boundary so the caller can enqueue the worker's
# build job for it (task #4674).
_MARK_BOUNDARY_SQL = (
    "UPDATE checkpoints SET metadata = metadata || jsonb_build_object('compact_boundary', true)"
    " WHERE thread_id = %s AND checkpoint_ns = %s"
    "   AND checkpoint_id = ("
    "       SELECT checkpoint_id FROM checkpoints"
    "       WHERE thread_id = %s AND checkpoint_ns = %s"
    "       ORDER BY checkpoint_id DESC LIMIT 1)"
    " RETURNING checkpoint_id"
)

# The worker's build-job insert (task #4674): one pending compact job per new
# boundary, deduped by the live partial unique index (one pending/running job
# per agent and kind); `include_tail` is decided at claim time by the runner.
_ENQUEUE_COMPACT_JOB_SQL = (
    "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
    " VALUES (%s, 'compact', %s, 'pending', false)"
    " ON CONFLICT (agent_id, kind) WHERE status IN ('pending', 'running') DO NOTHING"
)


def _compact_agent_id(thread_id: str) -> int | None:
    """The agent id a compact-boundary thread denotes; None for foreign threads.

    Agent threads are `str(agent.id)`; a thread that is not plain ASCII digits
    belongs to foreign tooling and must not enqueue build jobs (the same rule
    the worker's scan applies in SQL).
    """
    return int(thread_id) if thread_id.isascii() and thread_id.isdigit() else None


def _enqueue_failed(agent_id: int, exc: Exception) -> None:
    """The enqueue-failure observability half — itself best-effort, never raises."""
    try:
        telemetry.emit(
            "telemetry",
            "hierarchy_enqueue_failed",
            attributes={"agent_id": agent_id, "error": f"{type(exc).__name__}: {exc}"},
        )
    except Exception:
        logger.warning("hierarchy enqueue-failure event could not be emitted", exc_info=True)


async def mark_compact_boundary(
    pool: AsyncConnectionPool,
    thread_id: str,
    *,
    checkpoint_ns: str = "",
) -> None:
    """Stamp the thread's newest checkpoint as a compaction boundary (idempotent).

    A compaction freezes the pre-compact history into a summary; the newest
    pre-compact checkpoint is the full-snapshot record of that segment. Stamping
    it makes every later trim keep it (see `_TRIM_SQL`), so each past compaction
    segment stays recoverable — the user's retention rule (Task #1125). Called by
    the agent-side compact paths right before the keep=1 trim; failure-tolerant
    callers, since a missed stamp only loses segment traceability, never
    recoverability (the summary survives regardless).

    It then best-effort enqueues the worker's build job for that boundary
    (task #4674): a missed enqueue degrades to the worker's 10-minute
    reconcile scan, never to a lost compact round.
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(_MARK_BOUNDARY_SQL, (thread_id, checkpoint_ns, thread_id, checkpoint_ns))
        row = await cur.fetchone()
    await _enqueue_compact_job(pool, thread_id, row[0] if row is not None else None)


async def _enqueue_compact_job(
    pool: AsyncConnectionPool, thread_id: str, boundary: str | None
) -> None:
    """Best-effort build-job enqueue for a fresh compact boundary (task #4674).

    Never raises and never blocks the compact round: any failure is a warning
    plus the `hierarchy_enqueue_failed` event. Idempotent via the live partial
    unique index; a pending job superseded by a newer boundary is merged by
    the run's own advance target.
    """
    agent_id = _compact_agent_id(thread_id)
    if agent_id is None or boundary is None or not settings.daemon.hierarchy_worker_enabled:
        return
    try:
        async with async_write_transaction(pool) as conn, conn.cursor() as cur:
            await cur.execute(_ENQUEUE_COMPACT_JOB_SQL, (agent_id, boundary))
    except Exception as exc:
        logger.warning(
            "hierarchy enqueue failed for agent {agent} (boundary {boundary}): {error!r}",
            agent=agent_id,
            boundary=boundary,
            error=exc,
        )
        _enqueue_failed(agent_id, exc)


def mark_compact_boundary_sync(
    pool: ConnectionPool,
    thread_id: str,
    *,
    checkpoint_ns: str = "",
) -> None:
    """Synchronous twin of `mark_compact_boundary` (gateway-side maintenance)."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(_MARK_BOUNDARY_SQL, (thread_id, checkpoint_ns, thread_id, checkpoint_ns))
        row = cur.fetchone()
    _enqueue_compact_job_sync(pool, thread_id, row[0] if row is not None else None)


def _enqueue_compact_job_sync(pool: ConnectionPool, thread_id: str, boundary: str | None) -> None:
    """Best-effort twin of `_enqueue_compact_job` over the sync pool."""
    agent_id = _compact_agent_id(thread_id)
    if agent_id is None or boundary is None or not settings.daemon.hierarchy_worker_enabled:
        return
    try:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(_ENQUEUE_COMPACT_JOB_SQL, (agent_id, boundary))
    except Exception as exc:
        logger.warning(
            "hierarchy enqueue failed for agent {agent} (boundary {boundary}): {error!r}",
            agent=agent_id,
            boundary=boundary,
            error=exc,
        )
        _enqueue_failed(agent_id, exc)


async def count_checkpoints(
    pool: AsyncConnectionPool,
    thread_id: str,
    *,
    checkpoint_ns: str = "",
) -> int:
    """Number of checkpoint rows stored for one thread / namespace."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s",
            (thread_id, checkpoint_ns),
        )
        row = await cur.fetchone()
    assert row is not None, "count(*) always returns one row"  # noqa: S101
    return row[0]


_TRIM_BATCH = 200
"""Max doomed checkpoints deleted per `_TRIM_SQL` statement.

Bound the per-statement delete (and its write/blob cascade) so a thread with
thousands of old checkpoints is trimmed in short transactions rather than one
long one. Large single DELETE statements are not lock-heavy (Postgres row locks
only), but they hold the reaper's connection for the whole cascade and delay
the dead tuples' VACUUM reclamation that the maintenance daemon's
`blob_vacuum` schedules — small statements keep both bounded."""

_TRIM_MAX_ROUNDS = 50
"""Safety cap on `_TRIM_SQL` batches per call (50 * 200 = 10k checkpoints).

A real thread never hits this (rn is a fixed ranking, so each batch shrinks
the doomed set), but a corrupted ranking must not let the reaper spin."""


async def trim_checkpoints(
    pool: AsyncConnectionPool,
    thread_id: str,
    *,
    keep: int = 5,
    checkpoint_ns: str = "",
    batch: int = _TRIM_BATCH,
    preserve_checkpoint_ids: Collection[str] = (),
) -> TrimCounts:
    """Delete a thread's older checkpoints, keeping the latest `keep`.

    Cascades in short atomic statements (`batch` doomed checkpoints each): the
    dropped checkpoints' writes go with them, and any blob no surviving
    checkpoint references is removed. The latest checkpoint is always kept
    (keep >= 1), so the next resume reads intact state. Compaction boundaries
    are exempt from every trim — a stamped segment anchor is never deleted.
    `preserve_checkpoint_ids` additionally retains operator-verified segment
    endpoints when the original best-effort boundary stamp was absent.

    Trimming is a legacy operation: the cluster default parks it (never-delete
    ruling, 2026-09-12, task #3180), and a delta-written thread is refused
    outright by the `delta_thread` exemption in `_TRIM_SQL` (delta replay
    counters in metadata or an EXT-7 `_DeltaSnapshot` blob). The exemption is
    explicit because the blob-absence accident does not hold at snapshot
    steps, where the trim guard alone would proceed (review #6143 x2b).

    Returns the per-table delete counts.
    """
    assert keep >= 1, f"keep must be >= 1 to preserve the latest checkpoint, got {keep}"  # noqa: S101
    total = [0, 0, 0]
    for _ in range(_TRIM_MAX_ROUNDS):
        async with async_write_transaction(pool) as conn, conn.cursor() as cur:
            await cur.execute(
                _TRIM_SQL,
                {
                    "thread_id": thread_id,
                    "ns": checkpoint_ns,
                    "keep": keep,
                    "batch": batch,
                    "preserve": list(preserve_checkpoint_ids),
                },
            )
            row = await cur.fetchone()
        assert row is not None, "the trailing SELECT always returns one row"  # noqa: S101
        total[0] += row[0]
        total[1] += row[1]
        total[2] += row[2]
        if row[0] < batch:
            break
    counts = TrimCounts(checkpoints=total[0], writes=total[1], blobs=total[2])
    logger.info(
        "[{label}] {body}",
        label="checkpoint-trim",
        event="checkpoint_trim",
        body=(
            f"thread={thread_id} ns={checkpoint_ns!r} keep={keep}: deleted"
            f" checkpoints={counts.checkpoints} writes={counts.writes} blobs={counts.blobs}"
        ),
    )
    return counts


def trim_checkpoints_sync(
    pool: ConnectionPool,
    thread_id: str,
    *,
    keep: int = 5,
    checkpoint_ns: str = "",
    batch: int = _TRIM_BATCH,
    preserve_checkpoint_ids: Collection[str] = (),
) -> TrimCounts:
    """Synchronous twin of `trim_checkpoints` for gateway-side maintenance
    loops (the events-maintenance daemon's checkpoint reaper runs on a sync
    `psycopg_pool.ConnectionPool`; the agent-side saver keeps the async one).

    Same batched atomic trim: keep the latest `keep` checkpoints of one thread,
    cascade the writes those checkpoints owned, and delete any blob no
    surviving checkpoint references — in `batch`-sized statements, looping until
    a short batch comes back (see `trim_checkpoints`).
    """
    assert keep >= 1, f"keep must be >= 1 to preserve the latest checkpoint, got {keep}"  # noqa: S101
    total = [0, 0, 0]
    for _ in range(_TRIM_MAX_ROUNDS):
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                _TRIM_SQL,
                {
                    "thread_id": thread_id,
                    "ns": checkpoint_ns,
                    "keep": keep,
                    "batch": batch,
                    "preserve": list(preserve_checkpoint_ids),
                },
            )
            row = cur.fetchone()
        assert row is not None, "the trailing SELECT always returns one row"  # noqa: S101
        total[0] += row[0]
        total[1] += row[1]
        total[2] += row[2]
        if row[0] < batch:
            break
    counts = TrimCounts(checkpoints=total[0], writes=total[1], blobs=total[2])
    logger.info(
        "[{label}] {body}",
        label="checkpoint-trim",
        event="checkpoint_trim",
        body=(
            f"thread={thread_id} ns={checkpoint_ns!r} keep={keep}: deleted"
            f" checkpoints={counts.checkpoints} writes={counts.writes} blobs={counts.blobs}"
        ),
    )
    return counts
