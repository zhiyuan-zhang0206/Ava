ALTER TABLE agent_pages ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE agent_pages ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;

-- Existing open pages receive the 24-hour default retroactively per the user ruling.
UPDATE agent_pages SET expires_at = created_at + interval '24 hours'
 WHERE expires_at IS NULL AND closed_at IS NULL;

CREATE INDEX IF NOT EXISTS agent_pages_expiry_idx ON agent_pages (expires_at)
 WHERE closed_at IS NULL AND expired_at IS NULL;

CREATE TABLE IF NOT EXISTS agent_shell_ttls (
    agent_id   BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    session_id BIGINT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, session_id)
);

CREATE INDEX IF NOT EXISTS agent_shell_ttls_expiry_idx ON agent_shell_ttls (expires_at);

COMMENT ON TABLE agent_shell_ttls IS 'Persistent shell sessions whose agent declared a TTL at creation (ava.shell.sessions.new/run_background ttl=). Reaped by the gateway TTL reaper; rows are removed when reaped or when the session dies (the reaper self-cleans).';
