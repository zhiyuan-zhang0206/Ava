-- A watcher registry row is meaningless without its spawning agent. Agent
-- rows are never deleted in the product (termination flips status), so this FK is a
-- guard rail; the agent_pages precedent uses CASCADE for a hypothetical
-- agent-row deletion, which should remove its owned runtime resources too.
-- The squashed baseline already carries this shape, so the constraint addition
-- is guarded for fresh-schema replay.
DELETE FROM agent_watchers w
WHERE NOT EXISTS (SELECT 1 FROM agents a WHERE a.id = w.agent_id);

ALTER TABLE agent_watchers ALTER COLUMN agent_id TYPE BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'agent_watchers'::regclass
          AND conname = 'agent_watchers_agent_id_fkey'
    ) THEN
        ALTER TABLE agent_watchers
            ADD CONSTRAINT agent_watchers_agent_id_fkey
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE;
    END IF;
END $$;
