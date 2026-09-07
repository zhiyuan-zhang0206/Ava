ALTER TABLE agents_meta
    ADD COLUMN last_turn_fatal_at TIMESTAMPTZ;

-- Manual recovery:
-- UPDATE agents_meta SET last_turn_fatal_at = NULL WHERE id = <agent_id>;
