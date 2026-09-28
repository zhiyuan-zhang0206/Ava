-- Restore atomic lease/manifest revocation without termination notices.
-- Existing messages and session history remain intact.
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
