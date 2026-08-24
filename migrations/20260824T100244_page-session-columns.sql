-- The page-server daemon persists the per-page health token so it can adopt
-- servers after restart, and records the persistent shell that owns each
-- serve() page. Idempotent because db/schema.sql is the current baseline.
ALTER TABLE agent_pages ADD COLUMN IF NOT EXISTS server_token TEXT;
ALTER TABLE agent_pages ADD COLUMN IF NOT EXISTS session_name TEXT;
