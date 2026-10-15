-- Google Antigravity joins the relay providers. Each Antigravity conversation
-- has a conversation UUID passed in relay_thread_id; relay_codex_remote is not
-- used.
ALTER TABLE agent_impersonations DROP CONSTRAINT agent_impersonations_relay_spec;

ALTER TABLE agent_impersonations ADD CONSTRAINT agent_impersonations_relay_spec CHECK (
    (relay_provider IS NULL
        AND relay_thread_id IS NULL
        AND relay_codex_remote IS NULL)
    OR (relay_provider = 'codex' AND relay_thread_id IS NOT NULL)
    OR (relay_provider = 'antigravity'
        AND relay_thread_id IS NOT NULL
        AND relay_codex_remote IS NULL)
    OR (relay_provider IN ('claude', 'dsh')
        AND relay_thread_id IS NULL
        AND relay_codex_remote IS NULL)
);
