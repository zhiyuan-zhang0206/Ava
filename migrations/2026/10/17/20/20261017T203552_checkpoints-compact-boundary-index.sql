-- The history views read one agent's compaction boundaries by filtering on the
-- JSONB marker `metadata->>'compact_boundary'`. Nothing served that predicate:
-- a long-lived thread carries hundreds of thousands of checkpoints (thread
-- 6090: 382,600 rows, 460 of them boundaries), so the read degraded into a heap
-- scan and sort over the whole thread. On 2026-10-17 19:18 CST it ran past the
-- 60 s statement ceiling and saturated I/O on the 17 GB table; the queue behind
-- it is what turned into the pool acquire timeouts agents died on.
--
-- A partial index keeps only the boundary rows, so the same read becomes an
-- index-only scan over one thread's boundaries (measured: cost 888,467 ->
-- 229.70, 0.267 ms for thread 6090). The predicate must match the reader
-- verbatim (`metadata->>'compact_boundary' = 'true'`) or the planner ignores it.
--
-- `checkpoints` belongs to the LangGraph saver: a fresh birth runs
-- PostgresSaver.setup() before migrations, so the table is present here. The
-- to_regclass guard covers only a schema-only database (the migration test
-- fixture), where an upstream table has not been created yet.
DO $$
BEGIN
    IF to_regclass('public.checkpoints') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS checkpoints_compact_boundary_idx
            ON public.checkpoints (thread_id, checkpoint_id DESC)
            WHERE metadata->>'compact_boundary' = 'true';
    ELSE
        RAISE NOTICE 'no public.checkpoints (schema-only database): skipped checkpoints_compact_boundary_idx';
    END IF;
END
$$;
