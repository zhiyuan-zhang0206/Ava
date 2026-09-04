-- Let least-privilege runtimes serialize publication admission without
-- granting ava_runner UPDATE authority over deployment_state.
CREATE OR REPLACE FUNCTION public.lock_runtime_publication_admission()
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT NULL::void
    FROM public.deployment_state
    WHERE id = 1
    FOR UPDATE
$function$;

REVOKE ALL ON FUNCTION public.lock_runtime_publication_admission() FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT EXECUTE ON FUNCTION public.lock_runtime_publication_admission() TO ava_runner;
    END IF;
END
$$;

COMMENT ON FUNCTION public.lock_runtime_publication_admission() IS
    'Take the deployment publication row lock for least-privilege runtime admission without granting rollout writes.';
