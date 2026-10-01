-- Widen the two CHECKs back to their pre-migration value sets. Nothing is
-- restored: the observations the up migration cleared were diagnostics the next
-- admission overwrites anyway, and no row is moved into 'restarting'.
--
-- Roll the code back with (or after) this file: code before it names both
-- values in its enums and parses them from the table.

ALTER TABLE agents_meta DROP CONSTRAINT IF EXISTS agents_meta_status_check;
ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_status_check
    CHECK (status IN ('running', 'idling', 'restarting', 'terminated'));

ALTER TABLE agents_meta DROP CONSTRAINT IF EXISTS agents_meta_last_admission_outcome_check;
ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_last_admission_outcome_check
    CHECK (last_admission_outcome IN
        ('admitted', 'maintenance_hold', 'publication_deferred',
         'resource_fence', 'admission_guard_refused'));
