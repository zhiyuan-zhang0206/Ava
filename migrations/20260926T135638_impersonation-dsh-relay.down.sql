-- Session history is permanent (DELETE guards), so dsh sessions cannot be
-- rewritten into the pre-dsh rule: refuse while any exists.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM agent_impersonations WHERE relay_provider = 'dsh') THEN
        RAISE EXCEPTION 'agent_impersonations holds dsh relay sessions; the pre-dsh relay rule cannot represent them';
    END IF;
END;
$$;

ALTER TABLE agent_impersonations DROP CONSTRAINT agent_impersonations_relay_spec;

ALTER TABLE agent_impersonations ADD CHECK (
    (relay_provider IS NULL
        AND relay_thread_id IS NULL
        AND relay_codex_remote IS NULL)
    OR (relay_provider = 'codex' AND relay_thread_id IS NOT NULL)
    OR (relay_provider = 'claude'
        AND relay_thread_id IS NULL
        AND relay_codex_remote IS NULL)
);
