"""The worker's tick — claim one job, run it; repeat until the queue is dry.

Hosted by the gateway's ScheduleManager through
`schedules/hierarchy-worker-schedule.py`: a built-in schedule whose per-minute
cron slot calls `run_tick()`. One tick drains the queue back-to-back (a
continuation job is due immediately, so a drain never stalls), and the slot
boundary paces only the idle wait. The manager launches the host at boot,
keeps it adopted across gateway restarts, and restarts it with backoff +
breaker if it crashes. Each job runs as a child process, so a big window's
memory and any crash stay contained; the parent's wait is bounded by the job
deadline — a wedged child is killed and its row recovered.

The queue is fed by the event trigger (task #4674): each compact boundary
enqueues its own job, so a tick CONSUMES — the reconcile scan runs only when
`hierarchy_fallback_scan_seconds` has elapsed (first tick after boot
included). The scan stays the safety net for lost events and stranded
retries, never the trigger. Before each claim the fleet's 24h regeneration
budget is checked: crossing it trips the persistent breaker and claiming
stops until an operator resets it (the §4 guardrails, task #4674). First
builds are paced separately: past their own 24h budget the claim parks them
and takes the rest, resuming as the window rolls — no trip.

Every tick reaps the orphan `running` rows whose holder process is gone
(`reap_orphans`, task #4975) — before the scan, so the same tick re-enqueues
those agents immediately: no deadline+grace wait (the scan's stale sweep
stays as the no-restart fallback) and no retry backoff (an interrupted run
is not an attempt outcome). A crash that leaves the child alive is not a
reap: the holder pid is the child's own.

Serial by construction — one child at a time, the cost guardrail pinned in
review (3187). Every DB step is idempotent and race-free (partial unique
index + atomic claim), so even a second worker process could only duplicate
work that is hash-idempotent anyway.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
import psycopg
from psycopg import Connection

from base import telemetry
from base.agents.history.hierarchy.jobs import ORPHAN_MARKER
from base.db import Database
from base.deploy.maintenance import admission
from base.log import logger
from services.hierarchy_worker.config import HierarchyWorkerConfig
from services.hierarchy_worker.scan import KIND_COMPACT, SILENT_BASELINE_MARKER, first_build, scan

# The deployed source root: base/ sits at the repo root in prod and in a
# worktree alike (the c9-daily-report precedent), and the child must import
# from the same checkout this runner executes.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# When the reconcile scan last ran (task #4674 B3). Module-global: the worker
# process is long-lived, so a timestamp is the whole state; None means "not
# yet this process" — the first tick after boot always scans (boot reconcile).
_fallback_scanned_at: datetime | None = None


@dataclass(frozen=True)
class ClaimedJob:
    """One claimed job row (id/agent/tail — the child reads the rest itself)."""

    id: int
    agent_id: int
    include_tail: bool


def _fallback_scan_due(now: datetime, config: HierarchyWorkerConfig) -> bool:
    """Whether the low-frequency reconcile pass is due (task #4674 B3)."""
    if _fallback_scanned_at is None:
        return True
    return now - _fallback_scanned_at >= timedelta(seconds=config.hierarchy_fallback_scan_seconds)


# A finished job row that was a first build is a compact job with `include_tail` (the claim
# backfills the flag for event-enqueued rows, so the stored value is the one the run used) —
# spelled out in the two budget queries below.


def _first_builds_deferred(conn: Connection, config: HierarchyWorkerConfig) -> bool:
    """Whether first builds wait for the next day: the rolling 24h window already holds
    `hierarchy_first_build_daily_budget_nodes` first-build nodes.

    A deferral, not a trip: the claim just skips first-build jobs and takes everything
    else; they resume as earlier jobs age out of the window, with no operator step.
    """
    row = conn.execute(
        "SELECT coalesce(sum(generated), 0) FROM hierarchy_jobs"
        " WHERE finished_at >= now() - interval '24 hours'"
        " AND kind = 'compact' AND include_tail"
    ).fetchone()
    spent = int(row[0]) if row is not None else 0
    return spent >= config.hierarchy_first_build_daily_budget_nodes


def _regen_budget_check(conn: Connection, config: HierarchyWorkerConfig) -> bool:
    """The fleet's 24h rolling regeneration budget (task #4674 §4).

    Returns True when a trip is active — the tick must stop claiming. Crossing
    the budget records the trip on the breaker's edge (readings while an
    unreset trip stands never rewrite it) and emits
    `hierarchy_regen_budget_tripped`; the stop persists until an operator
    resets the row with a note (`UPDATE hierarchy_worker_breaker SET reset_at =
    now(), reset_note = '<who/why>' WHERE id = 1`). After a reset, the first
    reading at or below budget sets `rearmed_at`, and only then — an armed,
    reset row — may a new excursion trip again.
    """
    budget = config.hierarchy_regen_daily_budget_nodes
    row = conn.execute(
        "SELECT coalesce(sum(generated), 0) FROM hierarchy_jobs"
        " WHERE finished_at >= now() - interval '24 hours'"
        " AND NOT (kind = 'compact' AND include_tail)"
    ).fetchone()
    total = int(row[0]) if row is not None else 0
    if total > budget:
        tripped = conn.execute(
            "INSERT INTO hierarchy_worker_breaker (id, tripped_at, tripped_reason)"
            " VALUES (1, now(), %s)"
            " ON CONFLICT (id) DO UPDATE SET tripped_at = now(),"
            " tripped_reason = excluded.tripped_reason,"
            " reset_at = NULL, reset_note = NULL, rearmed_at = NULL"
            " WHERE hierarchy_worker_breaker.rearmed_at IS NOT NULL"
            " RETURNING id",
            (f"24h generated {total} > budget {budget}",),
        ).fetchone()
        if tripped is not None:
            logger.error(
                "hierarchy regen budget tripped: {total} node generations in 24h"
                " exceed the budget of {budget} — claiming stops until an operator"
                " resets hierarchy_worker_breaker (reset_at + reset_note)",
                total=total,
                budget=budget,
            )
            telemetry.emit(
                "telemetry",
                "hierarchy_regen_budget_tripped",
                attributes={"window_nodes": total, "budget_nodes": budget},
            )
    else:
        conn.execute(
            "UPDATE hierarchy_worker_breaker SET rearmed_at = now()"
            " WHERE id = 1 AND reset_at IS NOT NULL AND rearmed_at IS NULL"
        )
    active = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM hierarchy_worker_breaker WHERE id = 1 AND reset_at IS NULL)"
    ).fetchone()
    return bool(active[0]) if active is not None else False


