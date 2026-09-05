DROP TABLE IF EXISTS work_failed_events;

ALTER TABLE inbound_messages
    DROP COLUMN IF EXISTS source_assertion_match,
    DROP COLUMN IF EXISTS content_hash,
    DROP COLUMN IF EXISTS source_transport,
    DROP COLUMN IF EXISTS source_verified_by;
