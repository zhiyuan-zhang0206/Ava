-- Task system notifications (assign / update / reminder) are delivered as a
-- new inbound kind 'system_note' (user ruling 2026-08-27): the claim node
-- renders them as a system note (NoteTag 'task') instead of a chat peer
-- message, so the timeline shows no Agent prefix / peer timestamp.
--
-- The CHECK constraint widens; no data movement. DROP IF EXISTS keeps the
-- body idempotent on a fresh bootstrap, where schema.sql already carries the
-- new shape (migration smoke replays on fresh).

ALTER TABLE inbound_messages
    DROP CONSTRAINT IF EXISTS inbound_messages_kind_check;

ALTER TABLE inbound_messages
    ADD CONSTRAINT inbound_messages_kind_check
    CHECK (kind IN (
        'chat',
        'system_note',
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
