DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM agent_impersonations WHERE event_delivery_protocol_version = 2) THEN
        RAISE EXCEPTION 'Cannot roll back: log-native impersonation leases exist and their event rows would be orphaned';
    END IF;
END $$;

DROP TRIGGER IF EXISTS agent_impersonation_entries_guard_source ON agent_impersonation_entries;
DROP FUNCTION IF EXISTS guard_impersonation_event_source();
DROP TRIGGER IF EXISTS agent_impersonations_finalize_event_log ON agent_impersonations;
DROP FUNCTION IF EXISTS finalize_impersonation_event_log_on_end();

CREATE OR REPLACE FUNCTION public.seal_impersonation_event_participant(
    p_lease_id UUID,p_source_key TEXT,p_state TEXT,p_failure_reason TEXT,p_item_count BIGINT,p_digest TEXT
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
    SELECT count(*) INTO actual_count FROM public.agent_impersonation_event_participant_items
    WHERE lease_id=p_lease_id AND source_key=p_source_key;
    IF actual_count<>p_item_count OR p_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'Receipt aggregate does not match its append-only items';
    END IF;
    UPDATE public.agent_impersonation_event_participants
    SET state='sealed',sealed_at=clock_timestamp(),item_count=p_item_count,manifest_digest=p_digest
    WHERE lease_id=p_lease_id AND source_key=p_source_key;
END;
$function$;

DROP FUNCTION IF EXISTS public.finalize_impersonation_event_log(UUID);

CREATE OR REPLACE FUNCTION public.close_impersonation_event_manifest_admission(p_lease_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
BEGIN
    UPDATE public.agent_impersonations SET manifest_admission_closed_at=clock_timestamp()
    WHERE id=p_lease_id AND automatic AND event_delivery_protocol_version=1
      AND manifest_admission_closed_at IS NULL;
    RETURN FOUND;
END;
$function$;

ALTER TABLE agent_impersonations
    DROP CONSTRAINT IF EXISTS agent_impersonations_event_delivery_protocol_version_check;
ALTER TABLE agent_impersonations
    ADD CONSTRAINT agent_impersonations_event_delivery_protocol_version_check
    CHECK (event_delivery_protocol_version = 1);

DROP INDEX IF EXISTS agent_impersonation_entries_source;
ALTER TABLE agent_impersonation_entries DROP COLUMN IF EXISTS source_key;
