"""One scan pass of the hierarchy worker — boundaries in, jobs out.

The pass reads every thread's newest compact boundary in one aggregated
query, baselines agents it has never seen (the silent baseline: no build for
pre-existing history — the worker only follows new compactions), and enqueues
a build job wherever an agent's last fully-covered boundary is behind its
newest one, or its last attempt was not clean. A second channel — the tail
seal (task #3981 C, off unless `hierarchy_tail_seal_enabled`) — enqueues a
`tail` job when an established agent has gone idle with unsealed trailing
activity. Enqueueing is idempotent and race-free by construction: the partial
unique index allows one live (pending/running) job per (agent, kind) and the
insert is ON CONFLICT DO NOTHING.

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

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from psycopg import Connection

from shared.config import settings
from shared.log import logger

# The job kinds: 'compact' seals the batches a new compaction boundary
# closed; 'tail' (task #3981 C) seals an idle agent's trailing stretch. P2c
# extends the CHECK constraint and this module (day-boundary backstop,
# on-demand) together.
KIND_COMPACT = "compact"
KIND_TAIL = "tail"

# The marker a first-sight retirement writes onto the retired job row (task
# #4674): a `done` row carrying `error` is never a build (`first_build` /
# `_has_clean_baseline` guard on it), so pre-existing history stays unbuilt.
# Both first-sight paths write it — the scan's baseline pass and the
# claim-side fallback (`runner._baseline_untracked`).
SILENT_BASELINE_MARKER = "silent baseline: pre-existing history is not built (task #3704)"

# 100ns ticks between the UUID epoch (1582-10-15) and the Unix epoch — the
# fixed offset decoding a UUIDv6 timestamp back to wall time.
_UUID_V6_EPOCH_TICKS = 0x1B21DD213814000

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

# The newest checkpoint per agent (any namespace) — the tail channel's idle
# and delta source. Same UUIDv6 ordering assumption as the boundary query.
_LATEST_CHECKPOINT_SQL = """
SELECT thread_id, max(checkpoint_id) AS latest
FROM checkpoints
WHERE thread_id ~ '^[0-9]+$'
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
    tail_enqueued: int = 0
    stale_recovered: int = 0


@dataclass(frozen=True)
class _LastJob:
    status: str
    failed: int
    skipped: int
    finished_at: datetime
    # The guardrail marker (task #4674): a `done` row's error text means the
    # attempt was cut (regen halt) or was a claim-time silent baseline — never
    # a plain budget truncation, so it must not take the continuation fast path.
    error: str | None = None


def _clean(last: _LastJob) -> bool:
    """A fully successful attempt: nothing failed and nothing was left over."""
    return last.status == "done" and last.failed == 0 and last.skipped == 0


def _pure_continuation(last: _LastJob) -> bool:
    """A budget-truncated attempt with no failures: the drain continues it.

    A `done` row carrying `error` is a guardrail cut (regen halt), not a
    plain truncation — it waits out the backoff like any non-clean attempt
    (the halt's contract: a runaway wave must not hot-loop, task #4674).
    """
    return last.status == "done" and last.failed == 0 and last.skipped > 0 and last.error is None


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
    tail_enqueued = _scan_tails(conn, state) if settings.daemon.hierarchy_tail_seal_enabled else 0
    return ScanOutcome(
        agents_tracked=len(state) + baselined,
        baselined=baselined,
        enqueued=enqueued,
        tail_enqueued=tail_enqueued,
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
    """Insert the silent baseline for agents seen for the first time.

    The same pass retires a first-sight agent's live pending job with the
    silent-baseline marker (task #4674, review #3242 F1): the event trigger
    may have enqueued one for this very boundary, and a claim running after
    this scan — with the agent now tracked — would otherwise treat it as a
    first build and materialize pre-existing history. Both orderings now
    decide the same: nothing builds on first sight, whichever path retires
    the job first (the claim's own fallback covers the window before this
    scan has run).
    """
    new = [
        (agent_id, boundaries[agent_id]) for agent_id in sorted(boundaries) if agent_id not in state
    ]
    if not new:
        return 0
    retired = 0
    for agent_id, boundary in new:
        # One transaction per agent: a crash between the two writes must not
        # leave a tracked agent with a live first-sight job — the very
        # ordering this retirement exists to prevent.
        with conn.transaction():
            conn.execute(
                "INSERT INTO hierarchy_worker_state (agent_id, last_processed_boundary)"
                " VALUES (%s, %s)"
                " ON CONFLICT (agent_id) DO NOTHING",
                (agent_id, boundary),
            )
            retired += conn.execute(
                "UPDATE hierarchy_jobs SET status = 'done', finished_at = now(), error = %s"
                " WHERE agent_id = %s AND kind = %s AND status = 'pending'",
                (SILENT_BASELINE_MARKER, agent_id, KIND_COMPACT),
            ).rowcount
    logger.info(
        "hierarchy scan: baselined {count} agent(s) silently (no build for"
        " pre-existing history); {retired} first-sight job(s) retired",
        count=len(new),
        retired=retired,
    )
    return len(new)


def _consider(conn: Connection, agent_id: int, latest: str, last_processed: str) -> int:
    """Decide one agent's compact due-ness; enqueue when due. Returns 1 on enqueue."""
    if _live_job(conn, agent_id):
        return 0
    last = _last_finished(conn, agent_id, KIND_COMPACT)
    behind = latest > last_processed
    if not (behind or (last is not None and not _clean(last))):
        return 0
    # `last is not None` is repeated so the type narrows through the gates.
    if last is not None and not _clean(last) and not _pure_continuation(last):
        streak = _nonclean_streak(conn, agent_id, KIND_COMPACT)
        delay_s = min(
            settings.daemon.hierarchy_retry_backoff_seconds * (2 ** (streak - 1)),
            settings.daemon.hierarchy_retry_backoff_cap_seconds,
        )
        if datetime.now(UTC) - last.finished_at < timedelta(seconds=delay_s):
            return 0
    include_tail = first_build(conn, agent_id)
    cursor = conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
        " VALUES (%s, %s, %s, 'pending', %s)"
        " ON CONFLICT (agent_id, kind) WHERE status IN ('pending', 'running') DO NOTHING",
        (agent_id, KIND_COMPACT, latest, include_tail),
    )
    if cursor.rowcount:
        logger.info(
            "hierarchy scan: enqueued job for agent {agent} (boundary {boundary}, first_build={first})",
            agent=agent_id,
            boundary=latest,
            first=include_tail,
        )
    return cursor.rowcount


