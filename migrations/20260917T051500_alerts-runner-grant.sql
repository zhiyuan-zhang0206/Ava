-- Re-affirm the ava_runner surface for alerts on EXISTING clusters (task
-- #3747). The start-readiness alert path (cli/commands/_probe.py) upserts its
-- firing instance and resolves it again on recovery, from the runner process
-- -- but the grant layer never carried an alerts entry: the role could SELECT
-- the table and nothing more. So on every pure agent-runner each start logged
-- "non-critical service alert resolve failed (InsufficientPrivilege)" and the
-- instance stayed open (prod was patched by hand; this migration is the
-- rollout trigger for the rest of the fleet).
--
-- Applying this file trips the start-path grant refresh
-- (refresh_runner_grants_after_migration in cli/commands/ensure_db_role.py),
-- which re-runs ensure_runner_role with the alerts entry and re-affirms the
-- whole runner surface.
--
-- Idempotent, and gated on the role's existence so the fresh-bootstrap smoke
-- (migration replay on a schema.sql DB, where the role does not exist yet)
-- stays green -- there, schema.sql and ensure_runner_role own the grants at
-- birth.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON alerts TO ava_runner;
    END IF;
END $$;
