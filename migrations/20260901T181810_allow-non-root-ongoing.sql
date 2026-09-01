-- Allow regular tasks to use 'ongoing' for long-running active work. The root
-- remains pinned to 'ongoing'; only the old non-root exclusion is removed.
-- DROP IF EXISTS keeps the migration replay-safe on a fresh bootstrap whose
-- schema.sql already carries this constraint shape.
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_root_status_ongoing;
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_root_status_ongoing
    CHECK (NOT is_root OR status = 'ongoing');