def _tracked(conn: Connection, agent_id: int) -> bool:
    """Whether the scan cursor already knows the agent (the baseline gate)."""
    row = conn.execute(
        "SELECT 1 FROM hierarchy_worker_state WHERE agent_id = %s", (agent_id,)
    ).fetchone()
    return row is not None


def _baseline_untracked(conn: Connection, job_id: int, agent_id: int, boundary: str) -> None:
    """Silent-baseline a never-seen agent and retire the job (task #4674).

    The same semantics the scan applies at first sight (`scan._baseline_new`):
    the worker follows new compactions only, so the boundary becomes the
    baseline and nothing builds for pre-existing history. The job row lands
    `done` with the marker text — a done row carrying `error` is never counted
    as a build (`scan.first_build` / `scan._has_clean_baseline`).
    """
    # One transaction: the state row and the retirement land together — a
    # crash between them would leave a tracked agent whose claimed job the
    # stale sweep later recovers into a retry that builds pre-existing
    # history as a first build (review #3242 nit 4).
    with conn.transaction():
        conn.execute(
            "INSERT INTO hierarchy_worker_state (agent_id, last_processed_boundary)"
            " VALUES (%s, %s) ON CONFLICT (agent_id) DO NOTHING",
            (agent_id, boundary),
        )
        conn.execute(
            "UPDATE hierarchy_jobs SET status = 'done', finished_at = now(), error = %s"
            " WHERE id = %s AND status = 'running'",
            (SILENT_BASELINE_MARKER, job_id),
        )
    logger.info(
        "hierarchy job {job} baselined silently for agent {agent} (boundary {boundary})",
        job=job_id,
        agent=agent_id,
        boundary=boundary,
    )


def _holder_alive(pid: int | None) -> bool:
    """Whether a running row's holder process still exists (task #4975).

    The holder is the job child, which stamps its own pid at boot
    (`execute.execute_job`); a pid recycled by an unrelated process reads
    as alive and the row falls back to the stale sweep, and a zombie is not
    live (the flaky-test checklist's rule) — the child exited, so nobody can
    finish or recover the row. Pids are the gateway host's own: every
    hierarchy job child runs there.
    """
    if pid is None:
        # The child died before registering (or predates the column): no holder.
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True  # exists, just not ours to signal


