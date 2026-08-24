ALTER TABLE agent_watchers
    DROP CONSTRAINT IF EXISTS agent_watchers_agent_id_fkey;

ALTER TABLE agent_watchers ALTER COLUMN agent_id TYPE INTEGER;
