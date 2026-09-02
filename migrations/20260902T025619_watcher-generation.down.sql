-- `reaped` records are retained generation history. The pre-generation status
-- set has no equivalent, so a rollback preserves their terminality as rebuilt.
UPDATE agent_watchers SET status = 'rebuilt' WHERE status = 'reaped';

ALTER TABLE agent_watchers
    DROP CONSTRAINT IF EXISTS agent_watchers_status_check;
ALTER TABLE agent_watchers
    ADD CONSTRAINT agent_watchers_status_check
    CHECK (status IN ('running', 'rebuilt', 'missed'));

ALTER TABLE agent_watchers DROP COLUMN IF EXISTS generation;
