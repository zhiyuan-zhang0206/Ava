-- A watcher row is durable desired state, not merely telemetry. Persist the
-- generation of the PTY record that created it so boot reconcile can retain a
-- superseded row as `reaped` rather than recreate it after a rollout. NULL
-- remains the legacy pre-generation value and is current only while the host
-- has no active allocation generation.
ALTER TABLE agent_watchers ADD COLUMN IF NOT EXISTS generation TEXT;

ALTER TABLE agent_watchers
    DROP CONSTRAINT IF EXISTS agent_watchers_status_check;
ALTER TABLE agent_watchers
    ADD CONSTRAINT agent_watchers_status_check
    CHECK (status IN ('running', 'rebuilt', 'missed', 'reaped'));
