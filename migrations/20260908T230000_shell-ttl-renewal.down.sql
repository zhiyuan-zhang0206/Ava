-- Shell TTL renewal rollback: drop the audit trail and the display columns.
-- Existing renewal history is lost (it is operational audit data, not
-- configuration); rows revert to the pre-renewal single-deadline shape.
DROP INDEX IF EXISTS agent_shell_ttl_renewals_agent_session_idx;
DROP TABLE IF EXISTS agent_shell_ttl_renewals;
ALTER TABLE agent_shell_ttls
    DROP COLUMN IF EXISTS renewals,
    DROP COLUMN IF EXISTS last_renewed_at;
