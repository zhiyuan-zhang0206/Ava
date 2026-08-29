-- Restore the 'open' status: widen the CHECK, flip the default back, and
-- recreate the old partial unique index. No data migration on the way down:
-- rows migrated from 'open' are now 'in_progress' and stay so (restoring the
-- status value would be guessing -- nothing distinguishes a migrated row from
-- one born 'in_progress'); 'open' is simply a legal value again.

-- 1. Widen the CHECK first (every current row is legal under the 5-value set).
ALTER TABLE agent_tasks DROP CONSTRAINT IF EXISTS agent_tasks_status_check;
ALTER TABLE agent_tasks ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN ('open', 'in_progress', 'done', 'cancelled', 'ongoing'));

-- 2. Flip the default back.
ALTER TABLE agent_tasks ALTER COLUMN status SET DEFAULT 'open';

-- 3. Restore the old partial unique index (drop the narrowed one first).
DROP INDEX IF EXISTS agent_tasks_title_unique_in_progress;
CREATE UNIQUE INDEX IF NOT EXISTS agent_tasks_title_unique_open
    ON agent_tasks (title) WHERE status IN ('open', 'in_progress');
