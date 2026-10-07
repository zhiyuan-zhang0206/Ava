-- Log-native impersonation leases (event_delivery_protocol_version = 2): the
-- producer writes each SDK/audit event body into agent_impersonation_entries
-- itself, so delivery completeness is a predicate over rows in this database
-- instead of a comparison with an external store. Idempotent throughout.

ALTER TABLE agent_impersonation_entries ADD COLUMN IF NOT EXISTS source_key TEXT;
CREATE INDEX IF NOT EXISTS agent_impersonation_entries_source
    ON agent_impersonation_entries(lease_id, source_key) WHERE source_key IS NOT NULL;

ALTER TABLE agent_impersonations
    DROP CONSTRAINT IF EXISTS agent_impersonations_event_delivery_protocol_version_check;
ALTER TABLE agent_impersonations
    ADD CONSTRAINT agent_impersonations_event_delivery_protocol_version_check
    CHECK (event_delivery_protocol_version IN (1, 2));

CREATE OR REPLACE FUNCTION public.close_impersonation_event_manifest_admission(p_lease_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE public.agent_impersonations SET manifest_admission_closed_at=clock_timestamp()
    WHERE id=p_lease_id AND automatic AND event_delivery_protocol_version IN (1, 2)
      AND manifest_admission_closed_at IS NULL;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    -- A lease that already ended (termination closes admission after ending it)
    -- completes here when every source is sealed; otherwise this is a no-op.
    PERFORM public.finalize_impersonation_event_log(p_lease_id);
    RETURN TRUE;
END;
$function$;

-- A log-native lease is complete once it has ended, admission is closed and every
-- source has sealed with a count equal to its rows. The predicate reads only this
-- database, so no external certifier is involved. Returns whether the lease is
-- complete after the call; not-yet-ready is not an error.
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
    IF lease.ended_at IS NULL OR lease.manifest_admission_closed_at IS NULL THEN
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
    SET events_completed_at=clock_timestamp(), events_cursor=NULL, handoff_document=NULL,
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

CREATE OR REPLACE FUNCTION public.seal_impersonation_event_participant(
    p_lease_id UUID,p_source_key TEXT,p_state TEXT,p_failure_reason TEXT,p_item_count BIGINT,p_digest TEXT
) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE
    actual_count BIGINT;
    protocol SMALLINT;
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
    SELECT event_delivery_protocol_version INTO protocol FROM public.agent_impersonations
    WHERE id=p_lease_id;
    IF protocol = 2 THEN
        SELECT count(*) INTO actual_count FROM public.agent_impersonation_entries
        WHERE lease_id=p_lease_id AND source_key=p_source_key;
        IF actual_count<>p_item_count THEN
            RAISE EXCEPTION 'Receipt count does not match its recorded rows';
        END IF;
    ELSE
        SELECT count(*) INTO actual_count FROM public.agent_impersonation_event_participant_items
        WHERE lease_id=p_lease_id AND source_key=p_source_key;
        IF actual_count<>p_item_count OR p_digest !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'Receipt aggregate does not match its append-only items';
        END IF;
    END IF;
    UPDATE public.agent_impersonation_event_participants
    SET state='sealed',sealed_at=clock_timestamp(),item_count=p_item_count,manifest_digest=p_digest
    WHERE lease_id=p_lease_id AND source_key=p_source_key;
    IF protocol = 2 THEN
        PERFORM public.finalize_impersonation_event_log(p_lease_id);
    END IF;
END;
$function$;

-- Every end path (release, expiry, abort, termination) sets ended_at; the lease
-- becomes complete in that same transaction when all sources are already sealed.
CREATE OR REPLACE FUNCTION finalize_impersonation_event_log_on_end() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    PERFORM public.finalize_impersonation_event_log(NEW.id);
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS agent_impersonations_finalize_event_log ON agent_impersonations;
CREATE TRIGGER agent_impersonations_finalize_event_log
    AFTER UPDATE OF ended_at ON agent_impersonations
    FOR EACH ROW
    WHEN (OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL
          AND NEW.event_delivery_protocol_version = 2)
    EXECUTE FUNCTION finalize_impersonation_event_log_on_end();

-- A source row is appended only while its source can still add events: a local
-- participant while its receipt is open, the central source while admission is open.
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
              AND manifest_admission_closed_at IS NULL
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
DROP TRIGGER IF EXISTS agent_impersonation_entries_guard_source ON agent_impersonation_entries;
CREATE TRIGGER agent_impersonation_entries_guard_source
    BEFORE INSERT ON agent_impersonation_entries
    FOR EACH ROW EXECUTE FUNCTION guard_impersonation_event_source();
