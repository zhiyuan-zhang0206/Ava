-- Reserve before host submission: a crash cannot erase an attempted delivery.
ALTER TABLE agent_impersonation_messages
    ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (delivery_attempts BETWEEN 0 AND 2),
    ADD COLUMN last_delivery_at TIMESTAMPTZ,
    ADD CONSTRAINT agent_impersonation_messages_delivery_consistent
        CHECK ((delivery_attempts = 0) = (last_delivery_at IS NULL));
