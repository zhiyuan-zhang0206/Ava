-- Push delivery + ACK semantics and start/end messages (Tasks #2649/#2650).
-- start_message: the native agent's required handoff brief, delivered by the
-- bound relay as the first host message at activation. Pre-deploy leases keep
-- the empty default; new accepts reject an empty brief.
ALTER TABLE agent_impersonations
    ADD COLUMN start_message TEXT NOT NULL DEFAULT '';

-- 'reminder': gateway-inserted lease-expiry renewal reminders, pushed through
-- the same relay envelope and ACKed like any inbox message.
ALTER TABLE inbound_messages DROP CONSTRAINT inbound_messages_kind_check;
ALTER TABLE inbound_messages
    ADD CONSTRAINT inbound_messages_kind_check CHECK (kind IN (
        'chat', 'system_note', 'compact_summary', 'compact_request', 'cancel',
        'terminate', 'restart', 'restart_completed', 'resurrect', 'fork',
        'heartbeat', 'reminder'
    ));
