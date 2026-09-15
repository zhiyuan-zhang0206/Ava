-- Reverse of drop-task-ongoing-status: restore the pre-removal shape
-- (adversarial-review discipline of the predecessor migrations -- symmetric to
-- the up body). The root moves back to 'ongoing' because the restored pin
-- requires it; regular tasks stay 'in_progress' -- restoring their pre-removal
-- value would be guessing (nothing distinguishes a migrated row from one born
-- in_progress), exactly as the allow-non-root-ongoing rollback did.

-- 1. Release the new constraints.
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_root_status_in_progress;
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_status_check;

-- 2. Move the root back to its pre-removal state ('ongoing').
UPDATE agent_tasks
SET status = 'ongoing'
WHERE is_root;

-- 3. Widen the status CHECK to admit 'ongoing' again.
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN ('in_progress', 'done', 'cancelled', 'ongoing'));

-- 4. Restore the pre-removal root pin (root pinned to 'ongoing'; regular
--    tasks unconstrained).
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_root_status_ongoing
    CHECK (NOT is_root OR status = 'ongoing');
