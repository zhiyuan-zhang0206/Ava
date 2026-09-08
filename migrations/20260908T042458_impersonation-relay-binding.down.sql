-- Relay binding removal: drop the liveness index, the spec constraint and the
-- relay columns. Existing rows lose their relay state; active leases at the
-- time of rollback fall back to the manual-relay era semantics.
DROP INDEX IF EXISTS agent_impersonations_relay_heartbeat;
ALTER TABLE agent_impersonations DROP CONSTRAINT IF EXISTS agent_impersonations_relay_spec;
ALTER TABLE agent_impersonations
    DROP COLUMN IF EXISTS relay_provider,
    DROP COLUMN IF EXISTS relay_thread_id,
    DROP COLUMN IF EXISTS relay_codex_remote,
    DROP COLUMN IF EXISTS relay_token_hash,
    DROP COLUMN IF EXISTS relay_heartbeat_at,
    DROP COLUMN IF EXISTS relay_last_failure_at;
