-- Retire two agents_meta values nothing writes any more
-- (decisions/2026-10-01-contract-the-retired-deploy-storage.md):
--
--   * status 'restarting': the update straggler reap was its last writer;
--   * last_admission_outcome 'publication_deferred': the publication fence was
--     its only writer.
--
-- A row still in 'restarting' is stuck, because no code handles that status
-- any more. The migration refuses to guess: it fails, and the operator repairs
-- the rows by hand before running it again. The refusal rolls the whole
-- migration back (one transaction), so the schema is unchanged.
DO $$
DECLARE
    stuck TEXT;
BEGIN
    SELECT string_agg(id::text, ', ' ORDER BY id) INTO stuck
    FROM agents_meta WHERE status = 'restarting';
    IF stuck IS NOT NULL THEN
        RAISE EXCEPTION
            'cannot retire status ''restarting'': agents_meta rows % are still in it and no code handles that status; repair them by hand (terminate, or set idling), then run the migration again',
            stuck;
    END IF;
END
$$;

-- The deferral was only the latest admission's diagnostic observation; the
-- next admission overwrites it. The pair check requires both columns NULL.
UPDATE agents_meta SET last_admission_outcome = NULL, last_admission_at = NULL
WHERE last_admission_outcome = 'publication_deferred';

ALTER TABLE agents_meta DROP CONSTRAINT IF EXISTS agents_meta_status_check;
ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_status_check
    CHECK (status IN ('running', 'idling', 'terminated'));

ALTER TABLE agents_meta DROP CONSTRAINT IF EXISTS agents_meta_last_admission_outcome_check;
ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_last_admission_outcome_check
    CHECK (last_admission_outcome IN
        ('admitted', 'maintenance_hold', 'resource_fence', 'admission_guard_refused'));
