-- The hierarchy worker's orphan reclaim (task #4975): a stop kills the worker
-- mid-job, the row stays `running`, and the next compact's enqueue used to be
-- swallowed by the live partial unique index while recovery waited out the
-- stale sweep and then the retry backoff (~50 min, measured 2026-10-04).
--
-- Two halves, one schema delta:
--
-- 1. `holder_pid` — the job child (`services/hierarchy_worker/execute.py`)
--    stamps its own pid at boot; the worker's reap fails any running row
--    whose holder pid is gone, before the scan, so the same tick re-enqueues
--    the agent's newest boundary (the marker rows are skipped by the pacing
--    reads, so no retry backoff either; the deadline+grace sweep stays as the
--    no-restart fallback).
-- 2. The enqueue's conflict supersede needs UPDATE on hierarchy_jobs for the
--    agent-side runner process: a running row past the deadline window (no
--    live holder can exist) is flipped to failed with the orphan marker and
--    the boundary re-inserts at once. Without the grant that path fails with
--    InsufficientPrivilege and the enqueue degrades to the silent swallow
--    this fix exists to remove.
--
-- The ALTER is idempotent (IF NOT EXISTS) and the GRANT is gated on the
-- role's existence so the fresh-bootstrap smoke (migration replay on a
-- schema.sql DB, where the role does not exist yet) stays green — there,
-- schema.sql and ensure_groups (`base/cluster/authority/groups.py`) own the
-- grants at birth; applying this file trips the start-path grant refresh
-- (ensure_groups off cli/commands/data_plane/bringup.py).
ALTER TABLE hierarchy_jobs ADD COLUMN IF NOT EXISTS holder_pid INTEGER;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT UPDATE ON hierarchy_jobs TO ava_runner;
    END IF;
END $$;
