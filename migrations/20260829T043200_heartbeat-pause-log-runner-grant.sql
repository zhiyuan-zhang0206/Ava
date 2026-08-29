-- Re-affirm the ava_runner surface for heartbeat_pause_log on EXISTING
-- clusters (task #1932). The original migration (20260828T191814) created the
-- table before the grant layer knew about it, so every cluster born before
-- this fix has an ava_runner that cannot INSERT the pause trail —
-- ava.self.pause_heartbeat fails with InsufficientPrivilege (prod was patched
-- by hand; this migration is the rollout trigger for the rest of the fleet).
--
-- Applying this file trips the start-path grant refresh
-- (refresh_runner_grants_after_migration in cli/commands/ensure_db_role.py),
-- which re-runs ensure_runner_role with the heartbeat_pause_log entry and
-- re-affirms the whole runner surface.
--
-- Idempotent, and gated on the role's existence so the fresh-bootstrap smoke
-- (migration replay on a schema.sql DB, where the role does not exist yet)
-- stays green -- there, schema.sql and the original migration already carry
-- these grants and ensure_runner_role owns them at birth.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON heartbeat_pause_log TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE heartbeat_pause_log_id_seq TO ava_runner;
    END IF;
END $$;
