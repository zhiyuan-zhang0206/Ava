-- A rollback preserves the legacy every-exit behavior for dynamic rows before
-- restoring the old closed set.
UPDATE agent_watchers SET notify = 'always' WHERE notify = 'agent';

ALTER TABLE agent_watchers
    DROP CONSTRAINT agent_watchers_notify_check;

ALTER TABLE agent_watchers
    ADD CONSTRAINT agent_watchers_notify_check
    CHECK (notify IN ('always', 'failure'));
