-- Root task status pinned to 'ongoing' (user ruling 2026-08-27): the system
-- root is permanently ongoing -- it can never be open, done, or cancelled.
-- The status CHECK gains the 'ongoing' value, the existing root row (seeded as
-- 'in_progress' pre-ruling) moves to 'ongoing', and a table-level CHECK makes
-- the pin self-verifying against direct DB writes, not just API guards.
--
-- ORDER MATTERS (adversarial review 2026-08-27, PR #746): on a pre-ruling
-- cluster the root is 'in_progress' and the OLD status CHECK (4 values) is
-- still active -- UPDATEing the root to 'ongoing' before dropping that CHECK
-- violates it and aborts the whole migration (cluster update hard-fails).
-- So: drop the old CHECK first, then backfill, then re-add the widened CHECK.
-- Every DROP is IF EXISTS so the body also runs on a fresh bootstrap, where
-- schema.sql already carries the new shape (migration smoke replays on fresh).

-- 1. Release the old 4-value status CHECK (idempotent on fresh bootstraps).
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_status_check;

-- 2. Backfill the root row (now unconstrained; only the root is touched).
UPDATE agent_tasks
SET status = 'ongoing'
WHERE is_root
  AND status <> 'ongoing';

-- 3. Re-add the status CHECK, widened with 'ongoing'.
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_status_check
    CHECK (status IN ('open', 'in_progress', 'done', 'cancelled', 'ongoing'));

-- 4. Rebuild the root-status pin (bidirectional; idempotent on fresh
--    bootstraps that already carry it from schema.sql).
ALTER TABLE agent_tasks
    DROP CONSTRAINT IF EXISTS agent_tasks_root_status_ongoing;
ALTER TABLE agent_tasks
    ADD CONSTRAINT agent_tasks_root_status_ongoing
    CHECK ((is_root AND status = 'ongoing') OR (NOT is_root AND status <> 'ongoing'));