def _scan_tails(conn: Connection, state: dict[int, str]) -> int:
    """Enqueue tail-seal jobs for idle agents (task #3981 C). Returns the count.

    Four gates per agent — idle (its newest checkpoint has been quiet for the
    idle window), delta (that checkpoint is newer than the last tail seal),
    pacing (interval / backoff / continuation, mirroring the compact path),
    and a per-tick cap — plus the precondition that the agent has an
    established baseline (a clean non-tail build): the tail channel continues
    coverage that exists, it never initializes one.
    """
    latest = {
        int(thread_id): str(checkpoint_id)
        for thread_id, checkpoint_id in conn.execute(_LATEST_CHECKPOINT_SQL).fetchall()
    }
    if not latest:
        return 0
    seals = {
        int(agent_id): str(seal) if seal is not None else None
        for agent_id, seal in conn.execute(
            "SELECT agent_id, last_tail_seal_cp_id FROM hierarchy_worker_state"
        ).fetchall()
    }
    now = datetime.now(UTC)
    idle_window = timedelta(minutes=settings.daemon.hierarchy_tail_idle_minutes)
    enqueued = 0
    for agent_id in sorted(latest):
        if enqueued >= settings.daemon.hierarchy_tail_max_per_tick:
            # The cap is per tick; the next pass picks the next agents up.
            break
        if agent_id not in state:
            # Not tracked (or baselined this very pass): no established
            # coverage to continue.
            continue
        newest = latest[agent_id]
        stamp = _checkpoint_time(newest)
        if stamp is None or now - stamp < idle_window:
            continue
        seal = seals.get(agent_id)
        if seal is not None and newest <= seal:
            # The delta gate: nothing written since the last tail seal.
            continue
        if not _has_clean_baseline(conn, agent_id) or _live_job(conn, agent_id):
            continue
        if not _tail_due_by_history(conn, agent_id, now):
            continue
        cursor = conn.execute(
            "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
            " VALUES (%s, %s, %s, 'pending', true)"
            " ON CONFLICT (agent_id, kind) WHERE status IN ('pending', 'running') DO NOTHING",
            (agent_id, KIND_TAIL, newest),
        )
        if cursor.rowcount:
            enqueued += cursor.rowcount
            logger.info(
                "hierarchy scan: enqueued tail job for agent {agent} (checkpoint {checkpoint})",
                agent=agent_id,
                checkpoint=newest,
            )
    return enqueued


def _checkpoint_time(checkpoint_id: str) -> datetime | None:
    """Decode a UUIDv6 checkpoint id's wall time; None when it is not v6.

    The ordering assumption (lexicographic == time order) is the codebase's;
    the idle gate additionally needs the wall time, hence the decode. A value
    that is not a version-6 UUID is skipped, not guessed at.
    """
    try:
        parsed = uuid.UUID(checkpoint_id)
    except ValueError:
        return None
    if parsed.version != 6:
        return None
    time_high = parsed.int >> 96
    time_mid = (parsed.int >> 80) & 0xFFFF
    time_low = (parsed.int >> 64) & 0x0FFF
    ticks = (time_high << 28) | (time_mid << 12) | time_low
    return datetime.fromtimestamp((ticks - _UUID_V6_EPOCH_TICKS) / 10_000_000, tz=UTC)


