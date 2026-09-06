ALTER TABLE agents_meta
    ADD COLUMN wake_suppressed_until TIMESTAMPTZ,
    ADD COLUMN wake_suppress_reason TEXT;

-- Manual recovery:
-- UPDATE agents_meta SET wake_suppressed_until = NULL, wake_suppress_reason = NULL WHERE id = <agent_id>;
