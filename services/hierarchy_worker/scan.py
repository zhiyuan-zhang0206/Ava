"""One scan pass of the hierarchy worker — boundaries in, jobs out.

The pass reads every thread's newest compact boundary in one aggregated
query, baselines agents it has never seen (the silent baseline: no build for
pre-existing history — the worker only follows new compactions), and enqueues
a build job wherever an agent's last fully-covered boundary is behind its
newest one, or its last attempt was not clean. Enqueueing is idempotent and
race-free by construction: the partial unique index allows one live
(pending/running) job per (agent, kind) and the insert is ON CONFLICT DO
NOTHING.

Retry gating lives here too. A clean continuation (done, nothing failed, a
budget left work over) is due immediately — a drain must not stall; anything
else that ended not-clean waits out an exponential backoff derived from the
trailing non-clean streak, so a deterministic failure retries about once a
day instead of hot-looping, and the next compact's job supersedes it anyway.
Stale `running` rows (a dead worker's leftover) are parked to `failed` once
they outlive their own hard deadline, which puts them on the same backoff
path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from psycopg import Connection

from shared.config import settings
from shared.log import logger

# The only job kind today; P2c extends the CHECK constraint and this module
# (day-boundary backstop, on-demand) together.
KIND_COMPACT = "compact"

# The newest compact boundary per thread. Ordering assumption: checkpoint ids
# are UUIDv6, so lexicographic order is time order — the same assumption the
# checkpoint trim SQL and the boundary index make. Non-numeric thread ids
# (foreign tooling's threads) are not agents and are skipped.
_LATEST_BOUNDARY_SQL = """
SELECT thread_id, max(checkpoint_id) AS latest
FROM checkpoints
WHERE metadata->>'compact_boundary' = 'true' AND thread_id ~ '^[0-9]+$'
GROUP BY thread_id
"""

# The trailing attempts the backoff streak counts; beyond this the delay is
# capped anyway, so a bounded read is enough.
_STREAK_WINDOW = 64


@dataclass(frozen=True)
class ScanOutcome:
    """One pass's effect, for logs and tests."""

    agents_tracked: int = 0
    baselined: int = 0
    enqueued: int = 0
    stale_recovered: int = 0


@dataclass(frozen=True)
class _LastJob:
    status: str
    failed: int
    skipped: int
    finished_at: datetime


def _clean(last: _LastJob) -> bool:
    """A fully successful attempt: nothing failed and nothing was left over."""
    return last.status == "done" and last.failed == 0 and last.skipped == 0


def _pure_continuation(last: _LastJob) -> bool:
    """A budget-truncated attempt with no failures: the drain continues it."""
    return last.status == "done" and last.failed == 0 and last.skipped > 0


def scan(conn: Connection) -> ScanOutcome:
    """Run one scan pass on an autocommit connection; return the pass's effect."""
    stale_recovered = _recover_stale(conn)
    boundaries: dict[int, str] = {
        int(thread_id): str(latest)
        for thread_id, latest in conn.execute(_LATEST_BOUNDARY_SQL).fetchall()
    }
    state: dict[int, str] = {
        int(agent_id): str(boundary)
        for agent_id, boundary in conn.execute(
            "SELECT agent_id, last_processed_boundary FROM hierarchy_worker_state"
        ).fetchall()
    }
    baselined = _baseline_new(conn, boundaries, state)
    enqueued = 0
    for agent_id, latest in sorted(boundaries.items()):
        last_processed = state.get(agent_id)
        if last_processed is None:
            # Baselined in this pass: the boundary itself is the baseline.
            continue
        enqueued += _consider(conn, agent_id, latest, last_processed)
    return ScanOutcome(
        agents_tracked=len(state) + baselined,
        baselined=baselined,
        enqueued=enqueued,
        stale_recovered=stale_recovered,
    )


def _recover_stale(conn: Connection) -> int:
    """Park `running` rows older than a job could legitimately live."""
    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.daemon.hierarchy_job_deadline_seconds
        + settings.daemon.hierarchy_stale_grace_seconds
    )
    cursor = conn.execute(
        "UPDATE hierarchy_jobs"
        " SET status = 'failed', finished_at = now(),"
        "     error = coalesce(error, 'worker process died mid-job (stale running row)')"
        " WHERE status = 'running' AND coalesce(started_at, created_at) < %s",
        (cutoff,),
    )
    return cursor.rowcount


