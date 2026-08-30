-- Reverses the CHECK swap only: data is NOT rolled back. Rows that were
-- hibernating at upgrade time were rewritten to idling by the .up and stay
-- idling (expand-contract, see the .up's comment).
ALTER TABLE agents_meta
    DROP CONSTRAINT IF EXISTS agents_meta_status_check;

ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_status_check
    CHECK (status IN ('running', 'idling', 'restarting', 'terminated', 'hibernating'));
