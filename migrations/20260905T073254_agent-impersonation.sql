-- Cooperative, same-machine external execution. PostgreSQL owns the lease;
-- Redis only announces changes. Existing agents and messages remain untouched.
CREATE TABLE IF NOT EXISTS agent_impersonations (
    id UUID PRIMARY KEY,
    agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    machine TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    rejection_reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('requested', 'accepted', 'active', 'released', 'rejected', 'expired')),
    ttl_seconds INTEGER NOT NULL CHECK (ttl_seconds BETWEEN 1 AND 86400),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_generation UUID,
    accepted_owner UUID,
    consent_version INTEGER NOT NULL DEFAULT 1,
    activated_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    summary_inbound_id BIGINT REFERENCES inbound_messages(id) ON DELETE SET NULL,
    plugin_delta JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(plugin_delta) = 'array'),
    delta_version INTEGER NOT NULL DEFAULT 0,
    applied_version INTEGER NOT NULL DEFAULT 0,
    CHECK (applied_version >= 0 AND applied_version <= delta_version),
    CHECK (jsonb_array_length(plugin_delta) = delta_version),
    CHECK ((accepted_generation IS NULL) = (accepted_owner IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_impersonations_one_open
    ON agent_impersonations(agent_id)
    WHERE status IN ('requested', 'accepted', 'active') OR delta_version > applied_version;
CREATE INDEX IF NOT EXISTS agent_impersonations_expiry ON agent_impersonations(expires_at)
    WHERE status IN ('requested', 'accepted', 'active');
CREATE INDEX IF NOT EXISTS agent_impersonations_retention ON agent_impersonations(ended_at)
    WHERE status IN ('released', 'rejected', 'expired');

-- Reading delivers without consuming. Only the explicit processing ACK changes
-- an inbound to done; an expired borrower leaves every unacknowledged row pending.
CREATE TABLE IF NOT EXISTS agent_impersonation_messages (
    lease_id UUID NOT NULL REFERENCES agent_impersonations(id) ON DELETE CASCADE,
    inbound_id BIGINT NOT NULL REFERENCES inbound_messages(id) ON DELETE CASCADE,
    acknowledged_at TIMESTAMPTZ,
    PRIMARY KEY (lease_id, inbound_id)
);

-- Every termination writer (including force/reaper) revokes in its own atomic
-- status transaction. Restart uses 'restarting' and preserves the active lease.
CREATE OR REPLACE FUNCTION revoke_terminated_impersonation() RETURNS trigger AS $$
BEGIN
    UPDATE agent_impersonations SET status='expired', ended_at=clock_timestamp()
    WHERE agent_id=NEW.id AND status IN ('requested','accepted','active');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS agents_meta_revoke_impersonation ON agents_meta;
CREATE TRIGGER agents_meta_revoke_impersonation
    AFTER UPDATE OF status ON agents_meta FOR EACH ROW
    WHEN (NEW.status = 'terminated')
    EXECUTE FUNCTION revoke_terminated_impersonation();
