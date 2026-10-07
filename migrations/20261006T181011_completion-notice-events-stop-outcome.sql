-- Expand step: completion notices no longer carry an outcome or exit code.
-- Code stops reading and writing `outcome` / `exit_code`; the columns and the
-- old (agent_id, source, outcome) unique constraint stay until a later
-- contract migration, so code from before this migration (still writing both
-- columns during a rollout) keeps working.
ALTER TABLE completion_notice_events ALTER COLUMN outcome DROP NOT NULL;
ALTER TABLE completion_notice_events DROP CONSTRAINT IF EXISTS completion_notice_events_exit_code_check;
-- Buffer rows are per (agent, source); an agent/source pair has one completion.
DELETE FROM completion_notice_events a
    USING completion_notice_events b
    WHERE a.agent_id = b.agent_id AND a.source = b.source AND a.id > b.id;
CREATE UNIQUE INDEX IF NOT EXISTS completion_notice_events_agent_source_unique
    ON completion_notice_events (agent_id, source);
