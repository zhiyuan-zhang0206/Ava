-- Bound delivery-watchdog wake retries per inbound while leaving the row claimable.
ALTER TABLE inbound_messages
    ADD COLUMN dispatch_count INT NOT NULL DEFAULT 0,
    ADD COLUMN last_dispatch_at TIMESTAMPTZ,
    ADD COLUMN poisoned_at TIMESTAMPTZ;

-- Manual recovery for an inbound whose underlying delivery failure is resolved:
-- UPDATE inbound_messages SET dispatch_count = 0, last_dispatch_at = NULL, poisoned_at = NULL WHERE id = <inbound_id>;