def _has_clean_baseline(conn: Connection, agent_id: int) -> bool:
    """Whether the agent has a clean non-tail build — the tail precondition.

    The tail channel continues an established baseline; an agent with no
    successful build yet waits for its first compact-driven one (that first
    build already seals its tail). A `done` row carrying `error` is a
    claim-time silent baseline or guardrail cut, never a build (task #4674) —
    sealing a tail over one would fabricate coverage out of nothing.
    """
    row = conn.execute(
        "SELECT EXISTS ("
        "  SELECT 1 FROM hierarchy_jobs"
        "  WHERE agent_id = %s AND kind = %s AND status = 'done'"
        "    AND coalesce(failed, 0) = 0 AND coalesce(skipped, 0) = 0"
        "    AND error IS NULL"
        ")",
        (agent_id, KIND_COMPACT),
    ).fetchone()
    assert row is not None  # noqa: S101 — a scalar SELECT always yields a row
    return bool(row[0])


def _live_job(conn: Connection, agent_id: int) -> bool:
    """Whether a live (pending/running) attempt already exists for the agent."""
    return (
        conn.execute(
            "SELECT 1 FROM hierarchy_jobs"
            " WHERE agent_id = %s AND status IN ('pending', 'running') LIMIT 1",
            (agent_id,),
        ).fetchone()
        is not None
    )


def _tail_due_by_history(conn: Connection, agent_id: int, now: datetime) -> bool:
    """The per-agent tail pacing: interval, backoff, continuation.

    Kind-scoped mirror of the compact channel's retry discipline: a clean
    tail waits out the minimum interval; a non-clean attempt waits out its
    exponential backoff; a pure continuation (done, no failures, skipped
    remainder) drains immediately.
    """
    last = _last_finished(conn, agent_id, KIND_TAIL)
    if last is None:
        return True
    if _clean(last):
        return now - last.finished_at >= timedelta(
            minutes=settings.daemon.hierarchy_tail_min_interval_minutes
        )
    if _pure_continuation(last):
        return True
    streak = _nonclean_streak(conn, agent_id, KIND_TAIL)
    delay_s = min(
        settings.daemon.hierarchy_retry_backoff_seconds * (2 ** (streak - 1)),
        settings.daemon.hierarchy_retry_backoff_cap_seconds,
    )
    return now - last.finished_at >= timedelta(seconds=delay_s)


def _last_finished(conn: Connection, agent_id: int, kind: str) -> _LastJob | None:
    """The agent's last finished attempt of this kind (pacing reads are
    kind-scoped — tail and compact decisions must not perturb each other)."""
    row = conn.execute(
        "SELECT status, coalesce(failed, 0), coalesce(skipped, 0),"
        "       coalesce(finished_at, started_at, created_at), error"
        " FROM hierarchy_jobs WHERE agent_id = %s AND kind = %s AND status IN ('done', 'failed')"
        " ORDER BY id DESC LIMIT 1",
        (agent_id, kind),
    ).fetchone()
    if row is None:
        return None
    return _LastJob(
        status=str(row[0]),
        failed=int(row[1]),
        skipped=int(row[2]),
        finished_at=row[3],
        error=str(row[4]) if row[4] is not None else None,
    )


def _nonclean_streak(conn: Connection, agent_id: int, kind: str) -> int:
    """Consecutive not-clean attempts of this kind at the head of the history."""
    rows = conn.execute(
        "SELECT status, coalesce(failed, 0), coalesce(skipped, 0)"
        " FROM hierarchy_jobs WHERE agent_id = %s AND kind = %s AND status IN ('done', 'failed')"
        " ORDER BY id DESC LIMIT %s",
        (agent_id, kind, _STREAK_WINDOW),
    ).fetchall()
    streak = 0
    for status, failed, skipped in rows:
        if status == "done" and int(failed) == 0 and int(skipped) == 0:
            break
        streak += 1
    return max(streak, 1)


def first_build(conn: Connection, agent_id: int) -> bool:
    """Whether this agent has never had a fully successful build.

    The first build is the full-retention-window one (review 3187: one-time
    and bounded), sealed through the tail — the same semantics as the manual
    first run; every later compact-driven pass leaves the tail pending.
    A `done` row carrying `error` is not a build (claim-time silent baseline
    or guardrail cut, task #4674), so it does not clear the first-build mode.
    """
    row = conn.execute(
        "SELECT NOT EXISTS ("
        "  SELECT 1 FROM hierarchy_jobs"
        "  WHERE agent_id = %s AND status = 'done'"
        "    AND coalesce(failed, 0) = 0 AND coalesce(skipped, 0) = 0"
        "    AND error IS NULL"
        ")",
        (agent_id,),
    ).fetchone()
    assert row is not None  # noqa: S101 — a scalar SELECT always yields a row
    return bool(row[0])
