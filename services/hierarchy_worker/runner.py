"""The resident worker loop — scan, claim one job, run it, repeat.

Hosted by the gateway's ScheduleManager through
`schedules/hierarchy-worker-schedule.py`: a resident schedule the manager
launches at boot, adopts across gateway restarts, and restarts with backoff +
breaker if it crashes. Each job runs as a child process, so a big window's
memory and any crash stay contained; the parent's wait is bounded by the job
deadline — a wedged child is killed and its row recovered.

Serial by construction — one child at a time, the cost guardrail pinned in
review (3187). The loop drains pending jobs back-to-back and sleeps only
when nothing is due. Every DB step is idempotent and race-free (partial
unique index + atomic claim), so even a second worker process could only
duplicate work that is hash-idempotent anyway.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import Connection

from services.hierarchy_worker.scan import scan
from shared.config import settings
from shared.db import connect
from shared.db_transaction import write_transaction
from shared.log import logger

# The deployed source root: shared/ sits at the repo root in prod and in a
# worktree alike (the c9-daily-report precedent), and the child must import
# from the same checkout this runner executes.
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ClaimedJob:
    """One claimed job row (id/agent/tail — the child reads the rest itself)."""

    id: int
    agent_id: int
    include_tail: bool


def claim_next(conn: Connection) -> ClaimedJob | None:
    """Claim the oldest pending job, atomically (None when none is pending)."""
    row = conn.execute(
        "UPDATE hierarchy_jobs SET status = 'running', started_at = now()"
        " WHERE id = (SELECT id FROM hierarchy_jobs WHERE status = 'pending'"
        "             ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)"
        " RETURNING id, agent_id, include_tail"
    ).fetchone()
    if row is None:
        return None
    return ClaimedJob(id=int(row[0]), agent_id=int(row[1]), include_tail=bool(row[2]))


def run_child(job: ClaimedJob) -> None:
    """Run one claimed job under its hard deadline; recover what it cannot write."""
    deadline_s = settings.daemon.hierarchy_job_deadline_seconds
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
            child.wait(timeout=settings.daemon.hierarchy_child_kill_grace_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        _recover(job.id, f"deadline exceeded ({deadline_s:.0f}s)")
        return
    if code != 0:
        _recover(job.id, f"child exited {code} without recording a result")
    else:
        # The child records its own outcome; this only catches a child that
        # died between the build and the row write (the no-op case is the
        # normal path).
        _recover(job.id, "child exited 0 without recording a result")


def _recover(job_id: int, error: str) -> None:
    """Park a job row the child could not finish (no-op once it is not running)."""
    with write_transaction() as conn:
        conn.execute(
            "UPDATE hierarchy_jobs SET status = 'failed', finished_at = now(), error = %s"
            " WHERE id = %s AND status = 'running'",
            (error, job_id),
        )


def loop_forever() -> None:
    """The resident loop. Never returns; a crash is the manager's restart path."""
    from shared.migrations import assert_schema_current

    assert_schema_current(settings.data_plane.db_url)
    logger.info("hierarchy worker started (pid {pid})", pid=os.getpid())
    while True:
        try:
            with connect(autocommit=True) as conn:
                outcome = scan(conn)
                if outcome.baselined or outcome.enqueued or outcome.stale_recovered:
                    logger.info(
                        "hierarchy scan: tracked={tracked} baselined={baselined}"
                        " enqueued={enqueued} stale_recovered={stale}",
                        tracked=outcome.agents_tracked,
                        baselined=outcome.baselined,
                        enqueued=outcome.enqueued,
                        stale=outcome.stale_recovered,
                    )
                job = claim_next(conn)
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
            logger.exception("hierarchy worker: scan iteration failed")
            time.sleep(settings.daemon.hierarchy_worker_poll_seconds)
            continue
        if job is None:
            time.sleep(settings.daemon.hierarchy_worker_poll_seconds)
            continue
        logger.info(
            "hierarchy job {job} claimed (agent {agent}, include_tail={tail})",
            job=job.id,
            agent=job.agent_id,
            tail=job.include_tail,
        )
        run_child(job)
