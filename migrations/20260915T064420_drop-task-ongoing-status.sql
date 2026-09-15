-- Drop the 'ongoing' task status (user ruling 2026-09-15): 'ongoing' was
-- never a needed state -- the system root is permanently 'in_progress' and
-- immutable, and regular tasks use in_progress/done/cancelled only. Every
-- remaining 'ongoing' row (the root + long-running regular tasks created under
-- the 2026-09-01 allowance) is migrated back to 'in_progress', the status
-- CHECK narrows to the three values, and the old root pin
-- (agent_tasks_root_status_ongoing) is replaced by its in_progress form.
--
-- ORDER MATTERS (same shape as the two predecessor migrations): on a
-- pre-removal cluster the old root pin requires root='ongoing' and the old
-- CHECK admits 'ongoing', so both constraints are dropped BEFORE the backfill
-- (updating the root first would abort the migration). Every DROP is
-- IF EXISTS so the body also runs on a fresh bootstrap, where schema.sql
-- already carries the new shape (the migration smoke replays on fresh).
--
-- The partial unique index on in_progress titles is untouched: if a moved
-- title collided with another in_progress title the backfill aborts loudly
-- (same behavior as the predecessor open -> in_progress backfill).

-- 1. Release both constraints (idempotent on fresh bootstraps).
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_root_status_ongoing;
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_status_check;

-- 2. Backfill every 'ongoing' row -- root and regular alike (the root is just
--    permanently in_progress; its status value was never special).
UPDATE agent_tasks
SET status = 'in_progress'
WHERE status = 'ongoing';

-- 3. Narrow the status CHECK back to the three values.
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN ('in_progress', 'done', 'cancelled'));

-- 4. Re-pin the root to 'in_progress' -- the equivalent form of the old
--    ongoing pin: the root can never be closed or reopened.
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_root_status_in_progress;
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_root_status_in_progress
    CHECK (NOT is_root OR status = 'in_progress');