def reap_orphans(conn: Connection) -> int:
    """Fail every `running` row whose holder process is gone, so its agent can
    rebuild immediately.

    Run at the top of every tick iteration, before the scan and any claim
    (task #4975): `ava stop` kills processes, so a stop-left row's child is
    gone — its pid is dead (or never registered) — while a crash that left the
    child alive is left alone for the child to finish. Each row lands `failed`
    with the orphan marker (`base.agents.history.hierarchy.jobs`): the pacing
    reads skip marker rows, so the re-enqueue neither waits out the job
    deadline + stale grace (the scan's sweep stays the no-restart fallback)
    nor the retry backoff — the run was interrupted, not attempted. Returns
    the number reaped; the `status = 'running'` guard settles races with a
    concurrent recovery.
    """
    dead: list[tuple[int, int, str, int | None, object]] = []
    for job_id, agent_id, kind, pid, started in conn.execute(
        "SELECT id, agent_id, kind, holder_pid, coalesce(started_at, created_at)"
        " FROM hierarchy_jobs WHERE status = 'running'"
    ).fetchall():
        if not _holder_alive(int(pid) if pid is not None else None):
            dead.append((int(job_id), int(agent_id), str(kind), pid, started))
    for job_id, agent_id, kind, pid, started in dead:
        conn.execute(
            "UPDATE hierarchy_jobs SET status = 'failed', finished_at = now(), error = %s"
            " WHERE id = %s AND status = 'running'",
            (ORPHAN_MARKER, job_id),
        )
        logger.warning(
            "hierarchy worker: reaped orphan running job {job} (agent {agent}, kind {kind},"
            " holder pid {pid}, started {started}) — no holder process left to finish it",
            job=job_id,
            agent=agent_id,
            kind=kind,
            pid=pid,
            started=started,
        )
    return len(dead)


def claim_next(
    conn: Connection, *, agents: frozenset[int] = frozenset(), defer_first_builds: bool = False
) -> ClaimedJob | None:
    """Claim the oldest pending job, atomically; never-seen compact jobs are
    silent-baselined instead of built (task #4674) — the scan's first-sight
    pass normally retires those first, so this claim-side branch covers the
    window before a scan has run.

    The event trigger enqueues a compact job for every boundary with
    `include_tail=false` — it cannot know whether the agent is still in
    first-build mode — so the claim re-reads `scan.first_build` and backfills
    the flag; the run side is where the child reads it from. Scan-enqueued
    rows already carry their enqueue-time value, and `first_build` is monotone
    (a success can only appear, and a live job blocks other work for the
    same agent), so that value never turns stale-true before the claim.
    `agents` is the rollout allowlist (empty = every agent): a pending job of an
    unlisted agent is left parked, not claimed. `defer_first_builds` (the first-build
    daily budget is spent) likewise parks a compact job whose agent is tracked but has
    no clean build yet — a never-tracked agent still baselines (no model call).

    Returns None when nothing (left) is pending — baselines drained in
    passing do not stop the drain.
    """
    while True:
        # The claim and its decision commit together: a crash must never leave
        # a first-sight job marked running without its retirement — the stale
        # sweep would recover it into a retry that builds pre-existing history
        # as a first build (review #3242 F1/nit 4).
        with conn.transaction():
            row = conn.execute(
                "UPDATE hierarchy_jobs SET status = 'running', started_at = now()"
                " WHERE id = (SELECT j.id FROM hierarchy_jobs j WHERE j.status = 'pending'"
                "             AND (cardinality(%s::bigint[]) = 0 OR j.agent_id = ANY(%s::bigint[]))"
                # A first-build candidate (deferred when its budget is spent): a compact job
                # of a tracked agent with no clean finished build (`scan.first_build`).
                "             AND (NOT %s OR NOT (j.kind = 'compact'"
                "               AND EXISTS (SELECT 1 FROM hierarchy_worker_state s"
                "                           WHERE s.agent_id = j.agent_id)"
                "               AND NOT EXISTS (SELECT 1 FROM hierarchy_jobs d"
                "                               WHERE d.agent_id = j.agent_id AND d.status = 'done'"
                "                                 AND coalesce(d.failed, 0) = 0"
                "                                 AND coalesce(d.skipped, 0) = 0"
                "                                 AND d.error IS NULL)))"
                "             ORDER BY j.id LIMIT 1 FOR UPDATE SKIP LOCKED)"
                " RETURNING id, agent_id, include_tail, kind, trigger_boundary",
                (sorted(agents), sorted(agents), defer_first_builds),
            ).fetchone()
            if row is None:
                return None
            job_id, agent_id = int(row[0]), int(row[1])
            include_tail, kind, boundary = bool(row[2]), str(row[3]), str(row[4])
            if kind == KIND_COMPACT and not _tracked(conn, agent_id):
                _baseline_untracked(conn, job_id, agent_id, boundary)
                continue
            if not include_tail and first_build(conn, agent_id):
                conn.execute(
                    "UPDATE hierarchy_jobs SET include_tail = true WHERE id = %s", (job_id,)
                )
                include_tail = True
            return ClaimedJob(id=job_id, agent_id=agent_id, include_tail=include_tail)


