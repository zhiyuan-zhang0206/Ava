-- Root task status pinned to 'ongoing' (user ruling 2026-08-27): the system
-- root is permanently ongoing -- it can never be open, done, or cancelled.
-- The status CHECK gains the 'ongoing' value, the existing root row (seeded as
-- 'in_progress' pre-ruling) moves to 'ongoing', and a table-level CHECK makes
-- the pin self-verifying against direct DB writes, not just API guards.
--
-- Idempotent: only rows still on the old value are touched; a re-run no-ops.
UPDATE agent_tasks
SET status = 'ongoing'
WHERE is_root
  AND status <> 'ongoing';

ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_status_check;
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN ('open', 'in_progress', 'done', 'cancelled', 'ongoing'));
-- Fresh bootstraps already carry this constraint (db/schema.sql); DROP IF EXISTS
-- keeps the migration idempotent on both shapes.
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_root_status_ongoing;
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_root_status_ongoing
    CHECK (NOT is_root OR status = 'ongoing');
