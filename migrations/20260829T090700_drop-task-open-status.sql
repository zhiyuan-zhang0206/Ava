-- Drop the 'open' task status (user ruling 2026-08-29): a task is born
-- 'in_progress' -- the DB default flips from 'open' to 'in_progress' -- every
-- existing 'open' row is migrated to 'in_progress', and the status CHECK
-- narrows to ('in_progress', 'done', 'cancelled', 'ongoing'). The partial
-- unique title index (agent_tasks_title_unique_open) is recreated under the
-- new name with the narrowed predicate. No backward-compat shim: 'open' is
-- gone from the vocabulary entirely (zero-shim user constraint).

-- 1. Migrate existing rows BEFORE narrowing the CHECK (the old CHECK admits
--    'open'; narrowing first would abort on the live rows). updated_at is
--    deliberately untouched: a migrated task keeps its real idle clock, so an
--    owner who has neglected an open task is reminded on the next daemon
--    sweep instead of getting a fresh silence window.
UPDATE agent_tasks SET status = 'in_progress' WHERE status = 'open';

-- 2. Flip the column default so future INSERTs without an explicit status are
--    born 'in_progress' (task_registry.create() relies on the default).
ALTER TABLE agent_tasks ALTER COLUMN status SET DEFAULT 'in_progress';

-- 3. Narrow the status CHECK: 'open' is no longer a legal value.
ALTER TABLE agent_tasks DROP CONSTRAINT IF EXISTS agent_tasks_status_check;
ALTER TABLE agent_tasks ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN ('in_progress', 'done', 'cancelled', 'ongoing'));

-- 4. Recreate the partial unique title index with the narrowed predicate and
--    the new name (the old name carries the removed status). IF NOT EXISTS
--    keeps the migration replay-safe on a fresh DB bootstrapped from the
--    already-updated db/schema.sql baseline (same idempotency convention as
--    the root-task-ongoing migration's DROP CONSTRAINT IF EXISTS + ADD).
DROP INDEX IF EXISTS agent_tasks_title_unique_open;
CREATE UNIQUE INDEX IF NOT EXISTS agent_tasks_title_unique_in_progress
    ON agent_tasks (title) WHERE status = 'in_progress';
