DROP INDEX IF EXISTS agent_shell_ttls_expiry_idx;
DROP TABLE IF EXISTS agent_shell_ttls;
DROP INDEX IF EXISTS agent_pages_expiry_idx;
ALTER TABLE agent_pages DROP COLUMN IF EXISTS expired_at;
ALTER TABLE agent_pages DROP COLUMN IF EXISTS expires_at;
