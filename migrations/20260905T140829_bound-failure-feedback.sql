-- Bound server-owned inbound provenance facts to their application contracts,
-- and count attempts to recover work-failure events left unfinished by a crash.
ALTER TABLE inbound_messages
    ALTER COLUMN source_verified_by TYPE VARCHAR(120),
    ALTER COLUMN source_transport TYPE VARCHAR(80),
    ALTER COLUMN content_hash TYPE VARCHAR(64);

ALTER TABLE work_failed_events
    ADD COLUMN delivery_attempts INT NOT NULL DEFAULT 0;
