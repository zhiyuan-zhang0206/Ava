-- Contract step of the impersonation event log: nothing reads or writes the
-- protocol-v1 manifest objects any more (census tables, freeze/certify
-- procedures, replay cursor, retention and integrity stamps). Drop them, narrow
-- the checks to what a log-native lease can hold, and rename the admission gate
-- (column and procedure) off the manifest vocabulary. Idempotent throughout.

DROP FUNCTION IF EXISTS public.admit_impersonation_event_certifier(UUID,TEXT);
DROP FUNCTION IF EXISTS public.freeze_impersonation_event_manifest(UUID,TEXT,BIGINT,TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.record_impersonation_event_retention_loss(UUID,TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.record_impersonation_event_integrity_alert(UUID);
DROP FUNCTION IF EXISTS public.certify_impersonation_event_delivery(UUID,TEXT);

DROP TABLE IF EXISTS agent_impersonation_event_expected_items;
DROP TABLE IF EXISTS agent_impersonation_event_expected_receipts;
DROP TABLE IF EXISTS agent_impersonation_event_participant_items;
DROP TABLE IF EXISTS agent_impersonation_event_certifiers;

DROP INDEX IF EXISTS agent_impersonations_manifest_pending;
DROP INDEX IF EXISTS agent_impersonations_events_pending;
ALTER TABLE agent_impersonations
    DROP COLUMN IF EXISTS events_cursor,
    DROP COLUMN IF EXISTS events_next_read_at,
    DROP COLUMN IF EXISTS manifest_frozen_at,
    DROP COLUMN IF EXISTS manifest_digest,
    DROP COLUMN IF EXISTS manifest_item_count,
    DROP COLUMN IF EXISTS manifest_envelope_floor_at,
    DROP COLUMN IF EXISTS event_delivery_retention_horizon_at,
    DROP COLUMN IF EXISTS event_delivery_integrity_alerted_at;
ALTER TABLE agent_impersonation_event_participants DROP COLUMN IF EXISTS manifest_digest;

-- Only the reasons a log-native lease can carry remain.
UPDATE agent_impersonations SET event_delivery_pending_reason = NULL
WHERE event_delivery_pending_reason NOT IN
    ('awaiting_session_end', 'awaiting_participant_seal', 'capture_failed');
ALTER TABLE agent_impersonations
    DROP CONSTRAINT IF EXISTS agent_impersonations_event_delivery_pending_reason_check;
ALTER TABLE agent_impersonations
    ADD CONSTRAINT agent_impersonations_event_delivery_pending_reason_check
    CHECK (event_delivery_pending_reason IN (
        'awaiting_session_end', 'awaiting_participant_seal', 'capture_failed'
    ));
ALTER TABLE agent_impersonations
    DROP CONSTRAINT IF EXISTS agent_impersonations_event_delivery_protocol_version_check;
ALTER TABLE agent_impersonations
    ADD CONSTRAINT agent_impersonations_event_delivery_protocol_version_check
    CHECK (event_delivery_protocol_version = 2);

-- Dropping the freeze check shifts PostgreSQL's generated check numbering; give the
-- relay-minted pairing check a stable name so every database agrees on it.
DO $$
DECLARE check_name TEXT;
BEGIN
    SELECT conname INTO check_name FROM pg_constraint
    WHERE conrelid = 'agent_impersonations'::regclass AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%relay_minted_generation IS NULL%relay_minted_owner IS NULL%'
      AND conname <> 'agent_impersonations_relay_minted_pair';
    IF check_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE agent_impersonations RENAME CONSTRAINT %I TO agent_impersonations_relay_minted_pair',
            check_name);
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'agent_impersonations'
          AND column_name = 'manifest_admission_closed_at'
    ) THEN
        ALTER TABLE agent_impersonations
            RENAME COLUMN manifest_admission_closed_at TO event_admission_closed_at;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.close_impersonation_event_admission(p_lease_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE public.agent_impersonations SET event_admission_closed_at=clock_timestamp()
    WHERE id=p_lease_id AND automatic AND event_delivery_protocol_version = 2
      AND event_admission_closed_at IS NULL;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    -- A lease that already ended (termination closes admission after ending it)
    -- completes here when every source is sealed; otherwise this is a no-op.
    PERFORM public.finalize_impersonation_event_log(p_lease_id);
    RETURN TRUE;
END;
$function$;

CREATE OR REPLACE FUNCTION public.seal_impersonation_event_participant(
    p_lease_id UUID,p_source_key TEXT,p_state TEXT,p_failure_reason TEXT,p_item_count BIGINT
) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE actual_count BIGINT;
BEGIN
    IF p_state NOT IN ('sealed','failed') THEN RAISE EXCEPTION 'Receipt transition must seal or fail'; END IF;
    PERFORM 1 FROM public.agent_impersonation_event_participants
    WHERE lease_id=p_lease_id AND source_key=p_source_key AND state='open' FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Receipt is not open'; END IF;
    IF p_state='failed' THEN
        IF p_failure_reason IS NULL THEN RAISE EXCEPTION 'Failed receipt requires a reason'; END IF;
        UPDATE public.agent_impersonation_event_participants SET state='failed',failure_reason=p_failure_reason
        WHERE lease_id=p_lease_id AND source_key=p_source_key;
        RETURN;
    END IF;
    SELECT count(*) INTO actual_count FROM public.agent_impersonation_entries
    WHERE lease_id=p_lease_id AND source_key=p_source_key;
    IF actual_count<>p_item_count THEN
        RAISE EXCEPTION 'Receipt count does not match its recorded rows';
    END IF;
    UPDATE public.agent_impersonation_event_participants
    SET state='sealed',sealed_at=clock_timestamp(),item_count=p_item_count
    WHERE lease_id=p_lease_id AND source_key=p_source_key;
    PERFORM public.finalize_impersonation_event_log(p_lease_id);
END;
$function$;

CREATE OR REPLACE FUNCTION public.finalize_impersonation_event_log(p_lease_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
DECLARE
    lease public.agent_impersonations%ROWTYPE;
    entry_no BIGINT;
    participants BIGINT;
    sdk_count BIGINT;
    api_count BIGINT;
BEGIN
    SELECT * INTO lease FROM public.agent_impersonations WHERE id=p_lease_id FOR UPDATE;
    IF NOT FOUND OR NOT lease.automatic OR lease.event_delivery_protocol_version IS DISTINCT FROM 2 THEN
        RETURN FALSE;
    END IF;
    IF lease.events_completed_at IS NOT NULL THEN
        RETURN TRUE;
    END IF;
    IF lease.ended_at IS NULL OR lease.event_admission_closed_at IS NULL THEN
        RETURN FALSE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants
        WHERE lease_id=p_lease_id AND state <> 'sealed'
    ) THEN
        RETURN FALSE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants p
        WHERE p.lease_id=p_lease_id AND p.item_count IS DISTINCT FROM (
            SELECT count(*) FROM public.agent_impersonation_entries e
            WHERE e.lease_id=p.lease_id AND e.source_key=p.source_key
        )
    ) THEN
        RAISE EXCEPTION 'Sealed source count differs from its recorded rows';
    END IF;
    UPDATE public.agent_impersonations
    SET events_completed_at=clock_timestamp(), handoff_document=NULL,
        event_delivery_pending_reason=NULL
    WHERE id=p_lease_id;
    UPDATE public.agent_impersonations SET next_entry=next_entry+1
    WHERE id=p_lease_id RETURNING next_entry-1 INTO entry_no;
    SELECT count(*) INTO participants FROM public.agent_impersonation_event_participants
    WHERE lease_id=p_lease_id;
    SELECT count(*) FILTER (WHERE kind='sdk_call'), count(*) FILTER (WHERE kind='api_event')
      INTO sdk_count, api_count
    FROM public.agent_impersonation_entries
    WHERE lease_id=p_lease_id AND source_key IS NOT NULL;
    INSERT INTO public.agent_impersonation_entries(lease_id,seq,kind,payload)
    VALUES(p_lease_id,entry_no,'lifecycle',jsonb_build_object(
        'event','event_delivery_complete',
        'event_count',sdk_count+api_count,
        'participant_count',participants,
        'sdk_call_count',sdk_count,
        'api_event_count',api_count
    ));
    RETURN TRUE;
