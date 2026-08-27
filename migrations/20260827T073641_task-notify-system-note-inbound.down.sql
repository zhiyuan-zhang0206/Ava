-- Reverse: narrow the kind CHECK back to the pre-ruling set. No 'system_note'
-- rows can exist after the up body ran (a brand-new kind, only new writers
-- produce it), so the narrow CHECK validates cleanly.

ALTER TABLE inbound_messages
    DROP CONSTRAINT IF EXISTS inbound_messages_kind_check;

ALTER TABLE inbound_messages
    ADD CONSTRAINT inbound_messages_kind_check
    CHECK (kind IN (
        'chat',
        'compact_summary',
        'compact_request',
        'cancel',
        'terminate',
        'restart',
        'restart_completed',
        'resurrect',
        'fork',
        'heartbeat'
    ));
