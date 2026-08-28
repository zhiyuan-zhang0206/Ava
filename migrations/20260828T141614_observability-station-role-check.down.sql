-- Restore the two-token constraint (rollback of the observability-station
-- capability token; a station-only role value would then violate the CHECK —
-- the intended fail-fast for an un-rolled-back station declaration).

ALTER TABLE machines DROP CONSTRAINT machines_role_check;
ALTER TABLE machines ADD CONSTRAINT machines_role_check
    CHECK (role <@ ARRAY['gateway', 'agent-runner']::text[]
           AND cardinality(role) >= 1);
