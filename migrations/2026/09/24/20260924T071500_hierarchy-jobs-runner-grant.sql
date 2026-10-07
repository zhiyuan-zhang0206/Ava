-- The compact-boundary event enqueue's ava_runner surface on EXISTING
-- clusters (task #4674): the agent process's `mark_compact_boundary` twin
-- INSERTs one hierarchy_jobs row per new compaction boundary — idempotent,
-- ON CONFLICT DO NOTHING against the live partial unique index; the SELECT
-- half and the id sequence ride the blanket grants (ensure_runner_role).
-- Without the grant every enqueue fails with InsufficientPrivilege and the
-- event trigger goes silently dark — the #1932 / #3549 / #3747 class.
--
-- Applying this file trips the start-path grant refresh
-- (refresh_runner_grants_after_migration in cli/commands/ensure_db_role.py),
-- which re-runs ensure_runner_role and re-affirms the whole runner surface.
--
-- Idempotent, and gated on the role's existence so the fresh-bootstrap smoke
-- (migration replay on a schema.sql DB, where the role does not exist yet)
-- stays green — there, schema.sql and ensure_runner_role own the grants at
-- birth.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT INSERT ON hierarchy_jobs TO ava_runner;
    END IF;
END $$;
