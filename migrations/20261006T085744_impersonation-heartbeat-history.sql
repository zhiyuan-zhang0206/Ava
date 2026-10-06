-- Preserve external heartbeat receipt in the impersonation history.
CREATE OR REPLACE FUNCTION record_impersonation_inbound() RETURNS trigger AS $$
DECLARE lease UUID; entry_no BIGINT;
BEGIN
    IF NEW.kind NOT IN ('chat','system_note','cancel','reminder','heartbeat') THEN RETURN NEW; END IF;
    -- Match native admission, activation and inbox claim lock order.
    PERFORM id FROM agents_meta WHERE id=NEW.agent_id FOR UPDATE;
    SELECT id INTO lease FROM agent_impersonations
    WHERE agent_id=NEW.agent_id AND status='active'
        AND expires_at>clock_timestamp() FOR UPDATE;
    IF lease IS NULL THEN RETURN NEW; END IF;
    UPDATE agent_impersonations SET next_entry=next_entry+1 WHERE id=lease RETURNING next_entry-1 INTO entry_no;
    INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,created_at,payload)
    VALUES(lease,entry_no,'message','inbound:' || NEW.id,NEW.created_at,jsonb_build_object(
        'direction','in','inbound_id',NEW.id,'kind',NEW.kind,'source',NEW.source,
        'content',NEW.content,'payload',NEW.payload));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
