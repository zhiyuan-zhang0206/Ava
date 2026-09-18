-- Reverse of hierarchy-tail-seal: drop the delta-gate column and narrow the
-- kind CHECK back to 'compact'. Tail attempt rows are deleted first — nothing
-- converts a tail attempt into a compact one, and the narrowed CHECK must
-- validate cleanly (the same shape the drop-ongoing-status rollback uses).

ALTER TABLE hierarchy_worker_state
    DROP COLUMN IF EXISTS last_tail_seal_cp_id;

DELETE FROM hierarchy_jobs WHERE kind = 'tail';

ALTER TABLE hierarchy_jobs
    DROP CONSTRAINT IF EXISTS hierarchy_jobs_kind_check;

ALTER TABLE hierarchy_jobs
    ADD CONSTRAINT hierarchy_jobs_kind_check
    CHECK (kind IN ('compact'));
