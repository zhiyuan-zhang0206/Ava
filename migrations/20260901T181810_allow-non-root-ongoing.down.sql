-- Restore the bidirectional root-status constraint. Regular ongoing tasks do
-- not fit the previous shape, so rollback returns them to 'in_progress'.
UPDATE agent_tasks
SET status = 'in_progress'
WHERE NOT is_root
  AND status = 'ongoing';

ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_root_status_ongoing;
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_root_status_ongoing
    CHECK ((is_root AND status = 'ongoing') OR (NOT is_root AND status <> 'ongoing'));
