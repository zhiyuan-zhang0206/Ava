-- Agent termination ends open impersonation leases in SQL. Close protocol-v1
-- manifest admission in the same transaction, like release, expiry and abort,
-- so terminal replay can freeze sealed receipts and certify the handoff
-- instead of leaving its event accounting pending forever. The admission door
-- is SECURITY DEFINER and a no-op for manual, legacy or already-closed leases.
CREATE OR REPLACE FUNCTION revoke_terminated_impersonation() RETURNS trigger AS $$
DECLARE
    ended_lease UUID;
BEGIN
    FOR ended_lease IN
        UPDATE agent_impersonations SET status='expired', ended_at=clock_timestamp()
        WHERE agent_id=NEW.id AND status IN ('requested','accepted','active')
        RETURNING id
    LOOP
        PERFORM close_impersonation_event_manifest_admission(ended_lease);
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
