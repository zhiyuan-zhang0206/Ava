-- Persist native interruption and completed-termination facts atomically with
-- lease revocation. The terminal reason reuses the bound relay's end transport.
CREATE OR REPLACE FUNCTION revoke_terminated_impersonation() RETURNS trigger AS $$
DECLARE
    ended_lease RECORD;
    interruption_id BIGINT;
BEGIN
    FOR ended_lease IN
        UPDATE agent_impersonations SET status='expired', ended_at=clock_timestamp(),
            rejection_reason='terminated: agent was terminated'
        WHERE agent_id=NEW.id AND status IN ('requested','accepted','active')
        RETURNING id, session_id
    LOOP
        PERFORM close_impersonation_event_manifest_admission(ended_lease.id);
        -- The native graph may be drained. Persist completed facts for its next
        -- resurrection, distinct from the graph's earlier acceptance marker.
        INSERT INTO inbound_messages(agent_id,content,kind,source,payload,created_at)
        VALUES(NEW.id,format('Impersonation session %s was interrupted because this agent was terminated. Unacknowledged messages remain pending; no external completion summary was supplied.', ended_lease.session_id),
            'system_note','system:impersonation',
            jsonb_build_object('impersonation_id',ended_lease.id,'note_tag','impersonation',
                'impersonation_termination_notice',TRUE),
            clock_timestamp())
        RETURNING id INTO interruption_id;
        UPDATE agent_impersonations SET summary_inbound_id=interruption_id
        WHERE id=ended_lease.id;
        INSERT INTO inbound_messages(agent_id,content,kind,source,payload,created_at)
        VALUES(NEW.id,'You were terminated.','system_note','system:impersonation',
            jsonb_build_object('impersonation_id',ended_lease.id,'note_tag','lifecycle_terminate',
                'impersonation_termination_notice',TRUE),
            clock_timestamp());
        UPDATE inbound_messages SET status='done'
        WHERE agent_id=NEW.id AND kind='reminder' AND status='pending'
            AND payload->>'lease_id'=ended_lease.id::text;
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
