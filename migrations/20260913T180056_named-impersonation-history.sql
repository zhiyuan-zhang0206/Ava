-- Public session numbers are scoped to an agent. UUIDs remain only as legacy
-- journal/checkpoint references so an upgrade does not invalidate live receipts.
ALTER TABLE agents ADD COLUMN impersonation_index BIGINT NOT NULL DEFAULT 0;
ALTER TABLE agent_impersonations
    ADD COLUMN session_id BIGINT,
    ADD COLUMN name TEXT NOT NULL DEFAULT '',
    ADD COLUMN executor_name TEXT NOT NULL DEFAULT '',
    ADD COLUMN process_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN automatic BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN summary TEXT,
    ADD COLUMN handoff_document JSONB,
    ADD COLUMN handoff_path TEXT,
    ADD COLUMN handoff_applied_at TIMESTAMPTZ,
    ADD COLUMN events_cursor JSONB,
    ADD COLUMN events_next_read_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN events_completed_at TIMESTAMPTZ,
    ADD COLUMN next_entry BIGINT NOT NULL DEFAULT 0;
WITH numbered AS (
    SELECT id, row_number() OVER (PARTITION BY agent_id ORDER BY created_at,id)-1 AS n
    FROM agent_impersonations
)
UPDATE agent_impersonations p SET session_id=n.n, name='Session ' || n.n,
    executor_name=p.source FROM numbered n WHERE n.id=p.id;
ALTER TABLE agent_impersonations ALTER COLUMN session_id SET NOT NULL;
ALTER TABLE agent_impersonations ADD CHECK (session_id >= 0);
UPDATE agents a SET impersonation_index=n.next_id FROM (
    SELECT agent_id,max(session_id)+1 AS next_id FROM agent_impersonations GROUP BY agent_id
) n WHERE a.id=n.agent_id;
ALTER TABLE agent_impersonation_messages
    DROP CONSTRAINT agent_impersonation_messages_lease_id_fkey;
ALTER TABLE agent_impersonations DROP CONSTRAINT agent_impersonations_pkey;
ALTER TABLE agent_impersonations ADD UNIQUE(id);
ALTER TABLE agent_impersonations ADD PRIMARY KEY(agent_id,session_id);
ALTER TABLE agent_impersonation_messages ADD FOREIGN KEY(lease_id)
    REFERENCES agent_impersonations(id) ON DELETE RESTRICT;

CREATE TABLE agent_impersonation_entries (
    lease_id UUID NOT NULL REFERENCES agent_impersonations(id) ON DELETE RESTRICT,
    seq BIGINT NOT NULL CHECK (seq >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('message','lifecycle','sdk_call','api_event')),
    event_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    payload JSONB NOT NULL,
    PRIMARY KEY(lease_id,seq),
    UNIQUE(lease_id,event_key)
);
CREATE INDEX agent_impersonations_events_pending ON agent_impersonations(machine,events_next_read_at)
WHERE automatic AND activated_at IS NOT NULL AND ended_at IS NOT NULL AND events_completed_at IS NULL;
CREATE INDEX agent_impersonation_entries_created ON agent_impersonation_entries(lease_id,created_at,seq);

-- Existing rows become permanent too. These are snapshots of available facts;
-- the migration does not invent unrecorded renewal events.
INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,created_at,payload)
SELECT id,0,'lifecycle','migration',created_at,jsonb_build_object(
    'event','migration_snapshot','status',status,'source',source,'created_at',created_at,
    'activated_at',activated_at,'ended_at',ended_at,'expires_at',expires_at)
FROM agent_impersonations;
WITH known_messages AS (
    SELECT lease_id,inbound_id,acknowledged_at FROM agent_impersonation_messages
    UNION ALL
    -- Already-active legacy sessions do not cross activate again after upgrade.
    SELECT p.id,i.id,NULL::timestamptz FROM agent_impersonations p
    JOIN inbound_messages i ON i.agent_id=p.agent_id
    WHERE p.status='active' AND p.expires_at>clock_timestamp() AND i.status='pending'
        AND i.kind IN ('chat','system_note','cancel','reminder')
        AND NOT EXISTS (SELECT 1 FROM agent_impersonation_messages m WHERE m.lease_id=p.id AND m.inbound_id=i.id)
)
INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,created_at,payload)
SELECT p.lease_id,row_number() OVER (PARTITION BY p.lease_id ORDER BY i.id),
    'message','inbound:' || i.id,i.created_at,jsonb_build_object(
        'direction','in','inbound_id',i.id,'kind',i.kind,'source',i.source,
        'content',i.content,'payload',i.payload,'acknowledged_at',p.acknowledged_at)
