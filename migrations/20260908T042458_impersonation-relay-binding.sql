-- Relay binding: every takeover request names its relay endpoint up front,
-- the accepting runtime provisions a scoped relay credential, and liveness is
-- heartbeated against the lease row itself. A lease without a relay binding is
-- never accepted silently: activation fails loudly (see fail_acceptance).
ALTER TABLE agent_impersonations
    ADD COLUMN relay_provider TEXT,
    ADD COLUMN relay_thread_id TEXT,
    ADD COLUMN relay_codex_remote TEXT,
    ADD COLUMN relay_token_hash TEXT,
    ADD COLUMN relay_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN relay_last_failure_at TIMESTAMPTZ;
ALTER TABLE agent_impersonations ADD CONSTRAINT agent_impersonations_relay_spec CHECK (
    (relay_provider IS NULL
        AND relay_thread_id IS NULL
        AND relay_codex_remote IS NULL)
    OR (relay_provider = 'codex' AND relay_thread_id IS NOT NULL)
    OR (relay_provider = 'claude'
        AND relay_thread_id IS NULL
        AND relay_codex_remote IS NULL)
);
CREATE INDEX IF NOT EXISTS agent_impersonations_relay_heartbeat
    ON agent_impersonations(agent_id, relay_heartbeat_at)
    WHERE status = 'active';
