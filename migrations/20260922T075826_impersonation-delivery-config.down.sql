-- Never shorten an active policy or erase attempts to fit the old constraint.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM agent_impersonations
        WHERE status IN ('requested', 'accepted', 'active')
          AND (ack_window_seconds <> 300 OR max_delivery_attempts <> 2)
    ) OR EXISTS (
        SELECT 1 FROM agent_impersonation_messages WHERE delivery_attempts > 2
    ) THEN
        RAISE EXCEPTION 'Cannot downgrade impersonation delivery policy without changing active leases or recorded attempts';
    END IF;
END $$;
ALTER TABLE agent_impersonation_messages
    DROP CONSTRAINT agent_impersonation_messages_delivery_attempts_check,
    ADD CONSTRAINT agent_impersonation_messages_delivery_attempts_check CHECK (delivery_attempts BETWEEN 0 AND 2);
ALTER TABLE agent_impersonations DROP COLUMN ack_window_seconds, DROP COLUMN max_delivery_attempts;
