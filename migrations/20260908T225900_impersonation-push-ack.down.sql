ALTER TABLE agent_impersonations DROP COLUMN IF EXISTS start_message;

ALTER TABLE inbound_messages DROP CONSTRAINT IF EXISTS inbound_messages_kind_check;
ALTER TABLE inbound_messages
    ADD CONSTRAINT inbound_messages_kind_check CHECK (kind IN (
        'chat', 'system_note', 'compact_summary', 'compact_request', 'cancel',
        'terminate', 'restart', 'restart_completed', 'resurrect', 'fork',
        'heartbeat'
    ));
