-- Reverse the bounds and remove retry accounting. Existing values are retained.
ALTER TABLE work_failed_events
    DROP COLUMN delivery_attempts;

ALTER TABLE inbound_messages
    ALTER COLUMN source_verified_by TYPE TEXT,
    ALTER COLUMN source_transport TYPE TEXT,
    ALTER COLUMN content_hash TYPE TEXT;