END;
$function$;

CREATE OR REPLACE FUNCTION guard_impersonation_event_source() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF NEW.source_key IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.kind NOT IN ('sdk_call','api_event') THEN
        RAISE EXCEPTION 'Only SDK and API events carry an event source';
    END IF;
    IF NEW.source_key = 'central' THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.agent_impersonations
            WHERE id=NEW.lease_id AND event_delivery_protocol_version=2
              AND event_admission_closed_at IS NULL
        ) THEN
            RAISE EXCEPTION 'Central event admission is closed';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1 FROM public.agent_impersonation_event_participants
        WHERE lease_id=NEW.lease_id AND source_key=NEW.source_key AND state='open'
    ) THEN
        RAISE EXCEPTION 'Event source receipt is not open';
    END IF;
    RETURN NEW;
END;
$$;

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
        PERFORM close_impersonation_event_admission(ended_lease.id);
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

-- The old signatures go once their replacements and every caller exist.
DROP FUNCTION IF EXISTS public.close_impersonation_event_manifest_admission(UUID);
DROP FUNCTION IF EXISTS public.seal_impersonation_event_participant(UUID,TEXT,TEXT,TEXT,BIGINT,TEXT);

REVOKE ALL ON FUNCTION public.close_impersonation_event_admission(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.seal_impersonation_event_participant(UUID,TEXT,TEXT,TEXT,BIGINT) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ava_runner') THEN
        GRANT EXECUTE ON FUNCTION public.close_impersonation_event_admission(UUID) TO ava_runner;
        GRANT EXECUTE ON FUNCTION public.seal_impersonation_event_participant(UUID,TEXT,TEXT,TEXT,BIGINT) TO ava_runner;
    END IF;
END $$;
