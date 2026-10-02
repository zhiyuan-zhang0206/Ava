-- Contract the retired protocol-v1 impersonation event machinery.
--
-- decisions/2026-10-02-impersonation-event-log-in-postgres.md replaced the
-- replay/certification protocol with the lease's own log, and 8d017a7ac deleted
-- the runtime that operated these objects (the replay consumer, the
-- reconciliation loop, the Loki comparison and retention probes, the
-- certification secret and seven settings). What stayed behind is inert: the
-- certification ledger and its certifier/certify functions, the expected-census
-- tables, the freeze, retention-loss and integrity-alert helpers, the two
-- pending-read indexes, and eight lease columns whose only readers and writers
-- lived in the deleted code.
--
-- A contract migration in the migration README's sense: the code that stopped
-- using these objects shipped first (this file merges after it on main, so no
-- wave can carry this drop without that code). The one surviving writer of a
-- dropped column -- finalize_impersonation_event_log clearing events_cursor --
-- leaves in the same change, and the three protocol-v1 pending reasons leave
-- the vocabulary with their writers.
--
-- Affected values were snapshotted before rollout: records/4886/snapshot-pre-drop.json
-- (task #4886). Every dropped lease column is NULL across the 96 terminal
-- legacy leases except events_next_read_at, which the old column default set on
-- every row; the dropped tables held two census rows and the v1 verification
-- secret (unreadable by the runner role by design).

DROP TABLE IF EXISTS agent_impersonation_event_expected_items;
DROP TABLE IF EXISTS agent_impersonation_event_expected_receipts;
DROP TABLE IF EXISTS agent_impersonation_event_certifiers;

DROP FUNCTION IF EXISTS public.admit_impersonation_event_certifier(UUID, TEXT);
DROP FUNCTION IF EXISTS public.certify_impersonation_event_delivery(UUID, TEXT);
DROP FUNCTION IF EXISTS public.freeze_impersonation_event_manifest(UUID, TEXT, BIGINT, TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.record_impersonation_event_retention_loss(UUID, TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.record_impersonation_event_integrity_alert(UUID);

DROP INDEX IF EXISTS agent_impersonations_manifest_pending;
DROP INDEX IF EXISTS agent_impersonations_events_pending;

ALTER TABLE public.agent_impersonations
    DROP CONSTRAINT IF EXISTS agent_impersonations_manifest_frozen_check;

ALTER TABLE public.agent_impersonations
    DROP COLUMN IF EXISTS events_cursor,
    DROP COLUMN IF EXISTS events_next_read_at,
    DROP COLUMN IF EXISTS manifest_frozen_at,
    DROP COLUMN IF EXISTS manifest_digest,
    DROP COLUMN IF EXISTS manifest_item_count,
    DROP COLUMN IF EXISTS manifest_envelope_floor_at,
    DROP COLUMN IF EXISTS event_delivery_retention_horizon_at,
    DROP COLUMN IF EXISTS event_delivery_integrity_alerted_at;

-- The three protocol-v1 reasons leave the vocabulary with their writers
-- (indexed-id listing, census mismatch, retention loss).
ALTER TABLE public.agent_impersonations
    DROP CONSTRAINT IF EXISTS agent_impersonations_event_delivery_pending_reason_check;
ALTER TABLE public.agent_impersonations
    ADD CONSTRAINT agent_impersonations_event_delivery_pending_reason_check
    CHECK (event_delivery_pending_reason IN (
        'awaiting_session_end', 'awaiting_participant_seal', 'capture_failed'
    ));
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
