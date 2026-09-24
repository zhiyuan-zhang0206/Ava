CREATE FUNCTION public.lock_impersonation_event_participant(p_lease_id UUID, p_source_key TEXT)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
DECLARE receipt_state TEXT;
BEGIN
    SELECT state INTO receipt_state FROM public.agent_impersonation_event_participants
    WHERE lease_id=p_lease_id AND source_key=p_source_key FOR UPDATE;
    RETURN receipt_state;
END;
$function$;

REVOKE ALL ON FUNCTION public.lock_impersonation_event_participant(UUID, TEXT) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ava_runner') THEN
        GRANT EXECUTE ON FUNCTION public.lock_impersonation_event_participant(UUID, TEXT) TO ava_runner;
    END IF;
END $$;
