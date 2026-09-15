-- Re-affirm the ava_runner surface for agent_impersonation_entries on
-- EXISTING clusters (task #3549). 20260913T180056 created the table (and the
-- lifecycle / inbound triggers that INSERT into it) without adding it to the
-- runner-grant layer, so every cluster past the runner-role cutover rejects
-- lease creation with InsufficientPrivilege on agent_impersonation_entries —
-- impersonation is unusable until the role can write the trail. (Prod is
-- patched by hand as the immediate unblock; this migration is the rollout for
-- the rest of the fleet.)
--
-- Applying this file trips the start-path grant refresh
-- (refresh_runner_grants_after_migration in cli/commands/ensure_db_role.py),
-- which re-runs ensure_runner_role and re-affirms the whole runner surface.
--
-- Idempotent, and gated on the role's existence so the fresh-bootstrap smoke
-- (migration replay on a schema.sql DB, where the role does not exist yet)
-- stays green -- there, schema.sql and ensure_runner_role own the grants at
-- birth.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT ON agent_impersonation_entries TO ava_runner;
    END IF;
END $$;