def run_child(job: ClaimedJob, config: HierarchyWorkerConfig, db: Database) -> None:
    """Run one claimed job under its hard deadline; recover what it cannot write."""
    deadline_s = config.hierarchy_job_deadline_seconds
    child = subprocess.Popen(  # noqa: S603 — fixed argv: our own interpreter, a static module path, and an int id
        [sys.executable, "-m", "services.hierarchy_worker.job", "--job-id", str(job.id)],
        cwd=_REPO_ROOT,
    )
    try:
        code = child.wait(timeout=deadline_s)
    except subprocess.TimeoutExpired:
        logger.error(
            "hierarchy job {job} exceeded its {deadline:.0f}s deadline — killing the child",
            job=job.id,
            deadline=deadline_s,
        )
        child.terminate()
        try:
            child.wait(timeout=config.hierarchy_child_kill_grace_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        _recover(job.id, f"deadline exceeded ({deadline_s:.0f}s)", db)
        return
    if code != 0:
        _recover(job.id, f"child exited {code} without recording a result", db)
    else:
        # The child records its own outcome; this only catches a child that
        # died between the build and the row write (the no-op case is the
        # normal path).
        _recover(job.id, "child exited 0 without recording a result", db)


def _recover(job_id: int, error: str, db: Database) -> None:
    """Park a job row the child could not finish (no-op once it is not running)."""
    with db.write_transaction() as conn:
        conn.execute(
            "UPDATE hierarchy_jobs SET status = 'failed', finished_at = now(), error = %s"
            " WHERE id = %s AND status = 'running'",
            (error, job_id),
        )


def run_tick(config: HierarchyWorkerConfig, db: Database) -> None:
    """One schedule tick: claim and run due jobs back-to-back — drain, not scan.

    The event trigger enqueues each compact boundary's job, so a tick
    consumes; the reconcile scan runs only when
    `hierarchy_fallback_scan_seconds` has elapsed since the last one (the
    first tick after boot always scans). Every iteration first reaps
    `running` rows whose holder process is gone (task #4975) — before the
    scan, so a stop-left row is failed and its agent re-enqueued in the same
    pass. A tripped 24h budget stops the tick before any claim.

    Returns when the queue is dry, the breaker is tripped, the unit is quiesced
    (the stop window), or after a transient failure — the next tick retries and nothing is lost. A
    code<->DB drift raises so the manager's crash path restarts the worker
    after a fix; no retry self-heals it.
    """
    global _fallback_scanned_at  # noqa: PLW0603 — process-local scan cadence
    if not config.hierarchy_worker_enabled:
        return
    while True:
        if admission.quiesced():
            return  # the stop window: no database work until `ava start` releases the hold
        try:
            with db.connect(autocommit=True) as conn:
                reap_orphans(conn)
                if _regen_budget_check(conn, config):
                    return
                now = datetime.now(UTC)
                if _fallback_scan_due(now, config):
                    outcome = scan(conn, config)
                    _fallback_scanned_at = now
                    if (
                        outcome.baselined
                        or outcome.enqueued
                        or outcome.tail_enqueued
                        or outcome.stale_recovered
                    ):
                        logger.info(
                            "hierarchy scan: tracked={tracked} baselined={baselined}"
                            " enqueued={enqueued} tail_enqueued={tail} stale_recovered={stale}",
                            tracked=outcome.agents_tracked,
                            baselined=outcome.baselined,
                            enqueued=outcome.enqueued,
                            tail=outcome.tail_enqueued,
                            stale=outcome.stale_recovered,
                        )
                job = claim_next(
                    conn,
                    agents=config.served_agents(),
                    defer_first_builds=_first_builds_deferred(conn, config),
                )
        except psycopg.ProgrammingError:
            # Code<->DB drift: no retry self-heals. Exit so the manager's
            # crash path (backoff + breaker + last_error) exposes it.
            logger.critical(
                "hierarchy worker: code<->DB drift (ProgrammingError) — exiting; "
                "the manager restarts after a fix",
                exc_info=True,
            )
            raise
        except Exception:
            logger.exception("hierarchy worker: tick iteration failed")
            return
        if job is None:
            return
        logger.info(
            "hierarchy job {job} claimed (agent {agent}, include_tail={tail})",
            job=job.id,
            agent=job.agent_id,
            tail=job.include_tail,
        )
        run_child(job, config, db)
