-- `agent` records that notify was omitted, so a recovered watcher continues
-- to resolve the current per-agent completion policy instead of freezing a
-- former default into its desired-state row.
ALTER TABLE agent_watchers
    DROP CONSTRAINT agent_watchers_notify_check;

ALTER TABLE agent_watchers
    ADD CONSTRAINT agent_watchers_notify_check
    CHECK (notify IN ('always', 'failure', 'agent'));
