-- Reverse: drop the root-status pin and the 'ongoing' value, moving the root
-- back to its pre-ruling 'in_progress' state.
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_root_status_ongoing;
ALTER TABLE agent_tasks
    DROP CONSTRAINT agent_tasks_status_check;
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN ('open', 'in_progress', 'done', 'cancelled'));

UPDATE agent_tasks
SET status = 'in_progress'
WHERE is_root
  AND status = 'ongoing';
