-- Lease closure restores the recorded native incarnation in the same transaction.
-- Lifecycle-cleared ownership and placement changes must remain authoritative.
CREATE OR REPLACE FUNCTION restore_native_impersonation_owner() RETURNS trigger AS $$
BEGIN
    IF OLD.accepted_generation IS NOT NULL AND OLD.accepted_owner IS NOT NULL THEN
        UPDATE agents_meta
        SET runtime_generation = OLD.accepted_generation,
            runtime_owner = OLD.accepted_owner
        WHERE id = OLD.agent_id AND machine = NEW.machine
          AND status IN ('running', 'idling')
          AND runtime_generation IS NOT NULL AND runtime_owner IS NOT NULL
          AND (runtime_generation, runtime_owner)
              IS DISTINCT FROM (OLD.accepted_generation, OLD.accepted_owner);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_impersonations_restore_native_owner
    AFTER UPDATE OF status ON agent_impersonations FOR EACH ROW
    WHEN (OLD.status IN ('requested', 'accepted', 'active')
          AND NEW.status IN ('released', 'expired'))
    EXECUTE FUNCTION restore_native_impersonation_owner();
