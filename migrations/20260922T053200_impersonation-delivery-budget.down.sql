-- Rolling back a live budget would grant fresh attempts after re-upgrade.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agent_impersonation_messages m
        JOIN agent_impersonations l ON l.id=m.lease_id
        WHERE l.status IN ('requested','accepted','active') AND m.delivery_attempts>0
    ) THEN
        RAISE EXCEPTION 'End active impersonations before removing delivery budgets';
    END IF;
END;
$$;

ALTER TABLE agent_impersonation_messages
    DROP CONSTRAINT agent_impersonation_messages_delivery_consistent,
    DROP COLUMN last_delivery_at,
    DROP COLUMN delivery_attempts;
