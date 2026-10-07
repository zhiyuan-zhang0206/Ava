-- Preserve the old policy for existing leases, including in-flight deadlines.
ALTER TABLE agent_impersonations
    ADD COLUMN ack_window_seconds INTEGER NOT NULL DEFAULT 300 CHECK (ack_window_seconds > 0),
    ADD COLUMN max_delivery_attempts INTEGER NOT NULL DEFAULT 2 CHECK (max_delivery_attempts > 0);
ALTER TABLE agent_impersonations ALTER COLUMN ack_window_seconds SET DEFAULT 180;

-- The per-lease budget is enforced by the serialized reservation transaction.
ALTER TABLE agent_impersonation_messages
    DROP CONSTRAINT agent_impersonation_messages_delivery_attempts_check,
    ADD CONSTRAINT agent_impersonation_messages_delivery_attempts_check CHECK (delivery_attempts >= 0);
