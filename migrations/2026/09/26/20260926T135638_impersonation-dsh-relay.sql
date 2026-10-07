-- DeepSeek Harness joins the relay providers. Like claude, its relay runs
-- inside the controller's own session: no thread id, no codex remote. The
-- baseline declared this rule as an unnamed CHECK; replace it by definition
-- with a named constraint so later changes can address it directly.
DO $$
DECLARE
    relay_check TEXT;
BEGIN
    SELECT conname INTO STRICT relay_check
    FROM pg_constraint
    WHERE conrelid = 'agent_impersonations'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%relay_provider%';
    EXECUTE format('ALTER TABLE agent_impersonations DROP CONSTRAINT %I', relay_check);
END;
$$;

ALTER TABLE agent_impersonations ADD CONSTRAINT agent_impersonations_relay_spec CHECK (
    (relay_provider IS NULL
        AND relay_thread_id IS NULL
        AND relay_codex_remote IS NULL)
    OR (relay_provider = 'codex' AND relay_thread_id IS NOT NULL)
    OR (relay_provider IN ('claude', 'dsh')
        AND relay_thread_id IS NULL
        AND relay_codex_remote IS NULL)
);
