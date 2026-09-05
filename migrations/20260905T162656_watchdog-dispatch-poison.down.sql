ALTER TABLE inbound_messages
    DROP COLUMN dispatch_count,
    DROP COLUMN last_dispatch_at,
    DROP COLUMN poisoned_at;