FROM known_messages p JOIN inbound_messages i ON i.id=p.inbound_id;
-- Keep legacy handoff bodies even if their ordinary inbound rows are later retired.
UPDATE agent_impersonations p SET summary=i.content FROM inbound_messages i
WHERE i.id=p.summary_inbound_id;
INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,created_at,payload)
SELECT p.id,COALESCE((SELECT max(seq)+1 FROM agent_impersonation_entries e WHERE e.lease_id=p.id),0),
    'lifecycle','legacy_handoff',i.created_at,jsonb_build_object(
        'event','legacy_handoff','inbound_id',i.id,'content',i.content,'payload',i.payload,'source',i.source)
FROM agent_impersonations p JOIN inbound_messages i ON i.id=p.summary_inbound_id;
UPDATE agent_impersonations p SET next_entry=e.n FROM (
    SELECT lease_id,max(seq)+1 AS n FROM agent_impersonation_entries GROUP BY lease_id
) e WHERE e.lease_id=p.id;
DROP INDEX agent_impersonations_one_open;
CREATE UNIQUE INDEX agent_impersonations_one_open ON agent_impersonations(agent_id)
WHERE status IN ('requested','accepted','active') OR delta_version>applied_version
    OR (automatic AND handoff_applied_at IS NULL);

CREATE FUNCTION allocate_impersonation_session() RETURNS trigger AS $$
BEGIN
    PERFORM id FROM agents_meta WHERE id=NEW.agent_id FOR UPDATE;
    UPDATE agents SET impersonation_index=impersonation_index+1 WHERE id=NEW.agent_id
        RETURNING impersonation_index-1 INTO NEW.session_id;
    IF NEW.name='' THEN NEW.name='Session ' || NEW.session_id; END IF;
    IF NEW.executor_name='' THEN NEW.executor_name=NEW.source; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER agent_impersonations_allocate BEFORE INSERT ON agent_impersonations
    FOR EACH ROW EXECUTE FUNCTION allocate_impersonation_session();

CREATE FUNCTION preserve_impersonation_history() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Impersonation history is permanent; updates and deletes are forbidden';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER agent_impersonations_preserve_history BEFORE DELETE ON agent_impersonations
    FOR EACH ROW EXECUTE FUNCTION preserve_impersonation_history();
CREATE TRIGGER agent_impersonation_entries_preserve_history
    BEFORE UPDATE OR DELETE ON agent_impersonation_entries
    FOR EACH ROW EXECUTE FUNCTION preserve_impersonation_history();

CREATE FUNCTION record_impersonation_lifecycle() RETURNS trigger AS $$
DECLARE entry_no BIGINT;
BEGIN
    IF TG_OP='UPDATE' AND (NEW.status,NEW.expires_at) IS NOT DISTINCT FROM (OLD.status,OLD.expires_at) THEN
        RETURN NEW;
    END IF;
    UPDATE agent_impersonations SET next_entry=next_entry+1 WHERE id=NEW.id RETURNING next_entry-1 INTO entry_no;
    INSERT INTO agent_impersonation_entries(lease_id,seq,kind,payload)
    VALUES(NEW.id,entry_no,'lifecycle',jsonb_build_object(
        'status',NEW.status,'expires_at',NEW.expires_at,'executor_name',NEW.executor_name,
        'session_id',NEW.session_id,'name',NEW.name,'machine',NEW.machine,
        'summary',NEW.summary,'reason',NEW.reason,'rejection_reason',NEW.rejection_reason,
        'source',COALESCE(NULLIF(current_setting('ava.impersonation_actor',true),''),'system:impersonation'),
        'previous_status',CASE WHEN TG_OP='UPDATE' THEN OLD.status ELSE NULL END,
        'previous_expires_at',CASE WHEN TG_OP='UPDATE' THEN OLD.expires_at ELSE NULL END));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER agent_impersonations_lifecycle AFTER INSERT OR UPDATE OF status,expires_at
    ON agent_impersonations FOR EACH ROW EXECUTE FUNCTION record_impersonation_lifecycle();

-- Preserve every inbound body independently of pending/ACK state and inbox retention.
CREATE FUNCTION record_impersonation_inbound() RETURNS trigger AS $$
DECLARE lease UUID; entry_no BIGINT;
BEGIN
    IF NEW.kind NOT IN ('chat','system_note','cancel','reminder') THEN RETURN NEW; END IF;
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
CREATE TRIGGER inbound_messages_impersonation_history AFTER INSERT ON inbound_messages
    FOR EACH ROW EXECUTE FUNCTION record_impersonation_inbound();
