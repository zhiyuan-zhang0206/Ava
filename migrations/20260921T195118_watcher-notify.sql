-- Preserve each watcher's shell completion policy across a recovery. The
-- non-null default backfills existing rows and keeps legacy registrations
-- fail-open: completion notices continue to be sent for every exit.
ALTER TABLE agent_watchers
    ADD COLUMN notify TEXT NOT NULL DEFAULT 'always'
    CHECK (notify IN ('always', 'failure'));