def _baseline_new(conn: Connection, boundaries: dict[int, str], state: dict[int, str]) -> int:
    """Insert the silent baseline for agents seen for the first time."""
    new = [
        (agent_id, boundaries[agent_id]) for agent_id in sorted(boundaries) if agent_id not in state
    ]
    if not new:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO hierarchy_worker_state (agent_id, last_processed_boundary)"
            " VALUES (%s, %s)"
            " ON CONFLICT (agent_id) DO NOTHING",
            new,
        )
    logger.info(
        "hierarchy scan: baselined {count} agent(s) silently (no build for pre-existing history)",
        count=len(new),
    )
    return len(new)


def _consider(conn: Connection, agent_id: int, latest: str, last_processed: str) -> int:
    """Decide one agent's due-ness; enqueue when due. Returns 1 on enqueue."""
    if (
        conn.execute(
            "SELECT 1 FROM hierarchy_jobs"
            " WHERE agent_id = %s AND status IN ('pending', 'running') LIMIT 1",
            (agent_id,),
        ).fetchone()
        is not None
    ):
        return 0
    last = _last_finished(conn, agent_id)
    behind = latest > last_processed
    if not (behind or (last is not None and not _clean(last))):
        return 0
    # `last is not None` is repeated so the type narrows through the gates.
    if last is not None and not _clean(last) and not _pure_continuation(last):
        streak = _nonclean_streak(conn, agent_id)
        delay_s = min(
            settings.daemon.hierarchy_retry_backoff_seconds * (2 ** (streak - 1)),
            settings.daemon.hierarchy_retry_backoff_cap_seconds,
        )
        if datetime.now(UTC) - last.finished_at < timedelta(seconds=delay_s):
            return 0
    include_tail = _first_build(conn, agent_id)
    cursor = conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
        " VALUES (%s, 'compact', %s, 'pending', %s)"
        " ON CONFLICT (agent_id, kind) WHERE status IN ('pending', 'running') DO NOTHING",
        (agent_id, latest, include_tail),
    )
    if cursor.rowcount:
        logger.info(
            "hierarchy scan: enqueued job for agent {agent} (boundary {boundary}, first_build={first})",
            agent=agent_id,
            boundary=latest,
            first=include_tail,
        )
    return cursor.rowcount


def _last_finished(conn: Connection, agent_id: int) -> _LastJob | None:
    row = conn.execute(
        "SELECT status, coalesce(failed, 0), coalesce(skipped, 0),"
        "       coalesce(finished_at, started_at, created_at)"
        " FROM hierarchy_jobs WHERE agent_id = %s AND status IN ('done', 'failed')"
        " ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    if row is None:
        return None
    return _LastJob(status=str(row[0]), failed=int(row[1]), skipped=int(row[2]), finished_at=row[3])


def _nonclean_streak(conn: Connection, agent_id: int) -> int:
    """Consecutive not-clean attempts at the head of this agent's history."""
    rows = conn.execute(
        "SELECT status, coalesce(failed, 0), coalesce(skipped, 0)"
        " FROM hierarchy_jobs WHERE agent_id = %s AND status IN ('done', 'failed')"
        " ORDER BY id DESC LIMIT %s",
        (agent_id, _STREAK_WINDOW),
    ).fetchall()
    streak = 0
    for status, failed, skipped in rows:
        if status == "done" and int(failed) == 0 and int(skipped) == 0:
            break
        streak += 1
    return max(streak, 1)


def _first_build(conn: Connection, agent_id: int) -> bool:
    """Whether this agent has never had a fully successful build.

    The first build is the full-retention-window one (review 3187: one-time
    and bounded), sealed through the tail — the same semantics as the manual
    first run; every later compact-driven pass leaves the tail pending.
    """
    row = conn.execute(
        "SELECT NOT EXISTS ("
        "  SELECT 1 FROM hierarchy_jobs"
        "  WHERE agent_id = %s AND status = 'done'"
        "    AND coalesce(failed, 0) = 0 AND coalesce(skipped, 0) = 0"
        ")",
        (agent_id,),
    ).fetchone()
    assert row is not None  # noqa: S101 — a scalar SELECT always yields a row
    return bool(row[0])
