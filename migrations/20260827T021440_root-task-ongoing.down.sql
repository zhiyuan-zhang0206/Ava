-- Reverse (adversarial review 2026-08-27, PR #746): symmetric to the up body.
-- The 4-value CHECK must NOT be re-added while the root is still 'ongoing'
-- (ADD CONSTRAINT validates existing rows and would fail) — so move the root
-- back to 'in_progress' under the 5-value CHECK first, then narrow it.

-- 1. Drop the root-status pin (bidirectional).
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_root_status_ongoing;

-- 2. Move the root back to its pre-ruling state (5-value CHECK still allows it).
UPDATE agent_tasks
SET status = 'in_progress'
WHERE is_root
  AND status = 'ongoing';

-- 3. Narrow the status CHECK back to the four assignable values.
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_status_check;
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN ('open', 'in_progress', 'done', 'cancelled'));
